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
