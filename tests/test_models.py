"""Tests for the Ticket IR and Entity models.

Coverage targets:
- All required fields on Ticket
- All optional fields
- All entity types (discriminated union)
- Severity and status enums
- Validators: UTC coercion, tag deduplication, title stripping
- Convenience methods: is_open, with_status, with_assignee
- Edge cases: empty description, max-length values, unusual unicode
- Adversarial inputs: wrong types, blank titles, invalid literals
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from pydantic import ValidationError

from ticketsync.models import (
    Ticket,
    AccountEntity,
    HostEntity,
    ProcessEntity,
    IpAddressEntity,
    FileEntity,
    UrlEntity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_ticket(**kwargs: object) -> Ticket:
    """Return a minimal valid Ticket, overriding with kwargs."""
    defaults: dict[str, object] = {
        "source_system": "test",
        "source_id": "T-001",
        "title": "A test ticket",
        "severity": "medium",
    }
    defaults.update(kwargs)
    return Ticket(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Required fields
# ---------------------------------------------------------------------------


class TestRequiredFields:
    def test_minimal_ticket_is_valid(self) -> None:
        t = make_ticket()
        assert t.title == "A test ticket"
        assert t.severity == "medium"
        assert t.status == "open"  # default

    def test_id_is_assigned_automatically(self) -> None:
        t = make_ticket()
        assert t.id  # not empty
        # Must be a valid UUID
        uuid.UUID(t.id)

    def test_two_tickets_have_different_ids(self) -> None:
        t1 = make_ticket()
        t2 = make_ticket()
        assert t1.id != t2.id

    def test_missing_source_system_raises(self) -> None:
        with pytest.raises(ValidationError):
            Ticket(source_id="x", title="x", severity="low")  # type: ignore[call-arg]

    def test_missing_source_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            Ticket(source_system="x", title="x", severity="low")  # type: ignore[call-arg]

    def test_missing_title_raises(self) -> None:
        with pytest.raises(ValidationError):
            Ticket(source_system="x", source_id="x", severity="low")  # type: ignore[call-arg]

    def test_missing_severity_raises(self) -> None:
        with pytest.raises(ValidationError):
            Ticket(source_system="x", source_id="x", title="x")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Severity levels
# ---------------------------------------------------------------------------


class TestSeverity:
    @pytest.mark.parametrize(
        "level",
        ["critical", "high", "medium", "low", "informational"],
    )
    def test_all_severity_levels_accepted(self, level: str) -> None:
        t = make_ticket(severity=level)
        assert t.severity == level

    def test_invalid_severity_raises(self) -> None:
        with pytest.raises(ValidationError):
            make_ticket(severity="urgent")

    def test_severity_case_sensitive(self) -> None:
        with pytest.raises(ValidationError):
            make_ticket(severity="Critical")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatus:
    @pytest.mark.parametrize(
        "status",
        ["open", "in_progress", "resolved", "closed"],
    )
    def test_all_statuses_accepted(self, status: str) -> None:
        t = make_ticket(status=status)
        assert t.status == status

    def test_default_status_is_open(self) -> None:
        t = make_ticket()
        assert t.status == "open"

    def test_invalid_status_raises(self) -> None:
        with pytest.raises(ValidationError):
            make_ticket(status="pending")


# ---------------------------------------------------------------------------
# Title validator
# ---------------------------------------------------------------------------


class TestTitleValidator:
    def test_title_is_stripped(self) -> None:
        t = make_ticket(title="  hello  ")
        assert t.title == "hello"

    def test_blank_title_raises(self) -> None:
        with pytest.raises(ValidationError):
            make_ticket(title="   ")

    def test_empty_title_raises(self) -> None:
        with pytest.raises(ValidationError):
            make_ticket(title="")

    def test_title_with_unicode(self) -> None:
        t = make_ticket(title="CPU超过95%阈值")
        assert "CPU" in t.title

    def test_title_with_newline_stripped(self) -> None:
        # Newline is not stripped by strip(), only leading/trailing spaces
        # Just ensure it doesn't error
        t = make_ticket(title="line one\nline two")
        assert "line one" in t.title

    def test_title_non_string_raises(self) -> None:
        with pytest.raises(ValidationError):
            make_ticket(title=123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tag deduplication
# ---------------------------------------------------------------------------


class TestTagDeduplication:
    def test_duplicate_tags_are_removed(self) -> None:
        t = make_ticket(tags=["alpha", "beta", "alpha", "gamma", "beta"])
        assert t.tags == ["alpha", "beta", "gamma"]

    def test_tags_preserve_order(self) -> None:
        t = make_ticket(tags=["z", "a", "m"])
        assert t.tags == ["z", "a", "m"]

    def test_empty_tags_allowed(self) -> None:
        t = make_ticket(tags=[])
        assert t.tags == []

    def test_non_string_tag_raises(self) -> None:
        with pytest.raises(ValidationError):
            make_ticket(tags=[1, 2, 3])  # type: ignore[arg-type]

    def test_tags_not_a_list_raises(self) -> None:
        with pytest.raises(ValidationError):
            make_ticket(tags="alpha,beta")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Datetime coercion
# ---------------------------------------------------------------------------


class TestDatetimeCoercion:
    def test_naive_datetime_gets_utc(self) -> None:
        naive = datetime(2026, 1, 1, 12, 0, 0)
        t = make_ticket(created_at=naive)
        assert t.created_at.tzinfo is not None
        assert t.created_at.tzinfo == timezone.utc

    def test_aware_datetime_preserved(self) -> None:
        aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t = make_ticket(created_at=aware)
        assert t.created_at == aware

    def test_iso_string_parsed(self) -> None:
        t = make_ticket(created_at="2026-03-15T10:30:00Z")
        assert t.created_at.year == 2026
        assert t.created_at.month == 3

    def test_iso_string_naive_gets_utc(self) -> None:
        t = make_ticket(created_at="2026-03-15T10:30:00")
        assert t.created_at.tzinfo == timezone.utc

    def test_invalid_datetime_raises(self) -> None:
        with pytest.raises(ValidationError):
            make_ticket(created_at=3.14)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Optional fields
# ---------------------------------------------------------------------------


class TestOptionalFields:
    def test_description_defaults_to_empty_string(self) -> None:
        t = make_ticket()
        assert t.description == ""

    def test_category_defaults_to_empty_string(self) -> None:
        t = make_ticket()
        assert t.category == ""

    def test_remediation_steps_defaults_to_empty(self) -> None:
        t = make_ticket()
        assert t.remediation_steps == ""

    def test_external_url_defaults_to_empty(self) -> None:
        t = make_ticket()
        assert t.external_url == ""

    def test_assignee_defaults_to_empty(self) -> None:
        t = make_ticket()
        assert t.assignee == ""

    def test_raw_defaults_to_empty_dict(self) -> None:
        t = make_ticket()
        assert t.raw == {}

    def test_entities_defaults_to_empty_list(self) -> None:
        t = make_ticket()
        assert t.entities == []

    def test_description_round_trips(self) -> None:
        t = make_ticket(description="very long\n\ndescription")
        assert t.description == "very long\n\ndescription"

    def test_raw_preserves_arbitrary_data(self) -> None:
        payload: dict[str, object] = {"vendor_key": "vendor_value", "count": 99}
        t = make_ticket(raw=payload)
        assert t.raw["vendor_key"] == "vendor_value"
        assert t.raw["count"] == 99


# ---------------------------------------------------------------------------
# Entity types
# ---------------------------------------------------------------------------


class TestEntityTypes:
    def test_account_entity(self) -> None:
        e = AccountEntity(uid="123456789012", name="prod-account", type="aws")
        assert e.kind == "account"
        assert e.uid == "123456789012"

    def test_host_entity(self) -> None:
        e = HostEntity(hostname="prod-web-01.example.com", ip="10.0.0.1", os="linux")
        assert e.kind == "host"
        assert e.hostname == "prod-web-01.example.com"

    def test_process_entity(self) -> None:
        e = ProcessEntity(pid=1234, name="nginx", cmd_line="/usr/sbin/nginx -g daemon off")
        assert e.kind == "process"
        assert e.pid == 1234

    def test_ip_address_entity(self) -> None:
        e = IpAddressEntity(ip="192.168.1.1", version="v4")
        assert e.kind == "ip_address"

    def test_ip_address_entity_v6(self) -> None:
        e = IpAddressEntity(ip="::1", version="v6")
        assert e.version == "v6"

    def test_file_entity(self) -> None:
        e = FileEntity(path="/etc/passwd")
        assert e.kind == "file"
        assert e.path == "/etc/passwd"

    def test_url_entity(self) -> None:
        e = UrlEntity(url="https://malicious.example.com/payload", domain="malicious.example.com")
        assert e.kind == "url"

    def test_entities_embedded_in_ticket(self) -> None:
        host = HostEntity(hostname="prod-db-01")
        ip = IpAddressEntity(ip="10.0.0.5")
        t = make_ticket(entities=[host, ip])
        assert len(t.entities) == 2
        kinds = {e.kind for e in t.entities}  # type: ignore[union-attr]
        assert kinds == {"host", "ip_address"}

    def test_entities_discriminated_union_serialized(self) -> None:
        host = HostEntity(hostname="myhost")
        t = make_ticket(entities=[host])
        data = t.model_dump()
        entity_data = data["entities"][0]
        assert entity_data["kind"] == "host"
        assert entity_data["hostname"] == "myhost"

    def test_entities_round_trip_via_json(self) -> None:
        orig = make_ticket(entities=[
            AccountEntity(uid="111", name="dev"),
            FileEntity(path="/tmp/evil.sh"),
        ])
        serialized = orig.model_dump_json()
        restored = Ticket.model_validate_json(serialized)
        assert len(restored.entities) == 2
        kinds = {e.kind for e in restored.entities}  # type: ignore[union-attr]
        assert "account" in kinds
        assert "file" in kinds


# ---------------------------------------------------------------------------
# Convenience methods
# ---------------------------------------------------------------------------


class TestConvenienceMethods:
    def test_is_open_for_open_ticket(self) -> None:
        t = make_ticket(status="open")
        assert t.is_open()

    def test_is_open_for_in_progress(self) -> None:
        t = make_ticket(status="in_progress")
        assert t.is_open()

    def test_is_open_false_for_resolved(self) -> None:
        t = make_ticket(status="resolved")
        assert not t.is_open()

    def test_is_open_false_for_closed(self) -> None:
        t = make_ticket(status="closed")
        assert not t.is_open()

    def test_with_status_returns_new_object(self) -> None:
        t = make_ticket(status="open")
        t2 = t.with_status("resolved")
        assert t.status == "open"  # original unchanged
        assert t2.status == "resolved"

    def test_with_status_preserves_id(self) -> None:
        t = make_ticket()
        t2 = t.with_status("closed")
        assert t.id == t2.id

    def test_with_assignee_returns_new_object(self) -> None:
        t = make_ticket()
        t2 = t.with_assignee("alice@example.com")
        assert t.assignee == ""
        assert t2.assignee == "alice@example.com"


# ---------------------------------------------------------------------------
# Serialization round-trips
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_model_dump_and_validate(self) -> None:
        original = make_ticket(
            description="desc",
            severity="critical",
            tags=["alpha", "beta"],
        )
        dumped = original.model_dump()
        restored = Ticket.model_validate(dumped)
        assert restored.title == original.title
        assert restored.severity == original.severity
        assert restored.tags == original.tags

    def test_json_round_trip(self) -> None:
        original = make_ticket(severity="high", status="in_progress")
        json_str = original.model_dump_json()
        restored = Ticket.model_validate_json(json_str)
        assert restored.id == original.id
        assert restored.severity == original.severity
        assert restored.status == original.status

    def test_json_round_trip_with_all_entity_types(self) -> None:
        t = Ticket(
            source_system="test",
            source_id="all-entities",
            title="All entity types",
            severity="medium",
            entities=[
                AccountEntity(uid="acc1"),
                HostEntity(hostname="h1"),
                ProcessEntity(pid=99),
                IpAddressEntity(ip="1.2.3.4"),
                FileEntity(path="/x"),
                UrlEntity(url="https://x.com"),
            ],
        )
        restored = Ticket.model_validate_json(t.model_dump_json())
        assert len(restored.entities) == 6
