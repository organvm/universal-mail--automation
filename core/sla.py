"""
Channel-aware SLA engine for multi-modal communications.

Translates real-time expectation differences between sync channels (SMS, Chat)
and async channels (Email). Applies priority boosts and computes response deadlines.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional


@dataclass(frozen=True)
class ChannelSLARule:
    """SLA parameters for a specific transport channel."""
    channel_id: str
    target_response_minutes: int
    escalate_to_critical_after_minutes: int
    tier_boost: int = 0  # e.g., +1 bumps Tier 2 to Tier 1


CHANNEL_SLAS: Dict[str, ChannelSLARule] = {
    "twilio": ChannelSLARule(
        channel_id="twilio",
        target_response_minutes=15,
        escalate_to_critical_after_minutes=60,
        tier_boost=1,  # SMS is immediate/sync; boost priority
    ),
    "sms": ChannelSLARule(
        channel_id="sms",
        target_response_minutes=15,
        escalate_to_critical_after_minutes=60,
        tier_boost=1,
    ),
    "slack": ChannelSLARule(
        channel_id="slack",
        target_response_minutes=60,
        escalate_to_critical_after_minutes=240,
        tier_boost=0,
    ),
    "discord": ChannelSLARule(
        channel_id="discord",
        target_response_minutes=120,
        escalate_to_critical_after_minutes=480,
        tier_boost=0,
    ),
    "email": ChannelSLARule(
        channel_id="email",
        target_response_minutes=1440,  # 24 hours
        escalate_to_critical_after_minutes=4320,  # 72 hours
        tier_boost=0,
    ),
}


def compute_sla(channel_id: str, base_tier: int, received_at: Optional[datetime] = None) -> tuple[int, datetime]:
    """
    Calculate adjusted priority tier and target deadline based on the channel's SLA.
    Returns (effective_tier, deadline_utc).
    """
    now = received_at or datetime.now(timezone.utc)
    sla = CHANNEL_SLAS.get(channel_id.lower(), CHANNEL_SLAS["email"])

    # Boost tier (lower number = higher priority, capped at Tier 1)
    effective_tier = max(1, base_tier - sla.tier_boost)
    deadline = now + timedelta(minutes=sla.target_response_minutes)

    return effective_tier, deadline
