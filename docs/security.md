# Security

## Threat Model

The deployment protects against credential publication, untrusted pull-request code execution, unauthenticated access to the Hermes backend, and accidental destruction of persistent state during upgrades.

It does not make Hermes Agent memory or session content trustworthy for sensitive data. Review the Hermes Agent documentation for data handling guarantees.

## Credential Handling

- GitHub authenticates with OIDC workload identity federation. No long-lived Snowflake credential is stored in GitHub.
- Tailscale authentication uses a reusable, non-ephemeral key stored as a Snowflake Secret. The key is injected into the container at runtime.
- Dashboard credentials are stored as scrypt hashes in Snowflake Secrets.
- Cortex proxy logs exclude authorization headers and token values.
- The proxy is bound to `127.0.0.1` inside the container. It is not reachable from outside.

## Pull Request Isolation

Pull request workflows have read-only GitHub permissions and never request an OIDC token.

## Least Privilege

The bootstrap role creates infrastructure. The deployment role is narrower. Review grants after bootstrap and remove any privilege that the deployment workflow does not require.

## Network Hardening

- Keep the Cortex proxy on loopback. Do not change `CORTEX_PROXY_BIND` to `0.0.0.0` in the container.
- Keep the Hermes service spec without a public endpoint for port 9119. Tailscale handles client connectivity.
- The `--accept-dns=false` Tailscale flag prevents the container's DNS resolver from being overwritten; SPCS internal hostnames must continue to resolve.

## Container Image Vulnerability Gate

Every pull request builds the image and scans it twice.

The **blocking** scan fails the build on CRITICAL and HIGH vulnerabilities, restricted to what this repository can fix. Three categories are excluded:

- **Unfixed vulnerabilities** (`ignore-unfixed`). Ubuntu kernel headers routinely carry CVEs with no released fix. Blocking on those would make every build red regardless of the diff, which trains reviewers to ignore the gate.
- **The Hermes agent tree and its seed** (`usr/local/lib/hermes-agent`, `opt/hermes-seed`). Their dependency set is resolved from the upstream `uv.lock` at the pinned `HERMES_GIT_SHA`. Forcing a newer package there would ship a combination upstream never resolved, so the upgrade lever is the pin.
- **Upstream compiled binaries** (`ollama`, `cloudflared`, `tailscale`, `tailscaled`). Their Go dependencies are fixed at the moment upstream built them and change only when upstream rebuilds. Confirmed in practice: cloudflared 2026.9.1, the current release, still embeds `golang.org/x/crypto v0.53.0`, and Tailscale 1.102.4 is the latest stable. Bumping the pin does not clear the finding.

The **advisory** scan covers the whole image, including all excluded categories, and never fails the build. Its purpose is that the excluded debt stays visible on every run rather than disappearing from view. Both scans are rendered by `scripts/summarize_trivy.py`, which prints CVE identifiers, installed and fixed versions, and the package path to the job summary.

Consequences to keep in mind:

- A red build means there is an action to take. Treat it as such.
- Review the advisory list when deciding whether to move `HERMES_GIT_SHA`, `CLOUDFLARED_VERSION` or `TAILSCALE_VERSION`. Check that the candidate release actually rebuilds against the fixed dependency before bumping; the version number alone does not tell you.
- The Ollama CLI is installed from the upstream `install.sh` without a version pin, so its Go dependency set is whatever upstream published at build time. This is the largest remaining source of advisory findings and the reason the binary is excluded rather than gated.
- Packages installed by this repository are governed at build time by `docker/hermes/assert_python_floors.py`, which fails the build if any copy of a distribution outside the excluded trees is below its security floor, and names the directory holding it.
- The build deletes `/root/.cache` before the image is finalized. The uv cache held unpacked copies of every wheel the resolver considered, which the scanner reported as shipped packages even though `/root` is masked at runtime by the block volume. Do not reintroduce a cache into the final image.
