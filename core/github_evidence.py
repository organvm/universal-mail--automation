"""Authenticated object-specific GitHub evidence with notification provenance."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import time

from core.flag_workflow import _atomic_write_private_json, _json_object_from_path, sha256_hex
from core.mail_inventory import seal, validate, validated_pages
from core.flag_transactions import AdvisoryFileLock


SIGNAL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/"
                    r"(pull|issues|actions/runs|security/dependabot|security/code-scanning|security/secret-scanning)/"
                    r"([1-9][0-9]*)(#[A-Za-z0-9_-]+)?")
RFC_SIGNAL = re.compile(r"<([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(pull|issues)/([1-9][0-9]*)(?:/[^<>@]*)?@github\.com>")


def discover_signals(messages):
    objects = {}
    for message in messages:
        text = json.dumps({k: message.get(k) for k in ("headers", "bodies", "subject", "server_metadata")})
        for match in list(SIGNAL.finditer(text)) + list(RFC_SIGNAL.finditer(text)):
            owner, repo, kind, number = match.groups()[:4]
            fragment = match.group(5) if len(match.groups()) == 5 else None
            obj = {"repository": owner + "/" + repo, "kind": kind, "number": int(number)}
            key = sha256_hex(obj)
            objects.setdefault(key, {"id": key, "object": obj, "notifications": []})
            provenance = {"message": message.get("identity", message.get("message_id")),
                          "provenance": message.get("provenance", []), "url": match[0],
                          "fragment": fragment, "evidence_receipt": message.get("content_hash")}
            if provenance not in objects[key]["notifications"]:
                objects[key]["notifications"].append(provenance)
    return list(objects.values())


def inventory_signal_messages(root):
    """Stream exact header signals without waiting for unrelated body reads.

    Header discovery is intake evidence only. It does not establish complete
    correspondence coverage or permission to archive a notification.
    """
    manifest = _json_object_from_path(root / "manifest.json", "inventory manifest")
    validate(manifest)
    if manifest.get("schema") != "uma.mail_inventory.v1" or manifest.get("complete") is not True:
        raise ValueError("complete inventory required for stable GitHub discovery")
    for state in manifest["surfaces"]:
        for page in validated_pages(root, manifest, state):
            for message in page["messages"]:
                if message["retention_class"] != "retained":
                    continue
                yield {**message, "content_hash": page["content_hash"], "provenance": [{
                    "identity": message["identity"], "native": message["native"],
                    "memberships": message["memberships"],
                    "inventory_sha256": manifest["content_hash"],
                    "discovery_scope": "retained_inventory_headers"}]}


class GitHubReader:
    def get(self, endpoint, *, paginate=False):
        if not endpoint.startswith(("repos/", "user")) or ".." in endpoint:
            raise ValueError("invalid GitHub evidence endpoint")
        command = ["gh", "api", "--method", "GET", endpoint]
        if paginate:
            command += ["--paginate", "--slurp"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError("authenticated GitHub read unavailable")
        return json.loads(result.stdout)


def resolve_signal(signal, reader):
    obj = signal["object"]
    base = "repos/" + obj["repository"]
    kind, number = obj["kind"], obj["number"]
    status, questions = "unresolved", []
    receipts = []

    def read(path, fields, *, paginate=False):
        response = reader.get(path, paginate=paginate)
        def project(row):
            return {k: row[k] for k in fields if k in row}
        if paginate:
            if not isinstance(response, list) or any(not isinstance(p, list) for p in response):
                raise ValueError("GitHub paginated collection malformed")
            safe = [[project(row) for row in page] for page in response]
        else:
            safe = project(response)
        receipts.append({"endpoint": path, "paginated": paginate, "evidence": safe})
        return response

    if kind == "pull":
        row = read(f"{base}/pulls/{number}", ("id", "node_id", "number", "state", "merged", "merged_at", "updated_at", "html_url", "requested_reviewers", "requested_teams"))
        read(f"{base}/pulls/{number}/reviews?per_page=100", ("id", "state", "submitted_at", "commit_id", "html_url"), paginate=True)
        read(f"{base}/issues/{number}/comments?per_page=100", ("id", "created_at", "updated_at", "html_url"), paginate=True)
        if row.get("merged") is True and row.get("state") == "closed":
            status = "resolved"
        elif row.get("state") == "closed":
            questions.append("Which exact merged successor or deliberate disposition superseded this unmerged PR?")
    elif kind == "issues":
        row = read(f"{base}/issues/{number}", ("id", "node_id", "number", "state", "state_reason", "closed_at", "updated_at", "html_url", "pull_request"))
        read(f"{base}/issues/{number}/timeline?per_page=100", ("id", "event", "created_at", "commit_id", "html_url"), paginate=True)
        if row.get("pull_request"):
            questions.append("This issue identity is a pull request; obtain its merge evidence.")
        elif row.get("state") == "closed" and row.get("state_reason") == "completed":
            status = "resolved"
        elif row.get("state") == "closed":
            questions.append("What evidence establishes supersession or deliberate non-action for this closed issue?")
    elif kind == "actions/runs":
        row = read(f"{base}/actions/runs/{number}", ("id", "workflow_id", "run_number", "run_attempt", "head_sha", "head_branch", "status", "conclusion", "updated_at", "html_url"))
        if row.get("status") == "completed" and row.get("conclusion") == "success":
            status = "resolved"
        else:
            questions.append("Has this exact failed run been resolved or superseded by a verified successful run on the current branch head?")
    elif kind.startswith("security/"):
        family = kind.split("/")[1]
        endpoint = {"dependabot": "dependabot/alerts", "code-scanning": "code-scanning/alerts", "secret-scanning": "secret-scanning/alerts"}[family]
        row = read(f"{base}/{endpoint}/{number}", ("number", "state", "fixed_at", "resolved_at", "dismissed_at", "dismissed_reason", "resolution", "html_url", "updated_at"))
        if row.get("state") in ("fixed", "resolved"):
            status = "resolved"
        elif row.get("state") == "dismissed":
            questions.append("Does the recorded security-alert dismissal establish an intentional non-action disposition?")
    return seal({"schema": "uma.github_object_evidence.v1", "object": obj,
                 "observed_at": datetime.now(timezone.utc).isoformat(), "status": status,
                 "questions": questions, "receipts": receipts, "notifications": signal["notifications"],
                 "authority": "evidence_only", "writes_performed": 0})


def resolve_batch(signals, *, root: Path, reader=None, limit=25):
    with AdvisoryFileLock(root / "github.lock"):
        return _resolve_batch(signals, root=root, reader=reader, limit=limit)


def _resolve_batch(signals, *, root: Path, reader=None, limit=25):
    if type(limit) is not int or not 1 <= limit <= 25:
        raise ValueError("GitHub evidence unit requires 1–25 objects")
    reader = reader or GitHubReader()
    profile = reader.get("user")
    source_hash = sha256_hex(signals)
    path = root / "manifest.json"
    if path.exists():
        manifest = _json_object_from_path(path, "GitHub resolver manifest")
        validate(manifest)
        if manifest["signals_sha256"] != source_hash or manifest["authenticated_login"] != profile["login"]:
            raise ValueError("GitHub resolver continuation mismatch")
        if manifest.get("schema") == "uma.github_research.v1":
            if manifest["next_object"] != len(manifest["results"]):
                raise ValueError("GitHub resolver cursor mismatch")
            manifest.update(schema="uma.github_research.v2", scan_cursor=manifest["next_object"], deferred=[])
            for index, result in enumerate(manifest["results"]):
                result["index"] = index
        if manifest.get("schema") != "uma.github_research.v2":
            raise ValueError("GitHub resolver manifest version mismatch")
        done = [r["index"] for r in manifest["results"]]
        done_set = set(done)
        pending = manifest["deferred"]
        cursor = manifest["scan_cursor"]
        if (type(cursor) is not int or not 0 <= cursor <= len(signals)
                or any(type(i) is not int for i in done + pending)
                or len(set(done + pending)) != len(done + pending)
                or set(done + pending) != set(range(cursor))
                or manifest["next_object"] != next((i for i in range(cursor) if i not in done_set), cursor)):
            raise ValueError("GitHub resolver cursor mismatch")
        for result in manifest["results"]:
            index = result["index"]
            if result["file"] != result["sha256"] + ".json" or result["object_id"] != signals[index]["id"]:
                raise ValueError("GitHub resolver receipt identity mismatch")
            evidence = _json_object_from_path(root / result["file"], "GitHub object receipt")
            validate(evidence)
            if (evidence["content_hash"] != result["sha256"] or evidence["object"] != signals[index]["object"]
                    or evidence["notifications"] != signals[index]["notifications"] or evidence["status"] != result["status"]):
                raise ValueError("GitHub resolver receipt lineage mismatch")
    else:
        manifest = {"schema": "uma.github_research.v2", "signals_sha256": source_hash,
                    "authenticated_login": profile["login"], "next_object": 0,
                    "scan_cursor": 0, "deferred": [], "results": [], "errors": []}

    def persist():
        done = {r["index"] for r in manifest["results"]}
        manifest["next_object"] = next((i for i in range(manifest["scan_cursor"]) if i not in done), manifest["scan_cursor"])
        manifest["researched_objects"] = len(done)
        manifest["unavailable_objects"] = len(manifest["deferred"])
        _atomic_write_private_json(path, seal(manifest), prefix=".tmp-github-research-")
    persist()
    started = time.monotonic()
    # A failed object retains its own question and retry checkpoint. It must
    # not prevent independent repositories from being researched. Once intake
    # is exhausted, deferred objects rotate through bounded retry units.
    indices = (list(range(manifest["scan_cursor"], min(len(signals), manifest["scan_cursor"] + limit)))
               if manifest["scan_cursor"] < len(signals) else manifest["deferred"][:limit])
    for index in indices:
        if time.monotonic() - started >= 480:
            break
        signal = signals[index]
        try:
            evidence = resolve_signal(signal, reader)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            manifest["errors"].append({"object_id": signal["id"], "type": type(exc).__name__,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "question": "Can this exact object be read through the authenticated repository, or is additional access or supersession evidence required?",
                "checkpoint": "next bounded deferred-object retry after remaining intake"})
            if index in manifest["deferred"]:
                manifest["deferred"].remove(index)
            manifest["deferred"].append(index)
            manifest["scan_cursor"] = max(manifest["scan_cursor"], index + 1)
            persist()
            continue
        filename = evidence["content_hash"] + ".json"
        _atomic_write_private_json(root / filename, evidence, prefix=".tmp-github-object-")
        manifest["results"].append({"index": index, "object_id": signal["id"], "status": evidence["status"],
                                    "file": filename, "sha256": evidence["content_hash"]})
        if index in manifest["deferred"]:
            manifest["deferred"].remove(index)
        manifest["scan_cursor"] = max(manifest["scan_cursor"], index + 1)
        persist()
    return manifest


def cmd_github(args):
    try:
        root = Path(args.output).expanduser()
        source_path = root / "signals.json"
        if args.operation == "discover":
            inventories = getattr(args, "inventory", [])
            if not args.research and not inventories:
                raise ValueError("GitHub discovery requires an explicit research or inventory source")
            messages = []
            for directory in args.research:
                research_root = Path(directory).expanduser()
                state = _json_object_from_path(research_root / "manifest.json", "research manifest")
                validate(state)
                if state.get("schema") != "uma.corpus_research.v1" or not state.get("complete"):
                    raise ValueError("complete collection required for stable GitHub discovery")
                for thread in state["threads"]:
                    for receipt in thread["receipts"]:
                        if receipt["file"] != "messages/" + receipt["sha256"] + ".json":
                            raise ValueError("research receipt path mismatch")
                        message = _json_object_from_path(research_root / receipt["file"], "research message")
                        validate(message)
                        if message["content_hash"] != receipt["sha256"]:
                            raise ValueError("research message lineage mismatch")
                        messages.append(message)
            count = len(messages)
            def stream():
                nonlocal count
                yield from messages
                for directory in inventories:
                    for message in inventory_signal_messages(Path(directory).expanduser()):
                        count += 1
                        yield message
            signals = discover_signals(stream())
            result = seal({"schema": "uma.github_signals.v1", "signals": signals})
            if source_path.exists() and _json_object_from_path(source_path, "signals") != result:
                raise ValueError("changed discovery requires a new output directory")
            _atomic_write_private_json(source_path, result, prefix=".tmp-signals-")
            print(json.dumps({"objects": len(signals), "messages": count, "writes_performed": 0}))
        else:
            source = _json_object_from_path(source_path, "signals")
            validate(source)
            if source.get("schema") != "uma.github_signals.v1":
                raise ValueError("invalid signal source")
            result = resolve_batch(source["signals"], root=root, limit=args.limit)
            print(json.dumps({"researched_objects": result["researched_objects"], "objects": len(source["signals"]),
                              "unavailable_objects": result["unavailable_objects"], "intake_cursor": result["scan_cursor"],
                              "writes_performed": 0}))
            return 0 if result["next_object"] == len(source["signals"]) else 20
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__, "writes_performed": 0}))
        return 2
    return 0


def add_parser(subparsers):
    parser = subparsers.add_parser("mail-github-evidence", help="Discover and resume authenticated exact-object GitHub research")
    parser.add_argument("operation", choices=("discover", "resolve"))
    parser.add_argument("--research", action="append", default=[])
    parser.add_argument("--inventory", action="append", default=[], help="Completed native inventory; exact header signals retain every folder membership")
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=25)
    parser.set_defaults(func=cmd_github)
