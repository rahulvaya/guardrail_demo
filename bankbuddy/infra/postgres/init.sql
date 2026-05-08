-- =============================================================================
-- BankBuddy - PostgreSQL bootstrap
--
-- Creates three schemas, each owned by a service-specific user:
--   app          - owned by app_user   (used by `api` service)
--   agent_memory - owned by agent_user (used by `agent` service for LangGraph checkpoints)
--   bank         - owned by bank_user  (used by `mock-bank` service)
--
-- Each user can only access its own schema. Defense-in-depth: even if one
-- service is compromised, the others' data is not directly readable.
-- =============================================================================

-- Read service credentials from environment (provided by the postgres image
-- via the entrypoint). We use psql variables sourced from \getenv.

\set app_user           `echo "$APP_DB_USER"`
\set app_password       `echo "$APP_DB_PASSWORD"`
\set agent_user         `echo "$AGENT_DB_USER"`
\set agent_password     `echo "$AGENT_DB_PASSWORD"`
\set bank_user          `echo "$BANK_DB_USER"`
\set bank_password      `echo "$BANK_DB_PASSWORD"`

-- ---------------------------------------------------------------------------
-- Roles
-- ---------------------------------------------------------------------------
CREATE ROLE :"app_user"   LOGIN PASSWORD :'app_password';
CREATE ROLE :"agent_user" LOGIN PASSWORD :'agent_password';
CREATE ROLE :"bank_user"  LOGIN PASSWORD :'bank_password';

-- ---------------------------------------------------------------------------
-- Schemas (owned by their respective service users)
-- ---------------------------------------------------------------------------
CREATE SCHEMA app          AUTHORIZATION :"app_user";
CREATE SCHEMA agent_memory AUTHORIZATION :"agent_user";
CREATE SCHEMA bank         AUTHORIZATION :"bank_user";

-- ---------------------------------------------------------------------------
-- Connect privileges
-- ---------------------------------------------------------------------------
GRANT CONNECT ON DATABASE bankbuddy TO :"app_user", :"agent_user", :"bank_user";

-- Each user defaults to its own schema search_path.
ALTER ROLE :"app_user"   SET search_path = app, public;
ALTER ROLE :"agent_user" SET search_path = agent_memory, public;
ALTER ROLE :"bank_user"  SET search_path = bank, public;

-- ---------------------------------------------------------------------------
-- Note: tables inside each schema are created by their respective services
-- on startup (SQLAlchemy / LangGraph PostgresSaver migrations).
-- ---------------------------------------------------------------------------
