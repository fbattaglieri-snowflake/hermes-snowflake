#!/bin/bash
set -e

# /root è block storage persistente: sopravvive a suspend/resume del servizio.
# Il rovescio della medaglia è che il mount MASCHERA tutto ciò che l'immagine
# contiene sotto /root — incluse le ~6.6MB di skill che l'installer di Hermes
# scrive in /root/.hermes. Per questo l'immagine tiene una copia in
# /opt/hermes-seed e qui la ripristiniamo.

HERMES_DIR=/root/.hermes
SEED_DIR=/opt/hermes-seed
VENV_PY=/usr/local/lib/hermes-agent/venv/bin/python
PROXY_PORT=8080
PROXY_BASE="http://127.0.0.1:${PROXY_PORT}/v1"
OLLAMA_BASE="${OLLAMA_INTERNAL_URL:-http://ollama-service:11434}"
ACTIVE_PROVIDER="${HERMES_PROVIDER:-snowflake-cortex-proxy}"
DEFAULT_MODEL="${HERMES_MODEL:-claude-sonnet-5}"

# SPCS inietta SNOWFLAKE_HOST, ma il default tiene lo script utilizzabile anche
# fuori. Esportato perché lo legge anche hermes_configure.py.
export SNOWFLAKE_HOST="${SNOWFLAKE_HOST:-${SNOWFLAKE_HOST_DEFAULT:-localhost}}"

log() { echo "[hermes] $*"; }

# ---------------------------------------------------------------- SSH
mkdir -p /root/.ssh && chmod 700 /root/.ssh
if [ -n "$SSH_PUBLIC_KEY" ]; then
    echo "$SSH_PUBLIC_KEY" > /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
fi
ssh-keygen -A
service ssh start || log "WARN: avvio sshd fallito"

# ---------------------------------------------------------------- PATH
# Il container gira come root, quindi l'installer usa il layout FHS:
# binario in /usr/local/bin, codice in /usr/local/lib/hermes-agent.
export PATH="/usr/local/bin:$PATH:/root/.local/bin:${HERMES_DIR}/bin"

if command -v hermes > /dev/null 2>&1; then
    log "hermes: $(command -v hermes)"
else
    log "WARN: binario hermes non in PATH — l'immagine non lo contiene?"
fi

# ---------------------------------------------------------------- Seed /root/.hermes
mkdir -p "$HERMES_DIR"
if [ -d "$SEED_DIR" ]; then
    # -n = no-clobber: i file già presenti sul volume (personalizzazioni utente,
    # sessioni, memorie) non vengono toccati; si ripristina solo ciò che manca.
    cp -a -n "$SEED_DIR/." "$HERMES_DIR/" 2>/dev/null || true
    log "seed applicato — skill presenti: $(ls "$HERMES_DIR/skills" 2>/dev/null | wc -l)"
else
    log "WARN: $SEED_DIR assente nell'immagine"
fi

# ---------------------------------------------------------------- Istruzioni Telegram
# Hermes NON espone al modello un tool di invio messaggi: toolsets.py dichiara
# esplicitamente che "agents do NOT get an agent-callable send_message tool —
# outbound platform messaging is handled outside the agent loop (cron delivery,
# the gateway kanban notifier, and the `hermes send` CLI)". Senza questa nota
# l'agente, non trovando un tool adatto, ripiega su computer_use (che non puo'
# funzionare: il container e' headless) oppure chiede all'utente come procedere.
# La via corretta e' il tool `terminal`, che l'agente ha, con `hermes send`.
# Il marcatore rende l'append idempotente ad ogni riavvio.
SOUL_FILE="${HERMES_DIR}/SOUL.md"
SOUL_MARK="<!-- spcs-telegram-v1 -->"
if [ -f "$SOUL_FILE" ] && ! grep -qF "$SOUL_MARK" "$SOUL_FILE" 2>/dev/null; then
    {
        printf '\n%s\n' "$SOUL_MARK"
        printf '## Invio messaggi su Telegram\n\n'
        printf 'Non esiste un tool di invio messaggi richiamabile dal modello.\n'
        printf 'Per inviare su Telegram usa il tool `terminal`:\n\n'
        printf '    hermes send --to telegram "testo del messaggio"\n\n'
        printf 'Il destinatario predefinito e` TELEGRAM_HOME_CHANNEL, gia`\n'
        printf 'configurato: non chiedere il chat_id se non te lo danno.\n'
        printf 'Per una chat diversa: `--to telegram:<chat_id>`.\n'
        printf 'Per elencare i target disponibili: `hermes send --list telegram`.\n'
        printf 'Non usare computer_use per Telegram: il container e` headless.\n'
    } >> "$SOUL_FILE"
    log "istruzioni Telegram aggiunte a SOUL.md"
