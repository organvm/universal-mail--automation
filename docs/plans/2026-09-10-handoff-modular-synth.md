# Active Handoff: Modular Synth & Multi-Channel Communications

## System Context
- **Repository**: `universal-mail--automation` (Universal Communications Synth)
- **Organ**: Organ III (Commerce)
- **Branch**: `feature/modular-synth-router`

## Current Capabilities
1. **Multi-Channel Intake Jacks**:
   - `POST /v1/intake`: Core decision engine accepting any `CommMessage` and returning tier, actions, and SLA deadlines.
   - `POST /v1/inbound/twilio`: Adapts raw Twilio SMS webhook payloads.
   - `POST /v1/inbound/generic`: Adapts generic JSON webhook payloads.
   - `POST /v1/dispatch`: Evaluates message and transmits across all active patch cables.
2. **PatchBay Routing**:
   - Evaluates `min_tier`, transport channel, and label match conditions.
   - Routes actions to `WEBHOOK`, `LOCAL_LOG` (append-only JSONL ledgers), `PROVIDER`, or in-process `CALLBACK` sinks.
3. **Configuration**:
   - Loads from `synth.yaml` (entities and patch cables) or falls back to `mail_automation.yaml`.
   - Sample available at `synth.yaml.example`.

## Next Operational Vectors
1. **External Repo Integration**:
   - Connect `vox` repository by pointing its SMS webhook forwarding to `POST /v1/inbound/twilio`.
   - Connect Slack event listeners to `POST /v1/inbound/generic`.
2. **24/7 Cloud Deployment**:
   - When deploying the API publicly, ensure `/v1/inbound/*` endpoints are protected by webhook signature verification (e.g. Twilio auth token validator).
3. **Autonomous Daemon Extraction**:
   - If non-email event volume exceeds email by >50%, execute the extraction blueprint in `new-repo-evaluation-and-criteria.md` using `conductor wip promote`.
