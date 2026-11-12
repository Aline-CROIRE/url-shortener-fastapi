#!/bin/sh
# Allow a read replica to stream changes from this (primary) database.
#
# Runs automatically when a NEW database volume is initialised
# (/docker-entrypoint-initdb.d). For an existing volume, run it once by hand:
#   docker compose exec db sh /docker-entrypoint-initdb.d/enable-replication.sh
# Safe to run more than once (idempotent). Development only: the password is fixed.
set -e

REPLICATION_USER="replicator"
REPLICATION_PASSWORD="replicator"
HBA_FILE="${PGDATA:-/var/lib/postgresql/data}/pg_hba.conf"
HBA_RULE="host replication ${REPLICATION_USER} all scram-sha-256"

# 1. A role that may only replicate (it cannot log in to read the tables)
psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-postgres}" --dbname postgres <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${REPLICATION_USER}') THEN
    CREATE ROLE ${REPLICATION_USER} WITH REPLICATION LOGIN PASSWORD '${REPLICATION_PASSWORD}';
  END IF;
END
\$\$;
SQL

# 2. The "door rule": replication connections from other containers, with a password
if ! grep -qF "${HBA_RULE}" "${HBA_FILE}"; then
  echo "${HBA_RULE}" >> "${HBA_FILE}"
fi

# 3. Re-read pg_hba.conf without restarting the database
psql --username "${POSTGRES_USER:-postgres}" --dbname postgres -c "SELECT pg_reload_conf();" >/dev/null
echo "Replication enabled for role '${REPLICATION_USER}'."
