-- Runs once, on an empty data directory. If the pgdata volume already exists
-- this script is skipped, so never rely on it for migrations.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Agno's PgVector defaults to the `ai` schema for knowledge tables; AgentOS
-- session/trace tables live in `public`.
CREATE SCHEMA IF NOT EXISTS ai AUTHORIZATION CURRENT_USER;
