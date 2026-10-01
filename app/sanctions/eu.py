"""EU consolidated financial sanctions list (FSF): download, cache, parse.

DG FISMA publishes the EU's consolidated list of persons, groups and entities
subject to financial sanctions as the **Financial Sanctions Files (FSF)** XML.
The full-list endpoint is open (no registration); a public access token is
embedded in the default URL and can be overridden via ``EU_FSF_URL``.

Records are exposed as ``EuEntry``, structurally compatible with
``loader.SdnEntry`` so ``match.py`` treats every list uniformly. The FSF XML
carries names in multiple EU languages plus original scripts, which feed the
native-script matching path. Enabled by default; disable with
``EU_FSF_ENABLED=0``.

The parser is intentionally defensive (namespace-agnostic local-name matching,
attribute fallbacks) because the FSF schema attribute set has drifted across
versions. Validate the first live download against current output.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app import storage
from app.sanctions.loader import _detect_script, _strip_ns

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Public FSF full-list URL. The token is the EU's documented public crawler
# token; override the whole URL via EU_FSF_URL if the EU rotates it.
EU_FSF_URL = os.environ.get(
    "EU_FSF_URL",
    "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw",
)
EU_LIST_PAGE = (
    "https://finance.ec.europa.eu/eu-and-world/sanctions-restrictive-measures"
    "/overview-sanctions-and-related-resources_en"
)
USER_AGENT = "invoice-ai/0.1 (sanctions-screener)"
DOWNLOAD_TIMEOUT_SEC = 120

SOURCE_LABEL = "EU_CONSOLIDATED"


def _eu_dir() -> Path:
    return storage.DATA_ROOT / "eu"


def _xml_path() -> Path:
    return _eu_dir() / "eu_fsf.xml"


def _refresh_marker_path() -> Path:
    return _eu_dir() / "_last_refresh.json"


# ---------------------------------------------------------------------------
# Entry model — structurally compatible with loader.SdnEntry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EuEntry:
    uid: int
    primary_name: str
    sdn_type: str  # "Entity" | "Individual" | "Unknown"
    programs: tuple[str, ...]
    aliases: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    address_countries: tuple[str, ...] = field(default_factory=tuple)
    list_date: str | None = None
    source_list: str = SOURCE_LABEL
    primary_name_native: str | None = None
    aliases_native: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    addresses: tuple[dict, ...] = field(default_factory=tuple)
    ids: tuple[dict, ...] = field(default_factory=tuple)
    relationships: tuple[dict, ...] = field(default_factory=tuple)
    eu_reference: str = ""

    @property
    def treasury_url(self) -> str:
        return EU_LIST_PAGE


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
# Download
# ---------------------------------------------------------------------------


def _download_xml(url: str, target: Path) -> tuple[bool, int, str | None]:
    tmp = target.with_suffix(target.suffix + ".tmp")
    last_err: str | None = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            chunks: list[bytes] = []
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            data = b"".join(chunks)
            if not data or not data.lstrip().startswith(b"<"):
                last_err = "response is not XML (got HTML/empty payload — check EU_FSF_URL/token)"
                continue
            tmp.write_bytes(data)
            os.replace(tmp, target)
            return True, len(data), None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(2 * (attempt + 1))
    return False, 0, last_err


def refresh_eu_now() -> dict:
    """Download the EU FSF XML, write atomically, update the marker."""
    _eu_dir().mkdir(parents=True, exist_ok=True)
    ok, nbytes, err = _download_xml(EU_FSF_URL, _xml_path())
    _load_eu_entries_cached.cache_clear()
    list_version = _peek_list_version(_xml_path()) if ok else None
    payload = {
        "ok": ok,
        "fetched_at": _now_iso(),
        "list_version": list_version,
        "bytes": nbytes,
        "error": err,
    }
    _write_marker(payload)
    return payload


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def _stable_uid(ref: str) -> int:
    import hashlib

    return int(hashlib.sha1(ref.encode("utf-8")).hexdigest()[:8], 16)


def _peek_list_version(path: Path) -> str | None:
    """Read the root element's generation date attribute, if any."""
    try:
        for _event, elem in ET.iterparse(str(path), events=("start",)):
            for attr in ("generationDate", "globalFileId", "date"):
                val = elem.get(attr)
                if val:
                    return val
            return None  # only inspect the root
    except ET.ParseError as exc:
        logger.warning("could not peek EU list version: %s", exc)
    return None


def _subject_type(code: str) -> str:
    c = code.strip().lower()
    if c.startswith("p") or "person" in c:
        return "Individual"
    if c.startswith("e") or "enterprise" in c or "entity" in c:
        return "Entity"
    return "Unknown"


