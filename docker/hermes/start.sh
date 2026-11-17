#!/bin/bash
set -e

# /root is the persistent SPCS block volume that survives suspend/resume and restarts.
# Because mounting /root masks everything written under /root during image build,
# the image keeps a clean seed in /opt/hermes-seed and restores missing files here.

HERMES_DIR=/root/.hermes
SEED_DIR=/opt/hermes-seed
VENV_PY=/usr/local/lib/hermes-agent/venv/bin/python
PROXY_PORT=8080
PROXY_BASE="http://127.0.0.1:${PROXY_PORT}/v1"
OLLAMA_BASE="${OLLAMA_INTERNAL_URL:-http://ollama-service:11434}"
ACTIVE_PROVIDER="${HERMES_PROVIDER:-snowflake-standard-responses}"
DEFAULT_MODEL="${HERMES_MODEL:-openai-gpt-6-astra}"
export STANDARD_PROXY_ORIGIN="${STANDARD_PROXY_ORIGIN:-}"
export AIGATEWAY_PROXY_ORIGIN="${AIGATEWAY_PROXY_ORIGIN:-}"

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
# Binary launcher in /usr/local/bin, checkout in /opt/hermes/hermes-agent
# (aliased at /usr/local/lib/hermes-agent).
export PATH="/usr/local/bin:$PATH:/root/.local/bin:${HERMES_DIR}/bin"

if command -v hermes > /dev/null 2>&1; then
    log "hermes binary: $(command -v hermes)"
else
    log "WARN: hermes binary not in PATH"
fi

# ---------------------------------------------------------------- Seed /root/.hermes
mkdir -p "$HERMES_DIR"
if [ -d "$SEED_DIR" ]; then
    # -n = no-clobber: existing files on the block volume (sessions, memory,
    # custom skills, configuration) are never overwritten.
    cp -a -n "$SEED_DIR/." "$HERMES_DIR/" 2>/dev/null || true
    log "seed applied — skills present: $(ls "$HERMES_DIR/skills" 2>/dev/null | wc -l)"
else
    log "WARN: $SEED_DIR missing in image"
fi

# Link the Hermes Python environment (/opt/hermes/installs) into /root/.hermes/installs
# so the runtime environment always matches the deployed image revision.
if [ -d /opt/hermes/installs ]; then
    if [ -e "$HERMES_DIR/installs" ] && [ ! -L "$HERMES_DIR/installs" ]; then
        mv "$HERMES_DIR/installs" "$HERMES_DIR/installs.pre-image-$(date +%Y%m%d%H%M%S)"
        log "existing installs/ directory backed up"
    fi
    ln -sfn /opt/hermes/installs "$HERMES_DIR/installs"
fi

# ---------------------------------------------------------------- Telegram SOUL.md instructions
# Hermes does not expose an agent-callable send_message tool; outbound messaging
# from inside the agent loop uses `hermes send --to telegram` via the terminal tool.
SOUL_FILE="${HERMES_DIR}/SOUL.md"
if [ -f "$SOUL_FILE" ] && [ -f /opt/migrate_soul.py ]; then
    python3 /opt/migrate_soul.py "$SOUL_FILE" || log "WARN: SOUL.md migration skipped"
fi

# ---------------------------------------------------------------- Hermes Configuration
# If an older hand-written config.yaml lacks installer sections, back it up and
# seed from the full installer template before patching.
if [ -f "$SEED_DIR/config.yaml" ] && [ -f "${HERMES_DIR}/config.yaml" ]; then
    if ! grep -q "^platform_toolsets:" "${HERMES_DIR}/config.yaml" 2>/dev/null; then
        cp -a "${HERMES_DIR}/config.yaml" \
            "${HERMES_DIR}/config.yaml.pre-v2.$(date +%s)"
        cp -a "$SEED_DIR/config.yaml" "${HERMES_DIR}/config.yaml"
        log "replaced non-installer config.yaml with image default (backup .pre-v2 saved)"
    fi
fi

if [ -x "$VENV_PY" ] && [ -f /opt/hermes_configure.py ]; then
    if "$VENV_PY" /opt/hermes_configure.py \
        --provider "$ACTIVE_PROVIDER" --model "$DEFAULT_MODEL"; then
        log "Hermes configuration applied (provider=${ACTIVE_PROVIDER} model=${DEFAULT_MODEL})"
    else
        log "WARN: Hermes configuration failed — leaving existing config unchanged"
    fi
    rm -f "${HERMES_DIR}/context_length_cache.yaml"
    if grep -nE '^[[:space:]]*(export[[:space:]]+)?TERMINAL_CWD[[:space:]]*=' \
        "${HERMES_DIR}/.env" 2>/dev/null; then
        log "WARN: TERMINAL_CWD still active in .env — migration did not apply"
    fi
