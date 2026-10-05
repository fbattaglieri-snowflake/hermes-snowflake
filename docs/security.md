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

Every pull request builds the image and runs a complete blocking HIGH/CRITICAL
scan. All upstream binaries, locked dependencies and unfixed findings are
included. A failed build skips the scan; it does not pass the security gate.
The summary reports findings even when the scan fails. Do not delete package
metadata, override locked dependencies, or bypass findings to obtain green CI.

Unresolved upstream findings remain release blockers. Package-floor assertions
are supplementary: they do not rewrite upstream locked trees, which remain in
the complete image scan. Old runs using partial gates are not security evidence
for the current image. Build caches are removed before image finalization.

The Hermes installer is retrieved from the application revision and checksum
verified, then selects that revision before dependency installation. The build
checks the actual launcher and interpreter outside the mounted root volume.
Ollama's mutable installer and tag-only base images remain supply-chain risks;
passing tests or scans is not a blanket security or reproducibility guarantee.
