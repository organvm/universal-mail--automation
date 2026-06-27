# Deploy

Status: deploy-ready for the single-tenant FastAPI product surface.

The app is one containerized FastAPI service:

- API: `/v1/*`
- Dashboard: `/app`
- Health: `/health`
- Agent surfaces: `/mcp`, `/acp/*`, `/.well-known/agent.json`, `/llms.txt`

## Verified Build

Validated on 2026-06-27:

- `python3 -m ruff check --select E9,F63,F7,F82 .`
- `python3 -m pytest -q` - 503 passed
- `node --test cloudflare/worker.test.mjs` - 13 passed
- `cd web && npm run lint -- --max-warnings=0`
- `cd web && npm run build`
- `python3 -m build --no-isolation --outdir /tmp/uma-dist-final`
- `python3 -m twine check /tmp/uma-dist-final/*`
- Wheel smoke: `umail --version` and `core.__version__` both return `0.2.0`
- In-process API smoke: `/health`, `/v1/billing/plans`, `/v1/senders/check`, `/app/`

Not run in this sandbox: `docker build` / `docker run` because Docker is not
installed, and a live `uvicorn` socket smoke because local port binding is
blocked. The same endpoints passed through `FastAPI TestClient`.

## Ship

```bash
docker build -t mail-api .
docker run -p 8000:8000 --env-file prod.env mail-api
```

The image honors platform-provided `$PORT`, so Render, Fly.io, Cloud Run, and
similar container hosts can run it unchanged.

Render is already described by `render.yaml`: create a Blueprint from this repo,
set the env vars below, then deploy. `scripts/deploy.sh render` can trigger a
deploy hook when `RENDER_DEPLOY_HOOK` is set.

## Remaining Gates

No code blockers remain. These external gates are required only for the matching
live capability:

| Gate | Required for | Env vars / action |
|---|---|---|
| Mailbox credentials | Live mailbox triage | Gmail: `GMAIL_OAUTH_OP_REF`, `GMAIL_TOKEN_OP_REF` or `OP_GMAIL_TOKEN_*`; IMAP: `IMAP_HOST`, `IMAP_USER`, `IMAP_PASS`; Outlook: `OUTLOOK_CLIENT_ID`, `OUTLOOK_TOKEN_CACHE` |
| Stripe billing | Checkout, portal, webhooks, ACP charges | `STRIPE_SECRET_KEY`, `STRIPE_PRICE_PRO`, `STRIPE_PRICE_BUSINESS`, `STRIPE_WEBHOOK_SECRET` |
| Receipt signing | Stable signed audit receipts across restarts | `RECEIPT_SIGNING_KEY` |
| Hosted MCP | `/mcp` on a public host | `MCP_ALLOWED_HOSTS=<your-domain>` |
| Cloudflare share demo | Worker deploy to `uma.4444j99.dev` | `CLOUDFLARE_API_TOKEN` in CI or `wrangler login` locally |

Credential-free endpoints are demoable immediately after deploy:
`/health`, `/app`, `/v1/billing/plans`, `/v1/senders/check`,
`/.well-known/agent.json`, and `/llms.txt`.

## Final Smoke

After deployment:

```bash
BASE=https://<host>
curl -fsS "$BASE/health"
curl -fsS "$BASE/v1/billing/plans"
curl -fsS -H 'content-type: application/json' \
  -d '{"sender":"clerk@courts.ca.gov"}' \
  "$BASE/v1/senders/check"
curl -fsS "$BASE/app/" >/dev/null
```
