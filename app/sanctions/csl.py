"""Trade.gov Consolidated Screening List (CSL): download, cache, parse.

Covers the BIS + State Dept slices of CSL — BIS Entity List, BIS Denied
Persons List, BIS Unverified List, BIS Military End User list, State DDTC
AECA Debarred parties, State Nonproliferation Sanctions. OFAC slices that
appear in CSL (SDN, SSI, FSE, PLC, etc.) are dropped here because
`app/sanctions/loader.py` already loads them directly from SDN.XML.

Download strategy: ITA publishes a single bulk JSON dump of the whole CSL
at `https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.json`
which is the right shape for offline candidate matching (we run our own
rapidfuzz scoring; the per-row REST search at
`https://api.trade.gov/gateway/v1/consolidated_screening_list/search`
is not suitable for that). The bulk endpoint does not currently require an
API key, but we still gate the refresh on `CSL_API_KEY` being set so the
operator has to opt in explicitly (CSL coverage is only useful when BIS /
State scrutiny is wanted — most users only need OFAC).

Records are exposed as `CslEntry`, a frozen dataclass that is
structurally compatible with `loader.SdnEntry` (same attribute names,
same types) so downstream `match.py` can treat the two lists uniformly.
`treasury_url` is overridden to return the CSL record's
`source_information_url` instead of an OFAC sanctionssearch URL.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app import storage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CSL_BULK_URL = (
    "https://data.trade.gov/downloadable_consolidated_screening_list"
    "/v1/consolidated.json"
)
USER_AGENT = "invoice-ai/0.1 (sanctions-screener)"
DOWNLOAD_TIMEOUT_SEC = 180  # bulk dump is ~10-30MB

# CSL `source` strings we KEEP — non-OFAC slices only.
_SOURCE_LABEL_MAP: dict[str, str] = {
    "Entity List (EL) - Bureau of Industry and Security": "CSL_BIS_ENTITY",
    "Entity List": "CSL_BIS_ENTITY",
    "Denied Persons List (DPL) - Bureau of Industry and Security": "CSL_BIS_DPL",
    "Denied Persons List": "CSL_BIS_DPL",
    "Unverified List (UVL) - Bureau of Industry and Security": "CSL_BIS_UVL",
    "Unverified List": "CSL_BIS_UVL",
    "Military End User (MEU) List - Bureau of Industry and Security": "CSL_BIS_MEU",
    "Military End User": "CSL_BIS_MEU",
    "ITAR Debarred (DTC) - State Department": "CSL_STATE_DDTC",
    "ITAR Debarred (DTC) - Bureau of International Security and Nonproliferation": "CSL_STATE_DDTC",
    "ITAR Debarred": "CSL_STATE_DDTC",
    "AECA Debarred List": "CSL_STATE_DDTC",
    "AECA Debarred": "CSL_STATE_DDTC",
    "Nonproliferation Sanctions (ISN) - State Department": "CSL_STATE_NONPROLIF",
    "Nonproliferation Sanctions (ISN) - Bureau of International Security and Nonproliferation": "CSL_STATE_NONPROLIF",
    "Nonproliferation Sanctions": "CSL_STATE_NONPROLIF",
}

# OFAC-sourced records to drop (already loaded via loader.py).
_OFAC_SOURCE_PREFIXES: tuple[str, ...] = (
    "Specially Designated Nationals",
    "Sectoral Sanctions",
    "Foreign Sanctions Evaders",
    "Non-SDN Palestinian",
    "Non-SDN Iran",
    "Non-SDN Chinese Military",
    "Non-SDN Menu-Based Sanctions",
    "Palestinian Legislative Council",
    "Capta",
)

# CSL `type` field → SdnEntry-compatible label.
_TYPE_LABEL_MAP: dict[str, str] = {
    "Entity": "Entity",
    "Individual": "Individual",
    "Vessel": "Vessel",
    "Aircraft": "Aircraft",
}


def _ofac_dir() -> Path:
    return storage.DATA_ROOT / "ofac"


def _csl_json_path() -> Path:
    return _ofac_dir() / "csl.json"


def _refresh_marker_path() -> Path:
    return _ofac_dir() / "_last_refresh_csl.json"


# ---------------------------------------------------------------------------
# Entry model — structurally compatible with loader.SdnEntry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CslEntry:
    """One CSL list entry, projected to match `SdnEntry`'s attribute surface."""

    uid: int
    primary_name: str
    sdn_type: str  # "Entity" | "Individual" | "Vessel" | "Aircraft" | "Unknown"
    programs: tuple[str, ...]
    aliases: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    address_countries: tuple[str, ...] = field(default_factory=tuple)
    list_date: str | None = None

    # CSL-only extensions (Agent A's extended SdnEntry will also carry these
    # under the same names; structurally compatible). Kept as tuple[...]/dict
    # to satisfy frozen=True.
    source_list: str = ""
    primary_name_native: str | None = None
    aliases_native: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    addresses: tuple[dict, ...] = field(default_factory=tuple)
    ids: tuple[dict, ...] = field(default_factory=tuple)
    relationships: tuple[dict, ...] = field(default_factory=tuple)

    # Private — used by `treasury_url` to return the CSL source page rather
    # than the OFAC sanctionssearch URL. Underscored so it's clearly not
    # part of the SdnEntry shared shape.
    _source_url: str = ""

    @property
    def treasury_url(self) -> str:
        return self._source_url or ""