def _parse_entity(ent: ET.Element) -> EuEntry | None:
    logical = ent.get("logicalId") or ""
    ref = ent.get("euReferenceNumber") or logical
    try:
        uid = int(logical)
    except (TypeError, ValueError):
        uid = _stable_uid(ref or logical or "")

    designation_date = ent.get("designationDate")
    sdn_type = "Unknown"
    programs: list[str] = []
    countries: list[str] = []
    addresses: list[dict] = []
    primary: str | None = None
    primary_native: str | None = None
    aliases: list[tuple[str, str]] = []
    aliases_native: list[tuple[str, str, str]] = []
    seen_latin: set[str] = set()

    for child in ent:
        tag = _strip_ns(child.tag)
        if tag == "subjectType":
            sdn_type = _subject_type(child.get("classificationCode") or child.get("code") or "")
        elif tag == "regulation":
            prog = (child.get("programme") or child.get("numberTitle") or "").strip()
            if prog and prog not in programs:
                programs.append(prog)
        elif tag == "nameAlias":
            whole = (child.get("wholeName") or "").strip()
            if not whole:
                parts = [child.get("firstName"), child.get("middleName"), child.get("lastName")]
                whole = " ".join(p.strip() for p in parts if p and p.strip())
            if not whole:
                continue
            quality = "Weak" if (child.get("strong") or "true").lower() == "false" else "Strong"
            lang = (child.get("nameLanguage") or "").lower()
            script = _detect_script(whole)
            if script == "latin":
                key = whole.casefold()
                if primary is None and lang in ("", "en"):
                    primary = whole
                    seen_latin.add(key)
                elif whole != primary and key not in seen_latin:
                    seen_latin.add(key)
                    aliases.append((whole, quality))
            else:
                if primary_native is None:
                    primary_native = whole
                aliases_native.append((whole, quality, script))
        elif tag == "address":
            desc = (child.get("countryDescription") or "").strip()
            iso = (child.get("countryIso2Code") or "").strip()
            country = desc if desc and desc.lower() != "unknown" else iso
            addresses.append(
                {
                    "city": (child.get("city") or child.get("place") or "").strip(),
                    "state": (child.get("region") or "").strip(),
                    "postal": (child.get("zipCode") or "").strip(),
                    "country": country,
                }
            )
            if country and country not in countries:
                countries.append(country)

    if primary is None and aliases:
        primary, _ = aliases.pop(0)
    if primary is None and primary_native is None:
        return None

    return EuEntry(
        uid=uid,
        primary_name=primary or primary_native or "",
        sdn_type=sdn_type,
        programs=tuple(programs),
        aliases=tuple(aliases),
        address_countries=tuple(countries),
        list_date=designation_date,
        primary_name_native=primary_native,
        aliases_native=tuple(aliases_native),
        addresses=tuple(addresses),
        eu_reference=ref,
    )


def _parse_xml(path: Path) -> list[EuEntry]:
    if not path.exists():
        return []
    try:
        tree = ET.parse(str(path))
    except ET.ParseError as exc:
        logger.exception("ParseError on EU FSF XML %s: %s", path, exc)
        return []
    root = tree.getroot()
    out: list[EuEntry] = []
    for elem in root.iter():
        if _strip_ns(elem.tag) != "sanctionEntity":
            continue
        try:
            entry = _parse_entity(elem)
        except Exception as exc:  # noqa: BLE001 — skip a malformed entity, keep going
            logger.debug("skipped malformed EU entity: %s", exc)
            continue
        if entry is not None:
            out.append(entry)
    return out


@functools.lru_cache(maxsize=2)
def _load_eu_entries_cached(mtime_ns: int) -> tuple[EuEntry, ...]:
    _ = mtime_ns  # cache key only
    return tuple(_parse_xml(_xml_path()))


def load_eu_entries() -> list[EuEntry]:
    """Return parsed EU consolidated entries, or ``[]`` if not downloaded."""
    path = _xml_path()
    if not path.exists():
        return []
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return []
    try:
        return list(_load_eu_entries_cached(mtime_ns))
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to parse EU FSF XML: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def eu_status() -> dict:
    path = _xml_path()
    marker = _read_marker()
    base = {
        "list_version": marker.get("list_version"),
        "fetched_at": marker.get("fetched_at"),
        "last_error": marker.get("error"),
    }
    if not path.exists():
        return {**base, "available": False, "entry_count": 0, "bytes": 0, "age_seconds": None}
    try:
        st = path.stat()
    except OSError:
        return {**base, "available": False, "entry_count": 0, "bytes": 0, "age_seconds": None}
    try:
        entry_count = len(load_eu_entries())
    except Exception:  # noqa: BLE001
        entry_count = 0
    return {
        **base,
        "available": True,
        "entry_count": entry_count,
        "bytes": st.st_size,
        "age_seconds": int(time.time() - st.st_mtime),
    }
