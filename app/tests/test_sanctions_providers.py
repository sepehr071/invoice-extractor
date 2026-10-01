"""Tests for the multi-list provider layer: UK + EU parsers, the registry,
and that non-OFAC entries flow through the shared matching pipeline.

Run with:
    .venv/Scripts/python.exe -m pytest app/tests/test_sanctions_providers.py -v
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from app.sanctions import eu, providers, uk
from app.sanctions.match import RAPIDFUZZ_AUTO_CONFIRM, screen_name


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_UK_ROWS = [
    ["UK Sanctions List", "Last updated 31/05/2026"],
    ["Unique ID", "Name 6", "Name 1", "Name Type",
     "Individual, Entity, Ship", "Regime Name", "Country",
     "Date Designated", "Name Non-Latin Script"],
    ["GBR0001", "Smith", "John", "Primary Name", "Individual",
     "Russia", "Russian Federation", "01/01/2023", ""],
    ["GBR0001", "Smythe", "Jon", "AKA", "Individual",
     "Russia", "Russian Federation", "01/01/2023", "Иван"],
    ["GBR0002", "ACME TRADING LLC", "", "Primary Name", "Entity",
     "Russia", "Russian Federation", "02/02/2024", ""],
]

_EU_XML = """<export xmlns="http://eu.europa.ec/fpi/fsd/export" generationDate="2026-05-30">
  <sanctionEntity logicalId="13" euReferenceNumber="EU.27.28" designationDate="2022-02-23">
    <subjectType classificationCode="person"/>
    <regulation programme="RUS"/>
    <nameAlias wholeName="Ivan Ivanov" strong="true" nameLanguage="en"/>
    <nameAlias wholeName="Иван Иванов" strong="true" nameLanguage="ru"/>
    <address city="Moscow" countryDescription="RUSSIAN FEDERATION" countryIso2Code="RU"/>
  </sanctionEntity>
  <sanctionEntity logicalId="14" euReferenceNumber="EU.99.1" designationDate="2023-01-01">
    <subjectType classificationCode="enterprise"/>
    <nameAlias wholeName="ACME TRADING LLC" strong="true"/>
  </sanctionEntity>
</export>"""


def _write_uk(tmp_path: Path) -> Path:
    buf = io.StringIO()
    csv.writer(buf).writerows(_UK_ROWS)
    p = tmp_path / "uk.csv"
    p.write_text(buf.getvalue(), encoding="utf-8")
    return p


def _write_eu(tmp_path: Path) -> Path:
    p = tmp_path / "eu.xml"
    p.write_text(_EU_XML, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# UK parser
# ---------------------------------------------------------------------------


def test_uk_parse_groups_by_unique_id(tmp_path: Path) -> None:
    entries = uk._parse_csv(_write_uk(tmp_path))
    assert len(entries) == 2
    by_name = {e.primary_name: e for e in entries}

    john = by_name["John Smith"]
    assert john.sdn_type == "Individual"
    assert ("Jon Smythe", "Strong") in john.aliases
    assert any(s == "cyrillic" for _, _, s in john.aliases_native)
    assert john.address_countries == ("Russian Federation",)
    assert "Russia" in john.programs
    assert john.source_list == "UK_SANCTIONS"

    acme = by_name["ACME TRADING LLC"]
    assert acme.sdn_type == "Entity"


def test_uk_uid_is_stable(tmp_path: Path) -> None:
    a = uk._parse_csv(_write_uk(tmp_path))
    b = uk._parse_csv(_write_uk(tmp_path))
    assert sorted(e.uid for e in a) == sorted(e.uid for e in b)
    assert all(isinstance(e.uid, int) for e in a)


def test_uk_empty_when_no_header(tmp_path: Path) -> None:
    p = tmp_path / "junk.csv"
    p.write_text("not,a,sanctions,list\n1,2,3,4\n", encoding="utf-8")
    assert uk._parse_csv(p) == []


# ---------------------------------------------------------------------------
# EU parser
# ---------------------------------------------------------------------------


def test_eu_parse_person_and_enterprise(tmp_path: Path) -> None:
    entries = eu._parse_xml(_write_eu(tmp_path))
    assert len(entries) == 2
    by_uid = {e.uid: e for e in entries}

    person = by_uid[13]
    assert person.primary_name == "Ivan Ivanov"
    assert person.sdn_type == "Individual"
    assert any(s == "cyrillic" for _, _, s in person.aliases_native)
    assert person.address_countries == ("RUSSIAN FEDERATION",)
    assert "RUS" in person.programs
    assert person.list_date == "2022-02-23"
    assert person.source_list == "EU_CONSOLIDATED"

    assert by_uid[14].sdn_type == "Entity"


def test_eu_peek_version(tmp_path: Path) -> None:
    assert eu._peek_list_version(_write_eu(tmp_path)) == "2026-05-30"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_has_all_lists() -> None:
    keys = {p.key for p in providers.PROVIDERS}
    assert {"OFAC", "UK_SANCTIONS", "EU_CONSOLIDATED", "US_CSL"} <= keys


def test_status_all_shape() -> None:
    st = providers.status_all()
    assert "lists" in st and isinstance(st["lists"], list)
    for entry in st["lists"]:
        assert {"key", "label", "authority", "enabled"} <= set(entry)


# ---------------------------------------------------------------------------
# Integration: non-OFAC entries flow through the shared matcher (duck typing)
# ---------------------------------------------------------------------------


def test_screen_name_matches_uk_and_eu(tmp_path: Path) -> None:
    entries = uk._parse_csv(_write_uk(tmp_path)) + eu._parse_xml(_write_eu(tmp_path))
    matches = screen_name("seller", "ACME TRADING LLC", "Russian Federation", entries)
    sources = {m.source_list for m in matches}
    assert "UK_SANCTIONS" in sources
    assert "EU_CONSOLIDATED" in sources
    assert all(m.score >= RAPIDFUZZ_AUTO_CONFIRM for m in matches)


def test_screen_name_native_script_path(tmp_path: Path) -> None:
    entries = eu._parse_xml(_write_eu(tmp_path))
    # Cyrillic query must match via the native-alias path.
    matches = screen_name("seller", "Иван Иванов", None, entries)
    assert any(m.uid == 13 and m.matched_script == "cyrillic" for m in matches)
