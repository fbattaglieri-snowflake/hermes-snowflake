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
ARG CLOUDFLARED_VERSION=2026.9.1
ARG CLOUDFLARED_SHA256=03f1f25d1cc93b9ad6c60569d44060bc4f17ed97075760ed8cfca4b12dcd68cc
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
#
# setuptools is pinned for the same reason it is scanned: the distro copy that
# apt installs alongside python3-pip is below the current security floor, and
# this interpreter — unlike the Hermes virtual environment, which is governed by
# the upstream lock file — is ours to upgrade.
RUN pip3 install --break-system-packages --ignore-installed \
    openai==3.7.0 \
    anthropic==1.3.0 \
    requests==2.33.0 \
    httpx==0.28.1 \
    PyJWT==2.15.0 \
    cryptography==50.0.1 \
    setuptools==84.0.0

# Replace the distro-owned copies through apt, including their actual code.
# Retain a local pip installation for the system interpreter and operators.
RUN python3 -m pip install --break-system-packages --ignore-installed pip==26.2.1 && \
    apt-get purge -y python3-pip python3-setuptools && \
    python3 -m pip --version && \
    python3 -c "import setuptools; assert setuptools.__version__ == '84.0.0'"

# Install Hermes Agent (into the image — no reinstall needed on every restart).
# The block volume is mounted on /root at runtime: anything installed under
# /root at build time would be masked. We therefore assert the FHS layout.
ARG HERMES_GIT_SHA=13ce0c5c675e843af70d19c9e5144249cd51c8d1
ARG HERMES_INSTALLER_SHA256=0582d9b1562efcb6e0ac62f4451021667830b830a72ce7d91eaea9fee8b6c09b
RUN curl -fsSL "https://raw.githubusercontent.com/NousResearch/hermes-agent/${HERMES_GIT_SHA}/scripts/install.sh" \
      -o /tmp/hermes-install.sh && \
    printf '%s  %s\n' "$HERMES_INSTALLER_SHA256" /tmp/hermes-install.sh | sha256sum -c - && \
    bash /tmp/hermes-install.sh --commit "$HERMES_GIT_SHA" --force-commit --non-interactive --skip-setup && \
    rm /tmp/hermes-install.sh && \
    HERMES_BIN="$(command -v hermes || true)" && \
    if [ -z "$HERMES_BIN" ]; then echo "ERROR: hermes not installed" >&2; exit 1; fi && \
    case "$HERMES_BIN" in \
      /root/*) echo "ERROR: hermes at $HERMES_BIN would be masked by the volume on /root" >&2; exit 1 ;; \
    esac && \
    echo "hermes installed at $HERMES_BIN"

# The pinned installer selects the commit BEFORE resolving dependencies into
# venv. Do not re-sync into uv's default .venv, which the launcher does not use.
# An intentional revision upgrade must also update the installer checksum.
RUN set -eu; \
    REPO=/usr/local/lib/hermes-agent; \
    test "$(git -C "$REPO" rev-parse HEAD)" = "$HERMES_GIT_SHA"; \
    VENV_PY="$REPO/venv/bin/python"; \
    test -x "$VENV_PY"; \
    case "$(readlink -f "$VENV_PY")" in /root/*) echo "ERROR: Python is masked by the root volume" >&2; exit 1 ;; esac; \
    grep -F "$VENV_PY" /usr/local/bin/hermes; \
    "$VENV_PY" -c "import hermes_cli, ruamel.yaml"; \
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

# Drop the build-time package caches.
#
# The uv cache under /root/.cache holds an unpacked copy of every wheel resolved
# while building the agent virtual environment, including versions the resolver
# considered and superseded. Nothing reads it at runtime — /root is masked by the
# block volume, and uv repopulates it on demand — but the image scan does read
# it, and reports each cached copy as a shipped package. Deleting it removes
# findings for code that was never going to execute, and shrinks the image.
#
# Must stay after every install step and after the seed copy above, which reads
# from /root/.hermes.
RUN rm -rf /root/.cache /tmp/* && \
    echo "build caches removed"

# Assert the floors the image scan enforces, while the paths are still visible.
#
# The scan reports a package name and a version but frequently no path, and this
# image contains three interpreters: the system one, the agent virtual
# environment, and whatever the Ollama installer brings. Failing here instead
# names the directory that holds the stale copy.
#
# /usr/local/lib/hermes-agent and its seed are excluded: their dependency set is
# resolved from the upstream uv.lock at the pinned HERMES_GIT_SHA. Forcing a
# newer package there would ship a combination upstream never resolved, so the
# upgrade lever is the pin, not an override. These paths remain INCLUDED in the
# complete blocking image scan in CI.
COPY docker/hermes/assert_python_floors.py /tmp/assert_python_floors.py
RUN python3 /tmp/assert_python_floors.py setuptools=78.1.1 \
      --exclude /usr/local/lib/hermes-agent \
      --exclude /opt/hermes-seed && \
    rm /tmp/assert_python_floors.py

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
