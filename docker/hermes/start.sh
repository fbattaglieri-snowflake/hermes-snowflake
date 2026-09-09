#!/bin/bash
set -e

# /root is persistent block storage: it survives suspend/resume of the service.
# The downside is that the mount MASKS everything the image contains under
# /root — including the ~6.6MB of skills that the Hermes installer writes into
# /root/.hermes. That is why the image keeps a copy in /opt/hermes-seed, which
# we restore here.

HERMES_DIR=/root/.hermes
SEED_DIR=/opt/hermes-seed
VENV_PY=/usr/local/lib/hermes-agent/venv/bin/python
PROXY_PORT=8080
PROXY_BASE="http://127.0.0.1:${PROXY_PORT}/v1"
OLLAMA_BASE="${OLLAMA_INTERNAL_URL:-http://ollama-service:11434}"
ACTIVE_PROVIDER="${HERMES_PROVIDER:-snowflake-cortex-proxy}"
DEFAULT_MODEL="${HERMES_MODEL:-claude-sonnet-5}"

# SPCS injects SNOWFLAKE_HOST, but the default keeps the script usable outside
# it as well. Exported because hermes_configure.py reads it too.
export SNOWFLAKE_HOST="${SNOWFLAKE_HOST:-${SNOWFLAKE_HOST_DEFAULT:-localhost}}"

log() { echo "[hermes] $*"; }

# ---------------------------------------------------------------- SSH
mkdir -p /root/.ssh && chmod 700 /root/.ssh
if [ -n "$SSH_PUBLIC_KEY" ]; then
    echo "$SSH_PUBLIC_KEY" > /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
fi
ssh-keygen -A
service ssh start || log "WARN: sshd startup failed"

# ---------------------------------------------------------------- PATH
# The container runs as root, so the installer uses the FHS layout:
# binary in /usr/local/bin, code in /usr/local/lib/hermes-agent.
export PATH="/usr/local/bin:$PATH:/root/.local/bin:${HERMES_DIR}/bin"

if command -v hermes > /dev/null 2>&1; then
    log "hermes: $(command -v hermes)"
else
    log "WARN: hermes binary not in PATH — is it missing from the image?"
fi

# ---------------------------------------------------------------- Seed /root/.hermes
mkdir -p "$HERMES_DIR"
if [ -d "$SEED_DIR" ]; then
    # -n = no-clobber: files already present on the volume (user customizations,
    # sessions, memories) are left untouched; only what is missing is restored.
    cp -a -n "$SEED_DIR/." "$HERMES_DIR/" 2>/dev/null || true
    log "seed applied — skills present: $(ls "$HERMES_DIR/skills" 2>/dev/null | wc -l)"
else
    log "WARN: $SEED_DIR missing from the image"
fi

# ---------------------------------------------------------------- Telegram instructions
# Hermes does NOT expose a message-sending tool to the model: toolsets.py states
# explicitly that "agents do NOT get an agent-callable send_message tool —
# outbound platform messaging is handled outside the agent loop (cron delivery,
# the gateway kanban notifier, and the `hermes send` CLI)". Without this note the
# agent, finding no suitable tool, falls back on computer_use (which cannot
# work: the container is headless) or asks the user how to proceed. The correct
# route is the `terminal` tool, which the agent does have, with `hermes send`.
# The marker makes the append idempotent across restarts.
#
# The marker is versioned because SOUL.md lives on the block volume: a volume
# provisioned by an earlier image already carries the v1 block, and the guard
# alone would keep it forever. Bumping the version appends the current text, and
# the awk pass first drops any superseded block, so the agent never sees two
# contradictory sets of instructions. The pass cuts from the old marker to the
# next HTML comment marker (or end of file), leaving anything appended after an
# unrelated marker untouched.
SOUL_FILE="${HERMES_DIR}/SOUL.md"
SOUL_MARK="<!-- spcs-telegram-v2 -->"
SOUL_MARK_SUPERSEDED="<!-- spcs-telegram-v1 -->"
if [ -f "$SOUL_FILE" ] && grep -qF "$SOUL_MARK_SUPERSEDED" "$SOUL_FILE" 2>/dev/null; then
    awk -v mark="$SOUL_MARK_SUPERSEDED" '
        index($0, mark) { skip = 1; next }
        skip && /^<!--/ { skip = 0 }
        !skip
    ' "$SOUL_FILE" > "${SOUL_FILE}.tmp" && mv "${SOUL_FILE}.tmp" "$SOUL_FILE"
    log "superseded Telegram block removed from SOUL.md"
