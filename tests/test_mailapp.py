"""Mail.app provider regressions."""

from datetime import datetime

from core.models import LabelAction
from providers.mailapp import MailAppProvider, _FIELD_SEP, _HDR_SEP


def _canned_list_output(rows):
    """Build the exact osascript stdout list_messages parses: one line per message with
    unit-separator (\\x1f) columns, header lines joined by record-separator (\\x1e), plus
    the trailing ---TOTAL: sentinel. `rows` is a list of
    (id, sender, subject, is_read, is_flagged, [header_lines])."""
    lines = []
    for r in rows:
        mid, sender, subject, read, flagged = r[0], r[1], r[2], r[3], r[4]
        hdr_lines = r[5] if len(r) > 5 else []
        bulk = _HDR_SEP.join(hdr_lines)
        lines.append(_FIELD_SEP.join([mid, sender, subject, read, flagged, bulk]))
    return "\n".join(lines) + f"\n---TOTAL:{len(rows)}"


def test_list_messages_captures_bulk_headers():
    provider = MailAppProvider()
    provider._run_applescript = lambda script: _canned_list_output([
        ("1", "Feross Aboukhadijeh <feross@socket.dev>", "Socket Weekly", "false", "false",
         ["List-Unsubscribe: <https://socket.dev/unsub>"]),
        ("2", "Micah Longo <micahlongo@gmail.com>", "Padavano v. MDC (Depositions)",
         "false", "false", []),
    ])

    result = provider.list_messages(limit=10)
    by_id = {m.id: m for m in result.messages}

    # Bulk newsletter carries the header; personal message carries none (fail-open).
    assert by_id["1"].headers.get("list-unsubscribe") == "<https://socket.dev/unsub>"
    assert by_id["2"].headers == {}


def test_bulk_headers_flow_end_to_end_to_obligation(tmp_path):
    """The full live path: provider fetch -> receipt row -> obligations_build. A swept
    message carrying List-Unsubscribe becomes a cls=bulk no-reply obligation; a personal
    message with no bulk headers stays reply-owed. Uses the real reported cases as fixtures."""
    import json
    import inbox_sweep
    from obligations_build import build

    # The 8 real cases: 5 junk (bulk-headered) suppressed; 3 real kept reply-owed.
    JUNK = [
        ("j1", "Feross Aboukhadijeh <feross@socket.dev>", "Socket Weekly",
         ["List-Unsubscribe: <https://socket.dev/unsub>"]),
        ("j2", "Hello Developer <developer@insideapple.apple.com>", "Hello Developer",
         ["List-Id: Hello Developer <hello.apple.com>"]),
        ("j3", "Laughing Buddha Comedy <laughingbuddhacomedy@buytickets.at>",
         "Your ticket confirmation", ["List-Unsubscribe: <mailto:u@buytickets.at>"]),
        ("j4", "Naga Saikumar <naga.saikumar@stage4solutions.com>", "Exciting opportunity",
         ["Precedence: bulk"]),
        ("j5", "ML <ml@ceiamerica.com>", "Quick check-in",
         ["List-Unsubscribe: <https://ceiamerica.com/unsub>"]),
    ]
    REAL = [
        ("r1", "Micah Longo <micahlongo@gmail.com>", "Padavano v. MDC (Depositions)"),
        ("r2", "Zafer Ramzan <zafer@algora.io>", "Air Space Intelligence interview"),
        ("r3", "Uruba Niazi <uruba.niazi@authplane.ai>", "quick question on FastMCP + auth"),
    ]
    rows = []
    for mid, sender, subject, hdrs in JUNK:
        rows.append((mid, sender, subject, "false", "false", hdrs))
    for mid, sender, subject in REAL:
        rows.append((mid, sender, subject, "false", "false", []))

    provider = MailAppProvider()
    provider._run_applescript = lambda script: _canned_list_output(rows)

    swept = inbox_sweep.classify_inbox(provider, "INBOX", limit=50)
    # Force every swept row to surface as a fire so it reaches the obligation cascade
    # (isolates the bulk-vs-precedent decision from the archive/keep triage).
    for r in swept:
        r["action"] = "fire"

    receipt = {"result": {"account": "acct", "total": len(swept)}, "rows": swept}
    (tmp_path / "inbox_sweep-acct.json").write_text(json.dumps(receipt), encoding="utf-8")

    ledger = build(str(tmp_path))
    reply_owed = {o["domain"] for o in ledger["obligations"] if o["requires_reply"]}
    bulk = {o["domain"] for o in ledger["obligations"] if o["cls"] == "bulk"}

    # Every junk sender suppressed to bulk; NONE of them reply-owed.
    for dom in ("socket.dev", "insideapple.apple.com", "buytickets.at",
                "stage4solutions.com", "ceiamerica.com"):
        assert dom in bulk, dom
        assert dom not in reply_owed, dom
    # The 3 real senders stay reply-owed.
    for dom in ("gmail.com", "algora.io", "authplane.ai"):
        assert dom in reply_owed, dom


def test_mailapp_star_accepts_due_date_from_base_apply_actions():
    provider = MailAppProvider()
    scripts = []
    provider._run_applescript = lambda script: scripts.append(script) or "ok"

    result = provider.apply_actions([
        LabelAction(
            message_id="123",
            sender="promo@some-shop.example",
            star=True,
            due_date=datetime(2026, 6, 1),
        )
    ])

    assert result.error_count == 0
    assert result.success_count == 1
    assert scripts and "flagged status" in scripts[0]
