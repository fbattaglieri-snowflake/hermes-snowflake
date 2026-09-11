FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    openssh-server \
    python3 python3-pip \
    curl wget git vim nano jq \
    build-essential zstd ttyd \
    && rm -rf /var/lib/apt/lists/*

# Install the Ollama CLI
RUN curl -fsSL https://ollama.com/install.sh | sh

# Install cloudflared (pinned version for reproducible builds)
ARG CLOUDFLARED_VERSION=2026.9.0
ARG CLOUDFLARED_SHA256=53b7a7a5420d188758d24341294acb0d1bca54296548ac05e38811a694ac6134
RUN curl -fsSL "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-amd64" \
    -o /usr/local/bin/cloudflared && \
    printf '%s  %s\n' "$CLOUDFLARED_SHA256" /usr/local/bin/cloudflared | sha256sum -c - && \
    chmod +x /usr/local/bin/cloudflared && \
    cloudflared --version

# Tailscale, for the Desktop Remote gateway.
#
# Goes into the image and NOT under /root: the block volume mounted on /root
# would mask the binaries, so a runtime download would have to be repeated on
# every container recreation. The *state*, by contrast, must live on the volume,
# otherwise the node re-registers on every boot and the tailnet IP changes,
# forcing every client to be reconfigured (see start.sh).
#
# Version pinned for the same reason as the Hermes pin: a non-reproducible build
# turns a rebuild done for an unrelated reason into an unrequested Tailscale
# upgrade.
ARG TAILSCALE_VERSION=1.102.4
RUN set -eu; \
    curl -fsSL -o /tmp/ts.tgz \
      "https://pkgs.tailscale.com/stable/tailscale_${TAILSCALE_VERSION}_amd64.tgz"; \
    tar xzf /tmp/ts.tgz -C /tmp; \
    install -m 0755 "/tmp/tailscale_${TAILSCALE_VERSION}_amd64/tailscaled" /usr/local/bin/tailscaled; \
    install -m 0755 "/tmp/tailscale_${TAILSCALE_VERSION}_amd64/tailscale" /usr/local/bin/tailscale; \
    rm -rf /tmp/ts.tgz "/tmp/tailscale_${TAILSCALE_VERSION}_amd64"; \
    tailscaled --version | head -1

# Install Python packages.
#
# --ignore-installed is required: some of these packages are also present as
# distro-managed apt packages, and without it pip refuses to replace them with
# the pinned versions.
RUN pip3 install --break-system-packages --ignore-installed \
    openai==3.7.0 \
    anthropic==1.3.0 \
    requests==2.33.0 \
    httpx==0.28.1 \
    PyJWT==2.13.0 \
    cryptography==50.0.1

# Install Hermes Agent (into the image — no reinstall needed on every restart).
# The block volume is mounted on /root at runtime: anything installed under
# /root at build time would be masked. We therefore assert the FHS layout.
RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash && \
    HERMES_BIN="$(command -v hermes || true)" && \
    if [ -z "$HERMES_BIN" ]; then echo "ERROR: hermes not installed" >&2; exit 1; fi && \
    case "$HERMES_BIN" in \
      /root/*) echo "ERROR: hermes at $HERMES_BIN would be masked by the volume on /root" >&2; exit 1 ;; \
    esac && \
    echo "hermes installed at $HERMES_BIN"

# Version pin. The upstream installer clones HEAD and exposes no revision flag
# (`hermes update` only accepts --branch, not a SHA), so without this step the
# build is NOT reproducible: two builds of the same commit of this repository
# can contain different Hermes versions, and a rebuild done for a proxy patch
# jumps the agent to an arbitrary version, mixing two changes into one.
#
# Override with --build-arg HERMES_GIT_SHA=<sha> to upgrade Hermes explicitly
# and traceably.
ARG HERMES_GIT_SHA=13ce0c5c675e843af70d19c9e5144249cd51c8d1
RUN set -eu; \
    REPO=/usr/local/lib/hermes-agent; \
    if ! git -C "$REPO" rev-parse --git-dir > /dev/null 2>&1; then \
        echo "ERROR: $REPO is not a git repo, cannot pin" >&2; exit 1; \
    fi; \
    git -C "$REPO" fetch --quiet origin "$HERMES_GIT_SHA" \
      || git -C "$REPO" fetch --quiet --unshallow origin \
      || git -C "$REPO" fetch --quiet origin; \
    git -C "$REPO" checkout --quiet --detach "$HERMES_GIT_SHA"; \
    VENV_PY="$REPO/venv/bin/python"; \
    UV="$(command -v uv || echo /root/.hermes/bin/uv)"; \
    # The installer resolves dependencies from uv.lock, not requirements.txt:
    # without a re-sync the venv would stay the one from HEAD on top of the
    # pinned code — exactly the combination nobody ever tested.
    if [ -f "$REPO/uv.lock" ]; then \
        ( cd "$REPO" && VIRTUAL_ENV="$REPO/venv" "$UV" sync --frozen ); \
    elif [ -f "$REPO/requirements.txt" ]; then \
        "$VENV_PY" -m pip install -r "$REPO/requirements.txt" \
          || "$UV" pip install --python "$VENV_PY" -r "$REPO/requirements.txt"; \
    else \
        echo "ERROR: neither uv.lock nor requirements.txt in $REPO" >&2; exit 1; \
    fi; \
    hermes --version; \
    echo "hermes pinned to $HERMES_GIT_SHA"

# python-telegram-bot: a dependency of `hermes send --to telegram`, which exits
# with 1 without it. The Hermes installer does not include it.
#
# Goes into the image, not at runtime: the venv lives under /usr/local, which is
# NOT the persistent volume (only /root is). A `uv pip install` run by the agent
# lands in the container's ephemeral layer and disappears on the first
# recreation — suspend/resume, ALTER SERVICE, restart — bringing the error back.
#
# pip is not exposed inside the venv (managed by uv), so we try `python -m pip`
# first and fall back to uv. The final import is an assertion: if the package is
# not importable the build fails here, instead of letting the problem surface on
# the first send.
RUN VENV_PY=/usr/local/lib/hermes-agent/venv/bin/python && \
    UV="$(command -v uv || echo /root/.hermes/bin/uv)" && \
    if "$VENV_PY" -m pip --version > /dev/null 2>&1; then \
        "$VENV_PY" -m pip install python-telegram-bot; \
    else \
        "$UV" pip install --python "$VENV_PY" python-telegram-bot; \
    fi && \
    "$VENV_PY" -c "import telegram; print('python-telegram-bot', telegram.__version__)"

# OpenAI->Cortex proxy in the image: start.sh copies it to /root/.hermes on every boot
COPY proxy/cortex_proxy.py /opt/cortex_proxy.py

# Seed for /root/.hermes: the installer writes config.yaml, .env, SOUL.md and
# ~6.6MB of bundled skills here, but at runtime the block volume mounted on
# /root masks them. We shelter them in /opt/hermes-seed; start.sh restores them
# if missing.
RUN mkdir -p /opt/hermes-seed && \
    cp -a /root/.hermes/. /opt/hermes-seed/ && \
    test -d /opt/hermes-seed/skills || (echo "ERROR: seed skills missing" >&2; exit 1) && \
    echo "seed created: $(du -sh /opt/hermes-seed | cut -f1)"

# Configuration script: patches the config.yaml generated by the installer
# instead of replacing it (the default is ~99KB of settings that must not be
# lost).
COPY docker/hermes/hermes_configure.py /opt/hermes_configure.py
COPY docker/hermes/migrate_soul.py /opt/migrate_soul.py

# Verified Cortex model list: a single source read by both the proxy and the
# configurator, so the two lists cannot diverge.
COPY proxy/models.json /opt/cortex_models.json

# Credential helper: prints the SPCS session token as JSON with a short TTL,
# used by 'key_cmd' in the Hermes config.
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
