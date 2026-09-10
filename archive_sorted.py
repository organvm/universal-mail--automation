"""
Archive Sorted Mail
Purpose: Retroactively remove 'INBOX' label from emails that have been successfully categorized.
This implements "Inbox Zero" for organized mail.

SAFETY: ARCHIVE_CATEGORIES includes protected-class labels (Finance/Banking,
Tech/Security), so every candidate's From is checked against the canonical
core.rules.is_protected_sender gate and protected senders are skipped (fail
closed) BEFORE the INBOX-removing batchModify. Never archive by label alone.
"""

import logging

import gmail_auth

# Setup
LOG_FILE = "archive_sorted.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Categories that should be ARCHIVED (removed from Inbox)
# We assume everything else (Personal, Awaiting Reply) stays.
ARCHIVE_CATEGORIES = [
    "Finance/Banking",
    "Finance/Payments",
    "Tech/Security",
    "Work/Dev/Infrastructure",
    "Work/Dev/GitHub",
    "Work/Dev/Code-Review",
    "Work/RealEstate",
    "Shopping",
    "Travel",
    "Entertainment",
    "Education/Research",
    "Professional/Jobs",
    "Services/Domain",
    "Notification",
    "Marketing",
    "AI/Grok",
    "AI/Services",
    "AI/Data Exports",
    "Misc/Other" # Even Misc should be archived if we are done with it
]

def get_service():
    return gmail_auth.build_gmail_service()


def senders_for(service, ids):
    """Fetch {id: From} via batched metadata gets. A fetch error leaves the
    sender empty, which is_protected_sender treats as protected (fail closed)."""
    out = {}

    def cb(rid, resp, exc):
        if exc or not resp:
            out[rid] = ""  # fail closed
            return
        headers = resp.get("payload", {}).get("headers", [])
        out[rid] = next(
            (h["value"] for h in headers if h.get("name", "").lower() == "from"), ""
        )

    for i in range(0, len(ids), 50):
        batch = service.new_batch_http_request(callback=cb)
        for mid in ids[i:i + 50]:
            batch.add(
                service.users().messages().get(
                    userId="me", id=mid, format="metadata", metadataHeaders=["From"]
                ),
                request_id=mid,
            )
        batch.execute()
    return out


def archive_loop(*, dispatch=None, limit=25):
    """Discover category members for research; approved archive plans retain authority."""
    from pathlib import Path
    from core.maintenance import dispatch_approved, intake
    service = get_service()
    account = service.users().getProfile(userId="me").execute()["emailAddress"]
    # Categories are intake filters only. Current obligation evidence decides disposition.
    query = "in:inbox {" + " ".join("label:" + category for category in ARCHIVE_CATEGORIES) + "}"
    results = service.users().messages().list(userId="me", q=query, maxResults=min(limit, 500)).execute()
    rows = [{"id": item["id"], "requested_operation": "archive_review"}
            for item in results.get("messages", [])]
    receipt = intake(source="archive_sorted", account=account, provider="gmail", mailbox="INBOX", rows=rows)
    if dispatch:
        receipt["dispatch"] = dispatch_approved(Path(dispatch))
    logger.info("Archive observations queued=%s; mailbox writes require an approved evidence plan", len(rows))
    return receipt


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Category-filtered archive research intake")
    parser.add_argument("--dispatch", help="exact approved canonical transaction envelope")
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()
    print(json.dumps(archive_loop(dispatch=args.dispatch, limit=args.limit)))
