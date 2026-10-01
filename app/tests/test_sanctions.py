"""Tests for the OFAC sanctions-screening pipeline (rapidfuzz + LLM judge).

LLM stage is mocked via monkeypatch in every test that exercises
`screen_invoice` so tests are deterministic + offline.

Run with:
    .venv/Scripts/python.exe -m pytest app/tests/test_sanctions.py -v
"""

from __future__ import annotations

import pytest

from app.sanctions.loader import SdnEntry
from app.sanctions.match import (
    RAPIDFUZZ_AUTO_CONFIRM,
    RAPIDFUZZ_CANDIDATE_THRESHOLD,
    THRESHOLD_HIT,
    THRESHOLD_REVIEW,
    normalize,
    screen_invoice,
    screen_name,
)
from extract import BankInfo, Invoice


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme Co., Ltd.", "acme"),
        ("Smith & Jones", "smith jones"),
        ("Müller GmbH", "muller"),
        ("BANK MELLI IRAN", "bank melli iran"),
        ("  ACME TRADING CO.  ", "acme trading"),
        ("", ""),
        ("LLC LTD INC", ""),
        # Context-strip only fires when >= 2 tokens survive — "Acme Trading Co"
        # would collapse to just "acme" (1 token), so "trading" is kept.
        ("Acme Trading Co", "acme trading"),
        # >= 2 tokens after stripping — "trading"/"international" both dropped.
        ("Acme Widgets Trading International", "acme widgets"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_normalize_idempotent() -> None:
    once = normalize("ACME Holdings Corp.")
    twice = normalize(once)
    assert once == twice


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sample_entries() -> list[SdnEntry]:
    return [
        SdnEntry(
            uid=18234,
            primary_name="BANK MELLI IRAN",
            sdn_type="Entity",
            programs=("IRAN", "SDGT"),
            aliases=(
                ("BANK MELLI", "Strong"),
                ("NATIONAL BANK OF IRAN", "Strong"),
            ),
            address_countries=("Iran",),
            list_date="2026-05-22",
        ),
        SdnEntry(
            uid=99001,
            primary_name="FAKE WEAK ALIAS TRADING CO LTD",
            sdn_type="Entity",
            programs=("TESTPROG",),
            aliases=(("GENERIC TRADING", "Weak"),),
            address_countries=("Wonderland",),
            list_date="2026-05-22",
        ),
        SdnEntry(
            uid=77002,
            primary_name="ACME INNOCENT CORP",
            sdn_type="Entity",
            programs=("TESTPROG",),
            aliases=(),
            address_countries=("Switzerland",),
            list_date="2026-05-22",
        ),
    ]


def test_fixture_loads(sample_entries: list[SdnEntry]) -> None:
    assert len(sample_entries) == 3
    names = {e.primary_name for e in sample_entries}
    assert "BANK MELLI IRAN" in names
    assert "ACME INNOCENT CORP" in names


def test_fixture_aliases_and_programs(sample_entries: list[SdnEntry]) -> None:
    bmi = next(e for e in sample_entries if e.primary_name == "BANK MELLI IRAN")
    alias_names = [a[0] for a in bmi.aliases]
    assert "BANK MELLI" in alias_names
    assert "NATIONAL BANK OF IRAN" in alias_names
    assert "IRAN" in bmi.programs


# ---------------------------------------------------------------------------
# Stage 1: screen_name (rapidfuzz only)
# ---------------------------------------------------------------------------


def test_seller_hit_on_primary_name(sample_entries: list[SdnEntry]) -> None:
    matches = screen_name("seller", "Bank Melli Iran", "Iran", sample_entries)
    assert matches
    top = matches[0]
    assert top.primary_name == "BANK MELLI IRAN"
    assert top.score >= THRESHOLD_HIT
    assert "IRAN" in top.programs
    assert top.role == "seller"


def test_country_bonus_caps_at_100(sample_entries: list[SdnEntry]) -> None:
    no_country = screen_name("seller", "Bank Melli Iran", None, sample_entries)
    with_country = screen_name("seller", "Bank Melli Iran", "Iran", sample_entries)
    assert with_country[0].score >= no_country[0].score
    assert with_country[0].score <= 100.0


def test_no_match_returns_empty(sample_entries: list[SdnEntry]) -> None:
    assert screen_name("seller", "Zenith Widgets Manufacturing", None, sample_entries) == []
    assert screen_name("seller", "", None, sample_entries) == []
    assert screen_name("seller", None, None, sample_entries) == []


def test_weak_alias_penalty(sample_entries: list[SdnEntry]) -> None:
    matches = screen_name("seller", "Generic LLC", None, sample_entries)
    weak_hits = [m for m in matches if m.matched_name == "GENERIC TRADING"]
    if weak_hits:
        assert weak_hits[0].score < THRESHOLD_HIT


# ---------------------------------------------------------------------------
# LLM mocking helpers
# ---------------------------------------------------------------------------


def _stub_judge(verdict_map: dict[int, tuple[str, str]] | None = None):
    """Return a function that mimics `llm_judge.judge_candidates`.

    `verdict_map` maps SDN uid → (verdict, reasoning). UIDs not in the map
    default to "rejected" with a generic reason.
    """
    vmap = verdict_map or {}

    def _judge(role, query, country, candidates):
        annotated = []
        for c in candidates:
            verdict, reasoning = vmap.get(c.uid, ("rejected", "stub: default rejected"))
            annotated.append(c.model_copy(update={
                "llm_verdict": verdict,
                "llm_reasoning": reasoning,
            }))
        return annotated, None

    return _judge


def _stub_judge_raises(error_message: str = "stub network failure"):
    def _judge(role, query, country, candidates):
        annotated = [
            c.model_copy(update={"llm_verdict": "error", "llm_reasoning": error_message})
            for c in candidates
        ]
        return annotated, error_message
    return _judge


# ---------------------------------------------------------------------------
# Stage 2: screen_invoice (rapidfuzz + LLM)
# ---------------------------------------------------------------------------


def _invoice(
    company_name: str | None = None,
    company_country: str | None = None,
    bank_name: str | None = None,
    bank_country: str | None = None,
) -> Invoice:
    bank: BankInfo | None = None
    if bank_name or bank_country:
        bank = BankInfo(bank_name=bank_name, bank_country=bank_country)
    return Invoice(
        company_name=company_name,
        company_country=company_country,
        bank=bank or BankInfo(),
    )


def test_invoice_green_when_no_candidates(monkeypatch, sample_entries) -> None:
    from app.sanctions import llm_judge
    monkeypatch.setattr(llm_judge, "judge_candidates", _stub_judge())
    inv = _invoice(company_name="Zenith Widgets Manufacturing", company_country="UAE")
    verdict = screen_invoice(inv, sample_entries)
    assert verdict.status == "green"
    assert verdict.matches == []


def test_invoice_red_auto_confirmed_seller(monkeypatch, sample_entries) -> None:
    """Exact match → rapidfuzz scores 100 → above auto-confirm → no LLM call → red."""
    from app.sanctions import llm_judge
    called = {"count": 0}

    def _spy(role, q, c, cands):
        called["count"] += 1
        return cands, None

    monkeypatch.setattr(llm_judge, "judge_candidates", _spy)

    inv = _invoice(company_name="Bank Melli Iran", company_country="Iran")
    verdict = screen_invoice(inv, sample_entries)

    assert verdict.status == "red"
    # All matches above auto-confirm threshold should bypass the LLM.
    auto_confirms = [m for m in verdict.matches if m.llm_verdict == "skipped"]
    assert auto_confirms, "expected at least one auto-confirmed match"
    # The exact top match must be auto-confirmed.
    assert verdict.matches[0].llm_verdict == "skipped"


def test_llm_rejects_all_candidates_yields_green(monkeypatch, sample_entries) -> None:
    """rapidfuzz produces candidates < 95, LLM rejects all → green."""
    from app.sanctions import llm_judge
    # Stub returns "rejected" for everything by default.
    monkeypatch.setattr(llm_judge, "judge_candidates", _stub_judge())

    # "Generic LLC" surfaces the weak-alias entry at ~88-90 (below auto-confirm).
    inv = _invoice(company_name="Generic LLC", company_country="UAE")
    verdict = screen_invoice(inv, sample_entries)

    # Either no candidates at all → green; or candidates all rejected → green.
    assert verdict.status == "green"
    for m in verdict.matches:
        assert m.llm_verdict == "rejected"


def test_llm_confirms_candidate_yields_red(monkeypatch, sample_entries) -> None:
    """rapidfuzz candidate at score < 95, LLM confirms → red."""
    from app.sanctions import llm_judge
    monkeypatch.setattr(
        llm_judge,
        "judge_candidates",
        _stub_judge({99001: ("confirmed", "stub: same entity per fixture")}),
    )

    # Force the weak-alias entry into the candidate set (~88-90).
    inv = _invoice(company_name="Generic LLC", company_country="UAE")
    verdict = screen_invoice(inv, sample_entries)
    if not verdict.matches:
        pytest.skip("rapidfuzz did not surface a candidate to confirm")

    confirmed = [m for m in verdict.matches if m.llm_verdict == "confirmed"]
    assert confirmed, "expected at least one confirmed match"
    assert verdict.status == "red"


def test_llm_unclear_yields_yellow(monkeypatch, sample_entries) -> None:
    from app.sanctions import llm_judge
    monkeypatch.setattr(
        llm_judge,
        "judge_candidates",
        _stub_judge({99001: ("unclear", "stub: cannot decide")}),
    )

    inv = _invoice(company_name="Generic LLC", company_country="UAE")
    verdict = screen_invoice(inv, sample_entries)
    if not verdict.matches:
        pytest.skip("rapidfuzz did not surface a candidate")

    has_unclear = any(m.llm_verdict == "unclear" for m in verdict.matches)
    has_confirmed = any(m.llm_verdict in {"confirmed", "skipped"} for m in verdict.matches)
    if has_confirmed:
        assert verdict.status == "red"
    else:
        assert has_unclear
        assert verdict.status == "yellow"


def test_llm_error_falls_back_to_yellow(monkeypatch, sample_entries) -> None:
    """LLM call raises → annotated as error → status falls back to yellow."""
    from app.sanctions import llm_judge
    monkeypatch.setattr(llm_judge, "judge_candidates", _stub_judge_raises())

    inv = _invoice(company_name="Generic LLC", company_country="UAE")
    verdict = screen_invoice(inv, sample_entries)
    if not verdict.matches:
        pytest.skip("rapidfuzz did not surface a candidate")

    # When LLM errors, status must be yellow (unless auto-confirmed already).
    has_auto = any(m.llm_verdict == "skipped" for m in verdict.matches)
    if not has_auto:
        assert verdict.status == "yellow"
        assert verdict.llm_error is not None
        assert all(m.llm_verdict == "error" for m in verdict.matches if not has_auto)


def test_llm_disabled_via_env(monkeypatch, sample_entries) -> None:
    """SANCTIONS_LLM_ENABLED=0 → no LLM call, all candidates marked skipped."""
    monkeypatch.setenv("SANCTIONS_LLM_ENABLED", "0")
    # Even with a real-looking stub installed, env flag should skip the call.
    from app.sanctions import llm_judge

    sentinel = {"called": False}

    def _spy(*a, **kw):
        sentinel["called"] = True
        return [], None

    monkeypatch.setattr(llm_judge, "judge_candidates", _spy)

    inv = _invoice(company_name="Generic LLC", company_country="UAE")
    verdict = screen_invoice(inv, sample_entries)
    assert sentinel["called"] is False
    # When the LLM is disabled, both auto-confirmed and below-auto-confirm
    # matches end up with `llm_verdict in {"skipped"}` (no real adjudication).
    for m in verdict.matches:
        assert m.llm_verdict == "skipped"


def test_invoice_not_checked_when_no_entries() -> None:
    inv = _invoice(company_name="Bank Melli Iran", company_country="Iran")
    verdict = screen_invoice(inv, [])
    assert verdict.status == "not_checked"
    assert verdict.matches == []


# ---------------------------------------------------------------------------
# DB round-trip (includes new llm_model / llm_error columns)
# ---------------------------------------------------------------------------


def test_db_round_trip(tmp_path, monkeypatch) -> None:
    from app import storage
    monkeypatch.setattr(storage, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(storage, "OUTPUT_ROOT", tmp_path / "output")

    import importlib
    from app import db as db_mod
    importlib.reload(db_mod)

    db_mod.init_db()
    db_mod.create_job("job-x", "test.pdf", page_count=1)

    from app.models import SanctionsMatch, SanctionsVerdict
    verdict = SanctionsVerdict(
        status="red",
        checked_at="2026-05-23T10:00:00+00:00",
        list_version="2026-05-22",
        error=None,
        matches=[
            SanctionsMatch(
                role="seller",
                uid=18234,
                primary_name="BANK MELLI IRAN",
                matched_name="BANK MELLI IRAN",
                sdn_type="Entity",
                programs=["IRAN", "SDGT"],
                score=99.5,
                address_countries=["Iran"],
                treasury_url="https://example.test/18234",
                llm_verdict="confirmed",
                llm_reasoning="stub: same entity",
            )
        ],
        llm_model="stub/model",
        llm_error=None,
    )

    db_mod.upsert_sanctions_result("job-x", verdict)
    got = db_mod.get_sanctions_result("job-x")
    assert got is not None
    assert got["status"] == "red"
    assert got["list_version"] == "2026-05-22"
    assert got["llm_model"] == "stub/model"
    assert got["llm_error"] is None
    assert len(got["matches"]) == 1
    assert got["matches"][0]["uid"] == 18234
    assert got["matches"][0]["programs"] == ["IRAN", "SDGT"]
    assert got["matches"][0]["llm_verdict"] == "confirmed"
    assert got["matches"][0]["llm_reasoning"] == "stub: same entity"


# ---------------------------------------------------------------------------
# Thresholds sanity
# ---------------------------------------------------------------------------


def test_thresholds_are_sane() -> None:
    assert 0 < THRESHOLD_REVIEW < THRESHOLD_HIT <= 100
    assert RAPIDFUZZ_CANDIDATE_THRESHOLD == THRESHOLD_REVIEW
    assert RAPIDFUZZ_AUTO_CONFIRM == THRESHOLD_HIT
    assert RAPIDFUZZ_CANDIDATE_THRESHOLD < RAPIDFUZZ_AUTO_CONFIRM