fi
if [ -f "$SOUL_FILE" ] && ! grep -qF "$SOUL_MARK" "$SOUL_FILE" 2>/dev/null; then
    {
        printf '\n%s\n' "$SOUL_MARK"
        printf '## Sending messages on Telegram\n\n'
        printf 'There is no message-sending tool callable by the model.\n'
        printf 'To send on Telegram, use the `terminal` tool:\n\n'
        printf '    hermes send --to telegram "message text"\n\n'
        printf 'The default recipient is TELEGRAM_HOME_CHANNEL, already\n'
        printf 'configured: do not ask for the chat_id unless given one.\n'
        printf 'For a different chat: `--to telegram:<chat_id>`.\n'
        printf 'To list the available targets: `hermes send --list telegram`.\n'
        printf 'Do not use computer_use for Telegram: the container is headless.\n'
    } >> "$SOUL_FILE"
    log "Telegram instructions added to SOUL.md"
fi

# ---------------------------------------------------------------- Hermes configuration
# The volume may contain a config.yaml hand-written in previous sessions, far
# poorer than the installer's one (which has ~25 default sections). Patching it
# as-is would leave Hermes without those defaults, so if the file is not
# recognizable as installer-generated it is replaced with the seed.
if [ -f "$SEED_DIR/config.yaml" ] && [ -f "${HERMES_DIR}/config.yaml" ]; then
    if ! grep -q "^platform_toolsets:" "${HERMES_DIR}/config.yaml" 2>/dev/null; then
        cp -a "${HERMES_DIR}/config.yaml" \
            "${HERMES_DIR}/config.yaml.pre-v2.$(date +%s)"
        cp -a "$SEED_DIR/config.yaml" "${HERMES_DIR}/config.yaml"
        log "non-installer config.yaml replaced with the image default (backup .pre-v2)"
    fi
fi

# Patches the installer's config.yaml (does not replace it): sets the Snowflake
# Cortex and Ollama providers and the per-model context_length, which is what
# avoids the "Context length exceeded (20 tokens)" error.
if [ -x "$VENV_PY" ] && [ -f /opt/hermes_configure.py ]; then
    if "$VENV_PY" /opt/hermes_configure.py \
        --provider "$ACTIVE_PROVIDER" --model "$DEFAULT_MODEL"; then
        log "Hermes config applied (provider=${ACTIVE_PROVIDER})"
    else
        log "WARN: Hermes configuration failed — config left unchanged"
    fi
    # The probe cache may contain the wrong context length detected before the
    # fix: it must be invalidated, and it will be repopulated correctly.
    rm -f "${HERMES_DIR}/context_length_cache.yaml"
    # Diagnostics: if an active line remains, Hermes prints the deprecation
    # warning at every startup. The value is a path, not a secret.
    if grep -nE '^[[:space:]]*(export[[:space:]]+)?TERMINAL_CWD[[:space:]]*=' \
        "${HERMES_DIR}/.env" 2>/dev/null; then
        log "WARN: TERMINAL_CWD still active in .env (line above) — migration not applied"
    fi
else
    log "WARN: venv or configuration script missing"
fi

