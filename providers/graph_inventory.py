"""Paginated four-account inventory's explicit-account Outlook implementation."""
from urllib.parse import quote, urlparse

from core.flag_workflow import sha256_hex
from providers.outlook import GRAPH_API_BASE


def read_mime(provider, message_id):
    url = GRAPH_API_BASE + "/me/messages/" + quote(message_id, safe="") + "/$value"
    response = provider._get_session().get(url, timeout=30, stream=True)
    try:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 10485760:
                raise RuntimeError("message exceeds bounded attachment budget")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        response.close()


def mime_headers(raw):
    import email
    from email import policy
    if b"\r\n\r\n" not in raw:
        raise ValueError("Graph MIME header boundary missing")
    parsed = email.message_from_bytes(raw, policy=policy.default)
    return [{"name": key, "value": str(value).encode("utf-8", errors="replace").decode("utf-8")}
            for key, value in parsed.raw_items()]


class GraphInventory:
    def __init__(self, provider):
        if not provider.account or not provider._access_token:
            raise ValueError("connected explicit Outlook account required")
        self.provider = provider

    def identity(self):
        identity = self.provider.verify_authenticated_identity()
        return {"provider": "outlook", **identity, "transport": "graph",
                "id_type": "ImmutableId", "authenticated": True}

    def _pages(self, url):
        seen = set()
        while url:
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or url in seen:
                raise ValueError("invalid Graph pagination link")
            seen.add(url)
            page = self.provider._api_get(url)
            if not isinstance(page.get("value"), list):
                raise ValueError("Graph page lacks collection")
            yield from page["value"]
            url = page.get("@odata.nextLink")

    def inventory_surfaces(self):
        excluded = {self.provider._api_get(GRAPH_API_BASE + "/me/mailFolders/" + name)["id"]
                    for name in ("junkemail", "deleteditems")}
        pending = [(GRAPH_API_BASE + "/me/mailFolders?includeHiddenFolders=true&$top=100", False)]
        result, seen = [], set()
        while pending:
            url, parent_excluded = pending.pop(0)
            for folder in self._pages(url):
                fid = folder["id"]
                if fid in seen:
                    raise ValueError("Graph folder identity repeated")
                seen.add(fid)
                is_excluded = parent_excluded or fid in excluded
                result.append({"id": fid, "name": folder["displayName"], "selectable": True,
                               "retention_class": "junk_trash" if is_excluded else "retained"})
                if folder["childFolderCount"]:
                    pending.append((GRAPH_API_BASE + "/me/mailFolders/" + quote(fid, safe="") +
                                    "/childFolders?includeHiddenFolders=true&$top=100", is_excluded))
        return result

    def inventory_snapshot(self, surface):
        return {"boundary": "graph_continuation_scan", "id_type": "ImmutableId",
                "folder_id": surface["id"]}

    def inventory_page(self, surface, snapshot, cursor, limit):
        # Some Graph list responses omit headers even with $select. Bound the
        # per-message detail fallback to twenty exact reads in a page.
        limit = min(limit, 20)
        url = cursor or (GRAPH_API_BASE + "/me/mailFolders/" + quote(surface["id"], safe="") +
                        "/messages?$top=" + str(limit) + "&$select=id,parentFolderId,conversationId,internetMessageId,"
                        "internetMessageHeaders,subject,from,receivedDateTime,sentDateTime,isRead,flag,categories,hasAttachments")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com":
            raise ValueError("invalid Graph continuation host")
        page = self.provider._api_get(url)
        if not isinstance(page.get("value"), list):
            raise ValueError("Graph page lacks messages")
        messages = []
        for row in page["value"]:
            if row["parentFolderId"] != surface["id"]:
                raise ValueError("Graph message moved during inventory")
            header_source = "graph"
            if "internetMessageHeaders" not in row:
                detail = self.provider._api_get(GRAPH_API_BASE + "/me/messages/" + quote(row["id"], safe=""),
                    params={"$select": "id,parentFolderId,internetMessageHeaders"})
                if detail.get("id") != row["id"] or detail.get("parentFolderId") != surface["id"]:
                    raise ValueError("Graph detail identity changed")
                if "internetMessageHeaders" not in detail:
                    row["internetMessageHeaders"] = mime_headers(read_mime(self.provider, row["id"]))
                    header_source = "mime"
                else:
                    row["internetMessageHeaders"] = detail["internetMessageHeaders"]
            headers = row["internetMessageHeaders"]
            header_map = {h["name"].lower(): h["value"] for h in headers}
            messages.append({"identity": {"provider": "outlook", "account": self.provider.account,
                "message_id": row["id"], "evidence_digest": sha256_hex(headers)},
                "native": {"id": row["id"], "id_type": "ImmutableId", "folder_id": surface["id"], "header_source": header_source},
                "memberships": [surface["id"]], "retention_class": surface["retention_class"],
                "thread_id": row["conversationId"], "rfc_message_id": row["internetMessageId"],
                "in_reply_to": header_map.get("in-reply-to", ""), "references": header_map.get("references", ""),
                "sender": row["from"]["emailAddress"]["address"], "subject": row["subject"],
                "date": row["receivedDateTime"], "headers": headers, "server_metadata": row})
        return {"messages": messages, "complete": not page.get("@odata.nextLink"),
                "next_cursor": page.get("@odata.nextLink")}

    def read_corpus_message(self, item):
        import base64
        import hashlib
        from core.corpus_research import extract_evidence
        identity = item["provenance"][0]["identity"]
        if identity["account"] != self.provider.account or identity["provider"] != "outlook":
            raise ValueError("Outlook research account mismatch")
        url = GRAPH_API_BASE + "/me/messages/" + quote(identity["message_id"], safe="")
        current = self.provider._api_get(url, params={"$select": "id,internetMessageHeaders"})
        raw = read_mime(self.provider, identity["message_id"])
        headers = mime_headers(raw) if item["provenance"][0]["native"].get("header_source") == "mime" else current.get("internetMessageHeaders")
        if current["id"] != identity["message_id"] or headers is None or sha256_hex(headers) != identity["evidence_digest"]:
            raise ValueError("Outlook research evidence changed")
        return {**extract_evidence(raw), "raw_rfc822_base64": base64.b64encode(raw).decode(),
                "raw_sha256": hashlib.sha256(raw).hexdigest(), "complete": True}