# ---------------------------------------------------------------------------
# Refresh marker helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_marker() -> dict:
    path = _refresh_marker_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_marker(payload: dict) -> None:
    path = _refresh_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def refresh_csl_now() -> dict:
    """Download the CSL bulk JSON, write it atomically, update the marker.

    Skipped (ok=False, no raise) when `CSL_API_KEY` is not set in env. The
    bulk endpoint itself currently does not enforce a key but we honor the
    env var as the operator's explicit opt-in to BIS / State coverage.

    Returns a status dict suitable for the HTTP response and audit log:
        {ok, fetched_at, list_version, bytes, error?}
    """
    api_key = (os.environ.get("CSL_API_KEY") or "").strip()
    if not api_key:
        payload = {
            "ok": False,
            "fetched_at": _now_iso(),
            "list_version": None,
            "bytes": 0,
            "error": "CSL_API_KEY not set",
        }
        _write_marker(payload)
        return payload

    _ofac_dir().mkdir(parents=True, exist_ok=True)
    target = _csl_json_path()
    tmp = target.with_suffix(target.suffix + ".tmp")

    # API key passed as query param per Trade.gov convention even though the
    # bulk endpoint may not require it. Harmless if ignored.
    url = f"{CSL_BULK_URL}?api_key={urllib.parse.quote(api_key, safe='')}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        payload = {
            "ok": False,
            "fetched_at": _now_iso(),
            "list_version": None,
            "bytes": 0,
            "error": str(exc),
        }
        _write_marker(payload)
        return payload

    # Parse-validate before swapping the file in — refuse to clobber a good
    # cached file with a corrupted download.
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as exc:
        payload = {
            "ok": False,
            "fetched_at": _now_iso(),
            "list_version": None,
            "bytes": len(data),
            "error": f"response is not JSON: {exc}",
        }
        _write_marker(payload)
        return payload

    if not isinstance(obj, dict) or not isinstance(obj.get("results"), list):
        payload = {
            "ok": False,
            "fetched_at": _now_iso(),
            "list_version": None,
            "bytes": len(data),
            "error": "JSON shape unexpected: missing top-level `results` array",
        }
        _write_marker(payload)
        return payload

    tmp.write_bytes(data)
    os.replace(tmp, target)

    _load_csl_entries_cached.cache_clear()

    list_version = None
    sources_meta = obj.get("sources_used") or obj.get("metadata") or {}
    if isinstance(sources_meta, dict):
        # No single authoritative "list_date" on CSL; surface the bulk's
        # publish date if present, else leave None.
        list_version = sources_meta.get("last_updated") or sources_meta.get("import_rate")

    payload = {
        "ok": True,
        "fetched_at": _now_iso(),
        "list_version": list_version,
        "bytes": len(data),
    }
    _write_marker(payload)
    return payload


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def _stable_uid(source: str, entity_number: object, name: str) -> int:
    """Reproducible 31-bit uid keyed on identity-bearing CSL fields."""
    return abs(hash((source, entity_number, name))) % (2**31)


def _classify_source(source: str) -> str | None:
    """Return the prefixed label, or None if this source must be skipped."""
    if not source:
        return None
    if source.startswith(_OFAC_SOURCE_PREFIXES):
        return None
    # Exact-match the curated map first, then loose contains-match as backup.
    label = _SOURCE_LABEL_MAP.get(source)
    if label:
        return label
    for needle, mapped in _SOURCE_LABEL_MAP.items():
        if needle and needle in source:
            return mapped
    return None


