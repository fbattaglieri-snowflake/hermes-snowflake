-- =============================================================================
-- 00_role_and_grants.sql — Least-Privilege Snowflake Role for Hermes Demo
-- =============================================================================
-- Creates a self-contained demo database, warehouse, and least-privilege role
-- (`HERMES_DEMO_ROLE`) so external readers without the maintainer's Snowflake
-- account can run the Hermes Snowflake Ops Copilot skill out of the box.
-- =============================================================================

USE ROLE ACCOUNTADMIN;

CREATE WAREHOUSE IF NOT EXISTS HERMES_DEMO_WH
  WAREHOUSE_SIZE = XSMALL
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE
  COMMENT = 'Synthetic demo warehouse for Hermes on Snowflake';

CREATE DATABASE IF NOT EXISTS HERMES_DEMO
  COMMENT = 'Synthetic incident & runbook data for Hermes Agent examples';

CREATE SCHEMA IF NOT EXISTS HERMES_DEMO.OPS;

CREATE ROLE IF NOT EXISTS HERMES_DEMO_ROLE
  COMMENT = 'Least-privilege role for Hermes Snowflake Ops Copilot example';

-- Core object grants
GRANT USAGE ON WAREHOUSE HERMES_DEMO_WH TO ROLE HERMES_DEMO_ROLE;
GRANT USAGE ON DATABASE HERMES_DEMO TO ROLE HERMES_DEMO_ROLE;
GRANT USAGE ON SCHEMA HERMES_DEMO.OPS TO ROLE HERMES_DEMO_ROLE;
GRANT SELECT ON FUTURE TABLES IN SCHEMA HERMES_DEMO.OPS TO ROLE HERMES_DEMO_ROLE;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA HERMES_DEMO.OPS TO ROLE HERMES_DEMO_ROLE;

-- Cortex AI inference entitlement (required for Cortex Search, Analyst, and LLM calls)
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE HERMES_DEMO_ROLE;

-- Optional (Option B — Snowflake Cortex AI Gateway):
-- Grant USAGE to send inference through AI GATEWAY SNOWFLAKE, and MONITOR if you
-- want Hermes to inspect its own OpenTelemetry spans in AGENT_TRACE_TABLE('SNOWFLAKE').
GRANT USAGE ON AI GATEWAY SNOWFLAKE TO ROLE HERMES_DEMO_ROLE;
GRANT MONITOR ON AI GATEWAY SNOWFLAKE TO ROLE HERMES_DEMO_ROLE;

-- Grant HERMES_DEMO_ROLE to your Hermes SPCS deploy role (adjust if customized)
-- so in-container `/snowflake/session/token` SQL API calls can `USE ROLE HERMES_DEMO_ROLE`:
-- GRANT ROLE HERMES_DEMO_ROLE TO ROLE HERMES_GITHUB_DEPLOY_ROLE;
GRANT ROLE HERMES_DEMO_ROLE TO ROLE SYSADMIN;
