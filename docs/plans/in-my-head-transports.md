# In My Head — Live-Capture Transports

**Program:** VOX (see `organvm/vox/spec/phase-3-ingest-receiver/plan.md`)
**Owner:** `universal-mail--automation`
**Status:** open
**Concern:** the transports — Twilio webhook + Gmail watch — live where accounts already live.

## Ideal form (sought, not fixed)
An incoming SMS/email is transparently forwarded to `vox /ingest/*` and the
rendered clip surfaced back, with no new account logic.

## Process
1. Reuse existing `gmail_auth.py` / `auth/onepassword.py` (1Password-backed) —
   no new credential code.
2. Add an adapter that, on received message, POSTs to `vox /ingest/email`
   (and `/ingest/sms` for Twilio).
3. Pull the resolved `voice_id` + hydrated key from the credential layer; call
   `vox /jobs/{id}/generate`; route the audio back to the user/surface.
4. Respect the his-hand registry: any *new* account action is a lever, not a chat task.

## Verify against meta/macro
- [ ] Uses the established 1Password-backed auth (universal-mail--automation
      AGENTS.md) — no secret in repo.
- [ ] `vox` called only as a pure receiver (Phase 3a contract).
- [ ] No ElevenLabs account creation here either — key comes from credential organ.

## Dispatch
`VOX-3b` → owner `universal-mail--automation`.

## Open by design
Which surfaces receive the rendered clip (mail draft, notification, app) is
decided as the loop is proven.
