"""TicketSync configuration loading.

Configuration is expressed in YAML and maps to typed Pydantic models.
The library does not read environment variables or files on its own — callers
pass the config dict (or a file path) to ``SyncConfig.from_yaml``.

Shape of a valid config file::

    source:
      type: local           # adapter registry key
      path: /tmp/src        # adapter-specific options

    destination:
      type: local
      path: /tmp/dst

    sync_mode: pull         # only 'pull' is supported in v0.2.0
    deduplication: true     # skip tickets already written (by source_id)
    lookback_hours: 24      # how far back fetch_new should look on first run

Adapter-specific options are passed verbatim to the adapter constructor as
``**kwargs``.  Unknown keys are silently forwarded so that adapters can evolve
without changing the config schema.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class AdapterConfig(BaseModel):
    """Configuration block for a single adapter (source or destination)."""

    type: str = Field(..., description="Adapter registry key, e.g. 'local'")
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Adapter-specific keyword arguments",
    )

    @model_validator(mode="before")
    @classmethod
    def extract_options(cls, values: Any) -> Any:
        """Pop ``type`` and treat every other key as an ``options`` entry."""
        if isinstance(values, dict):
            known = {"type", "options"}
            extra = {k: v for k, v in values.items() if k not in known}
            base = {k: v for k, v in values.items() if k in known}
            if extra:
                existing_opts: dict[str, Any] = base.get("options") or {}
                base["options"] = {**existing_opts, **extra}
            return base
        return values


class SyncConfig(BaseModel):
    """Top-level TicketSync configuration."""

    source: AdapterConfig
    destination: AdapterConfig
    sync_mode: Literal["pull"] = Field(
        "pull",
        description="Sync mode.  Only 'pull' is supported in v0.2.0.",
    )
    deduplication: bool = Field(
        True,
        description=(
            "If True, the engine will not write a ticket whose source_id "
            "has already been written to the destination."
        ),
    )
    lookback_hours: int = Field(
        24,
        ge=0,
        description=(
            "How many hours back fetch_new should look when no cursor is "
            "available.  0 means 'fetch everything'."
        ),
    )

    @classmethod
    def from_yaml(cls, source: str | Path | io.IOBase) -> "SyncConfig":
        """Load a ``SyncConfig`` from a YAML string, file path, or file object.

        Parameters
        ----------
        source:
            - A ``str`` containing raw YAML text.
            - A ``pathlib.Path`` (or any ``os.PathLike``) to a YAML file.
            - An open file object (must be readable as text).
        """
        if isinstance(source, (str, bytes)):
            # Heuristic: if it looks like a path on disk, load the file;
            # otherwise treat the string as raw YAML.
            path = Path(source)
            if path.exists() and path.is_file():
                text = path.read_text(encoding="utf-8")
            else:
                text = source if isinstance(source, str) else source.decode()
        elif isinstance(source, Path):
            text = source.read_text(encoding="utf-8")
        else:
            # file-like object
            raw = source.read()
            text = raw if isinstance(raw, str) else raw.decode()

        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError(
                f"YAML config must be a mapping at the top level, got {type(data)}"
            )
        return cls.model_validate(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SyncConfig":
        """Construct a ``SyncConfig`` directly from a Python dict."""
        return cls.model_validate(data)
