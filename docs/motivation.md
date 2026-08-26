# Motivation: Why the Cortex Proxy Is Required

Hermes Agent uses the OpenAI Chat Completions API protocol. Snowflake Cortex exposes an endpoint that claims the same protocol (`/api/v2/cortex/v1/chat/completions`), but in practice deviates from the specification in five ways. Each deviation produces a misleading error that obscures the real cause.

## The Five Deviations

### 1 — `max_tokens` is rejected

Snowflake Cortex requires `max_completion_tokens` (the newer OpenAI parameter). All versions of Hermes send `max_tokens`. Cortex returns HTTP 400 and the response body contains a misleading error that Hermes interprets as a context overflow: `Context length exceeded (N tokens). Cannot compress further.`

**Proxy fix:** renames `max_tokens` → `max_completion_tokens` on every outbound request.

### 2 — `finish_reason` is absent or empty

For non-OpenAI models (all Claude, Mistral, Llama), Cortex returns `"finish_reason": ""` on non-streaming responses and emits no `finish_reason` on streaming SSE chunks. The OpenAI specification requires `"stop"` or `"tool_calls"` on the final chunk.

Consequences in Hermes:
- Streaming: Hermes considers the response truncated and retries up to four times, producing repeated text.
- Tool calls: if a tool-call response has no `finish_reason`, Hermes continues accumulating tool calls. The next request contains an `assistant` turn with `tool_calls` but no matching `tool` turn. Cortex then rejects it with HTTP 400, and the session is permanently corrupted until closed.

**Proxy fix:** infers the correct `finish_reason` from the response content (if `tool_calls` is present → `"tool_calls"`, otherwise `"stop"`). For streaming, injects a synthetic terminal SSE chunk before `[DONE]`.

### 3 — `tools` and `reasoning_effort` are mutually exclusive on some models

Models in the `openai-gpt-5.6-*` family reject requests that contain both `tools` and any `reasoning_effort` value other than `"none"`. Omitting the parameter is not enough: the gateway applies a non-null default. Result: HTTP 400 `Function tools with reasoning_effort are not supported`.

Since Hermes Agent relies on tool calling for all agentic behaviour, these models are effectively unusable without intervention.

**Proxy fix:** forces `reasoning_effort="none"` when a request contains `tools` and the model is in the `tools_require_reasoning_effort_none` list. Also implements adaptive retry on that error message for future models with the same constraint.

### 4 — `GET /v1/models` returns 404

Hermes Desktop and the Hermes CLI fetch the model list at handshake. Without a valid response, the model dropdown is empty and the agent cannot start.

**Proxy fix:** serves the `models.json` catalog at `GET /v1/models` from a file that can be updated independently of the image (mounted from a Snowflake stage).

### 5 — Authentication requires SPCS OAuth headers

From inside SPCS, the Cortex endpoint requires:
- `Authorization: Bearer <service-oauth-token>` (read from `/snowflake/session/token`)
- `X-Snowflake-Authorization-Token-Type: OAUTH`

Standard OpenAI clients, including Hermes, send only `Authorization: Bearer <api-key>`. The proxy replaces these headers on every request, reading the rotating service token from disk to avoid expiry.

## The Pattern

OpenAI-family models behave correctly on this endpoint and need no normalisation. All other models (Claude, Mistral, Llama) pass through a translation layer that loses information. That the tool-call error uses Anthropic terminology (`toolUse`/`toolResult`) instead of OpenAI (`tool_calls`/`tool`) is diagnostic evidence of this. Deviation 1 is unusual: it affects OpenAI models too, indicating it is a gateway defect rather than a model-family translation issue.

Snowflake documentation states Chat Completions supports all model families. Using it with Claude is the documented path, not a workaround. The proxy makes that path reliable.
