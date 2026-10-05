#!/bin/sh
# Prints the SPCS session token in the format expected by Hermes' key_cmd.
#
# Why JSON and not the bare token: with "bare" output Hermes does not know the
# expiry and applies a 15-minute cache (_NO_TTL_REFRESH_SECONDS = 900). SPCS,
# however, rotates the token on the filesystem more often, so after a rotation
# the cached token goes stale and requests fail with 390303 "Invalid OAuth
# access token" mid-session.
#
# By declaring a short expires_in we force Hermes to re-read the file: that
# costs one local filesystem read, so it can be done often.
set -eu

TOKEN_PATH="${SPCS_TOKEN_PATH:-/snowflake/session/token}"

if [ ! -r "$TOKEN_PATH" ]; then
    echo "session token not readable at $TOKEN_PATH (outside SPCS?)" >&2
    exit 1
fi

TOKEN="$(tr -d '\r\n' < "$TOKEN_PATH")"

if [ -z "$TOKEN" ]; then
    echo "session token empty at $TOKEN_PATH" >&2
    exit 1
fi

printf '{"access_token":"%s","expires_in":120}\n' "$TOKEN"
