"""Re-read reviewed obligations and persistent Mail.app overrides before writes."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.flag_workflow import _json_object_from_path, OverrideStore, require_hex256, sha256_hex
from core.mail_inventory import seal, validate
from core.obligation_workflow import MessageIdentity, Obligation, reconcile


class ArchiveGuard:
    def __init__(self, evidence_path: Path, override_path: Path):
        self.evidence_path = evidence_path
        self.overrides = OverrideStore(override_path)

    def _source(self, identity):
        source = _json_object_from_path(self.evidence_path, "current archive guard evidence")
        validate(source)
        if source.get("schema") not in ("uma.archive_guard.v1", "uma.archive_guard.v2"):
            raise ValueError("versioned archive guard evidence required")
        now = datetime.now(timezone.utc)
        observed = datetime.fromisoformat(source["observed_at"])
        if observed.tzinfo is None or not timedelta(0) <= now - observed <= timedelta(hours=24):
            raise ValueError("archive guard evidence is stale")
        bindings = [binding for binding in source["bindings"] if binding["identity"] == identity]
        if len(bindings) != 1:
            raise ValueError("exact native-to-Mail.app override binding required")
        binding = bindings[0]
        if source["schema"] == "uma.archive_guard.v2":
            from core.native_mailapp_binding import validate_binding
            current = _json_object_from_path(Path(binding["path"]), "current native Mail.app binding")
            if current["content_hash"] != binding["sha256"] or current["identity"] != identity:
                raise ValueError("archive native binding changed")
            ref = validate_binding(current, now=now)
            if ref.ref_digest != binding["ref_digest"]:
                raise ValueError("archive override binding changed")
            reviewed = []
            for receipt in source["review_sources"]:
                review = _json_object_from_path(Path(receipt["path"]), "current correspondence review")
                validate(review)
                if review["content_hash"] != receipt["sha256"]:
                    raise ValueError("archive correspondence review changed; replan required")
                reviewed.extend(review["obligations"])
            if reviewed != source["obligations"]:
                raise ValueError("archive guard must preserve all reviewed sibling obligations")
        return source, binding

    def __call__(self, identity):
        source, binding = self._source(identity)
        now = datetime.now(timezone.utc)
        key = MessageIdentity.model_validate(identity).key
        ref_digest = binding["ref_digest"]
        require_hex256(ref_digest, "archive guard ref digest")
        obligations = [Obligation.model_validate(o) for o in source["obligations"]]
        rows = reconcile(obligations, now=now, coverage_gaps=source.get("coverage_gaps", []))["obligations"]
        siblings = [row for row in rows if any(MessageIdentity.model_validate(m).key == key for m in row["messages"])]
        if not siblings:
            raise ValueError("archive guard has no researched obligations for identity")
        return {"protected": any(row["protected"] or not row["archive_eligible"] for row in siblings),
                "human_override": any(row["human_override"] for row in siblings) or self.overrides.is_suppressed(ref_digest)}

    def content_evidence(self, identity):
        from core.native_mailapp_binding import content_evidence
        source, binding = self._source(identity)
        if source["schema"] != "uma.archive_guard.v2":
            raise ValueError("archive requires a validated full-content native binding")
        current = _json_object_from_path(Path(binding["path"]), "native content binding")
        return content_evidence(current)


def build_guard_evidence(*, binding_paths, review_paths, now=None):
    """Retain complete reviewed threads and live-revalidated native override bindings."""
    from core.native_mailapp_binding import validate_binding
    now = now or datetime.now(timezone.utc)
    bindings, reviews, obligations = [], [], []
    for path in review_paths:
        review = _json_object_from_path(Path(path), "correspondence review")
        validate(review)
        if review.get("schema") != "uma.correspondence_review.v1" or review.get("authority") != "review_only":
            raise ValueError("recorded correspondence review required")
        obligations.extend(review["obligations"])
        reviews.append({"path": str(Path(path).resolve()), "sha256": review["content_hash"]})
    if not obligations or len({o["id"] for o in obligations}) != len(obligations):
        raise ValueError("unique complete reviewed obligations required")
    for path in binding_paths:
        binding = _json_object_from_path(Path(path), "native Mail.app binding")
        ref = validate_binding(binding, now=now)
        key = MessageIdentity.model_validate(binding["identity"]).key
        if not any(key in {MessageIdentity.model_validate(m).key for m in o["messages"]} for o in obligations):
            raise ValueError("binding has no reviewed obligation")
        bindings.append({"identity": binding["identity"], "ref_digest": ref.ref_digest,
                         "path": str(Path(path).resolve()), "sha256": binding["content_hash"]})
    if not bindings or len({sha256_hex(b["identity"]) for b in bindings}) != len(bindings):
        raise ValueError("unique exact native bindings required")
    return seal({"schema": "uma.archive_guard.v2", "observed_at": now.isoformat(),
                 "bindings": bindings, "obligations": obligations, "review_sources": reviews,
                 "coverage_gaps": [], "writes_performed": 0})