# ---------------------------------------------------------------- Cortex proxy
# ESSENTIAL component, not an accessory: it translates max_tokens into
# max_completion_tokens (Cortex rejects the former with HTTP 400) and adds the
# OAUTH header. Without the proxy, Hermes fails on every non-OpenAI model.
if [ -f /opt/cortex_proxy.py ]; then
    cp /opt/cortex_proxy.py "${HERMES_DIR}/cortex_proxy.py"
fi

start_proxy() {
    nohup python3 "${HERMES_DIR}/cortex_proxy.py" >> /tmp/cortex_proxy.log 2>&1 &
}

proxy_up() {
    curl -fsS -o /dev/null -m 5 "${PROXY_BASE}/models" 2>/dev/null
}

if [ -f "${HERMES_DIR}/cortex_proxy.py" ] && [ -f /snowflake/session/token ]; then
    start_proxy
    for _ in $(seq 1 20); do
        if proxy_up; then
            log "Cortex proxy ready on ${PROXY_BASE}"
            break
        fi
        sleep 1
    done
    proxy_up || log "WARN: proxy not responding — Hermes will not work"

    # Watchdog: being on the critical path, a proxy crash would make Hermes
    # unusable until the service is restarted.
    (
        while true; do
            sleep 30
            if ! proxy_up; then
                log "WARN: proxy not responding — restarting"
                start_proxy
                sleep 5
            fi
        done
    ) &
else
    log "WARN: proxy cannot be started (script or session token missing)"
fi

# ---------------------------------------------------------------- Env
export OLLAMA_HOST="$OLLAMA_BASE"
# No OPENAI_BASE_URL/OPENAI_API_KEY: the provider configuration lives in
# config.yaml. Setting them here would create a second, divergent source of
# truth.

# ---------------------------------------------------------------- Cloudflare tunnel
if [ -n "$CF_TUNNEL_TOKEN" ]; then
    # SPCS blocks QUIC/UDP: http2 is mandatory.
    nohup cloudflared tunnel run --token "$CF_TUNNEL_TOKEN" --protocol http2 \
        > /tmp/cf_named.log 2>&1 &
    log "Cloudflare tunnel started (http2)"
    # The internal IP changes at every restart and must be updated in the private
    # CIDR route on Cloudflare: we log it to avoid hunting for it from the shell.
    log "container internal IPs: $(hostname -I 2>/dev/null || echo n/a)"
else
    log "CF_TUNNEL_TOKEN missing — tunnel not started"
fi

# ---------------------------------------------------------------- Gateway
# The gateway is not an accessory: it is the only process that listens to the
# messaging platforms AND it is the ticker that fires the cronjobs ("Gateway is
# not running — jobs won't fire automatically"). If it is not started at boot,
# every container recreation leaves the agent mute and the cron jobs stopped,
# silently: no errors, just no answers.
# `hermes gateway install` wants systemd, which does not exist in SPCS: it must
# be launched as a child process.
gateway_up() {
    pgrep -f "hermes gateway run" > /dev/null 2>&1
}

start_gateway() {
    # --replace: if a previous instance left the lock behind, replace it instead
    # of exiting with an error.
    setsid nohup hermes gateway run --replace \
        >> "${HERMES_DIR}/logs/gateway.log" 2>&1 < /dev/null &
}

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && command -v hermes > /dev/null 2>&1; then
    mkdir -p "${HERMES_DIR}/logs"
    # The gateway opens agent sessions on the first message: without the proxy
    # ready the first inference would fail, so it is started after the proxy.
    if proxy_up; then
        start_gateway
        for _ in $(seq 1 30); do
            gateway_up && break
            sleep 1
        done
        if gateway_up; then
            log "gateway started (messaging + cron ticker)"
        else
            log "WARN: gateway did not start — no answers and cron jobs stopped"
        fi

        # Watchdog, for the same reason as the proxy: the failure is silent.
        (
            while true; do
                sleep 60
                if ! gateway_up; then
                    log "WARN: gateway not active — restarting"
                    start_gateway
                    sleep 10
                fi
            done
        ) &
    else
        log "WARN: proxy not ready — gateway not started"
    fi
