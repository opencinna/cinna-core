#!/bin/bash
# Creates the test database if it does not already exist.
# This script is mounted into /docker-entrypoint-initdb.d/ and runs
# automatically when the PostgreSQL container initializes for the first time.

set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE app_test'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'app_test')\gexec
EOSQL
