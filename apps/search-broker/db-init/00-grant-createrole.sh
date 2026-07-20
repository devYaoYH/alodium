#!/bin/bash
# Grant CREATEROLE to POSTGRES_USER so init SQL scripts can create roles.
#
# PostgreSQL's docker-entrypoint runs init scripts as POSTGRES_USER
# (search_audit_owner).  This user is created by the entrypoint as a regular
# role without CREATEROLE, so any CREATE ROLE in SQL init scripts fails with
# "permission denied".  We run BEFORE the SQL scripts (alphabetical ordering)
# and connect as the local postgres superuser (trust auth on the Unix socket)
# to add CREATEROLE.  The SQL scripts that follow can then create the writer
# and reader roles without error.
set -e

psql -v ON_ERROR_STOP=1 --username postgres --dbname "$POSTGRES_DB" \
  -c "ALTER USER ${POSTGRES_USER} CREATEROLE;"
