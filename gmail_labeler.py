"""
Gmail Archive Labeling Automation v2.2 (Multi-Provider Refactor)
Author: Comprehensive Email Organization System
Purpose: Exhaustively label all emails in Gmail using Batch APIs and State Persistence.

This module now imports shared rules and state from the core module, enabling
consistent categorization across multiple email providers.
"""

import os
import re
import time
import json
import logging
import argparse
from collections import defaultdict

from googleapiclient.errors import HttpError

import gmail_auth

# Import shared rules and state from core module
from core.rules import (
    LABEL_RULES,
    categorize_message as _core_categorize,
)
from core.state import StateManager

# ============================================================================
# CONFIGURATION
# ============================================================================

# Gmail API scopes
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# State file for resuming interrupted runs
STATE_FILE = "labeler_state.json"
LOG_FILE = "gmail_labeler.log"

# Performance settings
BATCH_GET_SIZE = 20    # Number of 'get' requests per HTTP batch (tuned to reduce rate limits)
BATCH_MODIFY_SIZE = 1000 # Max IDs per batchModify call (API limit is 1000)
LIST_PAGE_SIZE = 500    # Max messages to list per page (API max 500)
BASE_BACKOFF_SECONDS = 10  # Initial delay when backing off rate limits
SYSTEM_LABELS = []  # Workflow labels are exclusively owned by evidence transactions.

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Note: LABEL_RULES, PRIORITY_LABELS, KEEP_IN_INBOX, and StateManager are now
# imported from core module for shared use across providers.

# ============================================================================ 
# CORE ENGINE
# ============================================================================ 

