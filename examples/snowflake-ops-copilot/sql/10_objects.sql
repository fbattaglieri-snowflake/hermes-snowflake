-- =============================================================================
-- 10_objects.sql — Synthetic Tables, Semantic View & Cortex Search Service
-- =============================================================================

USE ROLE ACCOUNTADMIN;
USE DATABASE HERMES_DEMO;
USE SCHEMA OPS;
USE WAREHOUSE HERMES_DEMO_WH;

CREATE TABLE IF NOT EXISTS HERMES_DEMO.OPS.SERVICE_INCIDENTS (
  INCIDENT_ID         VARCHAR(32)   NOT NULL PRIMARY KEY,
  OPENED_AT           TIMESTAMP_NTZ NOT NULL,
  RESOLVED_AT         TIMESTAMP_NTZ,
  REGION              VARCHAR(32)   NOT NULL,
  SERVICE_NAME        VARCHAR(64)   NOT NULL,
  SEVERITY            VARCHAR(16)   NOT NULL,
  STATUS              VARCHAR(24)   NOT NULL,
  DURATION_MINUTES    NUMBER(10, 0) NOT NULL,
  AFFECTED_TENANTS    NUMBER(10, 0) NOT NULL,
  ROOT_CAUSE_CATEGORY VARCHAR(64)   NOT NULL,
  SUMMARY             VARCHAR(1024) NOT NULL
)
COMMENT = 'Synthetic cloud service incident telemetry for Hermes SQL & Cortex Analyst demos';

CREATE TABLE IF NOT EXISTS HERMES_DEMO.OPS.RUNBOOK_ARTICLES (
  DOC_ID              VARCHAR(32)   NOT NULL PRIMARY KEY,
  SERVICE_NAME        VARCHAR(64)   NOT NULL,
  ROOT_CAUSE_CATEGORY VARCHAR(64)   NOT NULL,
  TITLE               VARCHAR(256)  NOT NULL,
  REMEDIATION_STEPS   VARCHAR(4096) NOT NULL,
  UPDATED_AT          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Synthetic operational runbooks indexed by Cortex Search';

ALTER TABLE HERMES_DEMO.OPS.RUNBOOK_ARTICLES SET CHANGE_TRACKING = TRUE;

-- Semantic View for governed natural-language analytics (Cortex Analyst)
CREATE OR REPLACE SEMANTIC VIEW HERMES_DEMO.OPS.INCIDENT_ANALYTICS
  TABLES (
    incidents AS HERMES_DEMO.OPS.SERVICE_INCIDENTS
      PRIMARY KEY (INCIDENT_ID)
      WITH SYNONYMS = ('outages', 'alerts', 'service incidents')
      COMMENT = 'Synthetic cloud service incidents'
  )
  FACTS (
    incidents.duration_minutes AS DURATION_MINUTES
      COMMENT = 'Incident duration in minutes',
    incidents.affected_tenants AS AFFECTED_TENANTS
      COMMENT = 'Number of tenants affected by the incident'
  )
  DIMENSIONS (
    incidents.incident_id AS INCIDENT_ID,
    incidents.opened_at AS OPENED_AT,
    incidents.region AS REGION,
    incidents.service_name AS SERVICE_NAME,
    incidents.severity AS SEVERITY,
    incidents.status AS STATUS,
    incidents.root_cause_category AS ROOT_CAUSE_CATEGORY
  )
  METRICS (
    incidents.incident_count AS COUNT(incidents.incident_id)
      COMMENT = 'Total number of incidents',
    incidents.mttr_minutes AS AVG(incidents.duration_minutes)
      COMMENT = 'Mean time to resolution in minutes',
    incidents.total_affected_tenants AS SUM(incidents.affected_tenants)
      COMMENT = 'Total affected tenants across incidents'
  )
  COMMENT = 'Governed semantic view over synthetic incident telemetry';

-- Cortex Search service over synthetic runbooks
CREATE OR REPLACE CORTEX SEARCH SERVICE HERMES_DEMO.OPS.RUNBOOK_SEARCH_SVC
  ON REMEDIATION_STEPS
  ATTRIBUTES SERVICE_NAME, ROOT_CAUSE_CATEGORY, TITLE
  WAREHOUSE = HERMES_DEMO_WH
  TARGET_LAG = '1 hour'
  AS (
    SELECT
      DOC_ID,
      SERVICE_NAME,
      ROOT_CAUSE_CATEGORY,
      TITLE,
      REMEDIATION_STEPS
    FROM HERMES_DEMO.OPS.RUNBOOK_ARTICLES
  );

-- Explicit least-privilege object grants to HERMES_DEMO_ROLE
GRANT SELECT ON TABLE HERMES_DEMO.OPS.SERVICE_INCIDENTS TO ROLE HERMES_DEMO_ROLE;
GRANT SELECT ON TABLE HERMES_DEMO.OPS.RUNBOOK_ARTICLES TO ROLE HERMES_DEMO_ROLE;
GRANT SELECT, REFERENCES ON SEMANTIC VIEW HERMES_DEMO.OPS.INCIDENT_ANALYTICS TO ROLE HERMES_DEMO_ROLE;
GRANT USAGE ON CORTEX SEARCH SERVICE HERMES_DEMO.OPS.RUNBOOK_SEARCH_SVC TO ROLE HERMES_DEMO_ROLE;
