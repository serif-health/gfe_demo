"""Runtime configuration, sourced from environment variables (prefix ``GFE_``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("GFE_DB_PATH", PROJECT_ROOT / "data" / "pricing.db"))
    )
    # Base URL used for Bundle.entry.fullUrl and identifier systems.
    fhir_base: str = field(default_factory=lambda: os.getenv("GFE_FHIR_BASE", "http://localhost:8000/fhir"))
    # Provider used when a request does not name one (e.g. a single-facility deployment).
    default_provider_npi: str | None = field(
        default_factory=lambda: os.getenv("GFE_DEFAULT_PROVIDER_NPI", "1520397849") or None
    )
    # Max lines of raw JSON/XML printed per body when trace=full.
    trace_body_lines: int = field(default_factory=lambda: int(os.getenv("GFE_TRACE_BODY_LINES", "80")))
    disclaimer: str = field(
        default_factory=lambda: os.getenv(
            "GFE_DISCLAIMER",
            "Good faith estimate generated from contracted and published pricing data. "
            "For estimation purposes only; not a guarantee of final charges.",
        )
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(settings: Settings) -> None:
    global _settings
    _settings = settings
