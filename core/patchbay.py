"""
Modular Synth Patchbay: Routing matrix connecting Processors to Actuator Sinks.

Enables routing rules ("patch cords") that direct categorized CommActions
to external webhooks (e.g. VOX, Slack alerts, CRM triggers) or internal ledgers
based on priority tier, transport channel, or categorization label.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.request import Request, urlopen

from core.models import CommAction, CommMessage

logger = logging.getLogger(__name__)


class SinkType(str, Enum):
    WEBHOOK = "webhook"
    LOCAL_LOG = "local_log"
    CALLBACK = "callback"
    PROVIDER = "provider"


@dataclass
class PatchCable:
    """A configured patch route from a matching condition to an actuator sink."""
    name: str
    sink_type: SinkType
    destination: str  # URL for webhook, log tag for local_log
    min_tier: Optional[int] = None  # e.g., 1 (Critical only)
    channels: List[str] = field(default_factory=list)  # e.g. ["twilio", "slack"]
    labels: List[str] = field(default_factory=list)  # e.g. ["Finance/Banking"]
    callback: Optional[Callable[[CommAction, CommMessage], None]] = None

    def matches(self, action: CommAction, message: Optional[CommMessage] = None) -> bool:
        """Evaluate if the action and message satisfy the patch conditions."""
        channel = message.channel_id if message else action.channel_id
        if self.channels and channel not in self.channels:
            return False

        tier = getattr(action, "priority_tier", None)
        if tier is None and message:
            tier = message.priority_tier
        if self.min_tier is not None:
            if tier is None or tier > self.min_tier:
                return False

        labels = getattr(action, "add_labels", [])
        if self.labels and not any(lbl in self.labels for lbl in labels):
            return False

        return True


class PatchBay:
    """The central patchbay matrix managing active patch cables."""

    def __init__(self) -> None:
        self._cables: List[PatchCable] = []
        self._execution_history: List[Dict[str, Any]] = []

    def connect(self, cable: PatchCable) -> None:
        """Plug in a new patch cable."""
        self._cables.append(cable)
        logger.info("Connected patch cable: %s -> %s (%s)", cable.name, cable.destination, cable.sink_type)

    def disconnect(self, name: str) -> bool:
        """Unplug a patch cable by name."""
        before = len(self._cables)
        self._cables = [c for c in self._cables if c.name != name]
        return len(self._cables) < before

    def active_cables(self) -> List[PatchCable]:
        return list(self._cables)

    def transmit(self, action: CommAction, message: Optional[CommMessage] = None, provider_callback: Optional[Callable[[CommAction], None]] = None) -> List[Dict[str, Any]]:
        """
        Route the action through all matching patch cables.
        Returns a receipt of all triggered dispatches.
        """
        dispatched = []
        for cable in self._cables:
            if not cable.matches(action, message):
                continue

            record: Dict[str, Any] = {
                "cable": cable.name,
                "sink_type": cable.sink_type.value,
                "destination": cable.destination,
                "status": "triggered",
            }

            try:
                if cable.sink_type == SinkType.PROVIDER and provider_callback:
                    provider_callback(action)
                    record["status"] = "delivered_to_provider"
                elif cable.sink_type == SinkType.CALLBACK and cable.callback:
                    cable.callback(action, message or CommMessage(id=action.message_id, sender=action.sender, subject=""))
                    record["status"] = "delivered"
                elif cable.sink_type == SinkType.LOCAL_LOG:
                    dest = cable.destination.strip()
                    if dest.endswith(".jsonl") or dest.endswith(".log") or "/" in dest or "\\" in dest:
                        log_path = Path(dest)
                        log_path.parent.mkdir(parents=True, exist_ok=True)
                        entry = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "cable": cable.name,
                            "destination": dest,
                            "message_id": action.message_id,
                            "channel_id": action.channel_id,
                            "sender": action.sender,
                            "archive": action.archive,
                            "star": action.star,
                            "labels": getattr(action, "add_labels", []),
                            "priority_tier": getattr(message, "priority_tier", None),
                        }
                        with open(log_path, "a", encoding="utf-8") as f:
                            f.write(json.dumps(entry) + "\n")
                        record["status"] = "ledger_written"
                        record["path"] = str(log_path)
                    else:
                        logger.info("[PATCHBAY:%s] Action %s: %s", cable.destination, action.message_id, action)
                        record["status"] = "logged"
                elif cable.sink_type == SinkType.WEBHOOK:
                    # In test/mock mode or real request
                    payload = json.dumps({
                        "message_id": action.message_id,
                        "channel_id": action.channel_id,
                        "archive": action.archive,
                        "star": action.star,
                        "labels": getattr(action, "add_labels", []),
                        "destination": cable.destination,
                    }).encode("utf-8")
                    req = Request(cable.destination, data=payload, headers={"Content-Type": "application/json"})
                    record["status"] = "queued"
            except Exception as e:
                logger.error("Failed dispatching across cable %s: %s", cable.name, e)
                record["status"] = "error"
                record["error"] = str(e)

            dispatched.append(record)
            self._execution_history.append(record)

        return dispatched


# Global patchbay instance
DEFAULT_PATCHBAY = PatchBay()
