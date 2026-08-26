-- One-time trust establishment. Run locally as an account administrator.
-- Values are substituted by Snowflake CLI using <% variable %> syntax.

USE ROLE ACCOUNTADMIN;

CREATE ROLE IF NOT EXISTS <% bootstrap_role %>;
CREATE ROLE IF NOT EXISTS <% deploy_role %>;

CREATE USER IF NOT EXISTS <% bootstrap_user %>
  TYPE = SERVICE
  DEFAULT_ROLE = <% bootstrap_role %>
  WORKLOAD_IDENTITY = (
    TYPE = OIDC
    ISSUER = 'https://token.actions.githubusercontent.com'
    SUBJECT = 'repo:<% github_repository %>:environment:bootstrap'
  )
  COMMENT = 'GitHub OIDC bootstrap identity for <% github_repository %>';

CREATE USER IF NOT EXISTS <% deploy_user %>
  TYPE = SERVICE
  DEFAULT_ROLE = <% deploy_role %>
  WORKLOAD_IDENTITY = (
    TYPE = OIDC
    ISSUER = 'https://token.actions.githubusercontent.com'
    SUBJECT = 'repo:<% github_repository %>:environment:production'
  )
  COMMENT = 'GitHub OIDC production identity for <% github_repository %>';

GRANT ROLE <% bootstrap_role %> TO USER <% bootstrap_user %>;
GRANT ROLE <% deploy_role %> TO USER <% deploy_user %>;
GRANT ROLE <% deploy_role %> TO ROLE <% bootstrap_role %>;

GRANT CREATE DATABASE ON ACCOUNT TO ROLE <% bootstrap_role %>;
GRANT CREATE WAREHOUSE ON ACCOUNT TO ROLE <% bootstrap_role %>;
GRANT CREATE COMPUTE POOL ON ACCOUNT TO ROLE <% bootstrap_role %>;
GRANT CREATE INTEGRATION ON ACCOUNT TO ROLE <% bootstrap_role %>;
GRANT CREATE EXTERNAL ACCESS INTEGRATION ON ACCOUNT TO ROLE <% bootstrap_role %>;
GRANT BIND SERVICE ENDPOINT ON ACCOUNT TO ROLE <% bootstrap_role %>;
