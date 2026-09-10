"""Tests for core.envelope_policy — enforcing the lean, zero-fluff email envelope."""

from __future__ import annotations

import pytest

from core.envelope_policy import (
    EnvelopePolicy,
    EnvelopePolicyError,
    count_paragraphs,
    count_words,
    detect_fluff,
    validate_envelope,
)


CLEAN_ENVELOPE = """Hi Sarah,

Thanks for sending over the revised contract terms yesterday.

We reviewed the proposed timeline and agree with the milestones. Can you confirm the signing date by Thursday?

Best,
Anthony"""

BLOATED_AI_SLOP = """Dear Sarah,

I hope this email finds you well and you are having a wonderful week so far.

I am writing to follow up on our previous conversation regarding the prospective strategic alignment between our respective initiatives. As you may recall, we had previously deliberated upon the multifaceted dimensions of our engagement framework, and I wanted to ensure we are proactively aligned on all deliverables and key performance indicators.

Furthermore, please do not hesitate to reach out if you have any questions or require additional clarifications. I look forward to hearing back from you at your earliest convenience.

Best regards,
Anthony Padavano"""

EXCESSIVE_LENGTH_MESSAGE = """Hi Alex,

""" + ("This is an extensive analysis of the technical specifications for our next deployment. " * 15) + """

Best,
Anthony"""


def test_count_words():
    assert count_words("Hello world! This is a test.") == 6
    assert count_words("") == 0
    assert count_words("   \n\n  ") == 0


def test_count_paragraphs():
    assert count_paragraphs(CLEAN_ENVELOPE) == 4
    assert count_paragraphs("Single paragraph without breaks.") == 1
    assert count_paragraphs("") == 0


def test_detect_fluff():
    fluff = detect_fluff(BLOATED_AI_SLOP)
    assert len(fluff) >= 3
    # Clean envelope should have zero fluff
    assert detect_fluff(CLEAN_ENVELOPE) == []


def test_clean_envelope_passes():
    verdict = validate_envelope(CLEAN_ENVELOPE)
    assert verdict.is_valid is True
    assert len(verdict.violations) == 0
    assert verdict.word_count >= 20
    assert verdict.word_count <= 90
    assert "PASS" in verdict.summary()


def test_bloated_ai_slop_fails():
    verdict = validate_envelope(BLOATED_AI_SLOP)
    assert verdict.is_valid is False
    assert len(verdict.fluff_detected) > 0
    assert any("Opening cliche" in v or "fluff" in v for v in verdict.violations)
    assert verdict.remedy is not None
    assert "FAIL" in verdict.summary()


def test_excessive_length_fails_hard_limit():
    verdict = validate_envelope(EXCESSIVE_LENGTH_MESSAGE)
    assert verdict.is_valid is False
    assert verdict.word_count > 150
    assert any("exceeds hard limit" in v for v in verdict.violations)
    assert "attachment or artifact" in (verdict.remedy or "")


def test_attachment_remedy_suggested():
    # 105 words, no fluff, no attachments
    medium_text = "Hi Alex,\n\n" + ("We need to verify this specific configuration item carefully. " * 12) + "\n\nBest,\nAnthony"
    verdict = validate_envelope(medium_text, attachments=[])
    assert verdict.is_valid is True
    assert any("attachment" in w.lower() for w in verdict.warnings)


def test_custom_policy():
    strict_policy = EnvelopePolicy(hard_max_words=50)
    verdict = validate_envelope(CLEAN_ENVELOPE, policy=strict_policy)
    # CLEAN_ENVELOPE has ~30 words, should pass 50 words limit
    assert verdict.is_valid is True


def test_validate_message_envelope_raises():
    from email.message import EmailMessage
    from mail_send_safety import validate_message_envelope

    msg = EmailMessage()
    msg.set_content(BLOATED_AI_SLOP)

    with pytest.raises(EnvelopePolicyError) as exc_info:
        validate_message_envelope(msg, allow_unbounded=False)
    assert exc_info.value.verdict is not None
    assert exc_info.value.verdict.is_valid is False

    # When allow_unbounded=True, does not raise
    verdict = validate_message_envelope(msg, allow_unbounded=True)
    assert verdict.is_valid is False

