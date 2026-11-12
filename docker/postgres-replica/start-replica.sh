#!/bin/sh
# Start a READ-ONLY replica of the "db" service.
#
# First start (empty volume): copy the whole primary with pg_basebackup.
#   -R writes standby.signal + the connection info, so Postgres starts as a
#      standby that keeps streaming changes from the primary.
# Later starts: the copy already exists; Postgres just continues following.
set -e

PGDATA="${PGDATA:-/var/lib/postgresql/data}"
PRIMARY="host=db port=5432 user=replicator password=replicator"

if [ ! -s "${PGDATA}/PG_VERSION" ]; then
  echo "Cloning the primary into ${PGDATA} ..."
  mkdir -p "${PGDATA}"
  chown -R postgres:postgres "${PGDATA}"
  chmod 700 "${PGDATA}"
  # The primary may still be starting: try again until it answers
  until gosu postgres pg_basebackup --dbname="${PRIMARY}" --pgdata="${PGDATA}" \
        --wal-method=stream --write-recovery-conf --progress; do
    echo "Primary not ready yet, retrying in 2 seconds ..."
    rm -rf "${PGDATA:?}"/*
    sleep 2
  done
fi

# Run Postgres as the "postgres" user (never as root)
exec gosu postgres postgres -c hot_standby=on
