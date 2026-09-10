"""Deterministic Email Envelope Policy for Universal Mail Automation (UMA).

Enforces the Evolved Ideal Form:
  1. Emails are lean transmittal envelopes (target 30-90 words, soft cap 120, hard cap 150).
  2. Zero AI throat-clearing / banned cliches ("hope this email finds you well", etc.).
  3. Depth, data tables, and multi-topic documentation belong in attachments/artifacts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence


DEFAULT_MIN_WORDS = 10
DEFAULT_TARGET_MIN_WORDS = 30
DEFAULT_TARGET_MAX_WORDS = 90
DEFAULT_SOFT_MAX_WORDS = 120
DEFAULT_HARD_MAX_WORDS = 150
DEFAULT_SOFT_MAX_CHARS = 700
DEFAULT_HARD_MAX_CHARS = 800
DEFAULT_MAX_PARAGRAPHS = 4

# Anti-AI-slop patterns (case-insensitive)
BANNED_CLICHE_PATTERNS = [
    (r"\bhope this email finds you well\b", "Opening cliche: 'hope this email finds you well'"),
    (r"\bhope you('re| are) having a (great|wonderful|good) week\b", "Opening cliche: 'hope you are having a great week'"),
    (r"\bi am writing to (inform you|follow up on|touch base)\b", "Throat-clearing: 'I am writing to...'"),
    (r"\bplease do not hesitate to reach out\b", "Boilerplate closing: 'please do not hesitate to reach out'"),
    (r"\bfeel free to reach out\b", "Boilerplate closing: 'feel free to reach out'"),
    (r"\bat your earliest convenience\b", "Indefinite delay cliché: 'at your earliest convenience' (use specific date/time)"),
    (r"\bjust wanted to (check in|touch base)\b", "Filler opener: 'just wanted to check in/touch base'"),
    (r"\ballow me to introduce\b", "Formalistic preamble: 'allow me to introduce'"),
    (r"\bthank you in advance for your cooperation\b", "Presumptuous boilerplate: 'thank you in advance for your cooperation'"),
]


class EnvelopePolicyError(ValueError):
    """Raised when an email body violates the hard envelope bounds."""

    def __init__(self, message: str, *, verdict: Optional["EnvelopeVerdict"] = None) -> None:
        super().__init__(message)
        self.verdict = verdict


@dataclass(frozen=True)
class EnvelopePolicy:
    """Configurable thresholds for email envelope compliance."""

    min_words: int = DEFAULT_MIN_WORDS
    target_min_words: int = DEFAULT_TARGET_MIN_WORDS
    target_max_words: int = DEFAULT_TARGET_MAX_WORDS
    soft_max_words: int = DEFAULT_SOFT_MAX_WORDS
    hard_max_words: int = DEFAULT_HARD_MAX_WORDS
    soft_max_chars: int = DEFAULT_SOFT_MAX_CHARS
    hard_max_chars: int = DEFAULT_HARD_MAX_CHARS
    max_paragraphs: int = DEFAULT_MAX_PARAGRAPHS
    disallow_banned_cliches: bool = True


@dataclass
class EnvelopeVerdict:
    """Audit result of evaluating an email body against the envelope policy."""

    is_valid: bool
    word_count: int
    char_count: int
    paragraph_count: int
    fluff_detected: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)
    remedy: Optional[str] = None

    def summary(self) -> str:
        status = "PASS" if self.is_valid and not self.warnings else ("WARN" if self.is_valid else "FAIL")
        parts = [f"[{status}] Envelope: {self.word_count}w/{self.char_count}c in {self.paragraph_count}p"]
        if self.violations:
            parts.append(f"Violations: {'; '.join(self.violations)}")
        if self.warnings:
            parts.append(f"Warnings: {'; '.join(self.warnings)}")
        if self.remedy:
            parts.append(f"Remedy: {self.remedy}")
        return " | ".join(parts)


def count_words(text: str) -> int:
    """Return the count of natural language words, excluding whitespace and punctuation."""
    if not text:
        return 0
    words = re.findall(r"\b[\w'-]+\b", text)
    return len(words)


def count_paragraphs(text: str) -> int:
    """Return the count of distinct non-empty paragraphs."""
    if not text or not text.strip():
        return 0
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return len(paras)


def detect_fluff(text: str) -> List[str]:
    """Identify banned AI boilerplate and throat-clearing clichés in text."""
    if not text:
        return []
    matches = []
    for pattern, description in BANNED_CLICHE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            matches.append(description)
    return matches


def validate_envelope(
    body: str,
    *,
    attachments: Optional[Sequence[Any]] = None,
    policy: Optional[EnvelopePolicy] = None,
) -> EnvelopeVerdict:
    """Validate an email body against the Envelope Policy.

    Evaluates:
      - Word count bounds (target 30-90, soft max 120, hard max 150)
      - Character count bounds (soft max 700, hard max 800)
      - Paragraph structure (max 4)
      - Presence of banned AI-slop / throat-clearing cliches
      - Recommends attachments/artifacts if density/length warrants it
    """
    pol = policy or EnvelopePolicy()
    cleaned = (body or "").strip()
    words = count_words(cleaned)
    chars = len(cleaned)
    paras = count_paragraphs(cleaned)

    fluff = detect_fluff(cleaned)
    warnings: List[str] = []
    violations: List[str] = []
    remedies: List[str] = []

    # Hard violations
    if words > pol.hard_max_words:
        violations.append(
            f"Word count ({words}) exceeds hard limit of {pol.hard_max_words} words."
        )
        remedies.append(
            f"Move comprehensive details ({words - pol.target_max_words}+ extra words) into an attachment or artifact, and keep the email body under {pol.target_max_words} words."
        )

    if chars > pol.hard_max_chars:
        violations.append(
            f"Character count ({chars}) exceeds hard limit of {pol.hard_max_chars} chars."
        )

    if paras > pol.max_paragraphs:
        violations.append(
            f"Paragraph count ({paras}) exceeds maximum of {pol.max_paragraphs} paragraphs."
        )

    if pol.disallow_banned_cliches and fluff:
        violations.append(f"Found banned AI fluff clichés: {', '.join(fluff)}.")
        remedies.append("Remove boilerplate opening/closing filler and state the context/action immediately.")

    # Soft warnings
    if pol.soft_max_words < words <= pol.hard_max_words:
        warnings.append(
            f"Word count ({words}) exceeds soft target ({pol.soft_max_words} words). Consider trimming."
        )
    elif words < pol.target_min_words and words > 0:
        warnings.append(
            f"Word count ({words}) is below target minimum ({pol.target_min_words} words); ensure clear context is provided."
        )

    if pol.soft_max_chars < chars <= pol.hard_max_chars:
        warnings.append(
            f"Character count ({chars}) exceeds soft limit ({pol.soft_max_chars} chars)."
        )

    # If length is high but within limits and there are NO attachments, recommend an attachment
    has_attachments = bool(attachments and len(attachments) > 0)
    if words > pol.target_max_words and not has_attachments and not remedies:
        warnings.append("High-density message without attachments. Consider moving technical or tabular detail to an attachment.")

    is_valid = len(violations) == 0
    remedy = " ".join(remedies) if remedies else None

    return EnvelopeVerdict(
        is_valid=is_valid,
        word_count=words,
        char_count=chars,
        paragraph_count=paras,
        fluff_detected=fluff,
        warnings=warnings,
        violations=violations,
        remedy=remedy,
    )
