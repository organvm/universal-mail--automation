"""Re-read reviewed obligations and persistent Mail.app overrides before writes."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.flag_workflow import _json_object_from_path, OverrideStore, require_hex256
from core.mail_inventory import validate
from core.obligation_workflow import MessageIdentity, Obligation, reconcile


class ArchiveGuard:
    def __init__(self, evidence_path: Path, override_path: Path):
        self.evidence_path = evidence_path
        self.overrides = OverrideStore(override_path)

    def __call__(self, identity):
        source = _json_object_from_path(self.evidence_path, "current archive guard evidence")
        validate(source)
        if source.get("schema") != "uma.archive_guard.v1":
            raise ValueError("versioned archive guard evidence required")
        now = datetime.now(timezone.utc)
        observed = datetime.fromisoformat(source["observed_at"])
        if observed.tzinfo is None or not timedelta(0) <= now - observed <= timedelta(hours=24):
            raise ValueError("archive guard evidence is stale")
        key = MessageIdentity.model_validate(identity).key
        bindings = [binding for binding in source["bindings"] if binding["identity"] == identity]
        if len(bindings) != 1:
            raise ValueError("exact native-to-Mail.app override binding required")
        ref_digest = bindings[0]["ref_digest"]
        require_hex256(ref_digest, "archive guard ref digest")
        obligations = [Obligation.model_validate(o) for o in source["obligations"]]
        rows = reconcile(obligations, now=now, coverage_gaps=source.get("coverage_gaps", []))["obligations"]
        siblings = [row for row in rows if any(MessageIdentity.model_validate(m).key == key for m in row["messages"])]
        if not siblings:
            raise ValueError("archive guard has no researched obligations for identity")
        return {"protected": any(row["protected"] or not row["archive_eligible"] for row in siblings),
                "human_override": any(row["human_override"] for row in siblings) or self.overrides.is_suppressed(ref_digest)}
