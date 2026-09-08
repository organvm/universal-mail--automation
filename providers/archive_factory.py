"""Production archive adapters using existing account-bound credentials."""
from contextlib import contextmanager
import imaplib
import json
import ssl

from core.inventory_cli import mail_credentials


@contextmanager
def archive_provider(args, guard):
    if not args.account:
        raise ValueError("explicit archive account required")
    env = mail_credentials(args)
    if args.provider == "gmail":
        import httplib2
        import google_auth_httplib2
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        from providers.archive_native import GmailArchive
        info = json.loads(env.get("GMAIL_TOKEN_JSON", "null"))
        if not info:
            raise ValueError("existing Gmail OAuth token required")
        creds = Credentials.from_authorized_user_info(info)
        if not creds.has_scopes(["https://www.googleapis.com/auth/gmail.modify"]):
            raise ValueError("existing Gmail modify scope required; no scope expansion")
        if not creds.valid:
            class BoundedRequest(Request):
                def __call__(self, *positional, **kwargs):
                    kwargs["timeout"] = 30
                    return super().__call__(*positional, **kwargs)
            creds.refresh(BoundedRequest())
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
        service = build("gmail", "v1", http=http, cache_discovery=False)
        try:
            yield GmailArchive(service, account=args.account, guard=guard)
        finally:
            service.close()
    elif args.provider == "outlook":
        from providers.outlook import OutlookProvider
        from providers.archive_native import GraphArchive
        provider = OutlookProvider(client_id=env.get("OUTLOOK_CLIENT_ID"),
            token_cache_path=env.get("OUTLOOK_TOKEN_CACHE"), account=args.account)
        try:
            provider.connect()
            yield GraphArchive(provider, guard=guard)
        finally:
            provider.disconnect()
    elif args.provider == "icloud":
        from providers.imap_inventory import IMAPInventory
        from providers.imap_archive_native import IMAPArchive
        user, secret = env.get("ICLOUD_IMAP_USER"), env.get("ICLOUD_IMAP_PASS")  # allow-secret: in-memory credential references, no literals
        if not user or user.casefold() != args.account.casefold() or not secret:
            raise ValueError("selected iCloud credentials unavailable")
        host = env.get("ICLOUD_IMAP_HOST", "imap.mail.me.com")
        connection = imaplib.IMAP4_SSL(host, ssl_context=ssl.create_default_context(), timeout=30)
        try:
            connection.login(user, secret)
            surfaces = IMAPInventory(connection, account=user, provider="icloud", host=host).inventory_surfaces()
            archives = [s["id"] for s in surfaces if s["selectable"] and
                        "\\archive" in {a.casefold() for a in s["attributes"]}]
            if len(archives) != 1:
                raise ValueError("exactly one server-declared Archive folder required")
            yield IMAPArchive(connection, account=user, archive_mailbox=archives[0], guard=guard)
        finally:
            try:
                connection.logout()
            except (OSError, imaplib.IMAP4.error):
                pass
    else:
        raise ValueError("unsupported archive provider")
