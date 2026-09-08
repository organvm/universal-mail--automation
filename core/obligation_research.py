"""Read-only bounded correspondence collection. Assertions still require review."""
from datetime import datetime, timezone
from pathlib import Path
import email
import json
import re
import time

from core.flag_workflow import _atomic_write_private_json, _json_object_from_path, sha256_hex
from core.models import MessageReference


def research(provider, observation: dict, *, output: Path, thread_limit: int = 25,
             resume: dict | None = None) -> dict:
    if type(thread_limit) is not int or not 1 <= thread_limit <= 25:
        raise ValueError("thread limit must be 1–25")
    artifact = {"schema": "uma.obligation_research.v1", "generated_at": datetime.now(timezone.utc).isoformat(),
                "threads": [], "unattempted": [], "duplicates": [], "writes_performed": 0, "authority": "evidence_only"}
    started = time.monotonic()
    sent = {s["account"]: s["mailbox"] for s in observation["surfaces"]
            if s.get("mailbox", "").lower() in ("sent mail", "sent messages", "sent items", "sent")}
    rows = sorted(observation["messages"], key=lambda r: (r["native_index"] != 5, r["provider_id"]))
    source_hash = sha256_hex(observation)
    cursor = 0
    if resume is not None:
        body = {k: v for k, v in resume.items() if k != "content_hash"}
        if (resume.get("schema") != "uma.obligation_research.v1"
                or resume.get("content_hash") != sha256_hex(body)
                or resume.get("observation_sha256") != source_hash):
            raise ValueError("research resume lineage mismatch")
        cursor = resume.get("next_candidate")
        if type(cursor) is not int or not 0 <= cursor <= len(rows):
            raise ValueError("invalid research continuation")
        artifact = json.loads(json.dumps(resume))
        artifact["generated_at"] = datetime.now(timezone.utc).isoformat()
    artifact["observation_sha256"] = source_hash
    artifact["next_candidate"] = cursor
    seen = set()
    def persist():
        artifact["unattempted"] = [r["reference"] for r in rows[artifact["next_candidate"]:]]
        artifact["content_hash"] = sha256_hex({k: v for k, v in artifact.items() if k != "content_hash"})
        _atomic_write_private_json(output, artifact, prefix=".tmp-research-")
    persist()
    attempted = 0
    for position, row in enumerate(rows[cursor:], start=cursor):
        if attempted >= thread_limit or time.monotonic() - started > 570:
            break
        attempted += 1
        ref = MessageReference(**row["reference"])
        thread = {"seed": ref.__dict__, "messages": [], "blockers": [], "status": "review_required"}
        try:
            seed = provider.read_evidence_ref(ref)
            parsed = email.message_from_string(seed["headers"])
            anchor = str(parsed.get("Message-ID", ""))
            if anchor and (ref.account, anchor) in seen:
                artifact["duplicates"].append(ref.__dict__)
                artifact["next_candidate"] = position + 1
                persist()
                continue
            seen.add((ref.account, anchor or ref.provider_id))
            thread["messages"].append(seed)
            # Preserve each completed read even if the next provider call fails.
            artifact["in_progress"] = thread
            persist()
            if ref.account not in sent:
                thread["blockers"].append("sent_surface_unavailable")
            else:
                anchors = set(re.findall(r"<[^<>\s]+>", " ".join(str(parsed.get(k, ""))
                    for k in ("Message-ID", "In-Reply-To", "References"))))
                refs = provider.related_sent_refs(ref, seed["headers"], mailbox=sent[ref.account])
                for read_index, related in enumerate(refs, start=1):
                    if read_index >= 20 or time.monotonic() - started > 570:
                        thread["blockers"].append("thread_continuation_required")
                        break
                    item = provider.read_evidence_ref(related)
                    candidate = email.message_from_string(item["headers"])
                    candidate_ids = set(re.findall(r"<[^<>\s]+>", " ".join(str(candidate.get(k, ""))
                        for k in ("Message-ID", "In-Reply-To", "References"))))
                    if anchors & candidate_ids:
                        thread["messages"].append(item)
                    else:
                        thread["blockers"].append("non_thread_header_match_excluded")
                    persist()
        except RuntimeError:
            thread["blockers"].append("bounded_correspondence_read_unavailable")
        artifact["threads"].append(thread)
        artifact.pop("in_progress", None)
        artifact["next_candidate"] = position + 1
        persist()
    persist()
    return artifact


def cmd_research(args):
    from providers.mailapp import MailAppProvider
    try:
        observation = _json_object_from_path(Path(args.input).expanduser(), "observation")
        if observation.get("schema") != "uma.obligation_observation.v1":
            raise ValueError("a current observation artifact is required")
        with MailAppProvider() as provider:
            resume = (_json_object_from_path(Path(args.resume).expanduser(), "research resume")
                      if getattr(args, "resume", None) else None)
            result = research(provider, observation, output=Path(args.output).expanduser(),
                              thread_limit=args.thread_limit, resume=resume)
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}))
        return 2
    print(json.dumps({"threads": len(result["threads"]), "unattempted": len(result["unattempted"]),
                      "blockers": sum(len(t["blockers"]) for t in result["threads"]), "writes_performed": 0}))
    return 20 if result["unattempted"] or any(t["blockers"] for t in result["threads"]) else 0
