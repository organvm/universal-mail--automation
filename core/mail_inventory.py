"""Private resumable provider inventory. Successful pages alone advance cursors."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time

from core.flag_transactions import AdvisoryFileLock
from core.flag_workflow import _atomic_write_private_json, _json_object_from_path, sha256_hex


SCHEMA = "uma.mail_inventory.v1"


def seal(value: dict) -> dict:
    value["content_hash"] = sha256_hex({k: v for k, v in value.items() if k != "content_hash"})
    return value


def validate(value: dict) -> None:
    if value.get("content_hash") != sha256_hex({k: v for k, v in value.items() if k != "content_hash"}):
        raise ValueError("inventory manifest hash mismatch")


def inventory(provider, *, root: Path, max_pages: int = 25, page_size: int = 100,
              ceiling: int = 600) -> dict:
    """Resume one account, retaining exact provider boundaries and every membership.

    provider.identity() authenticates an explicit account; never a default account.
    Snapshot and page failures remain retryable on the same surface. No provider
    failure is interpreted as an empty page. Raw headers stay private in pages.
    """
    if type(max_pages) is not int or not 1 <= max_pages <= 25:
        raise ValueError("inventory work unit requires 1–25 pages")
    if type(page_size) is not int or not 1 <= page_size <= 500:
        raise ValueError("inventory page size requires 1–500 messages")
    if type(ceiling) is not int or not 30 <= ceiling <= 600:
        raise ValueError("inventory ceiling requires 30–600 seconds")
    started = time.monotonic()
    with AdvisoryFileLock(root / "inventory.lock"):
        identity = provider.identity()
        if not identity.get("account") or identity.get("authenticated") is not True:
            raise ValueError("authenticated explicit account identity required")
        path = root / "manifest.json"
        if path.exists():
            manifest = _json_object_from_path(path, "inventory manifest")
            validate(manifest)
            if manifest.get("schema") != SCHEMA or manifest["identity"] != identity:
                raise ValueError("inventory account lineage mismatch")
            # Validate existing page receipts before trusting the continuation.
            for surface in manifest["surfaces"]:
                for receipt in surface["pages"]:
                    page = _json_object_from_path(root / receipt["file"], "inventory page")
                    validate(page)
                    if page["content_hash"] != receipt["sha256"]:
                        raise ValueError("inventory page lineage mismatch")
        else:
            manifest = {"schema": SCHEMA, "identity": identity,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "discovery_complete": False, "surfaces": [], "writes_performed": 0}

        def persist():
            manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
            manifest["complete"] = (manifest["discovery_complete"] and
                                    all(s["status"] in ("complete", "nonselectable") for s in manifest["surfaces"]))
            _atomic_write_private_json(path, seal(manifest), prefix=".tmp-inventory-")

        persist()
        if not manifest["discovery_complete"]:
            surfaces = provider.inventory_surfaces()
            if len({s["id"] for s in surfaces}) != len(surfaces):
                raise ValueError("duplicate native surface identity")
            manifest["surfaces"] = [{"surface": s, "status": "pending", "snapshot": None,
                                      "cursor": None, "pages": [], "errors": []} for s in surfaces]
            manifest["discovery_complete"] = True
            persist()
        pages = 0
        for state in manifest["surfaces"]:
            if state["status"] in ("complete", "nonselectable"):
                continue
            surface = state["surface"]
            if surface.get("selectable") is False:
                state["status"] = "nonselectable"
                persist()
                continue
            while pages < max_pages and time.monotonic() - started < ceiling - 30:
                try:
                    if state["snapshot"] is None:
                        state["snapshot"] = provider.inventory_snapshot(surface)
                        persist()
                    page = provider.inventory_page(surface, state["snapshot"], state["cursor"], page_size)
                    if (page.get("complete") not in (True, False) or not isinstance(page.get("messages"), list)
                            or (not page["complete"] and page.get("next_cursor") == state["cursor"])):
                        raise ValueError("invalid/nonadvancing inventory page")
                except (RuntimeError, ValueError, OSError, KeyError) as exc:
                    state["status"] = "failed"
                    state["errors"].append({"at": datetime.now(timezone.utc).isoformat(),
                                             "type": type(exc).__name__})
                    persist()
                    break
                page.update(schema="uma.mail_inventory_page.v1", identity=identity,
                            surface=surface, snapshot_sha256=sha256_hex(state["snapshot"]),
                            cursor=state["cursor"], observed_at=datetime.now(timezone.utc).isoformat())
                seal(page)
                name = "pages/" + page["content_hash"] + ".json"
                _atomic_write_private_json(root / name, page, prefix=".tmp-inventory-page-")
                state["pages"].append({"file": name, "sha256": page["content_hash"],
                                       "messages": len(page["messages"])})
                state["cursor"] = page.get("next_cursor")
                state["status"] = "complete" if page["complete"] else "pending"
                pages += 1
                persist()
                if page["complete"]:
                    break
            if pages >= max_pages or time.monotonic() - started >= ceiling - 30:
                break
        manifest["last_unit_pages"] = pages
        persist()
        return manifest


def iter_messages(root: Path):
    manifest = _json_object_from_path(root / "manifest.json", "inventory manifest")
    validate(manifest)
    for state in manifest["surfaces"]:
        for receipt in state["pages"]:
            page = _json_object_from_path(root / receipt["file"], "inventory page")
            validate(page)
            if page["content_hash"] != receipt["sha256"]:
                raise ValueError("inventory page lineage mismatch")
            yield from page["messages"]
