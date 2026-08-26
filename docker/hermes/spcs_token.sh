#!/bin/sh
# Stampa il session token SPCS nel formato atteso da key_cmd di Hermes.
#
# Perche' JSON e non il token nudo: con output "bare" Hermes non conosce la
# scadenza e applica una cache di 15 minuti (_NO_TTL_REFRESH_SECONDS = 900).
# SPCS pero' ruota il token sul filesystem piu' spesso, quindi dopo una
# rotazione il token in cache diventa stale e le richieste falliscono con
# 390303 "Invalid OAuth access token" a meta' sessione.
#
# Dichiarando un expires_in breve costringiamo Hermes a rileggere il file:
# costa una read su filesystem locale, quindi si puo' fare spesso.
set -eu

TOKEN_PATH="${SPCS_TOKEN_PATH:-/snowflake/session/token}"

if [ ! -r "$TOKEN_PATH" ]; then
    echo "session token non leggibile in $TOKEN_PATH (fuori da SPCS?)" >&2
    exit 1
fi

TOKEN="$(tr -d '\r\n' < "$TOKEN_PATH")"

if [ -z "$TOKEN" ]; then
    echo "session token vuoto in $TOKEN_PATH" >&2
    exit 1
fi

printf '{"access_token":"%s","expires_in":120}\n' "$TOKEN"
