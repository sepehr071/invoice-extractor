"""Sanctions-list provider registry.

Single source of truth for *which* lists exist and how to load / refresh /
status each. ``load_all_entries``, ``POST /api/sanctions/refresh`` and
``GET /api/sanctions/status`` all drive off ``PROVIDERS`` so adding a list is
one entry here plus a loader module — no edits scattered across match.py and
main.py.

Every provider's ``load()`` returns objects structurally compatible with
``loader.SdnEntry`` (duck-typed by ``match.py``). A provider is *enabled* when
its ``enabled()`` predicate is true; disabled providers are skipped by load and
refresh but still reported (as ``enabled: false``) by status.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable

from app.sanctions import csl, eu, loader, uk

logger = logging.getLogger(__name__)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() not in {"", "0", "false", "no", "off"}


@dataclass(frozen=True)
class Provider:
    key: str                       # source_list label, e.g. "OFAC", "UK_SANCTIONS"
    label: str                     # human-readable name for the UI
    authority: str                 # issuing body, for the UI
    load: Callable[[], list]       # -> list of SdnEntry-compatible records
    refresh: Callable[[], dict]    # downloads, returns status payload
    status: Callable[[], dict]     # on-disk freshness dict
    enabled: Callable[[], bool]    # is this list active?


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        key="OFAC",
        label="OFAC (SDN + Consolidated)",
        authority="US Treasury / OFAC",
        load=loader.load_sdn_entries,
        refresh=loader.refresh_now,
        status=loader.list_status,
        enabled=lambda: True,
    ),
    Provider(
        key="UK_SANCTIONS",
        label="UK Sanctions List",
        authority="UK FCDO / OFSI",
        load=uk.load_uk_entries,
        refresh=uk.refresh_uk_now,
        status=uk.uk_status,
        enabled=lambda: _truthy(os.environ.get("UK_SANCTIONS_ENABLED", "1")),
    ),
    Provider(
        key="EU_CONSOLIDATED",
        label="EU Consolidated List",
        authority="European Commission / DG FISMA",
        load=eu.load_eu_entries,
        refresh=eu.refresh_eu_now,
        status=eu.eu_status,
        enabled=lambda: _truthy(os.environ.get("EU_FSF_ENABLED", "1")),
    ),
    Provider(
        key="US_CSL",
        label="US CSL (BIS / State)",
        authority="US Trade.gov",
        load=csl.load_csl_entries,
        refresh=csl.refresh_csl_now,
        status=csl.csl_status,
        enabled=lambda: bool((os.environ.get("CSL_API_KEY") or "").strip()),
    ),
)


def provider_by_key(key: str) -> Provider | None:
    return next((p for p in PROVIDERS if p.key == key), None)


def load_all_entries() -> list:
    """Union of every *enabled* list's on-disk entries (best-effort)."""
    entries: list = []
    for p in PROVIDERS:
        if not p.enabled():
            continue
        try:
            entries.extend(p.load())
        except Exception:  # noqa: BLE001 — one bad list must not sink screening
            logger.exception("provider %s load failed", p.key)
    return entries


def refresh_all() -> dict:
    """Refresh every enabled provider. ``ok`` is true if at least one succeeded."""
    results: dict[str, dict] = {}
    any_ok = False
    for p in PROVIDERS:
        if not p.enabled():
            results[p.key] = {"ok": False, "skipped": True, "error": "list not enabled"}
            continue
        try:
            r = dict(p.refresh())
        except Exception as exc:  # noqa: BLE001
            logger.exception("provider %s refresh failed", p.key)
            r = {"ok": False, "error": str(exc)}
        results[p.key] = r
        any_ok = any_ok or bool(r.get("ok"))
    return {"ok": any_ok, "lists": results}


def status_all() -> dict:
    """Per-list freshness for the frontend, in registry order."""
    out: list[dict] = []
    for p in PROVIDERS:
        try:
            s = dict(p.status())
        except Exception as exc:  # noqa: BLE001
            logger.exception("provider %s status failed", p.key)
            s = {"available": False, "last_error": str(exc), "entry_count": 0}
        s.update({"key": p.key, "label": p.label, "authority": p.authority, "enabled": p.enabled()})
        out.append(s)
    total = sum(int(s.get("entry_count") or 0) for s in out)
    return {"lists": out, "total_entry_count": total}
