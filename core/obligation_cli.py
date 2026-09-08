"""On-demand, private artifact interface. No public mutation endpoint."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from core.flag_workflow import _atomic_write_private_json, _json_object_from_path
from core.obligation_workflow import Obligation, evaluate_benchmark, flag_candidates, import_legacy, reconcile


def run(args) -> int:
    now = datetime.now(timezone.utc)
    try:
        source = _json_object_from_path(Path(args.input).expanduser(), "obligation input")
        if args.operation == "benchmark":
            result = evaluate_benchmark(source["cases"], now=now)
        elif args.operation == "flag-candidates":
            if not args.flag_plan:
                raise ValueError("--flag-plan is required")
            from core.flag_workflow import load_plan
            result = flag_candidates(source, load_plan(Path(args.flag_plan).expanduser()))
        else:
            if args.operation == "import-legacy":
                obligations = import_legacy(source, now=now)
                result = {"schema": "uma.obligation_evidence.v1",
                          "obligations": [o.model_dump(mode="json") for o in obligations],
                          "coverage_gaps": ["Legacy aggregates need exact identity and thread evidence"]}
            else:
                if source.get("schema") != "uma.obligation_evidence.v1":
                    raise ValueError("use import-legacy explicitly for older ledgers")
                obligations = [Obligation.model_validate(o) for o in source["obligations"]]
                result = reconcile(obligations, now=now, coverage_gaps=source.get("coverage_gaps", []),
                                   verification_receipts=source.get("verification_receipts", []))
        _atomic_write_private_json(Path(args.output).expanduser(), result, prefix=".tmp-obligations-")
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}))
        return 2
    print(json.dumps({"status": "written", "output": str(Path(args.output).expanduser()),
                      "schema": result["schema"], "authority": "shadow_only"}))
    return 0


def add_parser(subparsers):
    from core.archive_transactions import cmd_archive
    archive_parser = subparsers.add_parser("archive-workflow", help="Conditional archive contracts; unsupported adapters fail closed")
    archive_parser.add_argument("operation", choices=["plan", "apply", "verify", "reconcile", "rollback"])
    archive_parser.add_argument("--input", required=True)
    archive_parser.add_argument("--output", required=True)
    archive_parser.add_argument("--approval")
    archive_parser.add_argument("--provider", choices=["gmail", "icloud"], default="gmail")
    archive_parser.add_argument("--state-dir", default="~/.local/share/uma/archive")
    archive_parser.set_defaults(func=cmd_archive)
    from core.obligation_research import cmd_research
    research_parser = subparsers.add_parser("mail-research", help="Read at most 25 threads and 20 messages per thread")
    research_parser.add_argument("--input", required=True)
    research_parser.add_argument("--output", required=True)
    research_parser.add_argument("--thread-limit", type=int, default=25)
    research_parser.add_argument("--resume", help="Validated prior research artifact for the same observation")
    research_parser.set_defaults(func=cmd_research)
    from core.obligation_refresh import cmd_refresh
    refresh_parser = subparsers.add_parser("mail-observe", help="Refresh Inbox and all flagged surfaces without writes")
    refresh_parser.add_argument("--account", action="append", required=True)
    refresh_parser.add_argument("--output", required=True)
    refresh_parser.add_argument("--shadow", required=True)
    refresh_parser.add_argument("--limit", type=int, default=500)
    refresh_parser.set_defaults(func=cmd_refresh)
    parser = subparsers.add_parser("mail-workflow", help="Evidence-backed obligation shadow workflow")
    parser.add_argument("operation", choices=["plan", "reconcile", "import-legacy", "benchmark", "flag-candidates"])
    parser.add_argument("--flag-plan", help="Preserved versioned native mutation plan")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True, help="Private atomic JSON artifact")
    parser.set_defaults(func=run)
