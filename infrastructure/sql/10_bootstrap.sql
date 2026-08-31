-- Creates all reusable Snowflake objects for the Hermes on SPCS deployment.
-- Secrets that require user-supplied values (SSH key, Cloudflare token, Telegram)
-- are created with empty strings. Set them manually before deploying the service:
--
--   ALTER SECRET <% database %>.<% schema %>.HERMES_SSH_PUBKEY
--     SET SECRET_STRING = '<your public key>';
--
-- CF_TUNNEL_TOKEN, TELEGRAM_BOT_TOKEN, and TELEGRAM_HOME_CHANNEL are optional.

USE ROLE <% bootstrap_role %>;

CREATE DATABASE IF NOT EXISTS <% database %>
  COMMENT = 'Hermes Agent on Snowflake SPCS';
CREATE SCHEMA IF NOT EXISTS <% database %>.<% schema %>;

CREATE WAREHOUSE IF NOT EXISTS <% warehouse %>
  WAREHOUSE_SIZE = XSMALL
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE
  COMMENT = 'Deployment warehouse for Hermes on Snowflake';

CREATE COMPUTE POOL IF NOT EXISTS <% compute_pool %>
  MIN_NODES = 1
  MAX_NODES = 1
  INSTANCE_FAMILY = <% instance_family %>
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE
  AUTO_SUSPEND_SECS = 3600
  COMMENT = 'SPCS compute pool for Hermes';

CREATE IMAGE REPOSITORY IF NOT EXISTS
  <% database %>.<% schema %>.<% hermes_image_repository %>;

CREATE STAGE IF NOT EXISTS <% database %>.<% schema %>.HERMES_CONFIG
  DIRECTORY = (ENABLE = TRUE)
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
  COMMENT = 'Deployment specs for Hermes';

CREATE NETWORK RULE IF NOT EXISTS <% database %>.<% schema %>.HERMES_EGRESS_WEB
  TYPE = HOST_PORT
  MODE = EGRESS
  VALUE_LIST = ('0.0.0.0:443', '0.0.0.0:80')
  COMMENT = 'Outbound web egress for Hermes (Cloudflare tunnel, Telegram, agent tools)';

CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS <% egress_integration %>
  ALLOWED_NETWORK_RULES = (<% database %>.<% schema %>.HERMES_EGRESS_WEB)
  ALLOWED_AUTHENTICATION_SECRETS = NONE
  ENABLED = TRUE
  COMMENT = 'Outbound web access for Hermes agent and tunnel';

-- SSH public key for remote access. Must be set before deploying.
CREATE SECRET IF NOT EXISTS <% database %>.<% schema %>.HERMES_SSH_PUBKEY
  TYPE = GENERIC_STRING
  SECRET_STRING = ''
  COMMENT = 'SSH public key for Hermes remote access. Set before deploying the service.';

-- Cloudflare tunnel token: optional; enables persistent remote access.
CREATE SECRET IF NOT EXISTS <% database %>.<% schema %>.CF_TUNNEL_TOKEN
  TYPE = GENERIC_STRING
  SECRET_STRING = ''
  COMMENT = 'Cloudflare tunnel token. Leave empty to disable the tunnel.';

-- Telegram: optional; enables hermes send --to telegram.
CREATE SECRET IF NOT EXISTS <% database %>.<% schema %>.TELEGRAM_BOT_TOKEN
  TYPE = GENERIC_STRING
  SECRET_STRING = ''
  COMMENT = 'Telegram bot token. Leave empty to disable Telegram notifications.';

CREATE SECRET IF NOT EXISTS <% database %>.<% schema %>.TELEGRAM_HOME_CHANNEL
  TYPE = GENERIC_STRING
  SECRET_STRING = ''
  COMMENT = 'Telegram home channel ID (chat_id). Required when bot token is set.';

-- Tailscale: optional; enables the Desktop Remote gateway on port 9119.
-- Must be a REUSABLE, NON-EPHEMERAL auth key: a single-use key works on the
-- first boot only, and an ephemeral one makes Tailscale drop the node when it
-- goes offline, changing the tailnet IP on every restart. Needed only until
-- tailscaled state exists on the block volume, after which the node rejoins on
-- its own and keeps the same IP.
CREATE SECRET IF NOT EXISTS <% database %>.<% schema %>.TS_AUTHKEY
  TYPE = GENERIC_STRING
  SECRET_STRING = ''
  COMMENT = 'Tailscale reusable auth key (tskey-auth-...). Leave empty to disable the Desktop Remote gateway.';

GRANT USAGE ON DATABASE <% database %> TO ROLE <% deploy_role %>;
GRANT USAGE ON SCHEMA <% database %>.<% schema %> TO ROLE <% deploy_role %>;
GRANT USAGE ON WAREHOUSE <% warehouse %> TO ROLE <% deploy_role %>;
GRANT USAGE, MONITOR, OPERATE ON COMPUTE POOL <% compute_pool %> TO ROLE <% deploy_role %>;
GRANT READ, WRITE ON IMAGE REPOSITORY
  <% database %>.<% schema %>.<% hermes_image_repository %> TO ROLE <% deploy_role %>;
GRANT READ, WRITE ON STAGE <% database %>.<% schema %>.HERMES_CONFIG TO ROLE <% deploy_role %>;
GRANT CREATE SERVICE ON SCHEMA <% database %>.<% schema %> TO ROLE <% deploy_role %>;
GRANT USAGE ON INTEGRATION <% egress_integration %> TO ROLE <% deploy_role %>;
GRANT READ ON SECRET <% database %>.<% schema %>.HERMES_SSH_PUBKEY TO ROLE <% deploy_role %>;
GRANT READ ON SECRET <% database %>.<% schema %>.CF_TUNNEL_TOKEN TO ROLE <% deploy_role %>;
GRANT READ ON SECRET <% database %>.<% schema %>.TELEGRAM_BOT_TOKEN TO ROLE <% deploy_role %>;
GRANT READ ON SECRET <% database %>.<% schema %>.TELEGRAM_HOME_CHANNEL TO ROLE <% deploy_role %>;
GRANT BIND SERVICE ENDPOINT ON ACCOUNT TO ROLE <% deploy_role %>;
