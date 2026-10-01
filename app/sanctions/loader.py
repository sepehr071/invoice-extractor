"""OFAC sanctions list: download SDN_ADVANCED.XML + CONS_ADVANCED.XML, parse to `SdnEntry`.

Two sources (same schema, disjoint populations):
  - SDN  = blocked persons (https://.../SDN_ADVANCED.XML)
  - CONS = non-SDN consolidated (https://.../CONS_ADVANCED.XML) — SSI Russia,
          NS-PLC, NS-CMIC, FSE Iran/Syria, IRAN-EO13599, IFSR 561

Both files share the ADVANCED_XML schema. `load_sdn_entries()` returns the
UNION, each entry tagged via `source_list`. Refresh is manual via
`POST /api/sanctions/refresh`.
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
from typing import Iterable

from app import storage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SDN_XML_URL = (
    "https://sanctionslistservice.ofac.treas.gov"
    "/api/PublicationPreview/exports/SDN_ADVANCED.XML"
)
CONS_XML_URL = (
    "https://sanctionslistservice.ofac.treas.gov"
    "/api/PublicationPreview/exports/CONS_ADVANCED.XML"
)
USER_AGENT = "invoice-ai/0.1 (sanctions-screener)"
DOWNLOAD_TIMEOUT_SEC = 120

# OFAC PartySubTypeID → label. Stable across publications; verified against
# OFAC ref-value sets. Unknown subtype → "Unknown".
_PARTY_SUBTYPE_LABEL: dict[str, str] = {
    "1": "Vessel",
    "2": "Aircraft",
    "3": "Entity",
    "4": "Individual",
}

# OFAC ScriptID hardcoded fallback — only used when the XML's `ScriptValues`
# ref-set is missing. Real IDs come from `_build_refmaps(...)["ScriptValues"]`.
# Verified against May 2026 published XML: 215=Latin, 220=Cyrillic,
# 501=Chinese Simplified, 502=Chinese Traditional.
_SCRIPT_BY_ID: dict[str, str] = {
    "215": "latin",
    "220": "cyrillic",
    "501": "chinese",
    "502": "chinese",
}

# OFAC ValidityID: 1559 = valid, 1560 = invalid.
_VALID_ID = "1559"
_INVALID_ID = "1560"

# Static fallback for common RelationTypeID → readable label. Unknown codes
# pass through as `f"type-{code}"`. Source: OFAC ref values + observed XML.
_RELATION_TYPE_LABEL: dict[str, str] = {
    "15001": "owner-of",
    "15002": "owned-by",
    "15003": "parent-of",
    "15004": "subsidiary-of",
    "15005": "alias-of",
    "15006": "agent-of",
    "15007": "associate-of",
    "15008": "family-of",
    "15009": "linked-to",
}

# IDRegDocTypeID → label fallback table (covers most common docs; unknowns
# fall back to ref-value-set lookup, then `id-type-<code>`).
_IDREGDOC_TYPE_FALLBACK: dict[str, str] = {
    "1571": "Passport",
    "1572": "National ID",
    "1584": "Driver's License",
    "1626": "Tax ID",
    "1627": "Cedula No.",
    "91752": "LEI",
    "91761": "IMO",
}


def _ofac_dir() -> Path:
    return storage.DATA_ROOT / "ofac"


def _sdn_xml_path() -> Path:
    return _ofac_dir() / "sdn_advanced.xml"


def _cons_xml_path() -> Path:
    return _ofac_dir() / "cons_advanced.xml"


def _refresh_marker_path() -> Path:
    return _ofac_dir() / "_last_refresh.json"


# ---------------------------------------------------------------------------
# Entry model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SdnEntry:
    """One OFAC profile, parsed from ADVANCED_XML.

    Tuples used for hashability + lru_cache safety.
    """

    uid: int
    primary_name: str
    sdn_type: str  # "Entity" | "Individual" | "Vessel" | "Aircraft" | "Unknown"
    programs: tuple[str, ...]
    source_list: str = "SDN"  # "SDN" | "CONSOLIDATED"
    primary_name_native: str | None = None
    aliases: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # (name, quality, script). script in {"arabic","cyrillic","chinese","persian","other"}.
    aliases_native: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    # Each address: {"city","state","postal","country"} — values may be "".
    addresses: tuple[dict, ...] = field(default_factory=tuple)
    address_countries: tuple[str, ...] = field(default_factory=tuple)
    # Each id: {"type","number","country","is_legitimate"}.
    ids: tuple[dict, ...] = field(default_factory=tuple)
    # Each rel: {"target_uid": int, "type": str}.
    relationships: tuple[dict, ...] = field(default_factory=tuple)
    list_date: str | None = None

    @property
    def treasury_url(self) -> str:
        return f"https://sanctionssearch.ofac.treas.gov/Details.aspx?id={self.uid}"


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
# Script detection (used for native-alias tagging)
# ---------------------------------------------------------------------------


def _detect_script(s: str) -> str:
    """Classify a name by majority codepoint script.

    Returns one of: 'latin', 'arabic', 'cyrillic', 'chinese', 'other'.
    Persian is folded into 'arabic' (shared base block).
    """
    if not s:
        return "other"
    counts = {"latin": 0, "arabic": 0, "cyrillic": 0, "chinese": 0, "other": 0}
    for ch in s:
        cp = ord(ch)
        if cp < 0x80 and (ch.isalpha() or ch.isdigit()):
            counts["latin"] += 1
        elif 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F or 0xFB50 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF:
            counts["arabic"] += 1
        elif 0x0400 <= cp <= 0x04FF or 0x0500 <= cp <= 0x052F:
            counts["cyrillic"] += 1
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF:
            counts["chinese"] += 1
        elif ch.isalpha():
            counts["other"] += 1
    # Pick majority excluding 'latin' if non-Latin presence is dominant.
    best = max(counts, key=lambda k: counts[k])
    if best == "latin" and counts["latin"] > 0:
        return "latin"
    if counts[best] == 0:
        return "other"
    return best


# ---------------------------------------------------------------------------
# Download (per-file)
# ---------------------------------------------------------------------------


def _download_one(url: str, target: Path) -> tuple[bool, int, str | None]:
    """Download a single OFAC XML to `target` atomically. Returns (ok, bytes, error).

    Chunked read + bounded retry. OFAC's SLS endpoint sporadically drops the
    TLS connection mid-stream on the larger SDN_ADVANCED.XML (~120 MB), so
    a single `resp.read()` is unreliable; retry up to 3 times on incomplete
    reads / TLS EOFs before giving up.
    """
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
                last_err = "response is not XML (got HTML/empty payload)"
                continue
            tmp.write_bytes(data)
            os.replace(tmp, target)
            return True, len(data), None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(2 * (attempt + 1))
    return False, 0, last_err


def refresh_now() -> dict:
    """Download BOTH OFAC ADVANCED XML files, update marker, clear cache.

    Per-file failures don't abort the other download. `ok` is true if at
    least one file succeeded. Status payload exposes per-file fields plus
    legacy aggregate fields for back-compat.
    """
    _ofac_dir().mkdir(parents=True, exist_ok=True)

    sdn_ok, sdn_bytes, sdn_err = _download_one(SDN_XML_URL, _sdn_xml_path())
    cons_ok, cons_bytes, cons_err = _download_one(CONS_XML_URL, _cons_xml_path())

    # Invalidate parse cache regardless — one of the files may have been
    # written even if the other failed.
    _load_sdn_entries_cached.cache_clear()

    # Derive list_date per file (cheap re-parse of root attrs only).
    sdn_list_date = _peek_list_date(_sdn_xml_path()) if sdn_ok else None
    cons_list_date = _peek_list_date(_cons_xml_path()) if cons_ok else None

    fetched_at = _now_iso()
    payload = {
        "ok": sdn_ok or cons_ok,
        "fetched_at": fetched_at,
        # Aggregate / legacy fields:
        "list_version": sdn_list_date,
        "bytes": sdn_bytes + cons_bytes,
        "error": _join_errors(sdn_err, cons_err) if not (sdn_ok or cons_ok) else None,
        # Per-file fields:
        "sdn": {
            "ok": sdn_ok,
            "bytes": sdn_bytes,
            "list_date": sdn_list_date,
            "error": sdn_err,
        },
        "cons": {
            "ok": cons_ok,
            "bytes": cons_bytes,
            "list_date": cons_list_date,
            "error": cons_err,
        },
        "errors": {"sdn": sdn_err, "cons": cons_err},
    }
    _write_marker(payload)
    return payload


def _join_errors(*errs: str | None) -> str | None:
    parts = [e for e in errs if e]
    return "; ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def _strip_ns(tag: str) -> str:
    """Drop XML namespace prefix from a tag."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _local_iter(elem: ET.Element, name: str) -> Iterable[ET.Element]:
    """Iterate direct children whose local name == `name` (namespace-agnostic)."""
    for child in elem:
        if _strip_ns(child.tag) == name:
            yield child


