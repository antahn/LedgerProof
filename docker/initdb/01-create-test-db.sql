-- Runs once on first container start (docker-entrypoint-initdb.d).
-- POSTGRES_DB creates `ledgerproof`; the test database is created here.
CREATE DATABASE ledgerproof_test;
