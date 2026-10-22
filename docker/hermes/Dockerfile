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
RUN pip3 install --break-system-packages --ignore-installed \
    openai==3.7.0 \
    anthropic==1.3.0 \
    requests==2.33.0 \
    httpx==0.28.1 \
    PyJWT==2.13.0 \
    cryptography==50.0.1

# Installa Hermes Agent (nell'immagine — non richiede reinstall ad ogni restart).
# Il volume block viene montato su /root a runtime: qualsiasi cosa installata sotto
# /root a build time sarebbe mascherata. Verifichiamo quindi il layout FHS.
#
# L'installer attuale di default clona in $HERMES_HOME/hermes-agent, mette uv e i
# runtime in $HERMES_HOME/tools e pubblica il comando in ~/.local/bin: tutto sotto
# /root. --dir porta il checkout in /usr/local/lib, HERMES_RUNTIME_DIR (impostato
# per tutta l'immagine, come nel Dockerfile upstream) porta lo store dei runtime
# fuori dal volume, e il launcher viene linkato in /usr/local/bin.
# --skip-computer-use evita cua-driver, che richiede librerie X11 e non serve qui.
#
# Pin di versione: l'installer accetta --commit e prepara venv e dipendenze per
# quel commit, quindi build dello stesso commit di questo repository producono la
# stessa versione di Hermes. Sovrascrivibile con --build-arg HERMES_GIT_SHA=<sha>
# per aggiornare Hermes in modo esplicito e tracciabile (default: release v0.22.4).
ENV HERMES_RUNTIME_DIR=/usr/local/lib/hermes-runtime
ARG HERMES_GIT_SHA=4d04303818b61bf3aa09278af48356315515965b
RUN set -eu; \
    REPO=/usr/local/lib/hermes-agent; \
    curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/hermes-install.sh; \
    bash /tmp/hermes-install.sh --non-interactive --skip-computer-use \
      --dir "$REPO" --commit "$HERMES_GIT_SHA"; \
    rm -f /tmp/hermes-install.sh; \
    if ! command -v hermes > /dev/null 2>&1; then \
      for cand in "$REPO/.hermes/bin/hermes" /root/.local/bin/hermes; do \
        if [ -x "$cand" ]; then ln -sf "$(readlink -f "$cand")" /usr/local/bin/hermes; break; fi; \
      done; \
    fi; \
    HERMES_BIN="$(readlink -f "$(command -v hermes || true)" 2>/dev/null || true)"; \
    if [ -z "$HERMES_BIN" ]; then echo "ERRORE: hermes non installato" >&2; exit 1; fi; \
    case "$HERMES_BIN" in \
      /root/*) echo "ERRORE: hermes in $HERMES_BIN, sarebbe mascherato dal volume su /root" >&2; exit 1 ;; \
    esac; \
    HEAD_SHA="$(git -C "$REPO" rev-parse HEAD)"; \
    if [ "$HEAD_SHA" != "$HERMES_GIT_SHA" ]; then \
      echo "ERRORE: checkout a $HEAD_SHA, atteso $HERMES_GIT_SHA" >&2; exit 1; \
    fi; \
    # start.sh e i passi successivi usano $REPO/venv: se l'installer crea .venv o
    # un venv esterno, lo esponiamo con quel nome invece di riscrivere ogni chiamante.
    if [ ! -x "$REPO/venv/bin/python" ]; then \
      VENV_DIR=""; \
      for cfg in "$REPO/.venv/pyvenv.cfg" $(find "$HERMES_RUNTIME_DIR" -maxdepth 5 -name pyvenv.cfg 2>/dev/null); do \
        d="$(dirname "$cfg")"; \
        if [ -x "$d/bin/python" ] && "$d/bin/python" -c 'import hermes_cli' > /dev/null 2>&1; then VENV_DIR="$d"; break; fi; \
      done; \
      if [ -z "$VENV_DIR" ]; then \
        echo "ERRORE: venv di Hermes non trovato; candidati:" >&2; \
        find "$REPO" "$HERMES_RUNTIME_DIR" -maxdepth 5 -name pyvenv.cfg >&2 || true; exit 1; \
      fi; \
      ln -sfn "$VENV_DIR" "$REPO/venv"; \
    fi; \
    case "$(readlink -f "$REPO/venv")" in \
      /root/*) echo "ERRORE: venv sotto /root, sarebbe mascherato dal volume" >&2; exit 1 ;; \
    esac; \
    hermes --version; \
    echo "hermes $HEAD_SHA installato in $HERMES_BIN"

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
    UV="$(command -v uv || find "$HERMES_RUNTIME_DIR" -type f -name uv -perm -u+x | head -1)" && \
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
