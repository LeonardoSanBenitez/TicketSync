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

    sync_mode: pull         # only 'pull' supported currently
    dedup_strategy: time-based   # see below
    lookback_hours: 24      # used when dedup_strategy is 'time-based'
    deduplication: true     # in-process dedup (always on); kept for back-compat

Deduplication strategies
------------------------

Three strategies are available via ``dedup_strategy``:

``time-based`` (default, stateless)
    The engine passes a ``since`` cutoff computed from ``lookback_hours`` to
    ``source.fetch_new()``.  No state is written anywhere.  The lookback
    window must be wide enough to tolerate clock skew and missed runs — set
    a margin of a few minutes.

``tag-based`` (write-back to source)
    After a successful write to the destination, the engine calls
    ``source.mark_synced(source_id)`` to tag the source item as synced.
    On the next run, already-synced items are skipped.  Requires the source
    adapter to implement ``mark_synced``; read-only adapters (CloudWatch,
    GuardDuty, Security Hub) raise ``NotImplementedError`` and the engine
    silently skips the write-back without failing the run.

``destination-check`` (lookup before write)
    Before writing each ticket to the destination, the engine calls
    ``dest.find_by_source_coordinates(source_system, source_id)``.  If an
    existing destination record is found, the write is skipped.  This is
    the most reliable cross-run dedup but costs one extra API call per
    ticket.  Requires the destination adapter to implement
    ``find_by_source_coordinates``; if not implemented, the engine falls
    back to always writing.

Adapter-specific options are passed verbatim to the adapter constructor as
``**kwargs``.  Unknown keys are silently forwarded so that adapters can
evolve without changing the config schema.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

# Valid dedup strategy values
DedupStrategy = Literal["time-based", "tag-based", "destination-check"]


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
        description="Sync mode.  Only 'pull' is supported currently.",
    )
    dedup_strategy: DedupStrategy = Field(
        "time-based",
        description=(
            "Deduplication strategy: 'time-based' (fetch window only), "
            "'tag-based' (write-back label/tag to source after sync), or "
            "'destination-check' (lookup before every write)."
        ),
    )
    deduplication: bool = Field(
        True,
        description=(
            "If True, the engine will not write a ticket whose source_id "
            "has already been written to the destination within this run. "
            "This in-process dedup is always active regardless of "
            "dedup_strategy."
        ),
    )
    lookback_hours: int = Field(
        24,
        ge=0,
        description=(
            "How many hours back fetch_new should look when dedup_strategy "
            "is 'time-based'.  0 means 'fetch everything'."
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