def _project_aliases(raw: dict) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    primary = (raw.get("name") or "").strip()
    if primary:
        seen.add(primary.casefold())
    for alt in raw.get("alt_names") or []:
        alt = (alt or "").strip()
        if not alt:
            continue
        key = alt.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append((alt, "Strong"))
    return tuple(out)


def _project_addresses(raw: dict) -> tuple[tuple[dict, ...], tuple[str, ...]]:
    """Return (addresses, deduped countries)."""
    addrs: list[dict] = []
    countries: list[str] = []
    seen_countries: set[str] = set()
    for a in raw.get("addresses") or []:
        if not isinstance(a, dict):
            continue
        rec = {
            "city": (a.get("city") or "").strip() or None,
            "state": (a.get("state") or "").strip() or None,
            "postal": (a.get("postal_code") or a.get("postal") or "").strip() or None,
            "country": (a.get("country") or "").strip() or None,
        }
        addrs.append(rec)
        country = rec["country"]
        if country:
            key = country.casefold()
            if key not in seen_countries:
                seen_countries.add(key)
                countries.append(country)
    return tuple(addrs), tuple(countries)


def _project_ids(raw: dict) -> tuple[dict, ...]:
    out: list[dict] = []
    for i in raw.get("ids") or []:
        if not isinstance(i, dict):
            continue
        out.append({
            "type": (i.get("type") or "").strip() or None,
            "number": (i.get("number") or "").strip() or None,
            "country": (i.get("country") or "").strip() or None,
            "is_legitimate": True,
        })
    return tuple(out)


def _project_programs(raw: dict) -> tuple[str, ...]:
    progs = raw.get("programs") or []
    return tuple(str(p).strip() for p in progs if str(p).strip())


def _parse_json(json_path: Path) -> list[CslEntry]:
    raw_bytes = json_path.read_bytes()
    obj = json.loads(raw_bytes)
    results = obj.get("results") or []

    out: list[CslEntry] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        source = (r.get("source") or "").strip()
        label = _classify_source(source)
        if not label:
            continue
        primary = (r.get("name") or "").strip()
        if not primary:
            continue
        entity_number = r.get("entity_number") or r.get("id") or ""
        sdn_type = _TYPE_LABEL_MAP.get((r.get("type") or "").strip(), "Unknown")
        addresses, countries = _project_addresses(r)
        out.append(
            CslEntry(
                uid=_stable_uid(source, entity_number, primary),
                primary_name=primary,
                sdn_type=sdn_type,
                programs=_project_programs(r),
                aliases=_project_aliases(r),
                address_countries=countries,
                list_date=(r.get("start_date") or None),
                source_list=label,
                primary_name_native=None,
                aliases_native=(),
                addresses=addresses,
                ids=_project_ids(r),
                relationships=(),
                _source_url=(r.get("source_information_url") or r.get("source_list_url") or ""),
            )
        )
    return out


@functools.lru_cache(maxsize=2)
def _load_csl_entries_cached(mtime_ns: int) -> tuple[CslEntry, ...]:
    _ = mtime_ns  # cache key only
    return tuple(_parse_json(_csl_json_path()))


def load_csl_entries() -> list[CslEntry]:
    """Return parsed CSL entries (non-OFAC slices only), or `[]` if absent."""
    path = _csl_json_path()
    if not path.exists():
        return []
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return []
    try:
        return list(_load_csl_entries_cached(mtime_ns))
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to parse CSL JSON: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Status (for /api/sanctions/status)
# ---------------------------------------------------------------------------


def csl_status() -> dict:
    """Describe the on-disk CSL file for the frontend freshness widget."""
    path = _csl_json_path()
    marker = _read_marker()
    if not path.exists():
        return {
            "available": False,
            "list_version": marker.get("list_version"),
            "fetched_at": marker.get("fetched_at"),
            "last_error": marker.get("error"),
            "entry_count": 0,
            "bytes": 0,
            "age_seconds": None,
        }
    try:
        st = path.stat()
    except OSError:
        return {
            "available": False,
            "list_version": marker.get("list_version"),
            "fetched_at": marker.get("fetched_at"),
            "last_error": "stat failed",
            "entry_count": 0,
            "bytes": 0,
            "age_seconds": None,
        }
    try:
        entry_count = len(load_csl_entries())
    except Exception:  # noqa: BLE001
        entry_count = 0
    return {
        "available": True,
        "list_version": marker.get("list_version"),
        "fetched_at": marker.get("fetched_at"),
        "last_error": marker.get("error"),
        "entry_count": entry_count,
        "bytes": st.st_size,
        "age_seconds": int(time.time() - st.st_mtime),
    }
