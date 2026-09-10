"""Bounded live observation into private artifacts; never derives authority."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

from core.flag_workflow import _atomic_write_private_json, sha256_hex
from core.models import MessageReference
from core.obligation_workflow import MessageIdentity, Obligation, Question, reconcile


def refresh(provider, *, accounts: list[str], output: Path, limit: int = 500) -> dict:
    if not accounts or len(set(accounts)) != len(accounts):
        raise ValueError("explicit unique account scope is required")
    if not 1 <= limit <= 500:
        raise ValueError("observation limit must be 1–500 per surface")
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    observations, reports, gaps = {}, [], []
    artifact = {"schema": "uma.obligation_observation.v1", "generated_at": now.isoformat(),
                "accounts": accounts, "surfaces": reports, "messages": [], "coverage_gaps": gaps,
                "writes_performed": 0}

    def persist():
        artifact["messages"] = list(observations.values())
        _atomic_write_private_json(output, artifact, prefix=".tmp-observe-")

    persist()  # Fail before provider reads if private storage is unavailable.
    try:
        surfaces = provider.discover_surfaces(timeout_seconds=30)
    except RuntimeError:
        gaps.append("surface_discovery_unavailable")
        persist()
        return artifact
    # Inbox first, then every flagged surface. Do not hide failed folders.
    scoped = [s for s in surfaces if s.account in accounts]
    found_accounts = {s.account for s in scoped}
    gaps.extend("account_unavailable:" + a for a in accounts if a not in found_accounts)
    targets = [(s, False) for s in scoped if s.mailbox.upper() == "INBOX"]
    targets.extend((s, True) for s in scoped)
    for surface, flagged_only in targets:
        key = sha256_hex({"account": surface.account, "mailbox": surface.mailbox, "flagged_only": flagged_only})
        if time.monotonic() - started >= 590:
            reports.append({"surface_id": key, "status": "unattempted", "returned": 0})
            gaps.append("run_ceiling:" + key)
            continue
        result = provider.enumerate_flagged(account=surface.account, mailbox=surface.mailbox,
            mailbox_path=surface.path_components, flagged_only=flagged_only,
            limit=limit, timeout_seconds=30)
        reports.append({"surface_id": key, "account": surface.account, "mailbox": surface.mailbox,
                        "flagged_only": flagged_only, "status": result.status,
                        "complete": result.complete, "returned": len(result.rows),
                        "boundary": result.scanned_boundary})
        if not result.complete:
            gaps.append("surface_incomplete:" + key)
        for row in result.rows:
            message = asdict(row)
            message["flag_color"] = row.flag_color.value
            ref = MessageReference(provider="mailapp", account=row.account, mailbox=row.mailbox,
                provider_id=row.provider_id, observed_native_flag=row.native_index).with_resolved_evidence(
                    row.received_iso, row.sender, row.subject)
            message["reference"] = ref.__dict__
            observations[sha256_hex({"account": row.account, "id": row.provider_id})] = message
        persist()
    gaps.append("thread_and_sent_evidence_requires_bounded_research")
    persist()
    return artifact


def shadow_from_observation(observation: dict) -> dict:
    """One provisional research record per scoped message; no subject grouping."""
    now = datetime.now(timezone.utc)
    obligations = []
    for row in observation["messages"]:
        ref = row["reference"]
        oid = "observed-" + sha256_hex({"account": ref["account"], "id": ref["provider_id"]})
        msg = MessageIdentity(provider="mailapp", account=ref["account"],
            message_id=ref["provider_id"], evidence_digest=ref["evidence_digest"])
        obligations.append(Obligation(id=oid, title=row["subject"] or "Untitled message",
            thread_id=oid, messages=(msg,), questions=(Question(reason="missing_evidence",
                question="Which independent obligations remain after the relevant thread and Sent correspondence?",
                resolver="bounded_thread_and_sent_read", checkpoint=now + timedelta(days=1)),)))
    return reconcile(obligations, now=now, coverage_gaps=observation["coverage_gaps"])


def cmd_refresh(args):
    import json
    from providers.mailapp import MailAppProvider
    if getattr(args, "provider", None):
        from core.inventory_cli import cmd_inventory
        if len(args.account) != 1:
            print(json.dumps({"status": "blocked", "error": "native inventory manifests require one explicit account"}))
            return 2
        args.account = args.account[0]
        args.page_size = args.limit
        return cmd_inventory(args)
    try:
        if not args.shadow:
            raise ValueError("--shadow is required for legacy Mail.app observations")
        with MailAppProvider() as provider:
            artifact = refresh(provider, accounts=args.account, output=Path(args.output).expanduser(), limit=args.limit)
        shadow = shadow_from_observation(artifact)
        _atomic_write_private_json(Path(args.shadow).expanduser(), shadow, prefix=".tmp-shadow-")
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}))
        return 2
    print(json.dumps({"status": "partial" if artifact["coverage_gaps"] else "complete",
                      "messages": len(artifact["messages"]), "surfaces": len(artifact["surfaces"]),
                      "coverage_gaps": len(artifact["coverage_gaps"]), "writes_performed": 0}))
    return 20 if artifact["coverage_gaps"] else 0
