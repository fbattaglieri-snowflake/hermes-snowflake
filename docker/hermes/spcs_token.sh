#!/bin/sh
# Emit the SPCS OAuth session token in the JSON format expected by Hermes `key_cmd`.
#
# Why JSON with a short `expires_in` instead of a bare token string:
# When `key_cmd` emits a bare token, Hermes caches it for 15 minutes
# (_NO_TTL_REFRESH_SECONDS = 900). SPCS rotates `/snowflake/session/token` on
# disk more frequently, causing stale cached tokens to fail mid-session with
# error 390303 ("Invalid OAuth access token"). A short `expires_in` forces
# Hermes to re-read the local token file regularly.
set -eu

TOKEN_PATH="${SPCS_TOKEN_PATH:-/snowflake/session/token}"

if [ ! -r "$TOKEN_PATH" ]; then
    echo "session token not readable at $TOKEN_PATH (running outside SPCS?)" >&2
    exit 1
fi

TOKEN="$(tr -d '\r\n' < "$TOKEN_PATH")"

if [ -z "$TOKEN" ]; then
    echo "session token is empty at $TOKEN_PATH" >&2
    exit 1
fi

printf '{"access_token":"%s","expires_in":120}\n' "$TOKEN"
