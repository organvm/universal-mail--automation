# Cloudflare Share Demo

This repository includes a Cloudflare Worker share surface for quick partner review:

- Live URL: `https://universal-mail-automation-demo.ivixivi.workers.dev`
- Worker entrypoint: `cloudflare/worker.mjs`
- Worker config: `wrangler.toml`
- Static frontend assets: `web/`

## Purpose

The Worker is a public demo/share layer. It serves the commerce dashboard and a minimal same-origin API so reviewers can exercise the safety story without exposing the full Python backend or live mailbox credentials.

It is not the canonical product backend. The Python application remains the authoritative implementation for real provider operations, billing, ACP flows, and mailbox integrations.

## Verified Demo Endpoints

- `GET /health`
- `GET /app/`
- `POST /v1/senders/check`
- `POST /v1/triage/preview`
- `GET /v1/billing/plans`

Billing mutation endpoints intentionally return an unavailable response on the share demo when real billing is not configured.

## Deploy

Deploy from the repository root:

```sh
wrangler deploy
```

The deploy uses `wrangler.toml` and publishes the Worker plus `web/` assets. Verify the live surface after deploy before claiming the share URL is current.

## Safety Boundary

Do not present the Worker URL as the canonical production API unless that is explicitly decided later. It is a review surface for the dashboard and safety demonstration.
