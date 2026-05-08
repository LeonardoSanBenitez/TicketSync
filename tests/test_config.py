"""Tests for SyncConfig YAML loading and validation.

Coverage targets:
- Loading from raw YAML string
- Loading from file path
- Loading from file-like object
- AdapterConfig extra-key extraction
- SyncConfig defaults
- Adversarial YAML (non-mapping root, missing required fields)
- lookback_hours validation (must be >= 0)
- sync_mode validation (only 'pull' accepted in v0.2.0)
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

import pytest

from ticketsync.config import AdapterConfig, SyncConfig


MINIMAL_YAML = """
source:
  type: local
  path: /tmp/src

destination:
  type: local
  path: /tmp/dst
"""

FULL_YAML = """
source:
  type: opscenter
  region: us-east-1
  credentials: env

destination:
  type: local
  path: /tmp/dst

sync_mode: pull
deduplication: false
lookback_hours: 48
"""


class TestAdapterConfig:
    def test_minimal_adapter_config(self) -> None:
        cfg = AdapterConfig(type="local")
        assert cfg.type == "local"
        assert cfg.options == {}

    def test_extra_keys_become_options(self) -> None:
        cfg = AdapterConfig.model_validate({"type": "local", "path": "/tmp/x"})
        assert cfg.options["path"] == "/tmp/x"

    def test_multiple_extra_keys(self) -> None:
        cfg = AdapterConfig.model_validate({
            "type": "opscenter",
            "region": "eu-west-1",
            "credentials": "env",
        })
        assert cfg.options["region"] == "eu-west-1"
        assert cfg.options["credentials"] == "env"

    def test_explicit_options_dict(self) -> None:
        cfg = AdapterConfig(type="local", options={"path": "/x"})
        assert cfg.options["path"] == "/x"

    def test_type_required(self) -> None:
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AdapterConfig.model_validate({"path": "/x"})


class TestSyncConfigFromYaml:
    def test_load_from_string(self) -> None:
        cfg = SyncConfig.from_yaml(MINIMAL_YAML)
        assert cfg.source.type == "local"
        assert cfg.destination.type == "local"

    def test_load_from_file_path(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write(MINIMAL_YAML)
            fpath = Path(f.name)
        try:
            cfg = SyncConfig.from_yaml(fpath)
            assert cfg.source.type == "local"
        finally:
            fpath.unlink()

    def test_load_from_file_object(self) -> None:
        buf = io.StringIO(MINIMAL_YAML)
        cfg = SyncConfig.from_yaml(buf)
        assert cfg.source.type == "local"

    def test_load_full_yaml(self) -> None:
        cfg = SyncConfig.from_yaml(FULL_YAML)
        assert cfg.source.type == "opscenter"
        assert cfg.source.options["region"] == "us-east-1"
        assert cfg.deduplication is False
        assert cfg.lookback_hours == 48

    def test_defaults_applied(self) -> None:
        cfg = SyncConfig.from_yaml(MINIMAL_YAML)
        assert cfg.sync_mode == "pull"
        assert cfg.deduplication is True
        assert cfg.lookback_hours == 24

    def test_adapter_extra_keys_in_options(self) -> None:
        yaml_text = """
source:
  type: local
  path: /tmp/src
destination:
  type: local
  path: /tmp/dst
"""
        cfg = SyncConfig.from_yaml(yaml_text)
        assert cfg.source.options["path"] == "/tmp/src"
        assert cfg.destination.options["path"] == "/tmp/dst"

    def test_non_mapping_root_raises(self) -> None:
        with pytest.raises(ValueError):
            SyncConfig.from_yaml("- item1\n- item2\n")

    def test_missing_source_raises(self) -> None:
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SyncConfig.from_yaml("destination:\n  type: local\n")

    def test_missing_destination_raises(self) -> None:
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SyncConfig.from_yaml("source:\n  type: local\n")

    def test_invalid_sync_mode_raises(self) -> None:
        from pydantic import ValidationError
        yaml_text = MINIMAL_YAML + "\nsync_mode: push\n"
        with pytest.raises(ValidationError):
            SyncConfig.from_yaml(yaml_text)

    def test_negative_lookback_hours_raises(self) -> None:
        from pydantic import ValidationError
        yaml_text = MINIMAL_YAML + "\nlookback_hours: -1\n"
        with pytest.raises(ValidationError):
            SyncConfig.from_yaml(yaml_text)

    def test_zero_lookback_hours_allowed(self) -> None:
        yaml_text = MINIMAL_YAML + "\nlookback_hours: 0\n"
        cfg = SyncConfig.from_yaml(yaml_text)
        assert cfg.lookback_hours == 0

    def test_from_dict(self) -> None:
        data = {
            "source": {"type": "local", "path": "/tmp/a"},
            "destination": {"type": "local", "path": "/tmp/b"},
        }
        cfg = SyncConfig.from_dict(data)
        assert cfg.source.type == "local"

    def test_string_that_looks_like_nonexistent_path(self) -> None:
        # A non-existent path-like string should be treated as raw YAML
        # MINIMAL_YAML itself is not a file path, so it's parsed as YAML
        cfg = SyncConfig.from_yaml(MINIMAL_YAML)
        assert cfg.source.type == "local"
