# GeoIP data

Click locations (country, city) come from a city database in this folder, read by the
Celery worker. The data file is downloaded, never committed (it is git-ignored and
never built into the image; the worker mounts this folder read-only).

## Default: DB-IP City Lite (free, no account)

```bash
python -m app.scripts.download_geoip      # -> geoip/dbip-city-lite.mmdb (~130 MB)
docker compose up -d celery-worker        # the worker opens the new file
```

DB-IP publishes a new file every month: run the command again to refresh it. The script
checks the download before replacing the old file, so a failed download changes nothing.
License: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), credit:
"IP Geolocation by DB-IP" (https://db-ip.com). In Kubernetes an init container runs the
same script at every pod start (deploy/k8s/worker.yaml).

## Alternative: MaxMind GeoLite2-City

Same `.mmdb` format, a little more precise, needs a free MaxMind account: download
"GeoLite2 City" into this folder and set
`GEOIP_DATABASE_PATH=/app/geoip/GeoLite2-City.mmdb` (in `.env`).

Without any file, clicks are still saved, with an "Unknown" location.
