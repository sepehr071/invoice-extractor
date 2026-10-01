"""Fuzzy-matching logic: invoice fields -> sanctions-list entries.

Pure (no IO except `load_all_entries`). Imports rapidfuzz (already a project
dep) and the Pydantic models from `app.models`. The DB / HTTP layer never
touches this module's internals — they only call `screen_invoice()` and pass
the result on.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Literal, cast

from rapidfuzz import fuzz

from app.models import (
    Invoice,
    SanctionsMatch,
    SanctionsStatus,
    SanctionsVerdict,
)
from app.sanctions.loader import SdnEntry

SanctionsRole = Literal[
    "seller", "bank", "consignee", "customer", "beneficiary", "payable_to"
]

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Two-stage architecture:
#   Stage 1 (rapidfuzz, cheap, fast) — gates candidates. Threshold tuned for
#       broad recall: catches transliterations, abbreviations, typos. The LLM
#       second stage filters false positives.
#   Stage 2 (LLM, slower, smarter) — judges each candidate.
#
# Scores:
#   - score >= RAPIDFUZZ_AUTO_CONFIRM → skip LLM, treat as confirmed red hit
#     (exact / near-exact match — LLM would just rubber-stamp).
#   - RAPIDFUZZ_CANDIDATE_THRESHOLD <= score < RAPIDFUZZ_AUTO_CONFIRM →
#     send to LLM judge, final verdict = LLM judgment.
#   - score < RAPIDFUZZ_CANDIDATE_THRESHOLD → dropped, never seen.
RAPIDFUZZ_AUTO_CONFIRM: float = 95.0
# Lowered (was 75) — large-context LLM judge filters precision; bias to recall.
RAPIDFUZZ_CANDIDATE_THRESHOLD: float = 65.0

# Kept for backwards compatibility with imports / contract.md docs.
THRESHOLD_HIT: float = RAPIDFUZZ_AUTO_CONFIRM
THRESHOLD_REVIEW: float = RAPIDFUZZ_CANDIDATE_THRESHOLD

# Maximum candidates sent to the LLM per role per invoice. Raised (was 15) —
# large-context judge can absorb 100 candidates per role.
MAX_LLM_CANDIDATES_PER_ROLE: int = 100

# Penalty applied to weak aliases. Lowered (was 10) — LLM filters weak-alias
# noise now, so we don't need to penalize them out of the candidate set.
WEAK_ALIAS_PENALTY: float = 3.0

# Score bonus when invoice country matches the entry's address countries.
# Raised (was 3) — country match is strong same-entity signal.
COUNTRY_BONUS: float = 8.0

# Pure legal-entity suffixes — always stripped. Carry no semantic info.
ALWAYS_STRIP: frozenset[str] = frozenset({
    "ltd", "llc", "inc", "corp", "co", "company", "gmbh", "ag", "sa",
    "sarl", "spa", "srl", "ab", "oy", "plc", "pty", "bv", "nv", "kk",
    "kg", "ohg", "jsc", "ojsc", "pjsc", "fzco", "fze", "llp", "lp",
    "limited", "corporation", "group", "holdings", "the", "and",
})

# Context-strip — business modifiers that carry signal in short names but
# are noise in long names. Only drop if >= 2 tokens survive (otherwise we
# erase the only distinguishing token).
CONTEXT_STRIP: frozenset[str] = frozenset({
    "trading", "international", "global", "industries", "industrial",
    "enterprises", "enterprise", "manufacturing", "import", "export",
    "imports", "exports", "services",
})

# Back-compat: union of both sets, exposed for callers that still reference
# the old name. Do not use internally — call normalize() instead.
LEGAL_SUFFIXES: frozenset[str] = ALWAYS_STRIP | CONTEXT_STRIP


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _strip_diacritics(text: str) -> str:
    return (
        unicodedata.normalize("NFKD", text)
        .encode("ascii", "ignore")
        .decode("ascii")
    )


def normalize(name: str) -> str:
    """Aggressive normalization for Latin-script name comparison.

    - NFKD + ASCII strip (Müller -> muller).
    - Casefold.
    - `&` -> ` and `.
    - Non-alphanumeric -> space, collapse whitespace.
    - Drop ALWAYS_STRIP suffix tokens unconditionally.
    - Drop CONTEXT_STRIP modifiers only if >= 2 tokens survive.
    """
    if not name:
        return ""
    s = _strip_diacritics(name).casefold()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t and t not in ALWAYS_STRIP]
    trimmed = [t for t in tokens if t not in CONTEXT_STRIP]
    if len(trimmed) >= 2:
        tokens = trimmed
    return " ".join(tokens)


def _is_majority_non_latin(s: str) -> bool:
    """True iff `s` has more non-Latin codepoints than Latin letters.

    Combining marks excluded — they attach to letters, not standalone glyphs.
    """
    non_latin = sum(1 for c in s if ord(c) > 127 and not unicodedata.combining(c))
    latin = sum(1 for c in s if c.isascii() and c.isalpha())
    return non_latin > latin


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score(query_norm: str, candidate_norm: str) -> float:
    """rapidfuzz `WRatio` with a length-aware penalty.

    WRatio already handles word order via internal token-sort. We deliberately
    DO NOT take `max(..., token_set_ratio)` — token_set_ratio scores any token
    overlap as if the full string matched, which over-fires on common business
    nouns (e.g. "Trading", "International") even after suffix stripping.

    Length penalty: when the shorter string is < 70% of the longer one,
    scale the score by that ratio. Prevents a 1-token query like "Acme"
    from matching every 5-token candidate that contains "acme" as a substring,
    and kills the common-token noise floor where "BANK"-containing names
    score ~85-90 against each other without real overlap.
    """
    if not query_norm or not candidate_norm:
        return 0.0
    base = float(fuzz.WRatio(query_norm, candidate_norm))
    short = min(len(query_norm), len(candidate_norm))
    long_ = max(len(query_norm), len(candidate_norm))
    if long_ == 0:
        return 0.0
    ratio = short / long_
    if ratio < 0.7:
        return base * (ratio / 0.7)
    return base


def _country_match(invoice_country: str | None, entry_countries: tuple[str, ...]) -> bool:
    if not invoice_country or not entry_countries:
        return False
    needle = invoice_country.casefold().strip()
    return any(needle == c.casefold().strip() for c in entry_countries)


def _entry_field(entry: SdnEntry, name: str, default: Any) -> Any:
    """Safe attribute reader — loader shape is evolving (Agent A).

    Returns `default` when the attribute is missing OR present-but-falsy
    where the default is non-empty. Keeps this module forward-compatible
    with new optional fields the loader may add.
    """
    return getattr(entry, name, default)


def _native_aliases(entry: SdnEntry) -> tuple[tuple[str, str, str], ...]:
    """Return `(name_native, quality, script)` triples or empty when loader
    hasn't surfaced cross-script aliases yet.
    """
    val = _entry_field(entry, "aliases_native", ())
    out: list[tuple[str, str, str]] = []
    for row in val or ():
        # Be permissive about shape — loader may emit tuples or dicts.
        if isinstance(row, dict):
            name = (row.get("name") or "").strip()
            quality = (row.get("quality") or "Strong").strip()
            script = (row.get("script") or "other").strip()
        else:
            try:
                name, quality, script = row  # type: ignore[misc]
            except (TypeError, ValueError):
                continue
            name = (name or "").strip()
            quality = (quality or "Strong").strip()
            script = (script or "other").strip()
        if not name:
            continue
        out.append((name, quality, script))
    return tuple(out)


def _entry_addresses(entry: SdnEntry) -> list[dict]:
    val = _entry_field(entry, "addresses", None)
    if not val:
        return []
    out: list[dict] = []
    for row in val:
        if isinstance(row, dict):
            out.append(dict(row))
    return out


# ---------------------------------------------------------------------------
# Public matching API
# ---------------------------------------------------------------------------


def screen_name(
    role: SanctionsRole,
    query: str | None,
    country: str | None,
    entries: list[SdnEntry],
) -> list[SanctionsMatch]:
    """Score every entry against `query`; return ordered matches above threshold.

    Two code paths:
      - Latin query → normalize(query) vs normalize(primary_name + aliases).
        `matched_script = "latin"`.
      - Majority non-Latin query → raw rapidfuzz partial_ratio vs each
        `aliases_native` entry in its own script. `matched_script` = the
        script bucket of the winning alias (e.g. "arabic", "chinese").

    `role` is attached to each emitted match so the UI/judge can group by it.
    """
    if not query or not query.strip() or not entries:
        return []

    use_native_path = _is_majority_non_latin(query)
    query_norm = "" if use_native_path else normalize(query)

    # When the Latin path produces an empty normalized string AND the query
    # isn't non-Latin, we have nothing to match against. Bail.
    if not use_native_path and not query_norm:
        return []

    matches: list[SanctionsMatch] = []
    for entry in entries:
        best_score = 0.0
        best_label = ""
        best_script: str = "latin"

        if use_native_path:
            # Raw partial_ratio against each native alias in its own script.
            # Normalization would destroy CJK/Arabic glyphs via ASCII strip.
            raw_query = query.strip()
            for name_native, quality, script in _native_aliases(entry):
                cand = name_native.strip()
                if not cand:
                    continue
                raw = float(fuzz.partial_ratio(raw_query, cand))
                if quality.casefold() == "weak":
                    raw -= WEAK_ALIAS_PENALTY
                if raw > best_score:
                    best_score = raw
                    best_label = name_native
                    best_script = script or "other"
        else:
            # Latin path — primary_name + Latin aliases.
            candidates: list[tuple[str, str]] = [(entry.primary_name, "Strong")]
            candidates.extend(entry.aliases)
            for cand_name, quality in candidates:
                cand_norm = normalize(cand_name)
                if not cand_norm:
                    continue
                raw = _score(query_norm, cand_norm)
                if quality == "Weak":
                    raw -= WEAK_ALIAS_PENALTY
                if raw > best_score:
                    best_score = raw
                    best_label = cand_name
                    best_script = "latin"

        if best_score <= 0:
            continue

        if _country_match(country, entry.address_countries):
            best_score = min(100.0, best_score + COUNTRY_BONUS)

        if best_score < THRESHOLD_REVIEW:
            continue

        source_list = str(_entry_field(entry, "source_list", "SDN") or "SDN")
        addresses = _entry_addresses(entry) or None

        matches.append(
            SanctionsMatch(
                role=role,
                uid=entry.uid,
                primary_name=entry.primary_name,
                matched_name=best_label or entry.primary_name,
                sdn_type=entry.sdn_type,
                programs=list(entry.programs),
                score=round(best_score, 2),
                address_countries=list(entry.address_countries),
                treasury_url=entry.treasury_url,
                source_list=source_list,
                matched_script=best_script,
                addresses=addresses,
            )
        )

    matches.sort(key=lambda m: -m.score)
    return matches[:MAX_LLM_CANDIDATES_PER_ROLE]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _list_version_of(entries: list[SdnEntry]) -> str | None:
    for e in entries:
        if e.list_date:
            return e.list_date
    return None


def _annotate_skipped(candidates: list[SanctionsMatch]) -> list[SanctionsMatch]:
    """Mark every candidate `llm_verdict=skipped` — used when no LLM call ran."""
    return [
        c.model_copy(update={
            "llm_verdict": "skipped",
            "llm_reasoning": "LLM judge disabled or no API key — verdict from rapidfuzz only.",
        })
        for c in candidates
    ]


def _split_by_auto_confirm(
    candidates: list[SanctionsMatch],
) -> tuple[list[SanctionsMatch], list[SanctionsMatch]]:
    """Partition candidates into (auto_confirmed, needs_llm)."""
    auto: list[SanctionsMatch] = []
    needs: list[SanctionsMatch] = []
    for c in candidates:
        if c.score >= RAPIDFUZZ_AUTO_CONFIRM:
            auto.append(c.model_copy(update={
                "llm_verdict": "skipped",
                "llm_reasoning": (
                    f"rapidfuzz score {c.score:.1f} >= {RAPIDFUZZ_AUTO_CONFIRM}; "
                    "near-exact match, LLM bypassed."
                ),
            }))
        else:
            needs.append(c)
    return auto, needs


def _final_status(matches: list[SanctionsMatch]) -> SanctionsStatus:
    """Derive overall verdict status from per-match LLM judgements.

    Priority: any confirmed → red. Else any unclear/error → yellow.
    Else (all rejected, or no matches) → green.
    """
    if not matches:
        return cast(SanctionsStatus, "green")
    statuses = {m.llm_verdict for m in matches}
    if "confirmed" in statuses or "skipped" in statuses:
        # `skipped` == high-confidence rapidfuzz auto-confirm, treat as red.
        return cast(SanctionsStatus, "red")
    if "unclear" in statuses or "error" in statuses:
        return cast(SanctionsStatus, "yellow")
    return cast(SanctionsStatus, "green")


# Roles screened in `screen_invoice`. Order = priority for the UI.
# Each tuple: (role, getter(invoice) -> (name, country)).
def _roles_for(invoice: Invoice) -> list[tuple[SanctionsRole, str | None, str | None]]:
    bank_name: str | None = None
    bank_country: str | None = None
    beneficiary: str | None = None
    if invoice.bank is not None:
        bank_name = invoice.bank.bank_name
        bank_country = invoice.bank.bank_country
        beneficiary = invoice.bank.beneficiary
    return [
        ("seller", invoice.company_name or invoice.exporter_name, invoice.company_country),
        ("consignee", invoice.consignee_name, None),
        ("customer", invoice.customer_name, None),
        ("payable_to", invoice.payable_to, None),
        ("bank", bank_name, bank_country),
        ("beneficiary", beneficiary, None),
    ]


def screen_invoice(
    invoice: Invoice,
    entries: list[SdnEntry],
) -> SanctionsVerdict:
    """Two-stage screening: rapidfuzz candidate generation → LLM judge.

    Roles screened (skipped silently when the corresponding field is empty):
      seller, consignee, customer, payable_to, bank, beneficiary.

    Final `status`:
      - "red"     if any candidate is confirmed (or scored >= auto-confirm).
      - "yellow"  if any remaining candidate is "unclear" or LLM errored.
      - "green"   if all candidates rejected, or there were no candidates.
      - "not_checked" if no entries are loaded.
    """
    # Local import (not a cycle — llm_judge only imports app.models).
    # Kept inside the function so unit tests can monkeypatch
    # `app.sanctions.llm_judge.judge_candidates` cleanly.
    import app.sanctions.llm_judge as llm_judge_mod

    list_version = _list_version_of(entries)
    checked_at = _now_iso()

    if not entries:
        return SanctionsVerdict(
            status="not_checked",
            checked_at=checked_at,
            list_version=None,
            error=None,
            matches=[],
            llm_model=None,
            llm_error=None,
        )

    llm_enabled = llm_judge_mod.is_enabled()
    llm_model_used: str | None = llm_judge_mod.DEFAULT_JUDGE_MODEL if llm_enabled else None
    llm_errors: list[str] = []

    all_matches: list[SanctionsMatch] = []
    for role, name, country in _roles_for(invoice):
        if not name or not name.strip():
            continue
        candidates = screen_name(role, name, country, entries)
        judged = _judge_role(
            role,
            name,
            country,
            candidates,
            llm_enabled,
            llm_judge_mod,
            llm_errors,
        )
        all_matches.extend(judged)

    all_matches.sort(key=lambda m: -m.score)

    return SanctionsVerdict(
        status=_final_status(all_matches),
        checked_at=checked_at,
        list_version=list_version,
        error=None,
        matches=all_matches,
        llm_model=llm_model_used,
        llm_error="; ".join(llm_errors) if llm_errors else None,
    )


def _judge_role(
    role: str,
    name: str | None,
    country: str | None,
    candidates: list[SanctionsMatch],
    llm_enabled: bool,
    llm_judge_mod,
    error_accumulator: list[str],
) -> list[SanctionsMatch]:
    """Apply the LLM judge to the rapidfuzz-generated candidates for one role.

    Returns the candidate list annotated with `llm_verdict`/`llm_reasoning`.
    Auto-confirms candidates above `RAPIDFUZZ_AUTO_CONFIRM` (no LLM call).
    """
    if not candidates:
        return []

    auto, needs_llm = _split_by_auto_confirm(candidates)

    if not llm_enabled:
        # No LLM available — every non-auto candidate is rapidfuzz-only.
        return auto + _annotate_skipped(needs_llm)
    if not needs_llm:
        # Everything was auto-confirmed; nothing to judge.
        return auto

    annotated, err = llm_judge_mod.judge_candidates(role, name or "", country, needs_llm)
    if err:
        error_accumulator.append(f"{role}: {err}")
    return auto + annotated


# ---------------------------------------------------------------------------
# Multi-list union loader
# ---------------------------------------------------------------------------


def load_all_entries() -> list:
    """Return the union of every enabled sanctions list (OFAC, UK, EU, CSL).

    Thin wrapper over the provider registry — see ``app.sanctions.providers``.
    Kept here for back-compat with callers that import
    ``sanctions.match.load_all_entries``. Returns whatever is available on disk;
    lists that aren't downloaded yet contribute nothing (best-effort).
    """
    from app.sanctions import providers

    return providers.load_all_entries()
