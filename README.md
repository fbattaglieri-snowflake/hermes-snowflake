# Hermes Agent on Snowflake

Deploy [Hermes Agent](https://hermes-agent.nousresearch.com) (NousResearch) to Snowflake Snowpark Container Services — with persistent block storage, a Cortex AI compatibility proxy, Tailscale-based remote client access, and GitHub Actions OIDC deployment.

> **Disclaimer:** This independent project was developed and is maintained by **Francesco Battaglieri**, with the assistance of **Snowflake Cortex Code**. It is not an official release, product, service, publication, or support offering from Snowflake Inc. or NousResearch Inc. (authors of Hermes Agent). **Neither organization reviewed, approved, sponsored, endorsed, certified, authorized, commissioned, or warranted this project.** They bear no responsibility for it. Use is entirely at your own risk. See [DISCLAIMER.md](DISCLAIMER.md) for the full no-warranty, limitation-of-liability, non-affiliation, and user-responsibility terms.

## Why This Exists

Hermes Agent speaks the OpenAI Chat Completions protocol. Snowflake Cortex exposes the same interface, but deviates from the specification in six ways that break standard OpenAI clients:

| # | Deviation | Symptom in Hermes |
|---|---|---|
| 1 | `max_tokens` rejected — wants `max_completion_tokens` | `Context length exceeded (19 tokens)` on every call |
| 2 | `finish_reason` empty or absent | Truncated responses, text duplication, corrupted tool-call history |
| 3 | `tools` + `reasoning_effort` mutually exclusive on some models | `Function tools with reasoning_effort are not supported` |
| 4 | `GET /v1/models` returns 404 | Model list never populates at handshake |
| 5 | Token from `/snowflake/session/token` required, not `Authorization: Bearer` | 401 on every request without the SPCS header rewrite |

The `proxy/cortex_proxy.py` in this repository resolves all five in a minimal, dependency-free Python process that runs inside the container on `127.0.0.1:8080`. Hermes is configured to use this proxy as its LLM provider. The result: every Cortex model that supports tool calling works reliably with Hermes in full agent mode.

## Architecture

```text
Hermes Desktop (any PC with Tailscale)
    │
    │   Remote gateway  http://<tailnet-ip>:9119
    │   Auth: cookie / basic gate
    ▼
Tailscale DERP relay (TCP/443, node closest to your region)
    ▼
Container — SPCS (CPU compute pool, block storage on /root)
    ├── hermes serve --skip-build --host 0.0.0.0 --port 9119
    ├── cortex_proxy.py  127.0.0.1:8080  ← fixes the 6 Cortex deviations
    ├── tailscaled  (userspace networking, QUIC disabled by SPCS)
    ├── ttyd :7681  (web terminal, readiness probe target)
    ├── cloudflared  (fallback tunnel)
    └── sshd :22  (fallback access)
         │
         └─▶  Cortex AI Gateway  (/api/v2/cortex/v1/chat/completions)
                                  models verified per account
```

**Why Tailscale and not the public ingress?** Snowflake ingress consumes the `Authorization` header and requires a Snowflake PAT. Hermes Desktop sends a WebSocket upgrade that the ingress cannot handle. Tailscale bypasses the ingress entirely using DERP relay TCP on port 443, which passes through SPCS external access integrations.

## What Is Included

- Reproducible `linux/amd64` container image that installs Hermes via the official upstream installer.
- `cortex_proxy.py` — production-tested Cortex compatibility layer with watchdog.
- `models.json` — catalog of verified Cortex models with context windows, tool-calling constraints, and known-unavailable entries.
- `cortex_model_gate.py` — verifies a new model against the full proxy+upstream stack before promoting it to configuration.
- `refresh_cortex_models.py` — reads the Snowflake model catalog, tests every entry with a real call, and rewrites `models.json`.
- Parameterized SPCS service specification with block storage and Snowflake Secrets injection.
- GitHub Actions OIDC workflows for bootstrap, deploy, and ordered suspend/resume.
- Complete client setup guide: Hermes Desktop + Tailscale on Windows, macOS, and Linux.

## Prerequisites

- A Snowflake account with Snowpark Container Services enabled.
- Cortex AI available in your account's region, with at least one chat model you can call. **Verify this first**: model availability and the set of usable names differ by cloud (AWS, Azure, GCP), by region, and with the `CORTEX_ENABLED_CROSS_REGION` setting. Nothing else in this repository works without it.
- A compute pool instance family available on your cloud. Family names are **not** portable between clouds — check `SHOW COMPUTE POOLS` and the Snowflake documentation for your platform rather than copying the example value.
- A Tailscale account (free plan is sufficient; one tailnet shared between the container and all client machines).
- Docker or another OCI-compatible builder capable of producing `linux/amd64` images.
- A public GitHub repository with Actions enabled.

> Developed and exercised on Snowflake on AWS. Nothing in the design is AWS-specific, but the deployment has not been run on Azure or GCP, where SPCS and Cortex feature parity may differ. Treat the instance family, the model catalog, and Cortex availability as the three things to verify on your own platform.

## Deployment Flow

1. Fork or clone the repository.
2. Review [docs/motivation.md](docs/motivation.md), [docs/security.md](docs/security.md), and [docs/configuration.md](docs/configuration.md).
3. Run the one-time OIDC trust setup in `infrastructure/sql/00_oidc_trust.sql`.
4. Create the `bootstrap` and `production` GitHub environments; set the maintainer as the sole required reviewer.
5. Configure the repository variables listed in [docs/configuration.md](docs/configuration.md).
6. Run **Bootstrap Snowflake** manually.
7. Set the three Hermes dashboard secrets (`HERMES_DASH_USERNAME`, `HERMES_DASH_PASSWORD_HASH`, `HERMES_DASH_SECRET`) and the Tailscale auth key (`TS_AUTHKEY`) as Snowflake Secrets in the deployed schema.
8. Run **Deploy to Snowflake** manually and approve the `production` environment.
9. Follow [docs/client-setup.md](docs/client-setup.md) to connect Hermes Desktop.

## Documentation

- [Motivation and Cortex deviations](docs/motivation.md)
- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md)
- [Client setup: Hermes Desktop + Tailscale](docs/client-setup.md)
- [Model management: gate, refresh, promotion](docs/model-management.md)
- [Security](docs/security.md)
- [Operations: suspend, resume, upgrade](docs/operations.md)
- [Troubleshooting](docs/troubleshooting.md)

## License

Repository-authored code and documentation are licensed under Apache-2.0. Hermes Agent and all upstream dependencies retain their own licenses. Review [DISCLAIMER.md](DISCLAIMER.md) and [NOTICE](NOTICE) before using or redistributing this project.