else
    log "WARN: venv or hermes_configure.py missing"
fi

# ---------------------------------------------------------------- Embedded Cortex Proxy
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
            log "embedded Cortex proxy ready on ${PROXY_BASE}"
            break
        fi
        sleep 1
    done
    proxy_up || log "WARN: embedded proxy not responding"

    (
        while true; do
            sleep 30
            if ! proxy_up; then
                log "WARN: embedded proxy down — restarting"
                start_proxy
                sleep 5
            fi
        done
    ) &
else
    log "WARN: embedded proxy not started (script or SPCS session token missing)"
fi

# ---------------------------------------------------------------- Environment
export OLLAMA_HOST="$OLLAMA_BASE"

# ---------------------------------------------------------------- Cloudflare tunnel
if [ -n "$CF_TUNNEL_TOKEN" ]; then
    nohup cloudflared tunnel run --token "$CF_TUNNEL_TOKEN" --protocol http2 \
        > /tmp/cf_named.log 2>&1 &
    log "Cloudflare tunnel started (http2)"
    log "container internal IPs: $(hostname -I 2>/dev/null || echo n/a)"
else
    log "CF_TUNNEL_TOKEN absent — tunnel not started"
fi

# ---------------------------------------------------------------- Gateway & Cron Ticker
gateway_up() {
    python3 -c '
import json, pathlib, sys
home = pathlib.Path("/root/.hermes")
pid_file = home / "gateway.pid"
if pid_file.exists():
    try:
        rec = json.loads(pid_file.read_text())
        pid = rec.get("pid")
        if isinstance(pid, int) and pid > 1:
            argv = (pathlib.Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\x00")
            if any(argv[i:i+2] == [b"gateway", b"run"] for i in range(len(argv) - 1)):
                sys.exit(0)
    except Exception:
        pass
sys.exit(1)
' 2>/dev/null || pgrep -f "hermes gateway run" > /dev/null 2>&1
}

upstream_ready() {
    case "$ACTIVE_PROVIDER" in
        snowflake-aigateway-*)
            target="${AIGATEWAY_PROXY_ORIGIN:-http://llm-proxy-aigateway.core.svc.spcs.internal:8080}/healthz"
            curl -fsS -o /dev/null -m 5 "$target" 2>/dev/null
            ;;
        snowflake-standard-*)
            target="${STANDARD_PROXY_ORIGIN:-http://llm-proxy-standard.core.svc.spcs.internal:8080}/healthz"
            curl -fsS -o /dev/null -m 5 "$target" 2>/dev/null
            ;;
        *)
            proxy_up
            ;;
    esac
}

start_gateway() {
    setsid nohup hermes gateway run --replace \
        >> "${HERMES_DIR}/logs/gateway.log" 2>&1 < /dev/null &
}

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && command -v hermes > /dev/null 2>&1; then
    mkdir -p "${HERMES_DIR}/logs"
    for _ in $(seq 1 20); do
        upstream_ready && break
        sleep 1
    done
    if upstream_ready || proxy_up; then
        start_gateway
        for _ in $(seq 1 30); do
            gateway_up && break
            sleep 1
        done
        if gateway_up; then
            log "gateway started (messaging listener + cron ticker)"
        else
            log "WARN: gateway failed to start"
        fi

        (
            while true; do
                sleep 60
                if ! gateway_up; then
                    log "WARN: gateway not running — restarting"
                    start_gateway
                    sleep 10
                fi
            done
        ) &
    else
        log "WARN: upstream proxy not ready — gateway not started"
    fi
else
    log "TELEGRAM_BOT_TOKEN absent — gateway not started"
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

