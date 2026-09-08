"""Native inventory command; consumes existing credentials, never prints values."""
import imaplib
import json
import os
from pathlib import Path
import shlex
import ssl
import subprocess
import time

from core.mail_inventory import inventory


def existing_mail_env(path: Path) -> dict:
    """Read only mail variables from an existing hydration cache; no shell eval."""
    allowed = {"GMAIL_USER", "GMAIL_APP_PASSWORD", "ICLOUD_IMAP_USER", "ICLOUD_IMAP_PASS",
               "ICLOUD_IMAP_HOST", "OUTLOOK_CLIENT_ID", "OUTLOOK_TOKEN_CACHE"}
    result = {k: os.environ[k] for k in allowed if k in os.environ}
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, raw = line.removeprefix("export ").partition("=")
            if sep and key in allowed and key not in result:
                tokens = shlex.split(raw, comments=True)
                if len(tokens) != 1:
                    raise ValueError("invalid hydrated mail environment value")
                result[key] = tokens[0]
    return result


def cmd_inventory(args):
    from providers.imap_inventory import IMAPInventory
    from providers.graph_inventory import GraphInventory
    from providers.outlook import OutlookProvider
    connection = None
    graph = None
    started = time.monotonic()
    try:
        env = mail_credentials(args)
        if args.provider == "outlook":
            graph = OutlookProvider(client_id=env.get("OUTLOOK_CLIENT_ID"),
                token_cache_path=env.get("OUTLOOK_TOKEN_CACHE"), account=args.account)
            graph.connect()
            adapter = GraphInventory(graph)
        else:
            prefix = "GMAIL" if args.provider == "gmail" else "ICLOUD_IMAP"
            user = env.get(prefix + "_USER")
            secret = env.get("GMAIL_APP_PASSWORD" if args.provider == "gmail" else "ICLOUD_IMAP_PASS")  # allow-secret: in-memory credential reference, no literal
            if not user or user.casefold() != args.account.casefold() or not secret:
                raise ValueError("selected account credentials unavailable in existing hydration cache")
            host = "imap.gmail.com" if args.provider == "gmail" else env.get("ICLOUD_IMAP_HOST", "imap.mail.me.com")
            connection = imaplib.IMAP4_SSL(host, ssl_context=ssl.create_default_context(), timeout=30)
            connection.login(user, secret)
            adapter = IMAPInventory(connection, account=user, provider=args.provider, host=host)
        if getattr(args, "corpus", None):
            from core.corpus_research import research_corpus
            from core.flag_workflow import _json_object_from_path
            corpus = _json_object_from_path(Path(args.corpus).expanduser(), "corpus")
            units = getattr(args, "work_units", 1)
            if type(units) is not int or not 1 <= units <= 25:
                raise ValueError("work units require 1–25")
            result = None
            for _ in range(units):
                remaining = 600 - int(time.monotonic() - started)
                if remaining < 30:
                    break
                result = research_corpus(corpus, adapter, root=Path(args.output).expanduser(),
                                         thread_limit=args.thread_limit, ceiling=remaining)
                if result["complete"] or result["last_unit_reads"] == 0:
                    break
            if result is None:
                raise RuntimeError("authentication consumed research work-unit ceiling")
            print(json.dumps({"status": "collected" if result["complete"] else "partial",
                              "collected_threads": result["collected_threads"],
                              "threads": len(result["threads"]), "writes_performed": 0}))
            return 0 if result["complete"] else 20
        units = getattr(args, "work_units", 1)
        if type(units) is not int or not 1 <= units <= 25:
            raise ValueError("inventory work units require 1–25")
        result = None
        for _ in range(units):
            remaining = 600 - int(time.monotonic() - started)
            if remaining < 30:
                break
            result = inventory(adapter, root=Path(args.output).expanduser(),
                               max_pages=args.max_pages, page_size=args.page_size, ceiling=remaining)
            if result["complete"] or result["last_unit_pages"] == 0:
                break
        if result is None:
            raise RuntimeError("authentication consumed inventory work-unit ceiling")
        print(json.dumps({"status": "complete" if result["complete"] else "partial",
                          "surfaces": len(result["surfaces"]),
                          "complete_surfaces": sum(s["status"] in ("complete", "nonselectable") for s in result["surfaces"]),
                          "membership_records": sum(p["messages"] for s in result["surfaces"] for p in s["pages"]),
                          "unit_pages": result["last_unit_pages"], "writes_performed": 0}))
        return 0 if result["complete"] else 20
    except (OSError, ValueError, RuntimeError, imaplib.IMAP4.error, subprocess.TimeoutExpired) as exc:
        from core.flag_workflow import FlagWorkflowError
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__, "writes_performed": 0}))
        if isinstance(exc, FlagWorkflowError):
            print(str(exc))
        return 2
    finally:
        if graph is not None:
            graph.disconnect()
        if connection is not None:
            try:
                connection.logout()
            except (OSError, imaplib.IMAP4.error):
                pass


def mail_credentials(args):
    env = existing_mail_env(Path(args.env_file).expanduser())
    if args.op_item:
        result = subprocess.run(["op", "item", "get", args.op_item, "--vault", args.op_vault,
                                 "--format", "json"], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError("mail credential item unavailable")
        fields = {f.get("label"): f.get("value") for f in json.loads(result.stdout).get("fields", [])}
        for source, target in {"icloud_user": "ICLOUD_IMAP_USER", "icloud_pass": "ICLOUD_IMAP_PASS",
                               "outlook_client_id": "OUTLOOK_CLIENT_ID", "token_json": "GMAIL_TOKEN_JSON"}.items():
            if fields.get(source):
                env[target] = fields[source]
    return env


def add_parser(subparsers):
    corpus = subparsers.add_parser("mail-corpus", help="Build complete-account RFC correspondence corpus")
    corpus.add_argument("--inventory", action="append", required=True)
    corpus.add_argument("--output", required=True)
    corpus.set_defaults(func=cmd_corpus)
    parser = subparsers.add_parser("mail-inventory", help="Resume private native retained-mail inventory")
    parser.add_argument("--provider", choices=("gmail", "icloud", "outlook"), required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--env-file", default="~/.limen.env")
    parser.add_argument("--op-item", help="Existing mail credential item to consume in memory")
    parser.add_argument("--op-vault", default="Personal")
    parser.add_argument("--output", required=True, help="Private account-specific inventory directory")
    parser.add_argument("--max-pages", type=int, default=25)
    parser.add_argument("--work-units", type=int, default=1, help="Resume units within one shared 600-second ceiling")
    parser.add_argument("--page-size", type=int, default=100)
    parser.set_defaults(func=cmd_inventory)


def cmd_corpus(args):
    from core.corpus_research import build_corpus
    try:
        result = build_corpus([Path(p).expanduser() for p in args.inventory], output=Path(args.output).expanduser())
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__}))
        return 2
    print(json.dumps({"threads": len(result["threads"]),
                      "messages": sum(len(t["messages"]) for t in result["threads"]), "writes_performed": 0}))
    return 0
