"""Compatibility intake and bounded dispatch for the single mail workflow.

Legacy classifications are observations, never permission to clear a flag or
archive. Approved work goes through the same transaction CLI used interactively.
This module cannot send mail and never constructs a send command.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import subprocess
import sys

from core.flag_transactions import AdvisoryFileLock
from core.flag_workflow import _atomic_write_private_json, _json_object_from_path, sha256_hex
from core.mail_inventory import seal, validate


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = ROOT / "audit" / "evidence-workflow"
SYSTEM_LABELS = frozenset({"INBOX", "TRASH", "SPAM", "STARRED", "IMPORTANT", "UNREAD",
                           "SENT", "DRAFT", "CHAT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL",
                           "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS"})


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def intake(*, source: str, account: str, mailbox: str, rows: list[dict],
           provider: str = "mailapp", root: Path = EVIDENCE_ROOT) -> dict:
    """Retain exact legacy provenance for native binding and correspondence review.

    This is an immutable observation source inside the existing evidence store,
    not an alternative obligations ledger. Repeated identical reads deduplicate.
    """
    if not source or not account or not mailbox or not isinstance(rows, list):
        raise ValueError("intake requires source, exact account, mailbox and rows")
    payload = {"schema": "uma.legacy_intake.v1", "source": source, "account": account,
               "provider": provider, "mailbox": mailbox, "rows": rows,
               "authority": "observation_only", "mailbox_writes": 0,
               "questions": ["Bind every source identity to the native corpus before planning",
                             "Review complete correspondence and current service evidence"]}
    seal(payload)
    path = root / "intake" / "observations" / (payload["content_hash"] + ".json")
    with AdvisoryFileLock(root / "intake" / "intake.lock"):
        if not path.exists():
            _atomic_write_private_json(path, payload, prefix=".tmp-intake-")
        index_path = root / "intake" / "manifest.json"
        index = (_json_object_from_path(index_path, "intake manifest") if index_path.exists()
                 else seal({"schema": "uma.legacy_intake_manifest.v1", "sources": {}}))
        validate(index)
        key = sha256_hex([source, provider, account, mailbox])
        entry = index["sources"].setdefault(key, {"receipts": []})
        if payload["content_hash"] not in entry["receipts"]:
            entry["receipts"].append(payload["content_hash"])
        entry.update(latest=payload["content_hash"], observed_at=now_iso(),
                     source=source, provider=provider, account=account, mailbox=mailbox)
        _atomic_write_private_json(index_path, seal(index), prefix=".tmp-intake-")
    return {"status": "research_required", "authority": "observation_only",
            "intake_sha256": payload["content_hash"], "intake_path": str(path),
            "queued": len(rows), "flagged": 0, "unflagged": 0, "archived": 0,
            "unstarred": 0, "mailbox_writes": 0, "errors": 0}


def category_labels(add: list[str], remove: list[str]) -> tuple[list[str], list[str]]:
    """Category operations cannot alter provider workflow/system labels."""
    if any(not isinstance(label, str) or not label or label in SYSTEM_LABELS
           or label.startswith("CATEGORY_") for label in add + remove):
        raise ValueError("categorization cannot change system, Inbox, Junk, or workflow labels")
    if set(add) & set(remove):
        raise ValueError("cannot add and remove the same category")
    return sorted(set(add)), sorted(set(remove))


def category_plan(service, account: str, changes: list[dict]) -> dict:
    """Fresh Gmail category preview; existing labels only, no mailbox writes."""
    profile = service.users().getProfile(userId="me").execute()
    if profile["emailAddress"].casefold() != account.casefold():
        raise ValueError("authenticated Gmail account mismatch")
    labels = service.users().labels().list(userId="me").execute()["labels"]
    user_labels = {label["name"]: label["id"] for label in labels if label.get("type") == "user"}
    mutations = []
    for change in changes:
        add, remove = category_labels(change["add"], change.get("remove", []))
        missing = (set(add) | set(remove)) - user_labels.keys()
        if missing:
            raise ValueError("category labels require a separately reviewed label-creation operation")
        message = service.users().messages().get(userId="me", id=change["id"], format="minimal").execute()
        if message["id"] != change["id"]:
            raise ValueError("Gmail category message identity mismatch")
        mutation = {"id": message["id"], "before": sorted(message["labelIds"]),
                    "add": sorted(user_labels[name] for name in add),
                    "remove": sorted(user_labels[name] for name in remove),
                    "category_names": {"add": add, "remove": remove}}
        mutations.append(mutation)
    if not 1 <= len(mutations) <= 25 or len({m["id"] for m in mutations}) != len(mutations):
        raise ValueError("category plans require 1–25 distinct exact messages")
    return seal({"schema": "uma.category_plan.v1", "provider": "gmail", "account": account,
                 "created_at": now_iso(), "mutations": mutations,
                 "concurrency": {"local_writer_lock": True, "server_revision_precondition": "none"}})


def apply_categories(service, plan: dict, approval: dict, *, root: Path = EVIDENCE_ROOT) -> dict:
    """One-shot minimal category changes with approval, fresh state and readback."""
    validate(plan)
    current_time = datetime.now(timezone.utc)
    created = datetime.fromisoformat(plan["created_at"])
    expires = datetime.fromisoformat(approval["expires_at"])
    if (plan.get("schema") != "uma.category_plan.v1" or created.tzinfo is None
            or not timedelta(0) <= current_time - created <= timedelta(hours=24)
            or expires.tzinfo is None or not current_time < expires <= created + timedelta(hours=24)
            or approval.get("schema") != "uma.category_approval.v1"
            or approval.get("plan_sha256") != plan["content_hash"]
            or approval.get("authority") != "explicit_operator_selection"
            or not approval.get("selection_receipt")):
        raise ValueError("fresh exact category approval required")
    ids = approval.get("selected_ids", [])
    if not 1 <= len(ids) <= 25 or len(ids) != len(set(ids)):
        raise ValueError("category selection requires 1–25 distinct messages")
    by_id = {m["id"]: m for m in plan["mutations"]}
    if len(by_id) != len(plan["mutations"]) or not set(ids) <= by_id.keys():
        raise ValueError("category selection lineage mismatch")
    account = service.users().getProfile(userId="me").execute()["emailAddress"]
    if account.casefold() != plan["account"].casefold():
        raise ValueError("authenticated Gmail account mismatch")
    labels = service.users().labels().list(userId="me").execute()["labels"]
    user_labels = {label["name"]: label["id"] for label in labels if label.get("type") == "user"}
    state = root / "transactions" / "categories"
    path = state / (plan["content_hash"] + ".json")
    with AdvisoryFileLock(state / "writer.lock"):
        if path.exists():
            raise ValueError("category transaction already dispatched; reconcile without retrying a write")
        for mid in ids:
            mutation = by_id[mid]
            names = mutation["category_names"]
            add, remove = category_labels(names["add"], names["remove"])
            if (mutation["add"] != sorted(user_labels[name] for name in add)
                    or mutation["remove"] != sorted(user_labels[name] for name in remove)):
                raise ValueError("category label identity changed")
            observed = service.users().messages().get(userId="me", id=mid, format="minimal").execute()
            if observed["id"] != mid or sorted(observed["labelIds"]) != mutation["before"]:
                raise ValueError("category preflight conflict")
        receipt = {"schema": "uma.category_receipt.v1", "plan_sha256": plan["content_hash"],
                   "approval_sha256": sha256_hex(approval), "status": "prepared", "results": []}
        for mid in ids:
            mutation = by_id[mid]
            observed = service.users().messages().get(userId="me", id=mid, format="minimal").execute()
            if observed["id"] != mid or sorted(observed["labelIds"]) != mutation["before"]:
                receipt["status"] = "conflicted"
                break
            row = {"id": mid, "status": "intent"}
            receipt["results"].append(row)
            _atomic_write_private_json(path, seal(receipt), prefix=".tmp-category-")
            try:
                service.users().messages().modify(userId="me", id=mid,
                    body={"addLabelIds": mutation["add"], "removeLabelIds": mutation["remove"]}).execute(num_retries=0)
                after = service.users().messages().get(userId="me", id=mid, format="minimal").execute()
                expected = (set(mutation["before"]) | set(mutation["add"])) - set(mutation["remove"])
                row.update(status="verified" if after["id"] == mid and set(after["labelIds"]) == expected
                           else "conflicted", after=sorted(after["labelIds"]))
            except Exception:
                row["status"] = "uncertain"
            receipt["status"] = row["status"]
            _atomic_write_private_json(path, seal(receipt), prefix=".tmp-category-")
            if row["status"] != "verified":
                break
        _atomic_write_private_json(path, seal(receipt), prefix=".tmp-category-")
        return receipt


def _bound_artifact(entry: dict, name: str) -> Path:
    artifact = entry[name]
    if set(artifact) != {"path", "sha256"}:
        raise ValueError("dispatch artifact needs exact path and SHA-256")
    path = Path(artifact["path"]).expanduser().resolve(strict=True)
    value = _json_object_from_path(path, "dispatch " + name)
    if sha256_hex(value) != artifact["sha256"]:
        raise ValueError("dispatch artifact changed: " + name)
    return path


def dispatch_command(entry: dict) -> list[str]:
    """Allowlisted CLI grammar; stored mail text can never become executable code."""
    operation, account = entry["operation"], entry["account"]
    if not account or operation not in ("archive", "flags"):
        raise ValueError("unsupported approved maintenance operation")
    args = [sys.executable, str(ROOT / "cli.py")]
    if operation == "archive":
        if entry["provider"] not in ("gmail", "icloud", "outlook"):
            raise ValueError("unsupported archive provider")
        args += ["archive-workflow", "apply", "--provider", entry["provider"], "--account", account]
        fields = {"plan": "--input", "approval": "--approval", "guard_evidence": "--guard-evidence"}
        args += ["--output", str(Path(entry["receipt_output"]).expanduser())]
    else:
        args += ["flags", "apply", "--provider", "mailapp", "--account", account,
                 "--canary", "--human-selected"]
        fields = {"plan": "--plan", "snapshot": "--snapshot", "approval": "--approval"}
        args += ["--receipt-output", str(Path(entry["receipt_output"]).expanduser())]
    for name, flag in fields.items():
        args += [flag, str(_bound_artifact(entry, name))]
    if entry.get("state_dir"):
        args += ["--state-dir", str(Path(entry["state_dir"]).expanduser())]
    if operation == "archive" and entry.get("op_item"):
        args += ["--op-item", entry["op_item"], "--op-vault", entry.get("op_vault", "Personal")]
    return args


def dispatch_approved(path: Path, *, root: Path = EVIDENCE_ROOT, runner=subprocess.run) -> dict:
    """Dispatch one bounded approved plan. A lost child receipt freezes this envelope."""
    entry = _json_object_from_path(path, "maintenance dispatch")
    validate(entry)
    if entry.get("schema") != "uma.maintenance_dispatch.v1":
        raise ValueError("unsupported maintenance dispatch schema")
    command = dispatch_command(entry)
    plan = _json_object_from_path(_bound_artifact(entry, "plan"), "plan")
    if not 1 <= len(plan.get("mutations", [])) <= 25:
        raise ValueError("maintenance requires a nonempty plan bounded to 25 mutations")
    state = root / "transactions" / "maintenance"
    receipt_path = state / (entry["content_hash"] + ".json")
    with AdvisoryFileLock(state / "writer.lock"):
        if receipt_path.exists():
            previous = _json_object_from_path(receipt_path, "maintenance receipt")
            validate(previous)
            return {**previous, "replayed": False, "next_action": "canonical verification or reconciliation"}
        receipt = {"schema": "uma.maintenance_receipt.v1", "dispatch_sha256": entry["content_hash"],
                   "status": "intent", "started_at": now_iso(), "sends": 0}
        _atomic_write_private_json(receipt_path, seal(receipt), prefix=".tmp-maintenance-")
        try:
            result = runner(command, cwd=ROOT, capture_output=True, text=True, timeout=590, check=False)
            receipt.update(status="completed" if result.returncode == 0 else "needs_reconciliation",
                           returncode=result.returncode)
            output = Path(entry["receipt_output"]).expanduser()
            if output.exists():
                evidence = _json_object_from_path(output, "canonical transaction receipt")
                receipt.update(canonical_receipt_sha256=sha256_hex(evidence),
                               canonical_status=evidence.get("status", "inspect_receipt"))
            else:
                receipt["status"] = "needs_reconciliation"
        except (subprocess.TimeoutExpired, OSError, ValueError):
            receipt["status"] = "uncertain"
        _atomic_write_private_json(receipt_path, seal(receipt), prefix=".tmp-maintenance-")
        return receipt


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Canonical mail intake and approved transaction dispatch")
    parser.add_argument("operation", choices=("intake", "dispatch", "status", "category-plan", "category-apply"))
    parser.add_argument("--input")
    parser.add_argument("--approval")
    parser.add_argument("--output")
    parser.add_argument("--root", type=Path, default=EVIDENCE_ROOT)
    parser.add_argument("--source", default="mail-triage")
    parser.add_argument("--account")
    parser.add_argument("--mailbox", default="INBOX")
    parser.add_argument("--provider", default="mailapp")
    args = parser.parse_args(argv)
    try:
        if args.operation == "dispatch":
            result = dispatch_approved(Path(args.input), root=args.root)
        elif args.operation == "intake":
            value = _json_object_from_path(Path(args.input), "legacy observation")
            result = intake(source=args.source, account=args.account, mailbox=args.mailbox,
                            provider=args.provider, rows=value["rows"], root=args.root)
        elif args.operation.startswith("category-"):
            import gmail_auth
            service = gmail_auth.build_gmail_service()
            value = _json_object_from_path(Path(args.input), "category input")
            result = (category_plan(service, args.account, value["changes"]) if args.operation == "category-plan"
                      else apply_categories(service, value,
                           _json_object_from_path(Path(args.approval), "category approval"), root=args.root))
        else:
            path = args.root / "intake" / "manifest.json"
            value = _json_object_from_path(path, "intake manifest") if path.exists() else {"sources": {}}
            if path.exists():
                validate(value)
            result = {"schema": "uma.maintenance_status.v1", "source_count": len(value["sources"]),
                      "observation_receipts": sum(len(row["receipts"]) for row in value["sources"].values()),
                      "authority": "observation_only", "sending": "human_click_in_mailapp",
                      "research_complete": False, "rollout_complete": False}
        if args.output:
            _atomic_write_private_json(Path(args.output), result, prefix=".tmp-maintenance-")
        print(json.dumps(result))
        return 2 if result.get("status") in ("uncertain", "needs_reconciliation") else 0
    except (KeyError, TypeError, ValueError, OSError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc), "sends": 0}))
        return 2
