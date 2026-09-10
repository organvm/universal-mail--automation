import pytest
import datetime
from unittest.mock import MagicMock, patch

from cli import run_labeler
from core.models import CommMessage, LabelAction
from core.patchbay import DEFAULT_PATCHBAY, PatchCable, SinkType
from core.identity import DEFAULT_DIRECTORY, Entity
from core.config import Config
from providers.base import ProviderCapabilities, ListMessagesResult
from core.models import EmailMessage

class MockProvider:
    name = "mock"
    capabilities = ProviderCapabilities.CATEGORIES

    def __init__(self):
        self.messages = []
        self.details = {}
        self.applied_actions = []

    def list_messages(self, query, limit, page_token=None):
        return ListMessagesResult(messages=self.messages, next_page_token=None)

    def get_message_details(self, msg_id):
        return self.details.get(msg_id)

    def apply_actions(self, actions, audit=None):
        self.applied_actions.extend(actions)
        result = MagicMock()
        result.success_count = len(actions)
        result.error_count = 0
        result.errors = []
        return result


def test_synth_integration():
    provider = MockProvider()
    
    # Mock some emails
    msg1 = EmailMessage(
        id="msg-1",
        sender="partner@company.com",
        subject="Important contract",
        body="Here is the contract.",
        date=datetime.datetime.now(datetime.timezone.utc),
    )
    provider.messages = [msg1]
    provider.details = {"msg-1": msg1}

    # Setup identity
    ent = Entity(
        name="VIP Partner",
        emails={"partner@company.com"},
        is_vip=True,
    )
    DEFAULT_DIRECTORY.register(ent)

    # Setup patch cable
    webhook_hits = []
    def mock_webhook_callback(action, msg):
        webhook_hits.append((action, msg))
        
    cable = PatchCable(
        name="test-alert",
        sink_type=SinkType.CALLBACK,
        destination="mock",
        min_tier=1,
        callback=mock_webhook_callback
    )
    DEFAULT_PATCHBAY.connect(cable)

    try:
        run_labeler(
            provider=provider,
            query="",
            limit=10,
            dry_run=False,
            remove_label=None,
            state_file=None,
            tier_routing=True,
            vip_only=False,
        )
        
        # Verify provider applied actions
        assert len(provider.applied_actions) == 1
        act = provider.applied_actions[0]
        assert act.message_id == "msg-1"
        assert act.star is True
        
        # Verify webhook was fired because it's a Tier 1 VIP
        assert len(webhook_hits) == 1
        cb_action, cb_msg = webhook_hits[0]
        assert cb_action.message_id == "msg-1"
        
    finally:
        DEFAULT_PATCHBAY.disconnect("test-alert")
