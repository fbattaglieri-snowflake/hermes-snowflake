-- =============================================================================
-- 20_seed.sql — Idempotent Synthetic Seed Data (Incidents & Runbooks)
-- =============================================================================

USE ROLE ACCOUNTADMIN;
USE DATABASE HERMES_DEMO;
USE SCHEMA OPS;
USE WAREHOUSE HERMES_DEMO_WH;

MERGE INTO HERMES_DEMO.OPS.SERVICE_INCIDENTS AS tgt
USING (
  SELECT * FROM VALUES
    ('INC-1001', '2026-11-10 08:15:00'::TIMESTAMP_NTZ, '2026-11-10 08:47:00'::TIMESTAMP_NTZ, 'eu-central-1', 'auth-gateway',       'SEV1', 'RESOLVED', 32, 145, 'TLS_CERT_ROTATION',    'Expired intermediate certificate caused intermittent HTTP 502 handshakes.'),
    ('INC-1002', '2026-11-11 14:05:00'::TIMESTAMP_NTZ, '2026-11-11 14:29:00'::TIMESTAMP_NTZ, 'us-east-1',    'billing-worker',     'SEV2', 'RESOLVED', 24,  38, 'QUEUE_BACKPRESSURE',   'Retry storm after upstream webhook timeout saturated worker pool.'),
    ('INC-1003', '2026-11-13 03:40:00'::TIMESTAMP_NTZ, '2026-11-13 04:55:00'::TIMESTAMP_NTZ, 'eu-central-1', 'vector-indexer',     'SEV2', 'RESOLVED', 75,  62, 'MEMORY_OOM',           'Unbounded chunk batching exceeded container 8Gi cgroup limit.'),
    ('INC-1004', '2026-11-15 19:10:00'::TIMESTAMP_NTZ, '2026-11-15 19:28:00'::TIMESTAMP_NTZ, 'ap-northeast-1','realtime-websocket', 'SEV3', 'RESOLVED', 18,  19, 'IDLE_INGRESS_TIMEOUT', 'Missing 30s WebSocket ping frame allowed 90s L7 proxy idle disconnect.'),
    ('INC-1005', '2026-11-16 11:20:00'::TIMESTAMP_NTZ, '2026-11-16 12:02:00'::TIMESTAMP_NTZ, 'us-west-2',    'llm-router',         'SEV1', 'RESOLVED', 42, 210, 'RATE_LIMIT_BURST',     'Uncached 180k-token context window exhausted 60s ITPM quota bucket.'),
    ('INC-1006', '2026-11-17 07:05:00'::TIMESTAMP_NTZ, NULL,                                  'eu-central-1', 'llm-router',         'SEV2', 'OPEN',     35,  54, 'RATE_LIMIT_BURST',     'Elevated HTTP 429 rate observed during morning scheduled digest batch.')
  AS src (
    INCIDENT_ID, OPENED_AT, RESOLVED_AT, REGION, SERVICE_NAME,
    SEVERITY, STATUS, DURATION_MINUTES, AFFECTED_TENANTS, ROOT_CAUSE_CATEGORY, SUMMARY
  )
) AS src
ON tgt.INCIDENT_ID = src.INCIDENT_ID
WHEN MATCHED THEN UPDATE SET
  tgt.RESOLVED_AT         = src.RESOLVED_AT,
  tgt.STATUS              = src.STATUS,
  tgt.DURATION_MINUTES    = src.DURATION_MINUTES,
  tgt.AFFECTED_TENANTS    = src.AFFECTED_TENANTS,
  tgt.ROOT_CAUSE_CATEGORY = src.ROOT_CAUSE_CATEGORY,
  tgt.SUMMARY             = src.SUMMARY
WHEN NOT MATCHED THEN INSERT (
  INCIDENT_ID, OPENED_AT, RESOLVED_AT, REGION, SERVICE_NAME,
  SEVERITY, STATUS, DURATION_MINUTES, AFFECTED_TENANTS, ROOT_CAUSE_CATEGORY, SUMMARY
) VALUES (
  src.INCIDENT_ID, src.OPENED_AT, src.RESOLVED_AT, src.REGION, src.SERVICE_NAME,
  src.SEVERITY, src.STATUS, src.DURATION_MINUTES, src.AFFECTED_TENANTS, src.ROOT_CAUSE_CATEGORY, src.SUMMARY
);

MERGE INTO HERMES_DEMO.OPS.RUNBOOK_ARTICLES AS tgt
USING (
  SELECT * FROM VALUES
    ('RB-01', 'auth-gateway',       'TLS_CERT_ROTATION',    'TLS Leaf & Intermediate Bundle Rotation', '1. Verify certificate chain order via openssl s_client. 2. Trigger secret reload in ingress deployment. 3. Confirm zero TLS handshake errors over 5m window.'),
    ('RB-02', 'billing-worker',     'QUEUE_BACKPRESSURE',   'Webhook Retry Storm & Dead-Letter Drain', '1. Enable bounded exponential backoff (3s, 7s, 15s, 30s, 45s). 2. Pause non-critical replay consumers. 3. Drain DLQ in batches of 100.'),
    ('RB-03', 'vector-indexer',     'MEMORY_OOM',           'SPCS Container Memory OOM Remediation',   '1. Cap batch_size to 64 documents. 2. Stream JSON payloads instead of buffering >32 MiB in memory. 3. Scale compute pool or raise memory limit to 16Gi.'),
    ('RB-04', 'realtime-websocket', 'IDLE_INGRESS_TIMEOUT', 'WebSocket Keepalive Behind 90s Ingress',  '1. Emit server ping frames every 25 seconds. 2. Re-issue single-use WS tickets on 4401 reconnect.'),
    ('RB-05', 'llm-router',         'RATE_LIMIT_BURST',     'Mitigating Upstream LLM HTTP 429 Bursts', '1. Route lightweight auxiliary tasks (titles, approvals) to claude-haiku-5-5. 2. Respect Retry-After headers across a 100s backoff window. 3. Inspect per-agent token spikes in TABLE(AGENT_TRACE_TABLE(''SNOWFLAKE'')).')
  AS src (DOC_ID, SERVICE_NAME, ROOT_CAUSE_CATEGORY, TITLE, REMEDIATION_STEPS)
) AS src
ON tgt.DOC_ID = src.DOC_ID
WHEN MATCHED THEN UPDATE SET
  tgt.TITLE             = src.TITLE,
  tgt.REMEDIATION_STEPS = src.REMEDIATION_STEPS,
  tgt.UPDATED_AT        = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (
  DOC_ID, SERVICE_NAME, ROOT_CAUSE_CATEGORY, TITLE, REMEDIATION_STEPS
) VALUES (
  src.DOC_ID, src.SERVICE_NAME, src.ROOT_CAUSE_CATEGORY, src.TITLE, src.REMEDIATION_STEPS
);
