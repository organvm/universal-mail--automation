# Deploy

## Current status — 2026-06-27

This checkout is deploy-ready as a build candidate:

- Python tests: `500 passed`.
- Python lint: `python3 -m ruff check --select E9,F63,F7,F82 .` passed.
- Python package: `python3 -m build --no-isolation` produced
  `dist/universal_mail_automation-0.1.0-py3-none-any.whl` and
  `dist/universal_mail_automation-0.1.0.tar.gz`; `twine check dist/*` passed.
- Wheel smoke: `umail --version` and `import core; core.__version__` passed from
  the built wheel.
- Web: `npm ci --offline`, `npm run lint -- --max-warnings=0`, and
  `npm run build` passed; static export is in `web/out`.
- Cloudflare Worker unit tests: `node --test cloudflare/worker.test.mjs` passed.
- API smoke: `/health`, `/v1/billing/plans`, `/v1/senders/check`, and `/app/`
  passed through FastAPI `TestClient`.

Exactly what remains:

1. Merge/release gate: review CI for this branch/PR, then merge it to `main`.
   Do not merge stale duplicate PRs one by one; close or supersede them after this
   lands. PR #77's critical stdlib-shadowing fix is included here via
   `platform/` -> `uma_platform/`.
2. Outward deploy credentials: set `CLOUDFLARE_API_TOKEN` for the share Worker
   deploy, or set a container host target (`RENDER_DEPLOY_HOOK`, Fly/Render/GHCR
   auth) before publishing outside this machine.
3. Revenue secrets for paid/live operation: set Stripe price/webhook secrets and
   provider mailbox credentials in the host environment. Credential-free demo
   endpoints already run without these.

Local limitations during this verification: this sandbox cannot bind listening
ports and has no `docker` binary, so the container smoke was represented by the
in-process FastAPI smoke plus package/web/worker builds.

The service is a single FastAPI app (API at `/v1/*`, dashboard at `/app`, health at
`/health`). It ships as a `Dockerfile` that honors a platform-provided `$PORT`, so it
runs unchanged on any container host.

## Local

```bash
pip install -r requirements.txt -r requirements-api.txt
uvicorn api.app:app --reload        # http://127.0.0.1:8000/app
```

## Docker (anywhere)

```bash
docker build -t mail-api .
docker run -p 8000:8000 --env-file prod.env mail-api
```

## Render (blueprint included)

Push the repo, then in Render: **New + → Blueprint** and pick this repo. `render.yaml`
provisions a Docker web service with a `/health` check. Set provider credentials as
environment variables in the dashboard (below).

## Fly.io

```bash
fly launch --dockerfile Dockerfile --internal-port 8000   # generates fly.toml
fly secrets set GMAIL_OAUTH_OP_REF=... GMAIL_TOKEN_OP_REF=...
fly deploy
```

## Credentials (set as host env vars — never commit)

Single-tenant for now: the server holds the mailbox credentials. Per provider:

| Provider | Env vars |
|---|---|
| Gmail | `GMAIL_OAUTH_OP_REF`, `GMAIL_TOKEN_OP_REF` (or the `OP_GMAIL_TOKEN_*` triplet) |
| IMAP | `IMAP_HOST`, `IMAP_USER`, `IMAP_PASS` (or 1Password refs) |
| Outlook | `OUTLOOK_CLIENT_ID`, `OUTLOOK_TOKEN_CACHE` |
| Mail.app | local macOS only (not container-deployable) |

The pure endpoints (`/health`, `/v1/senders/check`) and the dashboard's live
protected-sender check need **no** credentials — so a deployed instance is demoable
immediately, before any mailbox is connected.

## Commerce & agent surfaces

The same app serves the money + agent surfaces. All of these are **fail-soft**:
absent the relevant secret, the catalog/pure endpoints still work and the
money/charge endpoints return a clean `503 billing is not configured`.

| Surface | Endpoint(s) | Secret(s) needed |
|---|---|---|
| Pricing catalog | `GET /v1/billing/plans` | none |
| Subscription checkout | `POST /v1/billing/checkout` | `STRIPE_SECRET_KEY`, `STRIPE_PRICE_PRO`, `STRIPE_PRICE_BUSINESS` |
| Customer portal | `POST /v1/billing/portal` | `STRIPE_SECRET_KEY` |
| Stripe webhook | `POST /v1/billing/webhook` | `STRIPE_WEBHOOK_SECRET` |
| Signed receipt | `GET /v1/audit/{run_id}` | `RECEIPT_SIGNING_KEY` (else ephemeral) |
| Agentic Commerce (ACP) | `/acp/checkout_sessions*`, `GET /acp/feed.json` | `STRIPE_SECRET_KEY` (for the charge) |
| MCP tools (Streamable HTTP) | `/mcp` | `MCP_ALLOWED_HOSTS` = your host(s) |
| Agent discovery | `GET /.well-known/agent.json`, `GET /llms.txt` | none |

Webhook setup: in the Stripe dashboard, point a webhook at
`https://<host>/v1/billing/webhook` and subscribe to `checkout.session.completed`,
`customer.subscription.*`, `invoice.paid`, `invoice.payment_failed`. Locally:
`stripe listen --forward-to localhost:8000/v1/billing/webhook`.

MCP: the deploy image (Python 3.11) installs `requirements-mcp.txt`, so `/mcp` is
live. Set `MCP_ALLOWED_HOSTS=<your-domain>` (DNS-rebinding protection is on by
default; use `*` only if a proxy already validates Host). Local stdio for Claude
Desktop etc.: `python -m mcp_server`. See `docs/agent-commerce.md`.

Regenerate the static GTM artifacts (`pricing.md`, `llms.txt`,
`.well-known/agent.json`, `server.json`) after any pricing change:
`PUBLIC_BASE_URL=https://<host> python scripts/gen_commerce_artifacts.py`.

## Next milestone

Multi-tenant auth (customers connect their **own** mailbox via OAuth) and the MCP
OAuth 2.1 Resource Server (`/.well-known/oauth-protected-resource`) are the next
bricks — both are credential-gated on registering OAuth apps. See the product
roadmap in `docs/plans/`.
