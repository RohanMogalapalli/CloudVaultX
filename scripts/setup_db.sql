-- CloudVaultX: create the application database and user in local PostgreSQL.
-- Run as the postgres superuser (psql will prompt for the install password):
--
--   "C:\Program Files\PostgreSQL\18\bin\psql" -U postgres -h localhost -f scripts\setup_db.sql
--
-- Adjust the password below if you want something other than cloudvaultx
-- (keep it in sync with DATABASE_URL in .env).

CREATE USER cloudvaultx WITH PASSWORD 'cloudvaultx';
CREATE DATABASE cloudvaultx OWNER cloudvaultx;
GRANT ALL PRIVILEGES ON DATABASE cloudvaultx TO cloudvaultx;