else
    log "TELEGRAM_BOT_TOKEN missing — gateway not started"
fi

# ---------------------------------------------------------------- .bashrc
if ! grep -q "hermes-spcs-env v2" /root/.bashrc 2>/dev/null; then
cat >> /root/.bashrc << BEOF

# hermes-spcs-env v2
export PATH="/usr/local/bin:\$PATH:/root/.local/bin:${HERMES_DIR}/bin"
export OLLAMA_HOST="${OLLAMA_BASE}"
BEOF
    log ".bashrc updated"
fi

# ---------------------------------------------------------------- Self-test
# Interactive access is awkward (SSH depends on WARP, the web terminal corrupts
# pasting), so the boot checks by itself and leaves the outcome in the service
# logs, readable with SYSTEM$GET_SERVICE_LOGS. Disable with HERMES_SELFTEST=0.
if [ "${HERMES_SELFTEST:-1}" = "1" ] && command -v hermes > /dev/null 2>&1; then
    (
        # 1. The credentials helper: if it does not print valid JSON, key_cmd fails.
        if /opt/spcs_token.sh > /tmp/selftest_token.json 2>/dev/null; then
            log "SELFTEST token helper: OK ($(wc -c < /tmp/selftest_token.json) bytes)"
        else
            log "SELFTEST token helper: FAILED"
        fi

        # 2. Direct path to Cortex with the same headers Hermes uses.
        # Without -f: with -f, curl discards the body on HTTP errors and the
        # Snowflake message is lost, which is the only thing useful for diagnosis.
        HTTP_CODE="$(curl -sS -m 90 -o /tmp/selftest_direct.json -w '%{http_code}' \
            -X POST "https://${SNOWFLAKE_HOST}/api/v2/cortex/v1/chat/completions" \
            -H "Authorization: Bearer $(tr -d '\r\n' < /snowflake/session/token)" \
            -H "X-Snowflake-Authorization-Token-Type: OAUTH" \
            -H "Content-Type: application/json" \
            -d '{"model":"claude-sonnet-5","messages":[{"role":"user","content":"ping"}]}' \
            2>/tmp/selftest_direct.err || echo "curl-error")"
        if [ "$HTTP_CODE" = "200" ]; then
            log "SELFTEST direct Cortex: OK (HTTP 200)"
        else
            log "SELFTEST direct Cortex: FAILED (HTTP ${HTTP_CODE}) host=${SNOWFLAKE_HOST} body=$(tr -d '\n' < /tmp/selftest_direct.json | head -c 300) err=$(tr -d '\n' < /tmp/selftest_direct.err | head -c 150)"
        fi

        # 2b. The test that matters: same payload WITH max_tokens through the
        # proxy. Directly against Cortex this would give HTTP 400; if it returns
        # 200 here, the translation into max_completion_tokens is working.
        PROXY_CODE="$(curl -sS -m 90 -o /tmp/selftest_proxy.json -w '%{http_code}' \
            -X POST "${PROXY_BASE}/chat/completions" \
            -H "Content-Type: application/json" \
            -d '{"model":"claude-sonnet-5","messages":[{"role":"user","content":"ping"}],"max_tokens":64}' \
            2>/tmp/selftest_proxy.err || echo "curl-error")"
        if [ "$PROXY_CODE" = "200" ]; then
            log "SELFTEST proxy with max_tokens: OK (HTTP 200 — translation active)"
        else
            log "SELFTEST proxy with max_tokens: FAILED (HTTP ${PROXY_CODE}) body=$(tr -d '\n' < /tmp/selftest_proxy.json | head -c 300)"
        fi

        # 2c. Tool calling on a reasoning model: Cortex rejects 'tools' if
        # reasoning_effort is not "none", and the proxy rewrites it. Without this
        # fix the gpt-5.6-* models are unusable for an agent.
        TOOLS_CODE="$(curl -sS -m 90 -o /tmp/selftest_tools.json -w '%{http_code}' \
            -X POST "${PROXY_BASE}/chat/completions" \
            -H "Content-Type: application/json" \
            -d '{"model":"openai-gpt-5.6-terra","messages":[{"role":"user","content":"what time is it?"}],"reasoning_effort":"medium","tools":[{"type":"function","function":{"name":"get_time","description":"time","parameters":{"type":"object","properties":{}}}}]}' \
            2>/dev/null || echo "curl-error")"
        if [ "$TOOLS_CODE" = "200" ]; then
            log "SELFTEST tool calling on gpt-5.6: OK (HTTP 200 — reasoning_effort rewritten)"
        else
            log "SELFTEST tool calling on gpt-5.6: FAILED (HTTP ${TOOLS_CODE}) body=$(tr -d '\n' < /tmp/selftest_tools.json | head -c 250)"
        fi

        # 3. Hermes end-to-end with the default config. Note: '--provider' also
        # requires '--model', otherwise the CLI exits with a usage error.
        run_hermes_test() {
            label="$1"; shift
            timeout 240 hermes -z "Reply only: pong" "$@" \
                > "/tmp/selftest_${label}.log" 2>&1 || true
            out="$(tr '\n' ' ' < "/tmp/selftest_${label}.log" | tail -c 250)"
            if grep -qiE "context length exceeded" "/tmp/selftest_${label}.log"; then
                log "SELFTEST hermes[${label}]: FAILED (Cortex rejected the request) — ${out}"
            elif grep -qiE "requires --model|^usage:|unrecognized argument|HTTP [45][0-9][0-9]|Invalid OAuth|Traceback" "/tmp/selftest_${label}.log"; then
                log "SELFTEST hermes[${label}]: FAILED — ${out}"
            elif [ -s "/tmp/selftest_${label}.log" ]; then
                log "SELFTEST hermes[${label}]: OK — ${out}"
            else
                log "SELFTEST hermes[${label}]: FAILED (no output)"
            fi
        }

        # Only the default path: it is the one the user will actually use.
        run_hermes_test "default"

        # 4. Prerequisites of `hermes send --to telegram`. This failure is
        # silent: without the telegram module the command exits with 1 only
        # when the user tries to send, and the venv lives outside the persistent
        # volume, so an installation done at runtime disappears at the first
        # container recreation. Checking it at boot surfaces the regression in
        # the logs instead of during use.
        VENV_PY_TG=/usr/local/lib/hermes-agent/venv/bin/python
        if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
            log "SELFTEST telegram: token missing — platform not configured"
        elif ! "$VENV_PY_TG" -c "import telegram" > /dev/null 2>&1; then
            log "SELFTEST telegram: FAILED — python-telegram-bot module missing from the venv"
        else
            TG_VER="$("$VENV_PY_TG" -c "import telegram; print(telegram.__version__)" 2>/dev/null)"
            log "SELFTEST telegram: OK (python-telegram-bot ${TG_VER}, chat ${TELEGRAM_HOME_CHANNEL:-not set})"
        fi
    ) &
