# GeoIP data (optional)

Put MaxMind's **GeoLite2-City.mmdb** here to give clicks a country and a city.

1. Create a free account at <https://www.maxmind.com/en/geolite2/signup>.
2. Download "GeoLite2 City" (format: GeoIP2 Binary `.mmdb`) and put the file in this folder.
3. Set `GEOIP_DATABASE_PATH=/app/geoip/GeoLite2-City.mmdb` (in `.env`), then
   `docker compose up -d celery-worker`.

The file is licensed data: it is git-ignored and never built into the image (the worker
mounts this folder read-only). MaxMind updates it weekly; replace the file and restart
the worker. Without it, clicks are saved with an "Unknown" location.
