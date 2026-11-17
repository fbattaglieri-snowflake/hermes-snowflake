---
name: snowflake-ops-copilot
description: Query synthetic Snowflake incident telemetry, Cortex Search runbooks, and Cortex AI Gateway OpenTelemetry traces from inside SPCS using the rotating OAuth session token.
---

# Snowflake Ops Copilot (SPCS Synthetic Demo Skill)

Use this skill whenever the user asks about **service incidents, MTTR, affected tenants, operational runbooks, or Cortex AI Gateway trace telemetry** in the `HERMES_DEMO.OPS` demo environment.

## 1. Zero-Secret Authentication Inside SPCS

Inside `HERMES_SERVICE`, Snowflake automatically mounts and rotates `/snowflake/session/token`. Never hardcode PATs or passwords. Always authenticate against `https://${SNOWFLAKE_HOST}/api/v2/statements` with `X-Snowflake-Authorization-Token-Type: OAUTH` and least-privilege role `HERMES_DEMO_ROLE`.

> **Snowflake SQL API Binding Rule:** Every scalar `value` inside `bindings` in `/api/v2/statements` **must be serialized as a JSON string** (e.g., `"10"`, `"true"` with `TO_BOOLEAN(?)` in SQL — never raw JSON `true` or raw integer `10`).

## 2. Querying Structured Incident Telemetry (`HERMES_DEMO.OPS.SERVICE_INCIDENTS`)

Run via the `terminal` tool:

```bash
python3 - <<'PY'
import json, os, pathlib, urllib.request

host = os.environ["SNOWFLAKE_HOST"].replace("_", "-")
token = pathlib.Path("/snowflake/session/token").read_text().strip()
payload = {
    "statement": """
        SELECT INCIDENT_ID, REGION, SERVICE_NAME, SEVERITY, STATUS,
               DURATION_MINUTES, AFFECTED_TENANTS, ROOT_CAUSE_CATEGORY, SUMMARY
        FROM HERMES_DEMO.OPS.SERVICE_INCIDENTS
        WHERE SEVERITY IN (?, ?)
        ORDER BY OPENED_AT DESC
    """,
    "warehouse": "HERMES_DEMO_WH",
    "database": "HERMES_DEMO",
    "schema": "OPS",
    "role": "HERMES_DEMO_ROLE",
    "bindings": {
        "1": {"type": "TEXT", "value": "SEV1"},
        "2": {"type": "TEXT", "value": "SEV2"},
    },
}
req = urllib.request.Request(
    f"https://{host}/api/v2/statements",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {token}",
        "X-Snowflake-Authorization-Token-Type": "OAUTH",
        "Content-Type": "application/json",
        "Accept": "application/json",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as resp:
    doc = json.loads(resp.read().decode("utf-8"))
    cols = [c["name"] for c in doc["resultSetMetaData"]["rowType"]]
    print(json.dumps([dict(zip(cols, row)) for row in doc.get("data", [])], indent=2))
PY
```

## 3. Semantic Runbook Retrieval via Cortex Search (`RUNBOOK_SEARCH_SVC`)

```bash
python3 - <<'PY'
import json, os, pathlib, urllib.request

host = os.environ["SNOWFLAKE_HOST"].replace("_", "-")
token = pathlib.Path("/snowflake/session/token").read_text().strip()
search_query = {"query": "HTTP 429 rate limit burst mitigation", "columns": ["DOC_ID", "TITLE", "SERVICE_NAME", "REMEDIATION_STEPS"], "limit": 3}
payload = {
    "statement": "SELECT PARSE_JSON(SNOWFLAKE.CORTEX.SEARCH_PREVIEW('HERMES_DEMO.OPS.RUNBOOK_SEARCH_SVC', ?)) AS HITS",
    "warehouse": "HERMES_DEMO_WH",
    "database": "HERMES_DEMO",
    "schema": "OPS",
    "role": "HERMES_DEMO_ROLE",
    "bindings": {"1": {"type": "TEXT", "value": json.dumps(search_query)}},
}
req = urllib.request.Request(
    f"https://{host}/api/v2/statements",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {token}",
        "X-Snowflake-Authorization-Token-Type": "OAUTH",
        "Content-Type": "application/json",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as resp:
    doc = json.loads(resp.read().decode("utf-8"))
    print(doc["data"][0][0])
PY
```

## 4. Inspecting Hermes Spans in Cortex AI Gateway (`AGENT_TRACE_TABLE`)

When Hermes runs over Option B (`snowflake-proxy-cortex-aigateway`), inspect recent `hermes-spcs` turns directly:

```sql
SELECT
  start_timestamp,
  trace:"trace_id"::STRING                                    AS trace_id,
  record:"name"::STRING                                       AS span_name,
  record_attributes:"gen_ai.agent.name"::STRING               AS agent_name,
  record_attributes:"gen_ai.request.model"::STRING            AS model,
  TRY_TO_NUMBER(record_attributes:"http.status_code"::STRING) AS http_status,
  TRY_TO_NUMBER(record_attributes:"gen_ai.usage.input_tokens"::STRING)  AS input_tokens,
  TRY_TO_NUMBER(record_attributes:"gen_ai.usage.output_tokens"::STRING) AS output_tokens
FROM TABLE(AGENT_TRACE_TABLE('SNOWFLAKE'))
WHERE timestamp >= DATEADD('hour', -2, CURRENT_TIMESTAMP())
  AND COALESCE(
        record_attributes:"gen_ai.agent.name"::STRING,
        record_attributes:"http.request.header.snow-agent-name"::STRING
      ) = 'hermes-spcs'
ORDER BY start_timestamp DESC
LIMIT 20;
```
