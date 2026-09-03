FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    openssh-server \
    python3 python3-pip \
    curl wget git vim nano jq \
    build-essential zstd ttyd \
    && rm -rf /var/lib/apt/lists/*

# Installa Ollama CLI
RUN curl -fsSL https://ollama.com/install.sh | sh

# Installa cloudflared (pinned version for reproducible builds)
ARG CLOUDFLARED_VERSION=2026.8.3
RUN curl -fsSL "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-amd64" \
    -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared && \
    cloudflared --version

# Tailscale, per il Remote gateway del Desktop.
#
# Va nell'immagine e NON sotto /root: il volume block montato su /root
# mascherebbe i binari, quindi un download a runtime andrebbe rifatto a ogni
# ricreazione del container. Lo *stato* invece deve stare sul volume, altrimenti
# il nodo si re-registra a ogni boot e l'IP del tailnet cambia, obbligando a
# riconfigurare ogni client (vedi start.sh).
#
# Versione pinnata per lo stesso motivo del pin di Hermes: una build non
# riproducibile trasforma un rebuild fatto per altro in un aggiornamento di
# Tailscale non richiesto.
ARG TAILSCALE_VERSION=1.102.3
RUN set -eu; \
    curl -fsSL -o /tmp/ts.tgz \
      "https://pkgs.tailscale.com/stable/tailscale_${TAILSCALE_VERSION}_amd64.tgz"; \
    tar xzf /tmp/ts.tgz -C /tmp; \
    install -m 0755 "/tmp/tailscale_${TAILSCALE_VERSION}_amd64/tailscaled" /usr/local/bin/tailscaled; \
    install -m 0755 "/tmp/tailscale_${TAILSCALE_VERSION}_amd64/tailscale" /usr/local/bin/tailscale; \
    rm -rf /tmp/ts.tgz "/tmp/tailscale_${TAILSCALE_VERSION}_amd64"; \
    tailscaled --version | head -1

# Installa pacchetti Python
RUN pip3 install --break-system-packages \
    openai==3.7.0 \
    anthropic==1.3.0 \
    requests==2.33.0 \
    httpx==0.28.1 \
    PyJWT==2.13.0 \
    cryptography==50.0.1

# Installa Hermes Agent (nell'immagine — non richiede reinstall ad ogni restart).
# Il volume block viene montato su /root a runtime: qualsiasi cosa installata sotto
# /root a build time sarebbe mascherata. Verifichiamo quindi il layout FHS.
RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash && \
    HERMES_BIN="$(command -v hermes || true)" && \
    if [ -z "$HERMES_BIN" ]; then echo "ERRORE: hermes non installato" >&2; exit 1; fi && \
    case "$HERMES_BIN" in \
      /root/*) echo "ERRORE: hermes in $HERMES_BIN, sarebbe mascherato dal volume su /root" >&2; exit 1 ;; \
    esac && \
    echo "hermes installato in $HERMES_BIN"

# Pin di versione. L'installer upstream clona HEAD e non espone alcun flag di
# revisione (`hermes update` accetta solo --branch, non uno SHA), quindi senza
# questo passo la build NON e' riproducibile: due build dello stesso commit di
# questo repository possono contenere versioni diverse di Hermes, e un rebuild
# fatto per una patch del proxy fa saltare l'agente a una versione arbitraria,
# mescolando due cambiamenti in uno.
#
# Sovrascrivibile con --build-arg HERMES_GIT_SHA=<sha> per aggiornare Hermes in
# modo esplicito e tracciabile.
ARG HERMES_GIT_SHA=13ce0c5c675e843af70d19c9e5144249cd51c8d1
RUN set -eu; \
    REPO=/usr/local/lib/hermes-agent; \
    if ! git -C "$REPO" rev-parse --git-dir > /dev/null 2>&1; then \
        echo "ERRORE: $REPO non e' un repo git, pin impossibile" >&2; exit 1; \
    fi; \
    git -C "$REPO" fetch --quiet origin "$HERMES_GIT_SHA" \
      || git -C "$REPO" fetch --quiet --unshallow origin \
      || git -C "$REPO" fetch --quiet origin; \
    git -C "$REPO" checkout --quiet --detach "$HERMES_GIT_SHA"; \
    VENV_PY="$REPO/venv/bin/python"; \
    UV="$(command -v uv || echo /root/.hermes/bin/uv)"; \
    # L'installer risolve le dipendenze da uv.lock, non da requirements.txt:
    # senza ri-sync il venv resterebbe quello di HEAD sopra il codice pinnato,
    # cioe' esattamente la combinazione mai testata da nessuno.
    if [ -f "$REPO/uv.lock" ]; then \
        ( cd "$REPO" && VIRTUAL_ENV="$REPO/venv" "$UV" sync --frozen ); \
    elif [ -f "$REPO/requirements.txt" ]; then \
        "$VENV_PY" -m pip install -r "$REPO/requirements.txt" \
          || "$UV" pip install --python "$VENV_PY" -r "$REPO/requirements.txt"; \
    else \
        echo "ERRORE: ne uv.lock ne requirements.txt in $REPO" >&2; exit 1; \
    fi; \
    hermes --version; \
    echo "hermes pinnato a $HERMES_GIT_SHA"

# python-telegram-bot: dipendenza di `hermes send --to telegram`, che senza di
# essa esce con 1. L'installer di Hermes non la include.
#
# Va nell'immagine, non a runtime: il venv sta sotto /usr/local, che NON e' il
# volume persistente (solo /root lo e'). Un `uv pip install` eseguito
# dall'agente finisce nel layer effimero del container e sparisce alla prima
# ricreazione — suspend/resume, ALTER SERVICE, restart — riportando l'errore.
#
# pip non e' esposto dentro il venv (gestito da uv), quindi si tenta prima
# `python -m pip` e si ricade su uv. L'import finale e' un'asserzione: se il
# pacchetto non e' importabile la build fallisce qui, invece di lasciare il
# problema emergere al primo invio.
RUN VENV_PY=/usr/local/lib/hermes-agent/venv/bin/python && \
    UV="$(command -v uv || echo /root/.hermes/bin/uv)" && \
    if "$VENV_PY" -m pip --version > /dev/null 2>&1; then \
        "$VENV_PY" -m pip install python-telegram-bot; \
    else \
        "$UV" pip install --python "$VENV_PY" python-telegram-bot; \
    fi && \
    "$VENV_PY" -c "import telegram; print('python-telegram-bot', telegram.__version__)"

# Proxy OpenAI->Cortex nell'immagine: start.sh lo copia su /root/.hermes ad ogni boot
COPY proxy/cortex_proxy.py /opt/cortex_proxy.py

# Seed di /root/.hermes: l'installer scrive qui config.yaml, .env, SOUL.md e ~6.6MB
# di skill bundled, ma a runtime il volume block montato su /root le maschera.
# Le mettiamo al riparo in /opt/hermes-seed; start.sh le ripristina se mancanti.
RUN mkdir -p /opt/hermes-seed && \
    cp -a /root/.hermes/. /opt/hermes-seed/ && \
    test -d /opt/hermes-seed/skills || (echo "ERRORE: seed skills mancante" >&2; exit 1) && \
    echo "seed creato: $(du -sh /opt/hermes-seed | cut -f1)"

# Script di configurazione: patcha il config.yaml generato dall'installer invece di
# sostituirlo (il default e' ~99KB di impostazioni che non vanno perse).
COPY docker/hermes/hermes_configure.py /opt/hermes_configure.py

# Elenco modelli Cortex verificati: sorgente unica letta sia dal proxy sia dal
# configuratore, così le due liste non possono divergere.
COPY proxy/models.json /opt/cortex_models.json

# Helper credenziali: stampa il session token SPCS come JSON con TTL breve,
# usato da 'key_cmd' nel config di Hermes.
COPY docker/hermes/spcs_token.sh /opt/spcs_token.sh
RUN chmod +x /opt/spcs_token.sh

# SSH config
RUN mkdir /var/run/sshd && \
    sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config && \
    sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config && \
    sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config

COPY docker/hermes/start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 7681

ENTRYPOINT ["/start.sh"]
