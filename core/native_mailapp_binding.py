"""Read-only evidence bindings between native mail identities and Mail.app.

A configured display name never establishes account identity. Bindings require
the authenticated inventory account, a unique scoped RFC lookup, and complete
message equality. Only osascript's documented-in-our-transport newline conversion
is accommodated; headers, MIME boundaries, attachments and body text stay bound.
"""
import base64
from datetime import datetime, timedelta, timezone
import email
from email import policy
import hashlib
from pathlib import Path

from core.flag_workflow import _json_object_from_path, require_hex256
from core.mail_inventory import seal, validate
from core.models import MessageReference, _stable_digest
from core.obligation_workflow import MessageIdentity


SCHEMA = "uma.native_mailapp_binding.v1"


def _fresh(value, now):
    observed = datetime.fromisoformat(value)
    if observed.tzinfo is None or not timedelta(0) <= now - observed <= timedelta(hours=24):
        raise ValueError("native binding evidence is stale or future-dated")


def _load(path):
    result = _json_object_from_path(Path(path), "native Mail.app binding source")
    validate(result)
    return result


def _transport_bytes(raw):
    # text=True in the osascript runner performs universal-newline conversion.
    # Strict UTF-8 keeps an unsupported representation from becoming false proof.
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _rfc_id(raw):
    parsed = email.message_from_bytes(raw, policy=policy.default)
    ids = parsed.get_all("Message-ID", [])
    if len(ids) != 1:
        raise ValueError("one RFC Message-ID header required")
    result = str(ids[0]).strip()
    if (not result.startswith("<") or not result.endswith(">") or result.count("<") != 1
            or result.count(">") != 1 or any(c.isspace() for c in result)):
        raise ValueError("invalid RFC Message-ID header")
    return result


def _read_evidence(binding, now):
    source = binding["research_evidence"]
    evidence = _load(source["path"])
    if evidence.get("schema") != "uma.corpus_message_evidence.v1" or evidence["content_hash"] != source["sha256"]:
        raise ValueError("native research receipt changed")
    _fresh(evidence["observed_at"], now)
    if evidence.get("complete") is not True or not evidence.get("provenance"):
        raise ValueError("complete native evidence required")
    # Older cross-folder dedup receipts did not independently read each copy.
    if evidence["provenance"][0]["identity"] != binding["identity"]:
        raise ValueError("native evidence must cover this exact independently read identity")
    raw = base64.b64decode(evidence["raw_rfc822_base64"], validate=True)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != evidence["raw_sha256"] or digest != source["raw_sha256"]:
        raise ValueError("native full-content evidence changed")
    if binding["identity"]["provider"] == "gmail":
        if (evidence.get("header_representation") != "imap_header_same_fetch"
                or evidence.get("native_headers_sha256") != binding["identity"]["evidence_digest"]):
            raise ValueError("Gmail inventory header and full MIME must be bound by the same native FETCH")
    return evidence, raw


def _source_lineage(binding, now):
    sources = binding["sources"]
    inventory = _load(sources["inventory"]["path"])
    corpus = _load(sources["corpus"]["path"])
    research = _load(sources["research_manifest"]["path"])
    identity = binding["identity"]
    if (inventory.get("schema") != "uma.mail_inventory.v1" or inventory.get("complete") is not True
            or inventory["content_hash"] != sources["inventory"]["sha256"]
            or inventory["identity"] != binding["account_mapping"]["authenticated_native_identity"]
            or inventory["identity"].get("authenticated") is not True
            or any(inventory["identity"].get(k) != identity[k] for k in ("provider", "account"))):
        raise ValueError("authenticated native account inventory lineage mismatch")
    if (corpus.get("schema") != "uma.research_corpus.v1"
            or corpus["content_hash"] != sources["corpus"]["sha256"]
            or {"identity": inventory["identity"], "inventory_sha256": inventory["content_hash"]} not in corpus["sources"]
            or research.get("schema") != "uma.corpus_research.v1"
            or research["corpus_sha256"] != corpus["content_hash"]):
        raise ValueError("native research corpus lineage mismatch")
    evidence, raw = _read_evidence(binding, now)
    item_matches = [(thread, item) for thread in corpus["threads"] for item in thread["messages"]
                    if item["id"] == evidence["message_id"]]
    if len(item_matches) != 1 or item_matches[0][1]["provenance"] != evidence["provenance"]:
        raise ValueError("native receipt corpus membership mismatch")
    thread = item_matches[0][0]
    states = [state for state in research["threads"] if state["id"] == thread["id"]]
    relative = "messages/" + evidence["content_hash"] + ".json"
    expected_path = Path(sources["research_manifest"]["path"]).parent / relative
    if Path(binding["research_evidence"]["path"]).resolve() != expected_path.resolve():
        raise ValueError("native receipt path does not belong to research")
    receipt = {"file": relative, "sha256": evidence["content_hash"]}
    if len(states) != 1 or states[0]["receipts"].count(receipt) != 1:
        raise ValueError("native receipt no longer belongs to current research")
    return evidence, raw


