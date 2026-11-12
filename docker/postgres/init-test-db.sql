-- Runs once, the first time the Postgres container starts with an empty volume.
-- The main database (POSTGRES_DB) is created automatically; this adds a second,
-- separate database that only the tests use.
CREATE DATABASE url_shortener_test;
