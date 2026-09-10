"""
Multi-channel identity resolution for universal communications.

Maps varied transport identifiers (RFC 5322 emails, E.164 phone numbers,
Slack handles, GitHub usernames) to unified Entity records. This enables
fail-closed sender protection and VIP prioritization across non-email channels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Set


def _normalize_phone(raw: str) -> str:
    """Strip non-digits and normalize US NANP 10/11 digit numbers."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


@dataclass
class Entity:
    """A canonical person, service, or institution across all transports."""
    name: str
    emails: Set[str] = field(default_factory=set)
    phones: Set[str] = field(default_factory=set)
    handles: Set[str] = field(default_factory=set)
    is_protected: bool = False
    is_vip: bool = False
    notes: str = ""

    def matches(self, identifier: str) -> bool:
        """Check if any known address, phone number, or handle matches."""
        raw = identifier.strip().lower()
        if raw in {e.lower() for e in self.emails}:
            return True
        norm_phone = _normalize_phone(raw)
        for phone in self.phones:
            if norm_phone and norm_phone == _normalize_phone(phone):
                return True
        if raw in {h.lower() for h in self.handles}:
            return True
        return False


class EntityDirectory:
    """In-memory directory and resolver for multi-channel entities."""

    def __init__(self, entities: Optional[list[Entity]] = None) -> None:
        self._entities: list[Entity] = entities or []
        self._by_email: Dict[str, Entity] = {}
        self._by_phone_digits: Dict[str, Entity] = {}
        self._by_handle: Dict[str, Entity] = {}
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        self._by_email.clear()
        self._by_phone_digits.clear()
        self._by_handle.clear()
        for ent in self._entities:
            for email in ent.emails:
                self._by_email[email.lower()] = ent
            for phone in ent.phones:
                digits = _normalize_phone(phone)
                if digits:
                    self._by_phone_digits[digits] = ent
            for handle in ent.handles:
                self._by_handle[handle.lower()] = ent

    def register(self, entity: Entity) -> None:
        """Register or update an entity."""
        self._entities.append(entity)
        self._rebuild_index()

    def resolve(self, identifier: str) -> Optional[Entity]:
        """Resolve any email, phone number, or handle to a canonical entity."""
        if not identifier:
            return None
        raw = identifier.strip().lower()
        if raw in self._by_email:
            return self._by_email[raw]
        digits = _normalize_phone(raw)
        if digits and digits in self._by_phone_digits:
            return self._by_phone_digits[digits]
        if raw in self._by_handle:
            return self._by_handle[raw]
        return None

    def is_protected(self, identifier: str) -> bool:
        """True if the identifier resolves to an entity flagged as protected."""
        entity = self.resolve(identifier)
        return bool(entity and entity.is_protected)

    def is_vip(self, identifier: str) -> bool:
        """True if the identifier resolves to an entity flagged as VIP."""
        entity = self.resolve(identifier)
        return bool(entity and entity.is_vip)


# Global default directory initialized with standard system entities
DEFAULT_DIRECTORY = EntityDirectory([
    Entity(
        name="Primary Financial Alerts",
        emails={"alerts@chase.com", "customer.service@citi.com"},
        phones={"+18005551234", "+18009556600"},
        handles={"@chase_support"},
        is_protected=True,
        is_vip=True,
        notes="Core banking notifications across SMS and email",
    ),
    Entity(
        name="Government Notices",
        emails={"notice@irs.gov", "clerk@courts.ca.gov"},
        phones={"+18008291040"},
        handles={},
        is_protected=True,
        is_vip=True,
        notes="Official government and legal communications",
    ),
])