fi

# ------------------------------------------------- Tailscale + Desktop backend
# The Desktop connects in "Remote gateway" mode to `hermes serve` on 9119,
# reached over the tailnet. Neither process was started at boot, so every
# container recreation left the client offline while the service reported READY.
#
# ttyd remains the final foreground exec and must NOT be touched: if Tailscale
# or `hermes serve` break, the web terminal is the only recovery channel.
TS_DIR=/root/tailscale
TS_SOCK="${TS_DIR}/sock"
TS_STATE="${TS_DIR}/tailscaled.state"
SERVE_PORT=9119
SERVE_BASE="http://127.0.0.1:${SERVE_PORT}"

tailscaled_up() {
    pgrep -x tailscaled > /dev/null 2>&1
}

serve_up() {
    # `hermes serve --status` is unreliable: it reports "No hermes dashboard
    # processes running" while the process is listening. We query the port.
    curl -fsS -o /dev/null -m 5 "${SERVE_BASE}/api/status" 2>/dev/null
}

start_tailscaled() {
    # SPCS does not expose /dev/net/tun and does not grant NET_ADMIN: userspace
    # networking is mandatory, not a preference.
    nohup tailscaled --tun=userspace-networking \
        --state="$TS_STATE" --socket="$TS_SOCK" \
        >> /tmp/tailscaled.log 2>&1 &
}