# ---------------------------------------------------------------- Boot Self-Test
if [ "${HERMES_SELFTEST:-1}" = "1" ] && command -v hermes > /dev/null 2>&1; then
    (
        if /opt/spcs_token.sh > /tmp/selftest_token.json 2>/dev/null; then
            log "SELFTEST token helper: OK ($(wc -c < /tmp/selftest_token.json) bytes)"
        else
            log "SELFTEST token helper: FAILED"
        fi

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
            log "SELFTEST direct Cortex: FAILED (HTTP ${HTTP_CODE}) host=${SNOWFLAKE_HOST} body=$(tr -d '\n' < /tmp/selftest_direct.json | head -c 300)"
        fi

        PROXY_CODE="$(curl -sS -m 90 -o /tmp/selftest_proxy.json -w '%{http_code}' \
            -X POST "${PROXY_BASE}/chat/completions" \
            -H "Content-Type: application/json" \
            -d '{"model":"claude-sonnet-5","messages":[{"role":"user","content":"ping"}],"max_tokens":64}' \
            2>/tmp/selftest_proxy.err || echo "curl-error")"
        if [ "$PROXY_CODE" = "200" ]; then
            log "SELFTEST embedded proxy with max_tokens: OK (HTTP 200)"
        else
            log "SELFTEST embedded proxy with max_tokens: FAILED (HTTP ${PROXY_CODE}) body=$(tr -d '\n' < /tmp/selftest_proxy.json | head -c 300)"
        fi

        run_hermes_test() {
            label="$1"; shift
            timeout 240 hermes -z "Reply with only: pong" "$@" \
                > "/tmp/selftest_${label}.log" 2>&1 || true
            out="$(tr '\n' ' ' < "/tmp/selftest_${label}.log" | tail -c 250)"
            if grep -qiE "context length exceeded" "/tmp/selftest_${label}.log"; then
                log "SELFTEST hermes[${label}]: FAILED (context length exceeded) — ${out}"
            elif grep -qiE "requires --model|^usage:|unrecognized argument|HTTP [45][0-9][0-9]|Invalid OAuth|Traceback" "/tmp/selftest_${label}.log"; then
                log "SELFTEST hermes[${label}]: FAILED — ${out}"
            elif [ -s "/tmp/selftest_${label}.log" ]; then
                log "SELFTEST hermes[${label}]: OK — ${out}"
            else
                log "SELFTEST hermes[${label}]: FAILED (no output)"
            fi
        }

        run_hermes_test "default"

        VENV_PY_TG=/usr/local/lib/hermes-agent/venv/bin/python
        if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
            log "SELFTEST telegram: token absent — platform not configured"
        elif ! "$VENV_PY_TG" -c "import telegram" > /dev/null 2>&1; then
            log "SELFTEST telegram: FAILED — python-telegram-bot missing in venv"
        else
            TG_VER="$("$VENV_PY_TG" -c "import telegram; print(telegram.__version__)" 2>/dev/null)"
            log "SELFTEST telegram: OK (python-telegram-bot ${TG_VER})"
        fi
    ) &
fi

# ------------------------------------------------- Tailscale + Desktop Remote Gateway (:9119)
TS_DIR=/root/tailscale
TS_SOCK="${TS_DIR}/sock"
TS_STATE="${TS_DIR}/tailscaled.state"
SERVE_PORT=9119
SERVE_BASE="http://127.0.0.1:${SERVE_PORT}"

tailscaled_up() {
    pgrep -x tailscaled > /dev/null 2>&1
}

serve_up() {
    curl -fsS -o /dev/null -m 5 "${SERVE_BASE}/api/status" 2>/dev/null
}

start_tailscaled() {
    nohup tailscaled --tun=userspace-networking \
        --state="$TS_STATE" --socket="$TS_SOCK" \
        >> /tmp/tailscaled.log 2>&1 &
}

start_serve() {
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
        if [ -s "$TS_STATE" ]; then
            tailscale --socket="$TS_SOCK" up \
                --hostname hermes-spcs --accept-dns=false \
                >> /tmp/tailscaled.log 2>&1 || \
                log "WARN: tailscale up failed with existing state"
        elif [ -n "${TS_AUTHKEY:-}" ]; then
            tailscale --socket="$TS_SOCK" up --authkey "$TS_AUTHKEY" \
                --hostname hermes-spcs --accept-dns=false \
                >> /tmp/tailscaled.log 2>&1 || \
                log "WARN: tailscale up failed with authkey"
        else
            log "WARN: no Tailscale state and TS_AUTHKEY absent — node not registered"
        fi

        TS_IP="$(tailscale --socket="$TS_SOCK" ip -4 2>/dev/null | head -1)"
        log "tailnet IP: ${TS_IP:-unassigned}"

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
        log "WARN: tailscaled did not start — Remote gateway unavailable"
    fi
else
    log "tailscaled missing in image — Remote gateway unavailable"
fi

log "ready — provider=${ACTIVE_PROVIDER} model=${DEFAULT_MODEL}; web terminal on :7681"

exec ttyd --port 7681 --writable bash -l
