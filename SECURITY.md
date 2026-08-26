# Security Policy

## Reporting a Vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub private vulnerability reporting, or contact the maintainer through the private contact method listed on the GitHub profile.

Include the affected component, reproduction steps, impact, and any proposed mitigation. Do not include live credentials, access tokens, account identifiers, private endpoint URLs, or customer data.

This process is provided on a best-effort basis and does not create a warranty, support obligation, or service-level commitment. See [DISCLAIMER.md](DISCLAIMER.md).

## Security Model

- GitHub Actions authenticates to Snowflake with short-lived OIDC workload identity tokens. No Snowflake credential is stored in GitHub.
- Runtime secrets (Tailscale auth key, Hermes dashboard password hash, Cloudflare tunnel token, SSH public key) are Snowflake Secret objects injected into SPCS containers.
- The Cortex proxy is exposed only on loopback (127.0.0.1) inside the Hermes container. It is not reachable from outside the container.
- Client connectivity uses Tailscale with userspace networking and DERP relay. It does not pass through the Snowflake ingress.
- The Hermes Dashboard uses cookie-based authentication with a bcrypt/scrypt password hash. The plaintext password is never stored.
- Pull request workflows never receive deployment permissions or secrets.
- Deployment requires manual approval by the repository maintainer.
