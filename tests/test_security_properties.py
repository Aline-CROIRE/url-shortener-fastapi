"""Property-based security test: the computer tries thousands of COMBINATIONS.

Hand-written tests check the cases we think of. Here Hypothesis invents random
sequences of actions (create users, log in, create links, switch them on and off,
delete, change tiers, switch accounts off, refresh, REPLAY old refresh tokens, ...)
and after every single request checks the real answer against a simple model of
what the rules say must happen:

- 401 when the token is missing, revoked, or the account is switched off
- 403 for someone else's link (Admin excepted), for Premium features on Free,
  and for one live link too many on a Free account (create or switch-on)
- 404 for a deleted link
- a refresh token works once; replaying a used one revokes every token at once

After every step it also compares the DATABASE with the model (each account's
number of live links), and listing never shows someone else's link.

Two techniques make the random search reach the deep cases:
- preconditions: an action is only chosen when it can do something, so no step
  is wasted (e.g. "replay" only once a used refresh token exists)
- a scaled-down Free limit (2 instead of 10, for this test only): the SAME code,
  but its edge is reachable in 3 steps instead of 11. The real 10 is covered by
  tests/test_tiers.py.

A mutation check proved the test can fail: with ownership, token revocation or
the Free switch-on rule broken on purpose, it finds a failing sequence each time.

When something breaks, Hypothesis shrinks it to the shortest sequence that still
breaks, so the failure is easy to read. More examples:  HYPOTHESIS_PROFILE=thorough
"""

import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, Phase, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.auth as auth_service_module
import app.services.urls as url_service_module
from app.core.config import get_settings
from app.core.security import hash_password
from app.models import URL, User, UserTier
from app.repositories.url import utc_now

AUTH = "/api/v1/auth"
URLS = "/api/v1/urls"
TARGET = "https://www.python.org/"
PASSWORD = "property-test-password"
SCALED_FREE_LIMIT = 2  # the real limit is 10: see the module docstring

# One real Argon2 hash, made once: creating users must not cost a hash each time
PASSWORD_HASH = hash_password(PASSWORD)

settings.register_profile(
    "default", max_examples=25, stateful_step_count=25, deadline=None, print_blob=True
)
settings.register_profile(
    "thorough", max_examples=400, stateful_step_count=60, deadline=None, print_blob=True
)
# For mutation checks ("does the test FAIL when a rule is broken on purpose?"): only
# the yes/no matters, so skip shrinking (finding the SHORTEST failing sequence is slow)
settings.register_profile(
    "mutation",
    max_examples=25,
    stateful_step_count=25,
    deadline=None,
    phases=[Phase.explicit, Phase.reuse, Phase.generate],
)
PROFILE = settings.get_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))

RunDb = Callable[..., Any]


@dataclass
class Account:
    """What the model believes about one user."""

    id: int
    email: str
    tier: UserTier
    active: bool = True
    good_access: list[str] = field(default_factory=list)  # should work while active
    revoked_access: list[str] = field(default_factory=list)  # must never work again
    unused_refresh: list[str] = field(default_factory=list)  # works once
    used_refresh: list[str] = field(default_factory=list)  # replay = theft
    dead_refresh: list[str] = field(default_factory=list)  # revoked: just 401


@dataclass
class Link:
    """What the model believes about one short URL."""

    code: str
    owner: int
    is_active: bool = True
    deleted: bool = False

    @property
    def live(self) -> bool:
        return self.is_active and not self.deleted


def has_accounts(machine: "SecurityModel") -> bool:
    return bool(machine.accounts)


def has_accounts_and_links(machine: "SecurityModel") -> bool:
    return bool(machine.accounts and machine.links)


def has(kind: str) -> Callable[["SecurityModel"], bool]:
    """Precondition: some account holds a token of this kind (e.g. "used_refresh")."""
    return lambda machine: any(getattr(a, kind) for a in machine.accounts.values())