fi

# ---------------------------------------------------------------- Configurazione Hermes
# Il volume può contenere un config.yaml scritto a mano in sessioni precedenti,
# molto più povero di quello dell'installer (che ha ~25 sezioni di default).
# Patcharlo così com'è lascerebbe Hermes senza quei default, quindi se il file
# non è riconoscibile come generato dall'installer lo si rimpiazza col seed.
if [ -f "$SEED_DIR/config.yaml" ] && [ -f "${HERMES_DIR}/config.yaml" ]; then
    if ! grep -q "^platform_toolsets:" "${HERMES_DIR}/config.yaml" 2>/dev/null; then
        cp -a "${HERMES_DIR}/config.yaml" \
            "${HERMES_DIR}/config.yaml.pre-v2.$(date +%s)"
        cp -a "$SEED_DIR/config.yaml" "${HERMES_DIR}/config.yaml"
        log "config.yaml non-installer sostituito col default dell'immagine (backup .pre-v2)"
    fi
fi

# Patcha il config.yaml dell'installer (non lo sostituisce): imposta i provider
# Snowflake Cortex e Ollama e il context_length per modello, che è ciò che evita
# l'errore "Context length exceeded (20 tokens)".
if [ -x "$VENV_PY" ] && [ -f /opt/hermes_configure.py ]; then
    if "$VENV_PY" /opt/hermes_configure.py \
        --provider "$ACTIVE_PROVIDER" --model "$DEFAULT_MODEL"; then
        log "config Hermes applicato (provider=${ACTIVE_PROVIDER})"
    else
        log "WARN: configurazione Hermes fallita — config lasciato invariato"
    fi
    # La cache del probe può contenere il context length errato rilevato prima
    # del fix: va invalidata, verrà ripopolata correttamente.
    rm -f "${HERMES_DIR}/context_length_cache.yaml"
    # Diagnostica: se resta una riga attiva Hermes stampa l'avviso di deprecazione
    # ad ogni avvio. Il valore è un path, non un segreto.
    if grep -nE '^[[:space:]]*(export[[:space:]]+)?TERMINAL_CWD[[:space:]]*=' \
        "${HERMES_DIR}/.env" 2>/dev/null; then
        log "WARN: TERMINAL_CWD ancora attivo in .env (riga sopra) — migrazione non applicata"
    fi
else
    log "WARN: venv o script di configurazione mancanti"
fi

# ---------------------------------------------------------------- Proxy Cortex
# Componente ESSENZIALE, non un accessorio: traduce max_tokens in
# max_completion_tokens (Cortex rifiuta il primo con HTTP 400) e aggiunge
# l'header OAUTH. Senza proxy, Hermes fallisce su tutti i modelli non-OpenAI.
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
            log "proxy Cortex pronto su ${PROXY_BASE}"
            break
        fi
        sleep 1
    done
    proxy_up || log "WARN: proxy non risponde — Hermes non funzionerà"

    # Watchdog: essendo sul percorso critico, un crash del proxy renderebbe
    # Hermes inutilizzabile fino al riavvio del servizio.
    (
        while true; do
            sleep 30
            if ! proxy_up; then
                log "WARN: proxy non risponde — riavvio"
                start_proxy
                sleep 5
            fi
        done
    ) &
else
    log "WARN: proxy non avviabile (script o session token mancante)"
fi

# ---------------------------------------------------------------- Env
export OLLAMA_HOST="$OLLAMA_BASE"
# Nessun OPENAI_BASE_URL/OPENAI_API_KEY: la configurazione dei provider vive nel
# config.yaml. Impostarli qui creerebbe una seconda fonte di verità divergente.