def _local_find(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem:
        if _strip_ns(child.tag) == name:
            return child
    return None


def _local_findall(elem: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in elem if _strip_ns(c.tag) == name]


def _peek_list_date(path: Path) -> str | None:
    """Read just the root + a few children to extract list/publication date."""
    if not path.exists():
        return None
    try:
        for event, elem in ET.iterparse(str(path), events=("start", "end")):
            local = _strip_ns(elem.tag)
            if event == "start" and local == "Sanctions":
                # Version attribute often holds the publication identifier.
                ver = elem.get("Version")
                # Continue iterating to find date elements.
                continue
            if event == "end" and local in {"DateOfIssue", "Publish_Date"}:
                text = (elem.text or "").strip()
                if text:
                    return text
            if event == "end" and local in {"PublshInformation", "PublishInformation"}:
                # Try Publish_Date child.
                d = _local_find(elem, "Publish_Date")
                if d is not None and d.text:
                    return d.text.strip()
            # Stop after we leave Reference/Publish header area to avoid
            # streaming the whole file.
            if event == "end" and local in {"ReferenceValueSets", "Locations"}:
                return None
    except ET.ParseError as exc:
        logger.warning("could not peek list_date from %s: %s", path, exc)
    return None


# ----- Reference-value-set parsing ----------------------------------------


def _build_refmaps(root: ET.Element) -> dict[str, dict[str, str]]:
    """Build {valueset_name → {id → label}} from <ReferenceValueSets>."""
    out: dict[str, dict[str, str]] = {}
    rvs = _local_find(root, "ReferenceValueSets")
    if rvs is None:
        return out
    for valueset in rvs:
        vsname = _strip_ns(valueset.tag)
        # Each child is one value entry, with @ID and either text content
        # or a `Name`/`Description` attribute.
        m: dict[str, str] = {}
        for entry in valueset:
            vid = entry.get("ID")
            if vid is None:
                continue
            # Text content first.
            label = (entry.text or "").strip()
            if not label:
                # Common attribute patterns OFAC uses.
                label = (
                    entry.get("Name")
                    or entry.get("ISO2")
                    or entry.get("ISO3")
                    or ""
                ).strip()
            if label:
                m[vid] = label
        out[vsname] = m
    return out


# ----- Locations ----------------------------------------------------------


def _parse_locations(root: ET.Element, refmaps: dict[str, dict[str, str]]) -> dict[str, dict]:
    """Return {location_id → address_dict}. Keys: city/state/postal/country."""
    locs: dict[str, dict] = {}
    locroot = _local_find(root, "Locations")
    if locroot is None:
        return locs

    countries = refmaps.get("CountryValues", {})
    locparttypes = refmaps.get("LocPartTypeValues", {})

    for loc in _local_iter(locroot, "Location"):
        lid = loc.get("ID")
        if not lid:
            continue
        addr = {"city": "", "state": "", "postal": "", "country": ""}

        # Country.
        country_elem = _local_find(loc, "LocationCountry")
        if country_elem is not None:
            cid = country_elem.get("CountryID")
            if cid:
                addr["country"] = countries.get(cid, "")

        # Address parts (city/state/postal).
        for part in _local_findall(loc, "LocationPart"):
            ptype_id = part.get("LocPartTypeID", "")
            ptype_label = locparttypes.get(ptype_id, "").upper()
            val_elem = _local_find(part, "LocationPartValue")
            if val_elem is None:
                continue
            v_elem = _local_find(val_elem, "Value")
            value = (v_elem.text or "").strip() if v_elem is not None and v_elem.text else ""
            if not value:
                continue
            if "CITY" in ptype_label:
                addr["city"] = addr["city"] or value
            elif "STATE" in ptype_label or "PROVINCE" in ptype_label or "REGION" in ptype_label:
                addr["state"] = addr["state"] or value
            elif "POSTAL" in ptype_label or "ZIP" in ptype_label:
                addr["postal"] = addr["postal"] or value
            elif "COUNTRY" in ptype_label and not addr["country"]:
                addr["country"] = value

        locs[lid] = addr
    return locs


# ----- IDRegDocuments -----------------------------------------------------


def _parse_id_docs(
    root: ET.Element, refmaps: dict[str, dict[str, str]]
) -> dict[str, list[dict]]:
    """Return {identity_id → [id_dict, ...]}.

    id_dict keys: type, number, country, is_legitimate.
    """
    out: dict[str, list[dict]] = {}
    docroot = _local_find(root, "IDRegDocuments")
    if docroot is None:
        return out

    doctypes = refmaps.get("IDRegDocTypeValues", {})
    countries = refmaps.get("CountryValues", {})

    for doc in _local_iter(docroot, "IDRegDocument"):
        identity_id = doc.get("IdentityID")
        if not identity_id:
            continue
        type_id = doc.get("IDRegDocTypeID", "")
        type_label = (
            doctypes.get(type_id)
            or _IDREGDOC_TYPE_FALLBACK.get(type_id)
            or f"id-type-{type_id}"
        )
        country_id = doc.get("IssuedBy-CountryID", "")
        country = countries.get(country_id, "") if country_id else ""
        validity_id = doc.get("ValidityID", "")
        if validity_id == _VALID_ID:
            is_legit: bool | None = True
        elif validity_id == _INVALID_ID:
            is_legit = False
        else:
            is_legit = None

        num_elem = _local_find(doc, "IDRegistrationNo")
        number = (num_elem.text or "").strip() if num_elem is not None and num_elem.text else ""

        if not number and not type_label:
            continue
        out.setdefault(identity_id, []).append(
            {
                "type": type_label,
                "number": number,
                "country": country,
                "is_legitimate": is_legit,
            }
        )
    return out


# ----- Sanctions programs per profile -------------------------------------


def _parse_programs_by_profile(
    root: ET.Element, refmaps: dict[str, dict[str, str]]
) -> dict[str, list[str]]:
    """Return {profile_id → [program_name, ...]} via LegalBasis chain.

    SanctionsEntry → LegalBasisID → LegalBasis.SanctionsProgramID → Program name.
    """
    legal_to_program: dict[str, str] = {}
    legal_basis = refmaps.get("LegalBasisValues") or {}
    programs = refmaps.get("SanctionsProgramValues") or {}

    # Need richer access to LegalBasisValues → SanctionsProgramID attribute.
    rvs = _local_find(root, "ReferenceValueSets")
    if rvs is not None:
        lbset = _local_find(rvs, "LegalBasisValues")
        if lbset is not None:
            for lb in lbset:
                lid = lb.get("ID")
                pid = lb.get("SanctionsProgramID")
                if lid and pid:
                    prog_name = programs.get(pid) or ""
                    # OFAC ships SanctionsProgramID=1 with literal text "Unknown"
                    # as the catch-all bucket. Skip it and fall through to the
                    # LegalBasis text/short-ref, which carries the actual program.
                    if prog_name and prog_name.lower() != "unknown":
                        pname = prog_name
                    else:
                        pname = (
                            (lb.text or "").strip()
                            or (lb.get("LegalBasisShortRef") or "").strip()
                            or f"prog-{pid}"
                        )
                    legal_to_program[lid] = pname

    out: dict[str, list[str]] = {}
    entries = _local_find(root, "SanctionsEntries")
    if entries is None:
        return out
    for entry in _local_iter(entries, "SanctionsEntry"):
        profile_id = entry.get("ProfileID")
        if not profile_id:
            continue
        seen: set[str] = set()
        for event in _local_findall(entry, "EntryEvent"):
            lb_id = event.get("LegalBasisID", "")
            if not lb_id:
                continue
            pname = legal_to_program.get(lb_id)
            if not pname:
                # Fallback: try program name directly via legal_basis text.
                pname = legal_basis.get(lb_id) or f"legal-{lb_id}"
            if pname and pname not in seen:
                seen.add(pname)
                out.setdefault(profile_id, []).append(pname)
    return out


# ----- Relationships ------------------------------------------------------


def _parse_relationships(
    root: ET.Element, refmaps: dict[str, dict[str, str]]
) -> dict[str, list[dict]]:
    """Return {from_profile_id → [{target_uid, type}, ...]}."""
    out: dict[str, list[dict]] = {}
    rels = _local_find(root, "ProfileRelationships")
    if rels is None:
        return out
    reltypes = refmaps.get("RelationTypeValues", {})

    for rel in _local_iter(rels, "ProfileRelationship"):
        from_id = rel.get("From-ProfileID")
        to_id = rel.get("To-ProfileID")
        rtype_id = rel.get("RelationTypeID", "")
        if not from_id or not to_id:
            continue
        try:
            target_uid = int(to_id)
        except (TypeError, ValueError):
            continue
        rtype = (
            _RELATION_TYPE_LABEL.get(rtype_id)
            or (reltypes.get(rtype_id) or "").strip().lower().replace(" ", "-")
            or f"type-{rtype_id}"
        )
        out.setdefault(from_id, []).append({"target_uid": target_uid, "type": rtype})
    return out


# ----- DistinctParty / Profile / Identity / Alias -------------------------


def _collect_name_text(name_elem: ET.Element) -> str:
    """Concatenate a DocumentedName's parts in document order."""
    bits: list[str] = []
    for part in _local_findall(name_elem, "DocumentedNamePart"):
        val_elem = _local_find(part, "NamePartValue")
        if val_elem is not None and val_elem.text:
            txt = val_elem.text.strip()
            if txt:
                bits.append(txt)
    return " ".join(bits).strip()


def _script_of_name(name_elem: ET.Element, script_map: dict[str, str]) -> str:
    """Resolve script via @ScriptID on first NamePartValue, fall back to unicode detection."""
    for part in _local_findall(name_elem, "DocumentedNamePart"):
        val_elem = _local_find(part, "NamePartValue")
        if val_elem is None:
            continue
        sid = val_elem.get("ScriptID")
        if sid:
            mapped = _SCRIPT_BY_ID.get(sid) or script_map.get(sid, "").lower()
            if mapped:
                if "latin" in mapped:
                    return "latin"
                if "arab" in mapped or "persian" in mapped or "farsi" in mapped:
                    return "arabic"
                if "cyril" in mapped:
                    return "cyrillic"
                if "chinese" in mapped or "han" in mapped or "cjk" in mapped:
                    return "chinese"
                return mapped
    # Fallback: inspect text codepoints.
    return _detect_script(_collect_name_text(name_elem))


def _parse_distinct_parties(
    root: ET.Element,
    refmaps: dict[str, dict[str, str]],
    locations: dict[str, dict],
    id_docs: dict[str, list[dict]],
    programs_by_profile: dict[str, list[str]],
    rels_by_profile: dict[str, list[dict]],
    list_date: str | None,
    source_list: str,
) -> list[SdnEntry]:
    """Walk DistinctParty → emit one SdnEntry per profile."""
    out: list[SdnEntry] = []
    dp_root = _local_find(root, "DistinctParties")
    if dp_root is None:
        return out

    script_map = refmaps.get("ScriptValues", {})
    subtypes = refmaps.get("PartySubTypeValues", {})

    # Build a quick lookup of location IDs referenced by each profile, via
    # the address tree embedded in DistinctParty (profile/identity have
    # `Feature` and `Address` blocks pointing at location IDs).
    # In ADVANCED_XML, profile addresses are emitted as Feature children with
    # FeatureVersion → VersionLocation[@LocationID].
    for dp in _local_iter(dp_root, "DistinctParty"):
        fixed_ref = dp.get("FixedRef") or ""
        for profile in _local_iter(dp, "Profile"):
            profile_id = profile.get("ID") or fixed_ref
            try:
                uid = int(profile_id)
            except (TypeError, ValueError):
                continue
            subtype_id = profile.get("PartySubTypeID", "")
            sdn_type = (
                _PARTY_SUBTYPE_LABEL.get(subtype_id)
                or subtypes.get(subtype_id, "Unknown")
            )
            if sdn_type not in {"Entity", "Individual", "Vessel", "Aircraft"}:
                sdn_type = "Unknown"

            # ---- Names per identity ------------------------------------
            primary_name = ""
            primary_name_native: str | None = None
            aliases_latin: list[tuple[str, str]] = []
            aliases_native: list[tuple[str, str, str]] = []
            identity_ids: list[str] = []
            seen_latin: set[str] = set()
            seen_native: set[str] = set()

            for identity in _local_iter(profile, "Identity"):
                iid = identity.get("ID") or ""
                if iid:
                    identity_ids.append(iid)
                identity_primary = (identity.get("Primary", "").lower() == "true")

                for alias in _local_findall(identity, "Alias"):
                    low_quality = (alias.get("LowQuality", "").lower() == "true")
                    quality = "Weak" if low_quality else "Strong"
                    alias_primary = (alias.get("Primary", "").lower() == "true")

                    for name_elem in _local_findall(alias, "DocumentedName"):
                        text = _collect_name_text(name_elem)
                        if not text:
                            continue
                        script = _script_of_name(name_elem, script_map)
                        is_primary_slot = (
                            identity_primary and alias_primary and not primary_name
                        )

                        if script == "latin":
                            key = text.casefold()
                            if is_primary_slot and not primary_name:
                                primary_name = text
                                seen_latin.add(key)
                                continue
                            if key in seen_latin:
                                continue
                            seen_latin.add(key)
                            aliases_latin.append((text, quality))
                        else:
                            key = text.casefold()
                            if (
                                identity_primary
                                and alias_primary
                                and primary_name_native is None
                            ):
                                primary_name_native = text
                                seen_native.add(key)
                                continue
                            if key in seen_native:
                                continue
                            seen_native.add(key)
                            aliases_native.append((text, quality, script))

            # If we never tagged a "primary" alias but have aliases, promote
            # the first Latin one. Names with only non-Latin scripts: pick
            # the native name as the native primary, leave `primary_name` empty
            # so callers can fall back gracefully.
            if not primary_name and aliases_latin:
                primary_name, _ = aliases_latin.pop(0)
            if not primary_name and not primary_name_native and aliases_native:
                primary_name_native, _, _ = aliases_native.pop(0)

            if not primary_name and not primary_name_native:
                # Skip profile with no usable name at all.
                continue

            # ---- Addresses via features (best-effort) ------------------
            addresses: list[dict] = []
            address_countries: list[str] = []
            seen_country: set[str] = set()
            for feature in _local_findall(profile, "Feature"):
                fver = _local_find(feature, "FeatureVersion")
                if fver is None:
                    continue
                vloc = _local_find(fver, "VersionLocation")
                if vloc is None:
                    continue
                loc_id = vloc.get("LocationID", "")
                if not loc_id:
                    continue
                addr = locations.get(loc_id)
                if not addr:
                    continue
                addresses.append(addr)
                country = addr.get("country", "")
                if country:
                    key = country.casefold()
                    if key not in seen_country:
                        seen_country.add(key)
                        address_countries.append(country)

            # ---- IDs --------------------------------------------------
            ids_collected: list[dict] = []
            for iid in identity_ids:
                ids_collected.extend(id_docs.get(iid, []))

            # ---- Programs / relationships ----------------------------
            progs = tuple(programs_by_profile.get(profile_id, ()))
            rels = tuple(rels_by_profile.get(profile_id, ()))

            out.append(
                SdnEntry(
                    uid=uid,
                    source_list=source_list,
                    sdn_type=sdn_type,
                    programs=progs,
                    primary_name=primary_name or (primary_name_native or ""),
                    primary_name_native=primary_name_native,
                    aliases=tuple(aliases_latin),
                    aliases_native=tuple(aliases_native),
                    addresses=tuple(addresses),
                    address_countries=tuple(address_countries),
                    ids=tuple(ids_collected),
                    relationships=rels,
                    list_date=list_date,
                )
            )
    return out


def _parse_xml(xml_path: Path, source_list: str) -> list[SdnEntry]:
    """Parse one OFAC ADVANCED_XML file into SdnEntry list."""
    if not xml_path.exists():
        return []
    try:
        tree = ET.parse(str(xml_path))
    except ET.ParseError as exc:
        logger.exception("ParseError on %s: %s", xml_path, exc)
        return []
    root = tree.getroot()

    # Publish date.
    list_date: str | None = None
    publsh = _local_find(root, "PublshInformation") or _local_find(root, "PublishInformation")
    if publsh is not None:
        d = _local_find(publsh, "Publish_Date")
        if d is not None and d.text:
            list_date = d.text.strip()
    if not list_date:
        doi = _local_find(root, "DateOfIssue")
        if doi is not None and doi.text:
            list_date = doi.text.strip()

    refmaps = _build_refmaps(root)
    locations = _parse_locations(root, refmaps)
    id_docs = _parse_id_docs(root, refmaps)
    programs_by_profile = _parse_programs_by_profile(root, refmaps)
    rels_by_profile = _parse_relationships(root, refmaps)
    return _parse_distinct_parties(
        root,
        refmaps,
        locations,
        id_docs,
        programs_by_profile,
        rels_by_profile,
        list_date,
        source_list,
    )


# ---------------------------------------------------------------------------
# Cached load (keyed by both file mtimes)
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=2)
def _load_sdn_entries_cached(
    sdn_mtime_ns: int, cons_mtime_ns: int
) -> tuple[SdnEntry, ...]:
    """Pure helper — mtimes used as cache key only."""
    _ = (sdn_mtime_ns, cons_mtime_ns)
    out: list[SdnEntry] = []
    try:
        out.extend(_parse_xml(_sdn_xml_path(), "SDN"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to parse SDN ADVANCED XML: %s", exc)
    try:
        out.extend(_parse_xml(_cons_xml_path(), "CONSOLIDATED"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to parse CONS ADVANCED XML: %s", exc)
    return tuple(out)


def _mtime_or_zero(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def load_sdn_entries() -> list[SdnEntry]:
    """Return parsed union of SDN + CONS, or `[]` if neither file exists."""
    sdn_mtime = _mtime_or_zero(_sdn_xml_path())
    cons_mtime = _mtime_or_zero(_cons_xml_path())
    if sdn_mtime == 0 and cons_mtime == 0:
        return []
    try:
        return list(_load_sdn_entries_cached(sdn_mtime, cons_mtime))
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to load SDN entries: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _file_stat(path: Path) -> tuple[bool, int, int | None]:
    """Return (exists, size_bytes, age_seconds)."""
    if not path.exists():
        return False, 0, None
    try:
        st = path.stat()
    except OSError:
        return False, 0, None
    return True, st.st_size, int(time.time() - st.st_mtime)


def list_status() -> dict:
    """Describe on-disk OFAC files for the frontend freshness widget."""
    marker = _read_marker()
    sdn_available, sdn_bytes, sdn_age = _file_stat(_sdn_xml_path())
    cons_available, cons_bytes, cons_age = _file_stat(_cons_xml_path())

    sdn_marker = marker.get("sdn") or {}
    cons_marker = marker.get("cons") or {}
    sdn_list_date = sdn_marker.get("list_date") or marker.get("list_version")
    cons_list_date = cons_marker.get("list_date")

    try:
        entry_count = len(load_sdn_entries()) if (sdn_available or cons_available) else 0
    except Exception:  # noqa: BLE001
        entry_count = 0

    # Aggregate / legacy fields:
    ages = [a for a in (sdn_age, cons_age) if a is not None]
    age_seconds = min(ages) if ages else None

    return {
        # Legacy (preserved):
        "available": sdn_available,
        "list_version": sdn_list_date,
        "fetched_at": marker.get("fetched_at"),
        "last_error": marker.get("error") or sdn_marker.get("error") or cons_marker.get("error"),
        "entry_count": entry_count,
        "bytes": sdn_bytes + cons_bytes,
        "age_seconds": age_seconds,
        # Per-file:
        "sdn_available": sdn_available,
        "sdn_bytes": sdn_bytes,
        "sdn_list_date": sdn_list_date,
        "sdn_age_seconds": sdn_age,
        "sdn_error": sdn_marker.get("error"),
        "cons_available": cons_available,
        "cons_bytes": cons_bytes,
        "cons_list_date": cons_list_date,
        "cons_age_seconds": cons_age,
        "cons_error": cons_marker.get("error"),
    }
