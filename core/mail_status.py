"""Unified redacted mail status and historical terminal crosswalk."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from core.historical_intelligence import (
    DEFAULT_STALE_DAYS,
    HistoricalIntelligenceError,
    build_historical_intelligence,
)
from core.mail_action_ledger import MailActionLedgerError, build_action_ledger
from core.mail_action_plan import MailActionPlanError, build_action_plan
from core.mail_resolver_plan import MailResolverPlanError, build_resolver_plan
from core.mail_resolver_receipt import MailResolverReceiptError, build_resolver_ledger
from core.ops_summary import OpsReportError, build_ops_snapshot

MAIL_STATUS_SCHEMA = "uma.mail.status.v1"
MAIL_HISTORICAL_CROSSWALK_SCHEMA = "uma.mail.historical_crosswalk.v1"

TERMINAL_STATUSES = (
    "resolved",
    "represented_in_ops",
    "stale_noop",
    "open",
    "blocked",
    "needs_human",
)

PROCESSING_STATES = (
    "read_only_seen",
    "classified",
    "queued",
    "mutated",
    "drafted",
    "sent",
    "resolved",
    "blocked",
)

_PRIVATE_FIELDS = ["sender", "address", "subject", "body", "snippet", "raw_headers", "full_source_path"]


class MailStatusError(ValueError):
    """Raised when the unified status or crosswalk cannot be built."""

    def __init__(self, detail: str, *, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _format_dt(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _checked_at(now: Optional[datetime] = None) -> datetime:
    checked = now or datetime.now(timezone.utc)
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return checked.astimezone(timezone.utc)


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _read_json(path: Union[Path, str], *, label: str) -> Dict[str, Any]:
    target = Path(path).expanduser()
    if not target.is_file():
        raise MailStatusError(f"{label} input not found", status_code=404)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise MailStatusError(f"{label} input is not valid JSON") from e
    except OSError as e:
        raise MailStatusError(f"{label} input could not be read: {e}") from e
    if not isinstance(data, dict):
        raise MailStatusError(f"{label} input has invalid shape")
    return data


def _read_jsonl(path: Optional[Union[Path, str]], *, expected_schema: Optional[str] = None) -> List[Dict[str, Any]]:
    if path is None:
        return []
    target = Path(path).expanduser()
    if not target.exists():
        return []
    if not target.is_file():
        raise MailStatusError(f"{target.name} ledger path is not a file")
    rows: List[Dict[str, Any]] = []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise MailStatusError(f"{target.name} ledger could not be read: {e}") from e
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise MailStatusError(f"{target.name} ledger line {index} is not valid JSON") from e
        if not isinstance(row, dict):
            raise MailStatusError(f"{target.name} ledger line {index} has invalid shape")
        if expected_schema and row.get("schema") != expected_schema:
            raise MailStatusError(f"{target.name} ledger line {index} must be {expected_schema}")
        rows.append(row)
    return rows


def _history_rows(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = data.get("messages")
    if raw is None:
        raw = data.get("records")
    if not isinstance(raw, list):
        raise MailStatusError("historical mail input requires a messages array")
    return [row for row in raw if isinstance(row, dict)]


def _source_ref(path: Optional[Union[Path, str]]) -> Dict[str, Any]:
    if path is None:
        return {"supplied": False, "filename": None}
    target = Path(path).expanduser()
    return {
        "supplied": True,
        "filename": target.name,
        "exists": target.exists(),
        "stored_full_path": False,
    }


def _finding_index(intelligence: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    detail_by_id: Dict[str, Dict[str, Any]] = {}
    for collection in ("opportunities", "risks"):
        for item in intelligence.get(collection) or []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                detail_by_id[item["id"]] = item

    finding_by_evidence: Dict[str, Dict[str, Any]] = {}
    for finding in (intelligence.get("reconciliation") or {}).get("findings") or []:
        if not isinstance(finding, dict):
            continue
        detail = detail_by_id.get(str(finding.get("id"))) or {}
        merged = {**detail, **finding}
        for evidence_id in finding.get("evidence_ids") or detail.get("evidence_ids") or []:
            if isinstance(evidence_id, str):
                finding_by_evidence[evidence_id] = merged
    return finding_by_evidence, detail_by_id


def _terminal_from_finding(finding: Dict[str, Any]) -> str:
    ops_status = str(finding.get("ops_lane_status") or "")
    if ops_status == "represented_in_ops":
        return "represented_in_ops"

    kind = str(finding.get("kind") or "")
    status = str(finding.get("status") or "")
    recommended_lane = str(finding.get("recommended_lane") or "")
    approval_statuses = {"needs_human_review", "decision_needed"}
    if status in approval_statuses or kind == "legal_obligation" or recommended_lane == "draft_review":
        return "needs_human"
    if status == "needs_portal_verification" or kind in {
        "security_or_account",
        "provider_incident",
        "payment_or_billing",
    }:
        return "blocked"
    return "open"


def _terminal_from_evidence(evidence: Dict[str, Any]) -> str:
    labels = " ".join(str(label).lower() for label in evidence.get("mail_triage_labels") or [])
    direction = str(evidence.get("direction") or "").lower()
    scope = str(evidence.get("scope") or "").lower()
    if direction == "outbound" or " sent" in f" {labels}" or "legal sent" in labels:
        return "resolved"
    if any(token in labels for token in ("closed", "reviewed", "newsletter", "noise", "superseded")):
        return "stale_noop"
    if scope in {"junk", "spam", "trash", "deleted"}:
        return "stale_noop"
    return "stale_noop"


def _processing_state(terminal_status: str, evidence: Dict[str, Any], finding: Optional[Dict[str, Any]]) -> str:
    if terminal_status in {"resolved", "stale_noop"}:
        return "resolved"
    if terminal_status == "blocked":
        return "blocked"
    if terminal_status in {"represented_in_ops", "open", "needs_human"}:
        return "queued" if finding else "classified"
    if evidence.get("signals"):
        return "classified"
    return "read_only_seen"


def _apply_status(
    current: Dict[str, Any],
    *,
    terminal_status: str,
    processing_state: str,
    proof: Dict[str, Any],
) -> None:
    rank = {"resolved": 5, "stale_noop": 4, "represented_in_ops": 3, "needs_human": 2, "blocked": 1, "open": 0}
    current_rank = rank.get(str(current.get("terminal_status")), 0)
    incoming_rank = rank.get(terminal_status, 0)
    if incoming_rank >= current_rank or terminal_status in {"blocked", "needs_human"}:
        current["terminal_status"] = terminal_status
        current["processing_state"] = processing_state
        current.setdefault("proof", []).append(proof)


def _map_action_ledger(items: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    mapped: Dict[str, List[Dict[str, Any]]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("action_status") or "open")
        if status == "resolved":
            terminal, state = "resolved", "resolved"
        elif status == "ignored":
            terminal, state = "stale_noop", "resolved"
        elif status == "blocked":
            terminal, state = "blocked", "blocked"
        elif item.get("reason_code") == "needs_human":
            terminal, state = "needs_human", "queued"
        else:
            terminal, state = "open", "queued"
        proof = {
            "ledger": "action",
            "action_id": item.get("action_id"),
            "status": status,
            "last_receipt_id": item.get("last_receipt_id"),
        }
        for evidence_id in item.get("sample_evidence_ids") or []:
            if isinstance(evidence_id, str):
                mapped.setdefault(evidence_id, []).append(
                    {"terminal_status": terminal, "processing_state": state, "proof": proof}
                )
    return mapped


def _map_resolver_ledger(items: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    mapped: Dict[str, List[Dict[str, Any]]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("resolver_status") or "not_started")
        if status == "verified_resolved":
            terminal, state = "resolved", "resolved"
        elif status == "not_applicable":
            terminal, state = "stale_noop", "resolved"
        elif status == "verified_blocked":
            terminal, state = "blocked", "blocked"
        else:
            terminal, state = "open", "queued"
        proof = {
            "ledger": "resolver",
            "action_id": item.get("action_id"),
            "status": status,
            "last_receipt_id": item.get("last_receipt_id"),
        }
        for evidence_id in item.get("sample_evidence_ids") or []:
            if isinstance(evidence_id, str):
                mapped.setdefault(evidence_id, []).append(
                    {"terminal_status": terminal, "processing_state": state, "proof": proof}
                )
    return mapped


def _map_receipt_ledgers(
    *,
    draft_approval_path: Optional[Union[Path, str]],
    delivery_path: Optional[Union[Path, str]],
) -> Dict[str, List[Dict[str, Any]]]:
    mapped: Dict[str, List[Dict[str, Any]]] = {}
    draft_rows = _read_jsonl(draft_approval_path, expected_schema="uma.mail.draft_approval_receipt.v1")
    for row in draft_rows:
        evidence_id = row.get("evidence_id")
        if not isinstance(evidence_id, str):
            continue
        decision = str(row.get("decision") or "pending")
        if decision == "rejected":
            terminal, state = "stale_noop", "resolved"
        elif decision == "approved":
            terminal, state = "open", "drafted"
        else:
            terminal, state = "needs_human", "drafted"
        mapped.setdefault(evidence_id, []).append(
            {
                "terminal_status": terminal,
                "processing_state": state,
                "proof": {
                    "ledger": "draft_approval",
                    "draft_id": row.get("draft_id"),
                    "status": decision,
                    "receipt_id": row.get("receipt_id"),
                },
            }
        )

    delivery_rows = _read_jsonl(delivery_path, expected_schema="uma.mail.delivery_receipt.v1")
    for row in delivery_rows:
        evidence_id = row.get("evidence_id")
        if not isinstance(evidence_id, str):
            continue
        status = str(row.get("delivery_status") or "")
        if status == "sent_recorded":
            terminal, state = "resolved", "sent"
        elif status == "blocked":
            terminal, state = "blocked", "blocked"
        elif status in {"provider_draft_recorded", "provider_draft_requested"}:
            terminal, state = "open", "drafted"
        else:
            terminal, state = "open", "queued"
        mapped.setdefault(evidence_id, []).append(
            {
                "terminal_status": terminal,
                "processing_state": state,
                "proof": {
                    "ledger": "delivery",
                    "draft_id": row.get("draft_id"),
                    "status": status,
                    "receipt_id": row.get("receipt_id"),
                },
            }
        )
    return mapped


def _merge_maps(*maps: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    merged: Dict[str, List[Dict[str, Any]]] = {}
    for source in maps:
        for evidence_id, rows in source.items():
            merged.setdefault(evidence_id, []).extend(rows)
    return merged


def _build_ledgers(
    intelligence: Dict[str, Any],
    *,
    action_ledger_path: Optional[Union[Path, str]],
    resolver_ledger_path: Optional[Union[Path, str]],
    max_items: int,
    now: Optional[datetime],
) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    blockers: List[Dict[str, Any]] = []
    ledgers: Dict[str, Any] = {
        "action": {"supplied": action_ledger_path is not None, "available": False},
        "resolver": {"supplied": resolver_ledger_path is not None, "available": False},
    }
    action_map: Dict[str, List[Dict[str, Any]]] = {}
    resolver_map: Dict[str, List[Dict[str, Any]]] = {}
    try:
        action_plan = build_action_plan(intelligence, max_items=max_items, now=now)
        action_ledger = build_action_ledger(
            action_plan,
            receipt_path=Path(action_ledger_path).expanduser() if action_ledger_path else None,
            max_items=max_items,
            now=now,
        )
        ledgers["action"] = {
            "supplied": action_ledger_path is not None,
            "available": True,
            "schema": action_ledger.get("schema"),
            "kpis": action_ledger.get("kpis"),
        }
        action_map = _map_action_ledger(action_ledger.get("items") or [])

        resolver_plan = build_resolver_plan(action_plan, max_items=max_items, now=now)
        resolver_ledger = build_resolver_ledger(
            resolver_plan,
            receipt_path=Path(resolver_ledger_path).expanduser() if resolver_ledger_path else None,
            max_items=max_items,
            now=now,
        )
        ledgers["resolver"] = {
            "supplied": resolver_ledger_path is not None,
            "available": True,
            "schema": resolver_ledger.get("schema"),
            "kpis": resolver_ledger.get("kpis"),
            "invariant_rollup": resolver_ledger.get("invariant_rollup"),
        }
        resolver_map = _map_resolver_ledger(resolver_ledger.get("items") or [])
    except (MailActionPlanError, MailActionLedgerError, MailResolverPlanError, MailResolverReceiptError) as e:
        detail = getattr(e, "detail", str(e))
        blockers.append({"surface": "mail_ledgers", "status": "blocked", "detail": detail})
    return ledgers, _merge_maps(action_map, resolver_map), blockers


def build_historical_crosswalk(
    history_path: Union[Path, str],
    *,
    ops_report_path: Optional[Union[Path, str]] = None,
    action_ledger_path: Optional[Union[Path, str]] = None,
    resolver_ledger_path: Optional[Union[Path, str]] = None,
    draft_approval_path: Optional[Union[Path, str]] = None,
    delivery_path: Optional[Union[Path, str]] = None,
    stale_days: int = DEFAULT_STALE_DAYS,
    max_items: int = 100,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Assign every historical mail evidence item a terminal status."""
    checked_at = _checked_at(now)
    history = _read_json(history_path, label="historical mail")
    rows = _history_rows(history)
    try:
        intelligence = build_historical_intelligence(
            history_path,
            ops_report_path=ops_report_path,
            stale_days=stale_days,
            now=checked_at,
        )
    except HistoricalIntelligenceError as e:
        raise MailStatusError(e.detail, status_code=e.status_code) from e

    finding_by_evidence, _details = _finding_index(intelligence)
    ledgers, ledger_map, blockers = _build_ledgers(
        intelligence,
        action_ledger_path=action_ledger_path,
        resolver_ledger_path=resolver_ledger_path,
        max_items=max_items,
        now=checked_at,
    )
    receipt_map = _map_receipt_ledgers(
        draft_approval_path=draft_approval_path,
        delivery_path=delivery_path,
    )
    proof_map = _merge_maps(ledger_map, receipt_map)

    items: List[Dict[str, Any]] = []
    for evidence in intelligence.get("evidence") or []:
        if not isinstance(evidence, dict) or not isinstance(evidence.get("id"), str):
            continue
        evidence_id = evidence["id"]
        finding = finding_by_evidence.get(evidence_id)
        terminal = _terminal_from_finding(finding) if finding else _terminal_from_evidence(evidence)
        item = {
            "schema": "uma.mail.historical_crosswalk_item.v1",
            "evidence_id": evidence_id,
            "terminal_status": terminal,
            "processing_state": _processing_state(terminal, evidence, finding),
            "finding_id": finding.get("id") if finding else None,
            "kind": finding.get("kind") if finding else None,
            "recommended_lane": finding.get("recommended_lane") if finding else None,
            "ops_lane_status": finding.get("ops_lane_status") if finding else None,
            "occurred_at": evidence.get("occurred_at"),
            "direction": evidence.get("direction"),
            "scope": evidence.get("scope"),
            "state": evidence.get("state"),
            "signals": evidence.get("signals") or [],
            "provider_hints": evidence.get("provider_hints") or [],
            "proof": [],
        }
        for proof in proof_map.get(evidence_id) or []:
            _apply_status(
                item,
                terminal_status=proof["terminal_status"],
                processing_state=proof["processing_state"],
                proof=proof["proof"],
            )
        items.append(item)

    terminal_counts = Counter(str(item["terminal_status"]) for item in items)
    processing_counts = Counter(str(item["processing_state"]) for item in items)
    for status in TERMINAL_STATUSES:
        terminal_counts.setdefault(status, 0)
    for state in PROCESSING_STATES:
        processing_counts.setdefault(state, 0)
    terminal_total = sum(terminal_counts.values())
    exclusions: List[Dict[str, Any]] = []
    reconciled = len(rows) == terminal_total + len(exclusions)
    if not reconciled:
        blockers.append(
            {
                "surface": "historical_crosswalk",
                "status": "blocked",
                "detail": "source message count does not reconcile to terminal statuses plus exclusions",
            }
        )

    return {
        "schema": MAIL_HISTORICAL_CROSSWALK_SCHEMA,
        "status": "ok" if reconciled else "blocked",
        "mode": {
            "read_only": True,
            "mailbox_mutations": False,
            "sends": False,
            "deletes": False,
            "credential_changes": False,
            "terminal_accounting_only": True,
        },
        "source": {
            "history": _source_ref(history_path),
            "ops_report": _source_ref(ops_report_path),
            "message_count": len(rows),
            "evidence_items": len(items),
            "generated_at": history.get("generated_at"),
            "since": history.get("since"),
            "until_exclusive": history.get("until_exclusive"),
            "checked_at": _format_dt(checked_at),
        },
        "privacy": {
            "redacted": True,
            "omitted_fields": _PRIVATE_FIELDS,
            "public_safe": True,
        },
        "terminal_statuses": list(TERMINAL_STATUSES),
        "processing_states": list(PROCESSING_STATES),
        "kpis": {
            "source_messages": len(rows),
            "terminal_status_total": terminal_total,
            "explicit_exclusions": len(exclusions),
            "reconciled": reconciled,
            "terminal_status_counts": dict(sorted(terminal_counts.items())),
            "processing_state_counts": dict(sorted(processing_counts.items())),
            "open": terminal_counts["open"],
            "blocked": terminal_counts["blocked"],
            "needs_human": terminal_counts["needs_human"],
            "represented_in_ops": terminal_counts["represented_in_ops"],
            "resolved": terminal_counts["resolved"],
            "stale_noop": terminal_counts["stale_noop"],
        },
        "intelligence": {
            "schema": intelligence.get("schema"),
            "kpis": intelligence.get("kpis"),
            "answers": intelligence.get("answers"),
        },
        "ledgers": {
            **ledgers,
            "draft_approval": {"supplied": draft_approval_path is not None, "available": bool(draft_approval_path)},
            "delivery": {"supplied": delivery_path is not None, "available": bool(delivery_path)},
        },
        "blockers": blockers,
        "exclusions": exclusions,
        "items": items[: max(0, int(max_items))],
    }


