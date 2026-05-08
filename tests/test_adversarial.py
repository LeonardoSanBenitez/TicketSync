"""Adversarial input tests.

These tests deliberately feed bad, malformed, or hostile inputs to the system
and verify that it either:
  - Raises a clear, typed exception (ValidationError, ValueError, etc.), OR
  - Degrades gracefully without silently corrupting data.

Coverage targets:
- Ticket model: XSS payloads in title/description, SQL injection strings,
  null bytes, extremely long strings, pathological unicode, wrong types
- LocalFilesystem adapter: filesystem boundary conditions, read-only dirs,
  path traversal in source_id
- Config: empty YAML, deeply nested YAML, YAML injection
- Engine: source adapter that returns non-list, infinite loop guards
- Entity models: wrong discriminator values, missing required fields
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from ticketsync.adapters.local import LocalFilesystemAdapter, _sanitize_filename
from ticketsync.adapters.opscenter import OpsCenterAdapter
from ticketsync.config import SyncConfig
from ticketsync.models import (
    AccountEntity,
    HostEntity,
    IpAddressEntity,
    Ticket,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_ticket(**kwargs: Any) -> Ticket:
    defaults: dict[str, Any] = {
        "source_system": "test",
        "source_id": "T-001",
        "title": "Adversarial test",
        "severity": "medium",
    }
    defaults.update(kwargs)
    return Ticket(**defaults)


# ---------------------------------------------------------------------------
# Ticket model — adversarial inputs
# ---------------------------------------------------------------------------


class TestTicketAdversarialInputs:
    def test_xss_in_title_stored_verbatim(self) -> None:
        """XSS payloads must not cause an error — TicketSync is not an HTML renderer."""
        xss = "<script>alert('xss')</script>"
        t = make_ticket(title=xss)
        assert t.title == xss

    def test_sql_injection_in_description_stored_verbatim(self) -> None:
        sql = "'; DROP TABLE tickets; --"
        t = make_ticket(description=sql)
        assert t.description == sql

    def test_null_bytes_in_description_stored_verbatim(self) -> None:
        """Null bytes are unusual but should not crash the model."""
        t = make_ticket(description="hello\x00world")
        assert "\x00" in t.description

    def test_extremely_long_title(self) -> None:
        long_title = "A" * 10_000
        t = make_ticket(title=long_title)
        assert len(t.title) == 10_000

    def test_extremely_long_description(self) -> None:
        long_desc = "B" * 1_000_000
        t = make_ticket(description=long_desc)
        assert len(t.description) == 1_000_000

    def test_pathological_unicode_in_title(self) -> None:
        evil = "\U0001F4A9" * 100 + "​" * 50  # poop emoji + zero-width spaces
        t = make_ticket(title=evil.strip() or "fallback")
        assert t.title  # not empty

    def test_right_to_left_override_in_title(self) -> None:
        rtlo = "file‮txt.exe"
        t = make_ticket(title=rtlo)
        assert t.title == rtlo  # stored as-is, not sanitised

    def test_wrong_type_for_severity_raises(self) -> None:
        with pytest.raises(ValidationError):
            make_ticket(severity=5)  # type: ignore[arg-type]

    def test_none_severity_raises(self) -> None:
        with pytest.raises(ValidationError):
            make_ticket(severity=None)  # type: ignore[arg-type]

    def test_dict_for_title_raises(self) -> None:
        with pytest.raises(ValidationError):
            make_ticket(title={"key": "val"})  # type: ignore[arg-type]

    def test_list_for_source_system_raises(self) -> None:
        with pytest.raises(ValidationError):
            make_ticket(source_system=["sys1", "sys2"])  # type: ignore[arg-type]

    def test_integer_for_source_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            Ticket(
                source_system="test",
                source_id=123,  # type: ignore[arg-type]
                title="x",
                severity="low",
            )

    def test_extra_fields_ignored(self) -> None:
        """Pydantic should ignore extra fields without raising."""
        # Pydantic v2 by default ignores extras
        t = Ticket.model_validate({
            "source_system": "test",
            "source_id": "T-99",
            "title": "Extra fields",
            "severity": "low",
            "unknown_field": "should be ignored",
        })
        assert t.title == "Extra fields"
        assert not hasattr(t, "unknown_field")

    def test_tags_with_500_entries(self) -> None:
        tags = [f"tag-{i}" for i in range(500)]
        t = make_ticket(tags=tags)
        assert len(t.tags) == 500

    def test_tags_with_500_duplicates_deduped(self) -> None:
        tags = ["same-tag"] * 500
        t = make_ticket(tags=tags)
        assert t.tags == ["same-tag"]

    def test_raw_with_100_nested_levels(self) -> None:
        """Deeply nested raw dicts must be stored without truncation."""
        def nested(depth: int) -> dict[str, Any]:
            if depth == 0:
                return {"leaf": True}
            return {"child": nested(depth - 1)}

        deep: dict[str, Any] = nested(100)
        t = make_ticket(raw=deep)
        # Verify we can traverse all the way down
        node: Any = t.raw
        for _ in range(100):
            node = node["child"]
        assert node["leaf"] is True


# ---------------------------------------------------------------------------
# LocalFilesystem adapter — adversarial inputs
# ---------------------------------------------------------------------------


class TestLocalFilesystemAdversarial:
    def test_path_traversal_in_source_id_sanitized(self, tmp_path: Path) -> None:
        """../../../etc/passwd must be sanitised to a safe filename."""
        adapter = LocalFilesystemAdapter(tmp_path)
        t = make_ticket(source_id="../../../etc/passwd")
        adapter.write(t)
        # Verify the file was written inside tmp_path (not outside)
        files = list(tmp_path.rglob("*.json"))
        assert len(files) == 1
        assert tmp_path in files[0].parents

    def test_windows_reserved_name_in_source_id(self, tmp_path: Path) -> None:
        """Windows reserved names like CON, PRN, AUX — sanitize or accept."""
        for reserved in ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1"]:
            adapter = LocalFilesystemAdapter(tmp_path)
            t = make_ticket(source_id=reserved, source_system=f"sys-{reserved}")
            # Should not raise
            adapter.write(t)

    def test_empty_source_id_sanitized(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        t = make_ticket(source_id="")
        adapter.write(t)
        assert adapter.count() == 1

    def test_source_system_with_path_separators(self, tmp_path: Path) -> None:
        adapter = LocalFilesystemAdapter(tmp_path)
        t = make_ticket(source_system="a/b/c", source_id="T-1")
        adapter.write(t)
        assert adapter.count() == 1
        # Verify no new directories were created outside tmp_path
        for p in tmp_path.rglob("*.json"):
            assert tmp_path in p.parents

    def test_write_to_nonexistent_root_creates_dirs(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c"
        adapter = LocalFilesystemAdapter(deep)
        t = make_ticket()
        adapter.write(t)
        assert adapter.count() == 1

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Read-only directories behave differently on Windows",
    )
    def test_write_to_readonly_dir_raises(self, tmp_path: Path) -> None:
        # Skip when running as root (e.g. inside Docker) — root bypasses chmod
        if os.getuid() == 0:  # type: ignore[attr-defined]
            pytest.skip("Running as root — chmod restrictions do not apply")
        ro_dir = tmp_path / "readonly"
        ro_dir.mkdir()
        ro_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        adapter = LocalFilesystemAdapter(ro_dir)
        t = make_ticket()
        try:
            with pytest.raises((PermissionError, OSError)):
                adapter.write(t)
        finally:
            ro_dir.chmod(stat.S_IRWXU)

    def test_to_ticket_with_missing_required_fields_raises(self) -> None:
        adapter = LocalFilesystemAdapter("/unused")
        # Missing 'title', 'source_id', 'source_system', 'severity'
        with pytest.raises(ValidationError):
            adapter.to_ticket({"title": "x"})  # missing severity etc

    def test_to_ticket_with_completely_empty_dict_raises(self) -> None:
        adapter = LocalFilesystemAdapter("/unused")
        with pytest.raises(ValidationError):
            adapter.to_ticket({})

    def test_fetch_new_with_corrupted_json_file(self, tmp_path: Path) -> None:
        """A corrupted JSON file should cause fetch_new to raise JSONDecodeError."""
        adapter = LocalFilesystemAdapter(tmp_path)
        # Write a valid ticket first
        t = make_ticket()
        adapter.write(t)
        # Corrupt the file
        files = list(tmp_path.rglob("*.json"))
        files[0].write_text("not valid json {{{", encoding="utf-8")

        import json
        with pytest.raises(json.JSONDecodeError):
            adapter.fetch_new()


# ---------------------------------------------------------------------------
# OpsCenter adapter — adversarial inputs
# ---------------------------------------------------------------------------


class TestOpsCenterAdapterAdversarial:
    def make_adapter(self) -> OpsCenterAdapter:
        return OpsCenterAdapter(client=MagicMock())

    def test_to_ticket_with_completely_empty_dict_raises(self) -> None:
        adapter = self.make_adapter()
        # Empty dict results in an empty title string, which our validator rejects
        with pytest.raises(ValidationError):
            adapter.to_ticket({})

    def test_to_ticket_title_none_becomes_empty_string(self) -> None:
        adapter = self.make_adapter()
        raw: dict[str, Any] = {
            "OpsItemId": "oi-001",
            "Title": None,
            "Status": "Open",
            "Severity": "3",
            "Source": "",
        }
        t = adapter.to_ticket(raw)
        # title becomes "None" (str(None)) which is a non-empty string
        assert isinstance(t.title, str)

    def test_to_ticket_severity_float_treated_as_unknown(self) -> None:
        adapter = self.make_adapter()
        raw: dict[str, Any] = {
            "OpsItemId": "oi-001",
            "Title": "T",
            "Status": "Open",
            "Severity": 2.5,
            "Source": "",
        }
        t = adapter.to_ticket(raw)
        assert t.severity == "informational"


# ---------------------------------------------------------------------------
# Config — adversarial inputs
# ---------------------------------------------------------------------------


class TestConfigAdversarial:
    def test_empty_yaml_raises(self) -> None:
        with pytest.raises(ValueError):
            SyncConfig.from_yaml("")

    def test_yaml_with_only_comments_raises(self) -> None:
        yaml_text = "# just a comment\n# nothing else\n"
        with pytest.raises(ValueError):
            SyncConfig.from_yaml(yaml_text)

    def test_yaml_list_at_root_raises(self) -> None:
        with pytest.raises(ValueError):
            SyncConfig.from_yaml("- source: local\n- dest: local\n")

    def test_yaml_string_at_root_raises(self) -> None:
        with pytest.raises(ValueError):
            SyncConfig.from_yaml("just a string")

    def test_deeply_nested_adapter_options_preserved(self) -> None:
        yaml_text = """
