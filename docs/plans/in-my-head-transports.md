# In My Head — Live-Capture Transports

**Program:** VOX (see `organvm/vox/spec/phase-3-ingest-receiver/plan.md`)
**Owner:** `universal-mail--automation`
**Status:** adapter built; live account activation remains gated
**Concern:** the transports — Twilio webhook + Gmail watch — live where accounts already live.

## Ideal form (sought, not fixed)
An incoming SMS/email is transparently forwarded to `vox /ingest/*` and the
rendered clip surfaced back, with no new account logic.

## Process
1. Reuse existing `gmail_auth.py` / `auth/onepassword.py` (1Password-backed) —
   no new credential code.
2. `core/vox_transport.py` accepts only an already-authenticated Gmail/Twilio
   event and POSTs the Phase 3a form contract to `vox /ingest/email` or
   `/ingest/sms`.
3. Resolve the render profile at runtime through the existing credential
   loader, call `vox /jobs/{id}/generate`, and pass completed audio to a
   caller-supplied sink. The adapter has no provider catalog or output-surface
   table; absent optional selectors are left for VOX to resolve at runtime.
4. Provider keys remain hydrated in the credential organ / VOX runtime. They
   never cross the ingest payload and are never stored by this adapter.
5. Respect the his-hand registry: any *new* account action is a lever, not a chat task.

## Runtime wiring

- `VOX_BASE_URL` names the deployed receiver; there is no baked-in endpoint.
- `VOX_RENDER_PROFILE_OP_REF` points at a JSON credential-layer record with a
  required `voice_id` and optional, free-form `style_key` / `provider` values.
- `VOX_ACCESS_TOKEN_OP_REF` may point at a service bearer token when the
  deployed receiver is protected. It is never logged or copied into a receipt.
- The owning Gmail/Twilio integration supplies a non-secret authentication
  receipt reference before the adapter will make a network request.

## Verify against meta/macro
- [x] Uses the established 1Password-backed auth (universal-mail--automation
      AGENTS.md) — no secret in repo.
- [x] `vox` called only as a pure receiver (Phase 3a contract).
- [x] No ElevenLabs account creation here either — key comes from credential organ.

## Dispatch
`VOX-3b` → owner `universal-mail--automation`.

## Open by design
Which surfaces receive the rendered clip (mail draft, notification, app) is
decided as the loop is proven. Live Twilio webhook/Gmail watch registration and
credential hydration remain external account actions; the implementation does
not perform them.
