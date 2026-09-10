"""Read-only reconciliation of frozen and completed transactions.

Observation never unfreezes a transaction or permits a repeat write. A separate
review can use this receipt to decide recovery, preserving the original ledger.
"""
from datetime import datetime, timezone

from core.flag_transactions import TransactionEngine
from core.flag_workflow import sha256_hex, validate_plan_schema
from core.models import FlagColor
from providers.flag_codecs import mailapp_index_from_flag


def verify_transactions(plan, ledger, provider):
    mutations = validate_plan_schema(plan)
    entries = ledger.entries()
    selected = {e["mutation_id"]: e for e in entries if e["plan_sha256"] == plan["plan_hash"]}
    targets = [m for m in mutations if m.mutation_id in selected]
    if not targets:
        raise ValueError("no recorded transactions to verify")
    if len(targets) > 25:
        raise ValueError("verification batch exceeds 25; use an account-scoped bounded plan")
    results = []
    for mutation in targets:
        record = selected[mutation.mutation_id]
        ref = TransactionEngine._ref_for(mutation)
        status = "uncertain"
        try:
            live = provider.resolve_scoped(ref)
            details = provider.get_message_details_ref(ref)
            intended = FlagColor(record["intended_state"])
            native = mailapp_index_from_flag(intended)
            if live.evidence_digest != ref.evidence_digest:
                status = "conflicted"
            elif (live.observed_native_flag == native and details is not None
                  and details.is_starred == (native != -1)):
                status = "verified"
            elif live.observed_native_flag == mutation.observed_native_flag:
                status = "unchanged"
            else:
                status = "conflicted"
        except (RuntimeError, ValueError, KeyError):
            pass
        results.append({"mutation_id": mutation.mutation_id, "status": status,
                        "ledger_status": record["status"], "reference": ref.__dict__})
    result = {"schema": "uma.flags.verification.v1", "plan_sha256": plan["plan_hash"],
              "ledger_sha256": sha256_hex(entries), "observed_at": datetime.now(timezone.utc).isoformat(),
              "results": results, "writes_performed": 0, "replay_authorized": False}
    result["content_hash"] = sha256_hex(result)
    return result


def cmd_verify(args):
    import json
    from pathlib import Path
    from core.flag_transactions import TransactionLedger
    from core.flag_workflow import load_plan, _atomic_write_private_json
    from providers.mailapp import MailAppProvider
    try:
        plan = load_plan(Path(args.plan).expanduser())
        with MailAppProvider() as provider:
            result = verify_transactions(plan, TransactionLedger(Path(args.ledger).expanduser()), provider)
        _atomic_write_private_json(Path(args.output).expanduser(), result, prefix=".tmp-verification-")
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}))
        return 2
    print(json.dumps(result))
    return 0 if all(r["status"] == "verified" for r in result["results"]) else 20