def validate_binding(binding, *, now=None, revalidate_sources=True):
    """Return the exact Mail.app reference; historical validation can be pure."""
    now = now or datetime.now(timezone.utc)
    validate(binding)
    if binding.get("schema") != SCHEMA or binding.get("writes_performed") != 0:
        raise ValueError("versioned read-only native binding required")
    identity = MessageIdentity.model_validate(binding["identity"])
    _fresh(binding["observed_at"], now)
    reference = MessageReference(**binding["reference"])
    reference.validate_scoped()
    if (reference.provider != "mailapp" or reference.ref_digest != binding["ref_digest"]
            or not reference.provider_id.isascii() or not reference.provider_id.isdigit()
            or reference.provider_id.startswith("0")
            or type(reference.observed_native_flag) is not int or reference.observed_native_flag not in range(-1, 7)):
        raise ValueError("exact observed native Mail.app reference required")
    for digest in (reference.evidence_digest, reference.message_id_digest, reference.sender_digest,
                   reference.subject_digest, binding["ref_digest"]):
        require_hex256(digest, "native Mail.app reference evidence")
    mapping = binding["account_mapping"]
    native = mapping["authenticated_native_identity"]
    if (mapping["account"] != reference.account or not mapping["account_id"]
            or native.get("authenticated") is not True
            or native.get("provider") != identity.provider or native.get("account") != identity.account
            or sum(address.casefold() == identity.account.casefold() for address in mapping["email_addresses"]) != 1):
        raise ValueError("exact authenticated-to-configured account mapping required")
    proof = binding["mailapp_evidence"]
    if (proof.get("complete") is not True or proof.get("transport") != "osascript_unicode_newlines"
            or proof.get("content_equivalence") != "full_utf8_source_universal_newlines_only"
            or proof["source_sha256"] != proof["native_transport_sha256"]
            or _stable_digest(proof["message_id_header"]) != reference.message_id_digest
            or _rfc_id((proof["message_id_header"] + "\r\n\r\n").encode()) != proof["rfc_message_id"]):
        raise ValueError("complete Mail.app/native content equivalence required")
    for digest in (proof["source_sha256"], proof["native_transport_sha256"],
                   binding["research_evidence"]["sha256"], binding["research_evidence"]["raw_sha256"]):
        require_hex256(digest, "native full message proof")
    for key in ("inventory", "corpus", "research_manifest"):
        source = binding["sources"][key]
        if not Path(source["path"]).is_absolute():
            raise ValueError("absolute private source path required")
        require_hex256(source["sha256"], "native source receipt")
    if not Path(binding["research_evidence"]["path"]).is_absolute():
        raise ValueError("absolute native evidence path required")
    if revalidate_sources:
        _, raw = _source_lineage(binding, now)
        if (hashlib.sha256(_transport_bytes(raw)).hexdigest() != proof["source_sha256"]
                or _rfc_id(raw) != proof["rfc_message_id"]):
            raise ValueError("native full source no longer matches Mail.app binding")
    return reference


def content_evidence(binding, *, now=None):
    now = now or datetime.now(timezone.utc)
    validate_binding(binding, now=now)
    evidence, _ = _read_evidence(binding, now)
    return {"identity": binding["identity"], "raw_sha256": evidence["raw_sha256"],
            "native_headers_sha256": evidence.get("native_headers_sha256"),
            "header_representation": evidence.get("header_representation"),
            "receipt_sha256": evidence["content_hash"]}


def build_binding(*, identity, evidence_path, research_manifest_path, inventory_manifest_path,
                  corpus_path, mailapp, mailapp_account, mailbox, now=None):
    """Produce a sealed private proof after account-scoped, read-only Mail.app reads."""
    now = now or datetime.now(timezone.utc)
    identity = MessageIdentity.model_validate(identity).model_dump()
    inventory, corpus, research = (_load(path) for path in
                                   (inventory_manifest_path, corpus_path, research_manifest_path))
    evidence = _load(evidence_path)
    binding = {"schema": SCHEMA, "identity": identity, "observed_at": now.isoformat(),
               "writes_performed": 0, "sources": {
                   "inventory": {"path": str(Path(inventory_manifest_path).resolve()), "sha256": inventory["content_hash"]},
                   "corpus": {"path": str(Path(corpus_path).resolve()), "sha256": corpus["content_hash"]},
                   "research_manifest": {"path": str(Path(research_manifest_path).resolve()), "sha256": research["content_hash"]}},
               "research_evidence": {"path": str(Path(evidence_path).resolve()), "sha256": evidence["content_hash"],
                                     "raw_sha256": evidence["raw_sha256"]},
               "account_mapping": {"authenticated_native_identity": inventory["identity"]}}
    _, raw = _source_lineage(binding, now)
    mapping = mailapp.native_account_mapping(mailapp_account)
    if sum(address.casefold() == identity["account"].casefold() for address in mapping["email_addresses"]) != 1:
        raise ValueError("Mail.app configured addresses do not identify the authenticated native account")
    binding["account_mapping"].update(mapping)
    rfc_id = _rfc_id(raw)
    ref = mailapp.native_evidence_reference(account=mailapp_account, mailbox=mailbox, rfc_message_id=rfc_id)
    if ref.account != mailapp_account or ref.mailbox != mailbox:
        raise ValueError("Mail.app lookup escaped explicit scope")
    source = mailapp.read_native_source_ref(ref)
    if source.get("complete") is not True or source.get("reference") != ref.__dict__:
        raise ValueError("complete exact Mail.app source required")
    source_bytes = source["source"].encode("utf-8")
    if source_bytes != _transport_bytes(raw):
        raise ValueError("complete Mail.app and native source differ; binding remains unverified")
    header = source["native_state"]["message_id_raw"]
    binding.update(reference=ref.__dict__, ref_digest=ref.ref_digest,
                   mailapp_evidence={"source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                                     "native_transport_sha256": hashlib.sha256(_transport_bytes(raw)).hexdigest(),
                                     "complete": True, "transport": source["transport"],
                                     "content_equivalence": "full_utf8_source_universal_newlines_only",
                                     "rfc_message_id": rfc_id, "message_id_header": header})
    seal(binding)
    validate_binding(binding, now=now, revalidate_sources=False)
    return binding