start_serve() {
    # --skip-build is MANDATORY: without it the process stays alive but never
    # starts listening and serve.log stays empty, because it attempts to build
    # the web UI, which does not exist in the image. Binding on 0.0.0.0 enables
    # the authentication gate on its own.
    nohup hermes serve --skip-build --host 0.0.0.0 --port "$SERVE_PORT" \
        >> /root/serve.log 2>&1 &
}

if command -v tailscaled > /dev/null 2>&1; then
    mkdir -p "$TS_DIR"
    start_tailscaled
    for _ in $(seq 1 20); do
        tailscaled_up && break
        sleep 1
    done

    if tailscaled_up; then
        # With the state on the volume the node re-attaches without an authkey
        # and keeps the same IP: the key is only needed on first startup, or if
        # the state is lost. --accept-dns=false is deliberate: the container's
        # resolver must not be rewritten, it has to keep resolving the internal
        # SPCS hosts.
        if [ -s "$TS_STATE" ]; then
            tailscale --socket="$TS_SOCK" up \
                --hostname hermes-spcs --accept-dns=false \
                >> /tmp/tailscaled.log 2>&1 || \
                log "WARN: tailscale up failed with the existing state"
        elif [ -n "${TS_AUTHKEY:-}" ]; then
            tailscale --socket="$TS_SOCK" up --authkey "$TS_AUTHKEY" \
                --hostname hermes-spcs --accept-dns=false \
                >> /tmp/tailscaled.log 2>&1 || \
                log "WARN: tailscale up failed with the authkey"
        else
            log "WARN: no Tailscale state and TS_AUTHKEY missing — node not registered"
        fi

        TS_IP="$(tailscale --socket="$TS_SOCK" ip -4 2>/dev/null | head -1)"
        # The IP must be logged: the readinessProbe logs make it unrecoverable
        # after about half an hour, and it is needed to configure the clients.
        log "tailnet IP: ${TS_IP:-not assigned}"

        if [ -n "$TS_IP" ]; then
            start_serve
            for _ in $(seq 1 30); do
                serve_up && break
                sleep 1
            done
            if serve_up; then
                log "hermes serve ready on ${TS_IP}:${SERVE_PORT} (Desktop Remote gateway)"
                tailscale --socket="$TS_SOCK" serve --bg --tcp "$SERVE_PORT" \
                    "tcp://localhost:${SERVE_PORT}" >> /tmp/tailscaled.log 2>&1 || \
                    log "WARN: tailscale serve not configured"
            else
                log "WARN: hermes serve not responding — serve.log: $(tail -c 200 /root/serve.log 2>/dev/null | tr '\n' ' ')"
            fi

            # Watchdog: as with the proxy and the gateway, the failure is silent
            # — the Desktop simply stops connecting.
            (
                while true; do
                    sleep 60
                    tailscaled_up || start_tailscaled
                    if ! serve_up; then
                        log "WARN: hermes serve not responding — restarting"
                        start_serve
                        sleep 10
                    fi
                done
            ) &
        else
            log "WARN: no tailnet IP — hermes serve not started"
        fi
    else
        log "WARN: tailscaled did not start — Remote gateway not available"
    fi
else
    log "tailscaled missing from the image — Remote gateway not available"
fi

log "ready — provider=${ACTIVE_PROVIDER} model=${DEFAULT_MODEL}; web terminal on :7681"

exec ttyd --port 7681 --writable bash -l