class SecurityModel(RuleBasedStateMachine):
    """Random sequences of actions, each answer checked against the rules."""

    def __init__(self, client: TestClient, run_db: RunDb) -> None:
        super().__init__()
        self.client = client
        self.run_db = run_db
        self.accounts: dict[int, Account] = {}
        self.links: dict[str, Link] = {}

    # ---------- helpers ----------

    def live_count(self, owner: int) -> int:
        return sum(1 for link in self.links.values() if link.owner == owner and link.live)

    def token_for(self, account: Account) -> tuple[str, bool]:
        """Pick the token a request is sent with, and whether it SHOULD work."""
        if account.good_access:
            return account.good_access[-1], account.active
        if account.revoked_access:
            return account.revoked_access[-1], False
        return "no-token-yet", False

    def call(self, method: str, path: str, account: Account, **kwargs: Any) -> tuple[Any, bool]:
        token, token_ok = self.token_for(account)
        headers = {"Authorization": f"Bearer {token}"}
        response = getattr(self.client, method)(path, headers=headers, **kwargs)
        return response, token_ok

    def pick_account(self, data: st.DataObject, holding: str | None = None) -> Account:
        """A random account; with `holding`, only accounts that hold that kind of token."""
        ids = sorted(a.id for a in self.accounts.values() if not holding or getattr(a, holding))
        return self.accounts[data.draw(st.sampled_from(ids), label="account")]

    def pick_link(self, data: st.DataObject) -> Link:
        return self.links[data.draw(st.sampled_from(sorted(self.links)), label="link")]

    def login(self, account: Account) -> None:
        response = self.client.post(
            f"{AUTH}/login/", data={"username": account.email, "password": PASSWORD}
        )
        if not account.active:
            assert response.status_code == 401, response.text
            return
        assert response.status_code == 200, response.text
        tokens = response.json()
        account.good_access.append(tokens["access_token"])
        account.unused_refresh.append(tokens["refresh_token"])

    # ---------- accounts ----------

    @rule(tier=st.sampled_from(list(UserTier)))
    def add_account(self, tier: UserTier) -> None:
        email = f"prop-{secrets.token_hex(6)}@example.com"

        async def create(session: AsyncSession) -> int:
            user = User(
                email=email,
                username=email.split("@")[0],
                hashed_password=PASSWORD_HASH,
                tier=tier,
            )
            session.add(user)
            await session.flush()
            return user.id

        account = Account(id=self.run_db(create), email=email, tier=tier)
        self.accounts[account.id] = account
        self.login(account)

    @precondition(has_accounts)
    @rule(data=st.data())
    def login_again(self, data: st.DataObject) -> None:
        self.login(self.pick_account(data))

    @precondition(has_accounts)
    @rule(data=st.data(), tier=st.sampled_from(list(UserTier)))
    def change_tier(self, data: st.DataObject, tier: UserTier) -> None:
        account = self.pick_account(data)
        self.run_db(
            lambda session: session.execute(
                update(User).where(User.id == account.id).values(tier=tier)
            )
        )
        account.tier = tier

    @precondition(has_accounts)
    @rule(data=st.data(), active=st.booleans())
    def switch_account(self, data: st.DataObject, active: bool) -> None:
        account = self.pick_account(data)
        self.run_db(
            lambda session: session.execute(
                update(User).where(User.id == account.id).values(is_active=active)
            )
        )
        account.active = active

    # ---------- tokens ----------

    @precondition(has_accounts)
    @rule(data=st.data())
    def me(self, data: st.DataObject) -> None:
        account = self.pick_account(data)
        response, token_ok = self.call("get", f"{AUTH}/me/", account)
        assert response.status_code == (200 if token_ok else 401), response.text
        if token_ok:
            assert response.json()["id"] == account.id

    @precondition(has("revoked_access"))
    @rule(data=st.data())
    def use_a_revoked_access_token(self, data: st.DataObject) -> None:
        account = self.pick_account(data, holding="revoked_access")
        token = data.draw(st.sampled_from(account.revoked_access), label="revoked access")
        response = self.client.get(f"{AUTH}/me/", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401, response.text

    @precondition(has("unused_refresh"))
    @rule(data=st.data())
    def refresh_with_the_newest_token(self, data: st.DataObject) -> None:
        account = self.pick_account(data, holding="unused_refresh")
        token = account.unused_refresh.pop()
        response = self.client.post(f"{AUTH}/refresh/", json={"refresh_token": token})
        account.used_refresh.append(token)  # consumed, even when the answer is 401
        if not account.active:
            assert response.status_code == 401, response.text
            return
        assert response.status_code == 200, response.text
        tokens = response.json()
        account.good_access.append(tokens["access_token"])
        account.unused_refresh.append(tokens["refresh_token"])

    @precondition(has("used_refresh"))
    @rule(data=st.data())
    def replay_a_used_refresh_token(self, data: st.DataObject) -> None:
        """The theft case: a copy of an already used refresh token comes back."""
        account = self.pick_account(data, holding="used_refresh")
        stolen = data.draw(st.sampled_from(account.used_refresh), label="stolen")
        response = self.client.post(f"{AUTH}/refresh/", json={"refresh_token": stolen})
        assert response.status_code == 401, response.text
        # Every token of the account dies at once, access tokens included. The
        # server deletes the refresh rows, so a LATER replay of any of them is just
        # an unknown token (401, no second revocation): logins made after this
        # moment are the real user's and must keep working.
        account.revoked_access.extend(account.good_access)
        account.good_access.clear()
        account.dead_refresh.extend(account.unused_refresh + account.used_refresh)
        account.unused_refresh.clear()
        account.used_refresh.clear()

    @precondition(has("dead_refresh"))
    @rule(data=st.data())
    def use_a_revoked_refresh_token(self, data: st.DataObject) -> None:
        account = self.pick_account(data, holding="dead_refresh")
        token = data.draw(st.sampled_from(account.dead_refresh), label="revoked refresh")
        response = self.client.post(f"{AUTH}/refresh/", json={"refresh_token": token})
        assert response.status_code == 401, response.text

    # ---------- links ----------

    @precondition(has_accounts)
    @rule(data=st.data(), with_alias=st.booleans())
    def create_link(self, data: st.DataObject, with_alias: bool) -> None:
        account = self.pick_account(data)
        body: dict[str, Any] = {"original_url": TARGET}
        if with_alias:
            body["custom_alias"] = secrets.token_hex(4)
        response, token_ok = self.call("post", f"{URLS}/", account, json=body)

        is_free = account.tier == UserTier.FREE
        if not token_ok:
            expected = 401
        elif is_free and (with_alias or self.live_count(account.id) >= SCALED_FREE_LIMIT):
            expected = 403  # no alias on Free, and no live link past the limit
        else:
            expected = 201
        assert response.status_code == expected, response.text
        if expected == 201:
            code = response.json()["short_code"]
            self.links[code] = Link(code=code, owner=account.id)

    def expected_access(self, account: Account, token_ok: bool, link: Link) -> int | None:
        """401 / 404 / 403 for reading or changing a link, or None if allowed."""
        if not token_ok:
            return 401
        if link.deleted:
            return 404
        if link.owner != account.id and account.tier != UserTier.ADMIN:
            return 403
        return None

    @precondition(has_accounts_and_links)
    @rule(data=st.data())
    def read_link(self, data: st.DataObject) -> None:
        account, link = self.pick_account(data), self.pick_link(data)
        response, token_ok = self.call("get", f"{URLS}/{link.code}/", account)
        refused = self.expected_access(account, token_ok, link)
        assert response.status_code == (refused or 200), response.text
        if refused is None:
            assert response.json()["owner"]["id"] == link.owner

    @precondition(has_accounts_and_links)
    @rule(data=st.data(), switch_on=st.booleans())
    def switch_link(self, data: st.DataObject, switch_on: bool) -> None:
        account, link = self.pick_account(data), self.pick_link(data)
        response, token_ok = self.call(
            "put", f"{URLS}/{link.code}/", account, json={"is_active": switch_on}
        )
        expected = self.expected_access(account, token_ok, link)
        if expected is None:
            owner = self.accounts[link.owner]
            comes_back = switch_on and not link.is_active
            at_limit = self.live_count(owner.id) >= SCALED_FREE_LIMIT
            expected = 403 if comes_back and owner.tier == UserTier.FREE and at_limit else 200
        assert response.status_code == expected, response.text
        if expected == 200:
            link.is_active = switch_on

    @precondition(has_accounts_and_links)
    @rule(data=st.data())
    def delete_link(self, data: st.DataObject) -> None:
        account, link = self.pick_account(data), self.pick_link(data)
        response, token_ok = self.call("delete", f"{URLS}/{link.code}/", account)
        refused = self.expected_access(account, token_ok, link)
        assert response.status_code == (refused or 204), response.text
        if refused is None:
            link.deleted = True

    @precondition(has_accounts_and_links)
    @rule(data=st.data())
    def analytics(self, data: st.DataObject) -> None:
        account, link = self.pick_account(data), self.pick_link(data)
        response, token_ok = self.call("get", f"/api/v1/analytics/{link.code}/", account)
        if not token_ok:
            expected = 401
        elif account.tier == UserTier.FREE:
            expected = 403  # the tier is checked before the code
        else:
            expected = self.expected_access(account, token_ok, link) or 200
        assert response.status_code == expected, response.text

    # ---------- what must be true after EVERY step ----------

    @invariant()
    def live_links_in_the_database_match_the_rules(self) -> None:
        if not self.accounts:
            return

        async def counts(session: AsyncSession) -> dict[int, int]:
            rows = await session.execute(
                select(URL.owner_id, func.count())
                .where(URL.owner_id.in_(self.accounts), URL.is_live(utc_now()))
                .group_by(URL.owner_id)
            )
            return dict(rows.tuples().all())

        in_database = self.run_db(counts)
        for account_id in self.accounts:
            assert in_database.get(account_id, 0) == self.live_count(account_id)

    # ---------- listing: never someone else's link ----------

    @precondition(has_accounts)
    @rule(data=st.data())
    def list_my_links(self, data: st.DataObject) -> None:
        account = self.pick_account(data)
        response, token_ok = self.call("get", f"{URLS}/", account, params={"limit": 100})
        if not token_ok:
            assert response.status_code == 401, response.text
            return
        listed = {item["short_code"] for item in response.json()["items"]}
        mine = {c for c, link in self.links.items() if link.owner == account.id}
        assert listed <= mine, f"user {account.id} sees links of others: {listed - mine}"
        assert listed == {c for c in mine if not self.links[c].deleted}


def fast_password_check(password: str, hashed_password: str) -> bool:
    """A quick stand-in for Argon2 in THIS test only (hashing has its own tests).

    Argon2 is slow on purpose (about 0.3 s): with hundreds of logins it would make
    this test take many minutes. The rules tested here do not depend on it.
    """
    return hashed_password == PASSWORD_HASH and password == PASSWORD


@pytest.fixture
def test_conditions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift the rate limits, scale the Free limit down, and use a fast password check."""
    current = get_settings()
    for name in (
        "login_rate_limit",
        "register_rate_limit",
        "create_url_rate_limit",
        "login_account_rate_limit",
    ):
        monkeypatch.setattr(current, name, "1000000/minute")
    monkeypatch.setattr(url_service_module, "FREE_URL_LIMIT", SCALED_FREE_LIMIT)
    monkeypatch.setattr(auth_service_module, "verify_password", fast_password_check)


def test_random_sequences_never_break_the_security_rules(
    db_client: TestClient, run_db: RunDb, test_conditions: None
) -> None:
    run_state_machine_as_test(
        lambda: SecurityModel(db_client, run_db),
        settings=settings(PROFILE, suppress_health_check=[HealthCheck.too_slow]),
    )