def _build_ops(report_path: Optional[Union[Path, str]], *, now: datetime, max_age_hours: Optional[float]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    if not report_path:
        return None, [{"surface": "current_ops", "status": "blocked", "detail": "ops report path not supplied"}]
    try:
        snapshot = build_ops_snapshot(report_path, now=now, max_age_hours=max_age_hours)
    except OpsReportError as e:
        return None, [{"surface": "current_ops", "status": "blocked", "detail": e.detail}]
    blockers = []
    if (snapshot.get("freshness") or {}).get("is_stale"):
        blockers.append({"surface": "current_ops", "status": "blocked", "detail": "ops report is stale"})
    return snapshot, blockers


def _next_queue(crosswalk: Optional[Dict[str, Any]], ops_snapshot: Optional[Dict[str, Any]], *, limit: int) -> List[Dict[str, Any]]:
    queue: List[Dict[str, Any]] = []
    if crosswalk:
        for item in crosswalk.get("items") or []:
            if item.get("terminal_status") not in {"open", "blocked", "needs_human"}:
                continue
            queue.append(
                {
                    "source": "historical_crosswalk",
                    "evidence_id": item.get("evidence_id"),
                    "terminal_status": item.get("terminal_status"),
                    "processing_state": item.get("processing_state"),
                    "kind": item.get("kind"),
                    "recommended_lane": item.get("recommended_lane"),
                    "provider_hints": item.get("provider_hints") or [],
                }
            )
    if ops_snapshot:
        for lane in ops_snapshot.get("lanes") or []:
            if not isinstance(lane, dict):
                continue
            if _safe_int(lane.get("unread")) <= 0 and _safe_int(lane.get("messages")) <= 0:
                continue
            if lane.get("kind") in {"closed"}:
                continue
            queue.append(
                {
                    "source": "current_ops",
                    "lane_id": lane.get("id"),
                    "title": lane.get("title"),
                    "kind": lane.get("kind"),
                    "messages": lane.get("messages"),
                    "unread": lane.get("unread"),
                }
            )
    return queue[: max(0, int(limit))]


def build_mail_status(
    *,
    ops_report_path: Optional[Union[Path, str]] = None,
    history_path: Optional[Union[Path, str]] = None,
    action_ledger_path: Optional[Union[Path, str]] = None,
    resolver_ledger_path: Optional[Union[Path, str]] = None,
    draft_approval_path: Optional[Union[Path, str]] = None,
    delivery_path: Optional[Union[Path, str]] = None,
    stale_days: int = DEFAULT_STALE_DAYS,
    max_age_hours: Optional[float] = None,
    max_items: int = 100,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build the single public-safe UMA mail status receipt."""
    checked_at = _checked_at(now)
    ops_snapshot, blockers = _build_ops(ops_report_path, now=checked_at, max_age_hours=max_age_hours)

    crosswalk = None
    if history_path:
        try:
            crosswalk = build_historical_crosswalk(
                history_path,
                ops_report_path=ops_report_path,
                action_ledger_path=action_ledger_path,
                resolver_ledger_path=resolver_ledger_path,
                draft_approval_path=draft_approval_path,
                delivery_path=delivery_path,
                stale_days=stale_days,
                max_items=max_items,
                now=checked_at,
            )
            blockers.extend(crosswalk.get("blockers") or [])
        except MailStatusError as e:
            blockers.append({"surface": "historical_crosswalk", "status": "blocked", "detail": e.detail})
    else:
        blockers.append({"surface": "historical_crosswalk", "status": "blocked", "detail": "history path not supplied"})

    crosswalk_kpis = (crosswalk or {}).get("kpis") or {}
    ops_kpis = (ops_snapshot or {}).get("kpis") or {}
    open_historical = (
        _safe_int(crosswalk_kpis.get("open"))
        + _safe_int(crosswalk_kpis.get("blocked"))
        + _safe_int(crosswalk_kpis.get("needs_human"))
    )
    status = "ok" if not blockers and open_historical == 0 else "blocked" if blockers else "open"
    next_queue = _next_queue(crosswalk, ops_snapshot, limit=max_items)
    return {
        "schema": MAIL_STATUS_SCHEMA,
        "status": status,
        "mode": {
            "read_only": True,
            "mailbox_mutations": False,
            "sends": False,
            "deletes": False,
            "credential_changes": False,
            "apply_means_real_mailbox_mutation": True,
            "approval_required_for_sends_deletes_credentials": True,
            "processing_states": list(PROCESSING_STATES),
        },
        "source": {
            "checked_at": _format_dt(checked_at),
            "ops_report": _source_ref(ops_report_path),
            "history": _source_ref(history_path),
            "action_ledger": _source_ref(action_ledger_path),
            "resolver_ledger": _source_ref(resolver_ledger_path),
            "draft_approval_ledger": _source_ref(draft_approval_path),
            "delivery_ledger": _source_ref(delivery_path),
        },
        "privacy": {
            "redacted": True,
            "public_safe": True,
            "omitted_fields": _PRIVATE_FIELDS,
            "raw_mail_printed_to_stdout": False,
        },
        "current_ops": {
            "available": ops_snapshot is not None,
            "schema": (ops_snapshot or {}).get("schema"),
            "freshness": (ops_snapshot or {}).get("freshness"),
            "kpis": ops_kpis,
            "lanes": (ops_snapshot or {}).get("lanes", [])[:10],
        },
        "historical_crosswalk": {
            "available": crosswalk is not None,
            "schema": (crosswalk or {}).get("schema"),
            "status": (crosswalk or {}).get("status"),
            "kpis": crosswalk_kpis,
            "terminal_statuses": list(TERMINAL_STATUSES),
            "processing_states": list(PROCESSING_STATES),
        },
        "ledgers": (crosswalk or {}).get("ledgers", {}),
        "blockers": blockers,
        "next_queue": next_queue,
        "answers": {
            "what_ran": {
                "ops_summary": ops_snapshot is not None,
                "historical_crosswalk": crosswalk is not None,
                "read_only": True,
            },
            "what_mailbox_surface_was_covered": {
                "scoped_messages": ops_kpis.get("scoped_messages"),
                "inbox_messages": ops_kpis.get("inbox_messages"),
                "historical_messages": crosswalk_kpis.get("source_messages"),
            },
            "what_changed": {
                "mailbox_mutations": 0,
                "sends": 0,
                "deletes": 0,
                "credential_changes": 0,
            },
            "what_remains_open": {
                "historical_open": crosswalk_kpis.get("open"),
                "historical_blocked": crosswalk_kpis.get("blocked"),
                "historical_needs_human": crosswalk_kpis.get("needs_human"),
                "next_queue_items": len(next_queue),
            },
            "what_is_blocked": blockers,
            "is_historical_backlog_terminally_accounted_for": bool(crosswalk_kpis.get("reconciled")),
        },
    }