# ---------------------------------------------------------------- Cloudflare tunnel
if [ -n "$CF_TUNNEL_TOKEN" ]; then
    # SPCS blocca QUIC/UDP: http2 è obbligatorio.
    nohup cloudflared tunnel run --token "$CF_TUNNEL_TOKEN" --protocol http2 \
        > /tmp/cf_named.log 2>&1 &
    log "Cloudflare tunnel avviato (http2)"
    # L'IP interno cambia ad ogni restart e va aggiornato nella private CIDR
    # route su Cloudflare: lo logghiamo per non doverlo cercare dal terminale.
    log "IP interni del container: $(hostname -I 2>/dev/null || echo n/d)"
else
    log "CF_TUNNEL_TOKEN assente — tunnel non avviato"
fi

# ---------------------------------------------------------------- .bashrc
if ! grep -q "hermes-spcs-env v2" /root/.bashrc 2>/dev/null; then
cat >> /root/.bashrc << BEOF

# hermes-spcs-env v2
export PATH="/usr/local/bin:\$PATH:/root/.local/bin:${HERMES_DIR}/bin"
export OLLAMA_HOST="${OLLAMA_BASE}"
BEOF
    log ".bashrc aggiornato"
fi

# ---------------------------------------------------------------- Self-test
# L'accesso interattivo è scomodo (SSH dipende da WARP, il web terminal corrompe
# il paste), quindi il boot verifica da sé e lascia l'esito nei log del servizio,
# leggibili con SYSTEM$GET_SERVICE_LOGS. Disattivabile con HERMES_SELFTEST=0.
if [ "${HERMES_SELFTEST:-1}" = "1" ] && command -v hermes > /dev/null 2>&1; then
    (
        # 1. L'helper credenziali: se non stampa JSON valido, key_cmd fallisce.
        if /opt/spcs_token.sh > /tmp/selftest_token.json 2>/dev/null; then
            log "SELFTEST token helper: OK ($(wc -c < /tmp/selftest_token.json) byte)"
        else
            log "SELFTEST token helper: FALLITO"
        fi

        # 2. Percorso diretto verso Cortex con gli stessi header che usa Hermes.
        # Senza -f: con -f curl scarta il body sugli errori HTTP e si perde il
        # messaggio di Snowflake, che è l'unica cosa utile per diagnosticare.
        HTTP_CODE="$(curl -sS -m 90 -o /tmp/selftest_direct.json -w '%{http_code}' \
            -X POST "https://${SNOWFLAKE_HOST}/api/v2/cortex/v1/chat/completions" \
            -H "Authorization: Bearer $(tr -d '\r\n' < /snowflake/session/token)" \
            -H "X-Snowflake-Authorization-Token-Type: OAUTH" \
            -H "Content-Type: application/json" \
            -d '{"model":"claude-sonnet-5","messages":[{"role":"user","content":"ping"}]}' \
            2>/tmp/selftest_direct.err || echo "curl-error")"
        if [ "$HTTP_CODE" = "200" ]; then
            log "SELFTEST Cortex diretto: OK (HTTP 200)"
        else
            log "SELFTEST Cortex diretto: FALLITO (HTTP ${HTTP_CODE}) host=${SNOWFLAKE_HOST} body=$(tr -d '\n' < /tmp/selftest_direct.json | head -c 300) err=$(tr -d '\n' < /tmp/selftest_direct.err | head -c 150)"
        fi

        # 2b. Il test che conta: stesso payload CON max_tokens attraverso il
        # proxy. Diretto su Cortex questo darebbe HTTP 400; se qui torna 200 la
        # traduzione in max_completion_tokens sta funzionando.
        PROXY_CODE="$(curl -sS -m 90 -o /tmp/selftest_proxy.json -w '%{http_code}' \
            -X POST "${PROXY_BASE}/chat/completions" \
            -H "Content-Type: application/json" \
            -d '{"model":"claude-sonnet-5","messages":[{"role":"user","content":"ping"}],"max_tokens":64}' \
            2>/tmp/selftest_proxy.err || echo "curl-error")"
        if [ "$PROXY_CODE" = "200" ]; then
            log "SELFTEST proxy con max_tokens: OK (HTTP 200 — traduzione attiva)"
        else
            log "SELFTEST proxy con max_tokens: FALLITO (HTTP ${PROXY_CODE}) body=$(tr -d '\n' < /tmp/selftest_proxy.json | head -c 300)"
        fi

        # 2c. Tool calling su un modello reasoning: Cortex rifiuta 'tools' se
        # reasoning_effort non è "none", e il proxy lo riscrive. Senza questo fix
        # i modelli gpt-5.6-* sono inutilizzabili per un agente.
        TOOLS_CODE="$(curl -sS -m 90 -o /tmp/selftest_tools.json -w '%{http_code}' \
            -X POST "${PROXY_BASE}/chat/completions" \
            -H "Content-Type: application/json" \
            -d '{"model":"openai-gpt-5.6-terra","messages":[{"role":"user","content":"che ora e?"}],"reasoning_effort":"medium","tools":[{"type":"function","function":{"name":"get_time","description":"ora","parameters":{"type":"object","properties":{}}}}]}' \
            2>/dev/null || echo "curl-error")"
        if [ "$TOOLS_CODE" = "200" ]; then
            log "SELFTEST tool calling su gpt-5.6: OK (HTTP 200 — reasoning_effort riscritto)"
        else
            log "SELFTEST tool calling su gpt-5.6: FALLITO (HTTP ${TOOLS_CODE}) body=$(tr -d '\n' < /tmp/selftest_tools.json | head -c 250)"
        fi

        # 3. Hermes end-to-end col config di default. Nota: '--provider' pretende
        # anche '--model', altrimenti la CLI esce con un errore d'uso.
        run_hermes_test() {
            label="$1"; shift
            timeout 240 hermes -z "Rispondi solo: pong" "$@" \
                > "/tmp/selftest_${label}.log" 2>&1 || true
            out="$(tr '\n' ' ' < "/tmp/selftest_${label}.log" | tail -c 250)"
            if grep -qiE "context length exceeded" "/tmp/selftest_${label}.log"; then
                log "SELFTEST hermes[${label}]: FALLITO (Cortex ha rifiutato la richiesta) — ${out}"
            elif grep -qiE "requires --model|^usage:|unrecognized argument|HTTP [45][0-9][0-9]|Invalid OAuth|Traceback" "/tmp/selftest_${label}.log"; then
                log "SELFTEST hermes[${label}]: FALLITO — ${out}"
            elif [ -s "/tmp/selftest_${label}.log" ]; then
                log "SELFTEST hermes[${label}]: OK — ${out}"
            else
                log "SELFTEST hermes[${label}]: FALLITO (nessun output)"
            fi
        }

        # Solo il percorso di default: è quello che l'utente userà davvero.
        run_hermes_test "default"

        # 4. Prerequisiti di `hermes send --to telegram`. Questo guasto e'
        # silenzioso: senza il modulo telegram il comando esce con 1 solo
        # quando l'utente prova a inviare, e il venv sta fuori dal volume
        # persistente, quindi un'installazione fatta a runtime sparisce alla
        # prima ricreazione del container. Verificarlo al boot fa emergere la
        # regressione nei log invece che durante l'uso.
        VENV_PY_TG=/usr/local/lib/hermes-agent/venv/bin/python
        if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
            log "SELFTEST telegram: token assente — piattaforma non configurata"
        elif ! "$VENV_PY_TG" -c "import telegram" > /dev/null 2>&1; then
            log "SELFTEST telegram: FALLITO — modulo python-telegram-bot assente nel venv"
        else
            TG_VER="$("$VENV_PY_TG" -c "import telegram; print(telegram.__version__)" 2>/dev/null)"
            log "SELFTEST telegram: OK (python-telegram-bot ${TG_VER}, chat ${TELEGRAM_HOME_CHANNEL:-non impostata})"
        fi
    ) &
fi

log "pronto — provider=${ACTIVE_PROVIDER} model=${DEFAULT_MODEL}; web terminal su :7681"

exec ttyd --port 7681 --writable bash -l
