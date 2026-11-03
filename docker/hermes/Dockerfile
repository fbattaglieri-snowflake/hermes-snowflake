FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    openssh-server \
    python3 python3-pip \
    curl wget git vim nano jq \
    build-essential zstd ttyd \
    && rm -rf /var/lib/apt/lists/*

# Install Ollama CLI
RUN curl -fsSL https://ollama.com/install.sh | sh

# Install cloudflared (pinned version for reproducible builds)
ARG CLOUDFLARED_VERSION=2026.8.3
RUN curl -fsSL "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-amd64" \
    -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared && \
    cloudflared --version

# Install Tailscale for the Hermes Desktop Remote Gateway (:9119).
#
# Binaries must reside outside /root because the SPCS persistent block volume
# mounted at /root would mask any files written under /root during image build.
# Conversely, tailscaled state (/root/tailscale/tailscaled.state) lives on the
# persistent block volume so the node keeps a stable tailnet IP across restarts.
ARG TAILSCALE_VERSION=1.102.3
RUN set -eu; \
    curl -fsSL -o /tmp/ts.tgz \
      "https://pkgs.tailscale.com/stable/tailscale_${TAILSCALE_VERSION}_amd64.tgz"; \
    tar xzf /tmp/ts.tgz -C /tmp; \
    install -m 0755 "/tmp/tailscale_${TAILSCALE_VERSION}_amd64/tailscaled" /usr/local/bin/tailscaled; \
    install -m 0755 "/tmp/tailscale_${TAILSCALE_VERSION}_amd64/tailscale" /usr/local/bin/tailscale; \
    rm -rf /tmp/ts.tgz "/tmp/tailscale_${TAILSCALE_VERSION}_amd64"; \
    tailscaled --version | head -1

# Install Python helper packages used by embedded tools
RUN pip3 install --break-system-packages --ignore-installed \
    openai==3.7.0 \
    anthropic==1.3.0 \
    requests==2.33.0 \
    httpx==0.28.1 \
    PyJWT==2.13.0 \
    cryptography==50.0.1

# Install Hermes Agent outside /root so the runtime block volume mount does not
# mask the checkout, Python environments (installs/), or managed runtime tools.
#
# Build uses a dedicated home (/opt/hermes) with checkout in /opt/hermes/hermes-agent
# and HERMES_RUNTIME_DIR=/opt/hermes/tools. At runtime, HERMES_HOME is /root/.hermes
# (persisted on block storage) and start.sh links /opt/hermes/installs into it.
# /usr/local/lib/hermes-agent is kept as a stable symlink alias.
# --skip-computer-use avoids cua-driver, which requires X11 libraries in headless SPCS.
# --commit pins the exact upstream revision for deterministic, reproducible builds.
ENV HERMES_RUNTIME_DIR=/opt/hermes/tools
ARG HERMES_GIT_SHA=4d04303818b61bf3aa09278af48356315515965b
RUN set -eu; \
    REPO=/opt/hermes/hermes-agent; \
    curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/hermes-install.sh; \
    HERMES_HOME=/opt/hermes bash /tmp/hermes-install.sh --non-interactive \
      --skip-computer-use --dir "$REPO" --commit "$HERMES_GIT_SHA"; \
    rm -f /tmp/hermes-install.sh; \
    test -x "$REPO/.hermes/bin/hermes" || { echo "ERROR: hermes launcher missing" >&2; exit 1; }; \
    ln -sfn "$REPO/.hermes/bin/hermes" /usr/local/bin/hermes; \
    ln -sfn "$REPO" /usr/local/lib/hermes-agent; \
    HEAD_SHA="$(git -C "$REPO" rev-parse HEAD)"; \
    if [ "$HEAD_SHA" != "$HERMES_GIT_SHA" ]; then \
      echo "ERROR: checked out $HEAD_SHA, expected $HERMES_GIT_SHA" >&2; exit 1; \
    fi; \
    VENV_CFG="$(find /opt/hermes/installs -path '*/environments/*/venv/pyvenv.cfg')"; \
    if [ "$(printf '%s\n' "$VENV_CFG" | grep -c .)" != 1 ]; then \
      echo "ERROR: expected a single Hermes environment, found: $VENV_CFG" >&2; exit 1; \
    fi; \
    ln -sfn "$(dirname "$VENV_CFG")" "$REPO/venv"; \
    "$REPO/venv/bin/python" -c 'import hermes_cli'; \
    mkdir -p /root/.hermes && ln -sfn /opt/hermes/installs /root/.hermes/installs; \
    HERMES_HOME=/root/.hermes hermes --version; \
    echo "hermes $HEAD_SHA installed in $REPO"

# Install python-telegram-bot inside the Hermes virtualenv (required by
# `hermes send --to telegram` and the Telegram gateway listener).
RUN VENV_PY=/usr/local/lib/hermes-agent/venv/bin/python && \
    UV="$(command -v uv || find "$HERMES_RUNTIME_DIR" -type f -name uv -perm -u+x | head -1)" && \
    if "$VENV_PY" -m pip --version > /dev/null 2>&1; then \
        "$VENV_PY" -m pip install python-telegram-bot; \
    else \
        "$UV" pip install --python "$VENV_PY" python-telegram-bot; \
    fi && \
    "$VENV_PY" -c "import telegram; print('python-telegram-bot', telegram.__version__)"

# Embedded single-container fallback OpenAI->Cortex proxy
COPY proxy/cortex_proxy.py /opt/cortex_proxy.py

# Seed directory for /root/.hermes (config.yaml, .env, SOUL.md, and bundled skills).
# Excludes checkout, environments, runtime tools, caches, logs, telemetry, and backups.
RUN mkdir -p /opt/hermes-seed && \
    tar -C /opt/hermes --exclude=./hermes-agent --exclude=./installs --exclude=./tools \
      --exclude=./cache --exclude=./logs --exclude=./telemetry --exclude=./backups \
      -cf - . | tar -C /opt/hermes-seed -xf - && \
    test -d /opt/hermes-seed/skills || (echo "ERROR: seed skills directory missing" >&2; exit 1) && \
    echo "seed created: $(du -sh /opt/hermes-seed | cut -f1)"

# Configuration patcher and SOUL.md migration helper
COPY docker/hermes/hermes_configure.py /opt/hermes_configure.py
COPY docker/hermes/migrate_soul.py /opt/migrate_soul.py

# Verified Cortex models catalog for embedded fallback proxy
COPY proxy/models.json /opt/cortex_models.json

# SPCS rotating OAuth session token helper
COPY docker/hermes/spcs_token.sh /opt/spcs_token.sh
RUN chmod +x /opt/spcs_token.sh

# SSH daemon configuration
RUN mkdir /var/run/sshd && \
    sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config && \
    sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config && \
    sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config

COPY docker/hermes/start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 7681

ENTRYPOINT ["/start.sh"]