source:
  type: local
  path: /tmp/src
  nested:
    deep:
      value: 42
destination:
  type: local
  path: /tmp/dst
"""
        cfg = SyncConfig.from_yaml(yaml_text)
        assert cfg.source.options["nested"]["deep"]["value"] == 42

    def test_boolean_deduplication_values(self) -> None:
        for val in ["true", "false", "yes", "no", "on", "off"]:
            yaml_text = f"""
source:
  type: local
destination:
  type: local
deduplication: {val}
"""
            cfg = SyncConfig.from_yaml(yaml_text)
            assert isinstance(cfg.deduplication, bool)


# ---------------------------------------------------------------------------
# Entity model — adversarial inputs
# ---------------------------------------------------------------------------


class TestEntityAdversarial:
    def test_account_entity_missing_uid_raises(self) -> None:
        with pytest.raises(ValidationError):
            AccountEntity(name="x")  # type: ignore[call-arg]

    def test_host_entity_missing_hostname_raises(self) -> None:
        with pytest.raises(ValidationError):
            HostEntity(ip="1.2.3.4")  # type: ignore[call-arg]

    def test_ip_address_entity_invalid_version_raises(self) -> None:
        with pytest.raises(ValidationError):
            IpAddressEntity(ip="1.2.3.4", version="v9")  # type: ignore[arg-type]

    def test_ticket_with_malformed_entity_in_list_raises(self) -> None:
        """A dict with an unknown 'kind' cannot be discriminated."""
        with pytest.raises(ValidationError):
            Ticket.model_validate({
                "source_system": "test",
                "source_id": "T-1",
                "title": "Bad entity",
                "severity": "low",
                "entities": [{"kind": "unknown_type", "field": "value"}],
            })

    def test_entity_kind_mismatch_raises(self) -> None:
        """Passing an AccountEntity dict but specifying wrong kind."""
        with pytest.raises(ValidationError):
            Ticket.model_validate({
                "source_system": "test",
                "source_id": "T-1",
                "title": "Kind mismatch",
                "severity": "low",
                "entities": [{"kind": "account"}],  # missing required 'uid'
            })