class GmailLabeler:
    def __init__(self, remove_source_label=None):
        self.service = self._authenticate()
        self.state_manager = StateManager(STATE_FILE)
        self.label_cache = {}
        self.stats = self.state_manager.get_history()
        self.total_processed = self.state_manager.get_total()
        self.remove_source_label = remove_source_label
        self._init_labels()

    def _execute_with_backoff(self, func, description, max_retries=5):
        """Execute an API call with exponential backoff on rate limits."""
        delay = BASE_BACKOFF_SECONDS
        for attempt in range(1, max_retries + 1):
            try:
                return func()
            except HttpError as e:
                message = str(e)
                status = getattr(e.resp, "status", None)
                if status in (403, 429) and any(tag in message for tag in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded")):
                    logger.warning(f"{description} rate limited (attempt {attempt}/{max_retries}); sleeping {delay:.1f}s")
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise RuntimeError(f"{description} failed after {max_retries} retries due to rate limits.")

    def _authenticate(self):
        return gmail_auth.build_gmail_service(scopes=SCOPES)

    def _init_labels(self):
        """Read category identities; label creation is an explicit planned operation."""
        self.account = self.service.users().getProfile(userId="me").execute()["emailAddress"]
        result = self.service.users().labels().list(userId="me").execute()
        self.label_cache = {row["name"]: row["id"] for row in result.get("labels", [])}

    def categorize_message(self, headers):
        """Categorize based on headers using shared core rules."""
        return _core_categorize(headers)

    def process_batch(self, messages):
        """
        1. Fetch details (Batch Get)
        2. Categorize
        3. Apply changes (Batch Modify)
        """
        # 1. Prepare Batch Get
        batch_results = {}
        
        # Chunk the 'get' requests because batch limits exist (safe at 100)
        # However, we only have 500 max messages here. We can do 5 batches of 100.
        
        message_chunks = [messages[i:i + BATCH_GET_SIZE] for i in range(0, len(messages), BATCH_GET_SIZE)]
        
        for chunk in message_chunks:
            failed_ids = []
            def callback(request_id, response, exception):
                if exception:
                    failed_ids.append(request_id)
                    logger.warning(f"Error fetching message {request_id}: {exception}")
                else:
                    batch_results[request_id] = response

            batch_get = self.service.new_batch_http_request(callback=callback)
            for msg in chunk:
                batch_get.add(
                    self.service.users().messages().get(
                        userId="me", id=msg['id'], format="metadata", metadataHeaders=['From', 'Subject']
                    ),
                    request_id=msg['id']
                )
            self._execute_with_backoff(batch_get.execute, "batch get")
            time.sleep(2.0)

            # Retry failed fetches one-by-one with backoff.
            if failed_ids:
                logger.warning(f"Retrying {len(failed_ids)} messages after rate limits")
                for msg_id in failed_ids:
                    try:
                        resp = self._execute_with_backoff(
                            lambda: self.service.users().messages().get(
                                userId="me", id=msg_id, format="metadata", metadataHeaders=['From', 'Subject']
                            ).execute(),
                            f"retry get {msg_id}"
                        )
                        batch_results[msg_id] = resp
                        time.sleep(0.2)
                    except HttpError as e:
                        logger.error(f"Retry failed for message {msg_id}: {e}")
                time.sleep(1.0)

        # Preserve categorization as bounded, reviewable user-label proposals.
        # Inbox, flags, read state and unrelated metadata are never implied by a label.
        from core.maintenance import intake, category_labels
        rows = []
        for msg_id, data in batch_results.items():
            if not data:
                continue
            headers = data.get("payload", {}).get("headers", [])
            label_name = self.categorize_message(headers)
            self.stats[label_name] += 1
            remove = []
            if label_name != "Uncategorized" and "Uncategorized" in self.label_cache:
                remove.append("Uncategorized")
            if self.remove_source_label and label_name != self.remove_source_label:
                remove.append(self.remove_source_label)
            add, remove = category_labels([label_name], remove)
            rows.append({"id": msg_id, "headers": headers, "label": label_name,
                         "requested_operation": "category_labels", "add": add, "remove": remove,
                         "missing_labels": [name for name in add if name not in self.label_cache]})
        if rows:
            outcome = intake(source="gmail_labeler", provider="gmail", account=self.account,
                             mailbox="query:" + getattr(self, "query", "has:nouserlabels"), rows=rows)
            logger.info("Queued %s category proposals for canonical review", outcome["queued"])
        return len(batch_results)

    def run(self, query="has:nouserlabels"):
        logger.info(f"Starting category observation. Query: {query}")
        self.query = query
        
        # Note on Page Tokens: 
        # When processing a queue (e.g., removing labels so they no longer match the query),
        # using page tokens is unreliable because the result set shifts.
        # In this specific case, we prefer to keep asking for the first page if we expect items to disappear.
        # However, to be safe against infinite loops on items we *can't* process, 
        # using a page token is safer, provided we accept we might miss some due to shift.
        # A good hybrid: use page token, but if we process < limit, we might be done?
        # Actually, if we are draining a label (Misc/Other), items WILL be removed.
        # So page tokens are dangerous.
        # We will use page tokens from state ONLY if query is default. 
        # If custom query, we start fresh.
        
        page_token = None
        if query == "has:nouserlabels":
            page_token = self.state_manager.get_token()
        
        start_time = time.time()
        
        try:
            while True:
                # List messages
                results = self._execute_with_backoff(
                    lambda: self.service.users().messages().list(
                        userId="me", 
                        q=query, 
                        maxResults=LIST_PAGE_SIZE, 
                        pageToken=page_token
                    ).execute(),
                    "list messages"
                )
                
                messages = results.get("messages", [])
                
                if not messages:
                    logger.info("No more messages found matching query.")
                    if query == "has:nouserlabels":
                        self.state_manager.save(None, self.total_processed, self.stats)
                    break
                
                count = self.process_batch(messages)
                self.total_processed += count
                
                # Stats & State
                elapsed = time.time() - start_time
                rate = self.total_processed / elapsed if elapsed > 0 else 0
                logger.info(f"Processed batch of {count}. Total: {self.total_processed} (Rate: {rate:.1f} msg/s)")
                
                page_token = results.get("nextPageToken")
                
                if query == "has:nouserlabels":
                    self.state_manager.save(page_token, self.total_processed, self.stats)
                
                # Throttle to respect per-user query limits.
                time.sleep(2.0)
                
                if not page_token:
                    break

        except KeyboardInterrupt:
            logger.warning("Interrupted by user. Saving state...")
            self.state_manager.save(page_token, self.total_processed, self.stats)
        except Exception as e:
            logger.critical(f"Fatal error: {e}", exc_info=True)
            self.state_manager.save(page_token, self.total_processed, self.stats)
        finally:
            logger.info("Run finished.")
            self._print_final_stats()

    def _print_final_stats(self):
        print("\n" + "="*50)
        print("SESSION STATISTICS")
        print("="*50)
        print(f"Total Processed: {self.total_processed}")
        print("Distribution:")
        # Sort by count
        sorted_stats = sorted(self.stats.items(), key=lambda x: x[1], reverse=True)
        for label, count in sorted_stats:
            if count > 0:
                print(f"  {label:<25}: {count}")
        print("="*50 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gmail Labeling Automation")
    parser.add_argument("--query", type=str, default="has:nouserlabels", help="Gmail query to filter messages (default: has:nouserlabels)")
    parser.add_argument("--dispatch", help="exact approved canonical transaction envelope")
    parser.add_argument("--remove-label", type=str, help="Label to remove if a new category is found (e.g., 'Misc/Other')")
    
    args = parser.parse_args()
    
    app = GmailLabeler(remove_source_label=args.remove_label)
    app.run(query=args.query)
    if args.dispatch:
        from pathlib import Path
        from core.maintenance import dispatch_approved
        outcome = dispatch_approved(Path(args.dispatch))
        print(json.dumps(outcome))
        raise SystemExit(0 if outcome["status"] == "completed" else 2)
