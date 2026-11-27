"""Kubernetes manifests (deploy/k8s): probes, safe rollouts, security and sizing.

Read as YAML and cross-checked against the real app (routes, timeouts, the
connection math), so a manifest cannot silently drift away from the code.
Schema validation: kubeconform (see README).
"""

import runpy
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api.routes.health import CHECK_TIMEOUT_SECONDS
from app.core.capacity import RESERVED_CONNECTIONS, connections_needed

ROOT = Path(__file__).resolve().parent.parent
K8S = ROOT / "deploy" / "k8s"


def resources() -> list[dict[str, Any]]:
    """Every Kubernetes object in deploy/k8s (all files, all documents)."""
    found = []
    for path in sorted(K8S.glob("*.yaml")):
        found += [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]
    return found


def get(kind: str, name: str) -> dict[str, Any]:
    matches = [r for r in resources() if r["kind"] == kind and r["metadata"]["name"] == name]
    assert len(matches) == 1, f"{kind}/{name}: found {len(matches)}"
    return matches[0]


def container(deployment: dict[str, Any]) -> dict[str, Any]:
    [only] = deployment["spec"]["template"]["spec"]["containers"]
    return only


@pytest.fixture
def gunicorn_conf(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """gunicorn.conf.py's settings, without leaking what it sets into the environment.

    It sets PROMETHEUS_MULTIPROC_DIR; left in os.environ, every later /metrics in this
    test run would read an empty folder (setenv first: the test end restores "not set").
    """
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", "placeholder")
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR")
    return runpy.run_path(str(ROOT / "gunicorn.conf.py"))


def config() -> dict[str, str]:
    return get("ConfigMap", "url-shortener-config")["data"]


API = "url-shortener-api"
WORKER = "url-shortener-worker"
BEAT = "url-shortener-beat"
DEPLOYMENTS = (API, WORKER, BEAT)


# ---------- the three probes of the API ----------


def test_api_has_startup_liveness_and_readiness_probes() -> None:
    api = container(get("Deployment", API))

    assert api["startupProbe"]["httpGet"]["path"] == "/health/startup"
    # Liveness checks NOTHING outside the process: a database outage must never
    # restart every pod (a restart cannot fix the database)
    assert api["livenessProbe"]["httpGet"]["path"] == "/health/live"
    assert api["readinessProbe"]["httpGet"]["path"] == "/health/ready"


def test_probe_paths_answer_200_on_a_healthy_app_on_the_served_port(
    client: TestClient, gunicorn_conf: dict[str, Any]
) -> None:
    api = container(get("Deployment", API))
    gunicorn_port = int(gunicorn_conf["bind"].split(":")[1])

    assert api["ports"][0] == {"name": "http", "containerPort": gunicorn_port}
    for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
        assert api[probe]["httpGet"]["port"] == "http", probe
        # Called for real: a typo in a path would be a 404 and a pod that never starts
        assert client.get(api[probe]["httpGet"]["path"]).status_code == 200, probe


@pytest.mark.parametrize("probe", ["startupProbe", "livenessProbe", "readinessProbe"])
def test_probes_wait_longer_than_the_apps_own_checks(probe: str) -> None:
    api = container(get("Deployment", API))

    # Each check gives up after CHECK_TIMEOUT_SECONDS; the probe must wait longer, or
    # Kubernetes would cut a slow-but-honest answer and count a failure
    assert api[probe]["timeoutSeconds"] > CHECK_TIMEOUT_SECONDS


def test_startup_gets_time_before_liveness_could_restart_it() -> None:
    startup = container(get("Deployment", API))["startupProbe"]

    # Up to ~2.5 minutes to start (e.g. waiting for the migration job): only then
    # does liveness take over
    assert startup["failureThreshold"] * startup["periodSeconds"] >= 120


def test_readiness_reacts_faster_than_liveness() -> None:
    api = container(get("Deployment", API))
    ready, live = api["readinessProbe"], api["livenessProbe"]

    # Stop sending traffic quickly; restart only after a longer, sure failure
    assert ready["periodSeconds"] * ready["failureThreshold"] < (
        live["periodSeconds"] * live["failureThreshold"]
    )


# ---------- shutdown and rollouts ----------


def test_pods_get_longer_to_stop_than_gunicorn_needs(gunicorn_conf: dict[str, Any]) -> None:
    graceful = gunicorn_conf["graceful_timeout"]
    spec = get("Deployment", API)["spec"]["template"]["spec"]

    # Kubernetes kills the pod after this; Gunicorn must finish its requests first
    assert spec["terminationGracePeriodSeconds"] > graceful + 5


def test_api_pauses_before_stopping_so_no_request_hits_a_closing_pod() -> None:
    pre_stop = container(get("Deployment", API))["lifecycle"]["preStop"]

    # The pod is removed from the Service a moment AFTER it is told to stop; a short
    # pause lets that happen before Gunicorn stops accepting connections
    assert pre_stop["exec"]["command"][0] == "sleep"


def test_rolling_update_never_drops_below_the_wanted_api_pods() -> None:
    strategy = get("Deployment", API)["spec"]["strategy"]

    assert strategy["type"] == "RollingUpdate"
    assert strategy["rollingUpdate"]["maxUnavailable"] == 0


def test_exactly_one_beat_even_during_a_rollout() -> None:
    beat = get("Deployment", BEAT)["spec"]

    # Two beats = every scheduled job sent twice. "Recreate" stops the old pod
    # BEFORE starting the new one (a rolling update would overlap them)
    assert beat["replicas"] == 1
    assert beat["strategy"]["type"] == "Recreate"


def test_worker_liveness_asks_the_worker_itself() -> None:
    worker = container(get("Deployment", WORKER))

    command = " ".join(worker["livenessProbe"]["exec"]["command"])
    assert "inspect ping" in command
    assert "readinessProbe" not in worker  # it gets no HTTP traffic


def test_migrations_run_once_as_a_job() -> None:
    job = get("Job", "url-shortener-migrate")
    spec = job["spec"]["template"]["spec"]

    assert container({"spec": {"template": job["spec"]["template"]}})["command"] == [
        "alembic",
        "upgrade",
        "head",
    ]
    assert spec["restartPolicy"] == "Never"
    assert job["spec"]["backoffLimit"] >= 1


# ---------- sizing ----------


def test_web_concurrency_is_set_explicitly() -> None:
    # In a container os.cpu_count() sees the NODE's cores (e.g. 64), not the pod's CPU
    # limit: "2 x CPU + 1" would start 129 workers. So the count is always set.
    assert int(config()["WEB_CONCURRENCY"]) >= 1


def test_all_pods_together_fit_in_postgres_max_connections() -> None:
    cfg = config()
    api_replicas = get("Deployment", API)["spec"]["replicas"]
    worker_replicas = get("Deployment", WORKER)["spec"]["replicas"]

    needed = api_replicas * connections_needed(
        int(cfg["WEB_CONCURRENCY"]),
        int(cfg["DB_POOL_SIZE"]),
        int(cfg["DB_MAX_OVERFLOW"]),
        celery_processes=0,
    ) + worker_replicas * connections_needed(0, 0, 0, int(cfg["CELERY_CONCURRENCY"]))

    assert needed <= int(cfg["DB_MAX_CONNECTIONS"]) - RESERVED_CONNECTIONS


def test_cache_and_broker_use_different_redis_dbs() -> None:
    cfg = config()

    assert cfg["REDIS_URL"].endswith("/0")
    assert cfg["CELERY_BROKER_URL"].endswith("/1")


# ---------- security ----------


@pytest.mark.parametrize("name", DEPLOYMENTS)
def test_containers_run_locked_down(name: str) -> None:
    pod = get("Deployment", name)["spec"]["template"]["spec"]
    security = container(get("Deployment", name))["securityContext"]

    assert pod["securityContext"]["runAsNonRoot"] is True
    assert security["allowPrivilegeEscalation"] is False
    assert security["readOnlyRootFilesystem"] is True
    assert security["capabilities"]["drop"] == ["ALL"]
    # Read-only root: /tmp is the only writable place (metrics files, beat schedule)
    assert {"name": "tmp", "mountPath": "/tmp"} in container(get("Deployment", name))[
        "volumeMounts"
    ]


@pytest.mark.parametrize("name", DEPLOYMENTS)
def test_every_container_has_resource_requests_and_limits(name: str) -> None:
    limits = container(get("Deployment", name))["resources"]

    assert {"cpu", "memory"} <= set(limits["requests"])
    assert "memory" in limits["limits"]  # a leak is stopped by the node, not the neighbours


def test_images_are_pinned() -> None:
    images = [
        c["image"]
        for r in resources()
        if r["kind"] in ("Deployment", "Job")
        for c in r["spec"]["template"]["spec"]["containers"]
    ]

    assert images
    assert all(":" in image and not image.endswith(":latest") for image in images)


def test_secrets_are_never_in_the_configmap_or_the_kustomization() -> None:
    cfg = config()
    kustomization = yaml.safe_load((K8S / "kustomization.yaml").read_text(encoding="utf-8"))

    for secret_name in ("JWT_SECRET_KEY", "DATABASE_URL", "METRICS_TOKEN"):
        assert secret_name not in cfg
    # The example Secret holds placeholders: it must never be applied by accident
    assert "secret.example.yaml" not in kustomization["resources"]
    example = get("Secret", "url-shortener-secrets")["stringData"]
    assert all("change-me" in value for value in example.values())


def test_the_service_reaches_the_api_pods() -> None:
    service = get("Service", API)
    pod_labels = get("Deployment", API)["spec"]["template"]["metadata"]["labels"]

    assert service["spec"]["selector"].items() <= pod_labels.items()
    assert service["spec"]["ports"][0]["targetPort"] == "http"
