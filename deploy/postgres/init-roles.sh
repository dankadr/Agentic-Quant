#!/bin/bash
# Runs once, on first container start, as the postgres superuser.
# trading_owner runs migrations; trading_app gets DML only (no TRUNCATE, no DDL).
set -euo pipefail
OWNER_PW=$(cat /run/secrets/db_owner_password)
APP_PW=$(cat /run/secrets/db_app_password)

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE trading_owner LOGIN PASSWORD '${OWNER_PW}';
CREATE ROLE trading_app LOGIN PASSWORD '${APP_PW}';
CREATE DATABASE trading OWNER trading_owner;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname trading <<SQL
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO trading_owner;
GRANT USAGE ON SCHEMA public TO trading_app;
SQL
