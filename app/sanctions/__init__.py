"""OFAC sanctions screening for invoice-ai.

Provides:
    - `refresh_now()` / `load_sdn_entries()` / `list_status()` — disk + IO layer
      against OFAC SDN_ADVANCED.XML + CONS_ADVANCED.XML (one combined view).
    - `screen_invoice(invoice, entries)` — pure fuzzy-match logic that returns
      a `SanctionsVerdict` to attach to a job.

See `plan/now-i-want-to-cozy-wombat.md` and `app/contract.md` for design.
"""

from __future__ import annotations

from . import llm_judge, providers
from .loader import (
    SdnEntry,
    list_status,
    load_sdn_entries,
    refresh_now,
)
from .match import (
    RAPIDFUZZ_AUTO_CONFIRM,
    RAPIDFUZZ_CANDIDATE_THRESHOLD,
    THRESHOLD_HIT,
    THRESHOLD_REVIEW,
    normalize,
    screen_invoice,
    screen_name,
)
from .providers import load_all_entries, refresh_all, status_all

__all__ = [
    "RAPIDFUZZ_AUTO_CONFIRM",
    "RAPIDFUZZ_CANDIDATE_THRESHOLD",
    "SdnEntry",
    "THRESHOLD_HIT",
    "THRESHOLD_REVIEW",
    "list_status",
    "llm_judge",
    "load_all_entries",
    "load_sdn_entries",
    "normalize",
    "providers",
    "refresh_all",
    "refresh_now",
    "screen_invoice",
    "screen_name",
    "status_all",
]
