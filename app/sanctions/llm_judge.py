"""Stage 2 of sanctions screening: LLM judges rapidfuzz candidates.

Single batched OpenRouter call per role. Sends the invoice party's name +
full invoice context + numbered cards of rapidfuzz candidates with full
alias/address/id detail; receives a structured verdict
(`confirmed | rejected | unclear`) + short reasoning per candidate.

Purpose: kill false positives that rapidfuzz can't tell apart (shared common
tokens like "BANK"/"SAS", different countries, different entity types). The
LLM has world knowledge: it knows "Revolut Bank UAB" is a Lithuanian fintech
unrelated to OFAC's Cuban "HAVIN BANK LIMITED" even though both contain "BANK".

FED FEDS 2025-092 (Allen-Hatfield, Sep 2025): an LLM cascade given full context
reduces sanctions false positives ~92% vs name-only fuzzy alone.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models import SanctionsMatch

logger = logging.getLogger(__name__)

# Judge model resolution (first set wins):
#   SANCTIONS_MODEL  -> SANCTIONS_JUDGE_MODEL (legacy) -> EXTRACT_MODEL -> default
DEFAULT_JUDGE_MODEL = (
    os.environ.get("SANCTIONS_MODEL")
    or os.environ.get("SANCTIONS_JUDGE_MODEL")
    or os.environ.get("EXTRACT_MODEL", "openai/gpt-5-mini")
)

# When set to "0", "false", or "off" (case-insensitive), the stage 2 LLM
# call is skipped entirely; rapidfuzz scores alone determine the verdict.
_DISABLED_VALUES = {"0", "false", "off", "no", ""}


def is_enabled() -> bool:
    raw = os.environ.get("SANCTIONS_LLM_ENABLED", "1").strip().lower()
    return raw not in _DISABLED_VALUES


# ---------------------------------------------------------------------------
# Program-name semantics — common OFAC/BIS/State program codes the LLM will
# see in candidate cards. Used inline in the system prompt so the model
# doesn't have to guess what "SDGT" or "CMIC-EO13959" means.
# ---------------------------------------------------------------------------

_PROGRAM_GLOSSARY: list[tuple[str, str]] = [
    # OFAC counter-terrorism / narcotics
    ("SDGT", "Specially Designated Global Terrorist (EO 13224)"),
    ("SDT", "Specially Designated Terrorist"),
    ("FTO", "Foreign Terrorist Organization (State Dept)"),
    ("SDNTK", "Significant Foreign Narcotics Trafficker (Kingpin Act)"),
    ("SDNT", "Specially Designated Narcotics Trafficker"),
    ("ILLICIT-DRUGS-EO14059", "Illicit drug trade EO 14059"),
    # OFAC Iran
    ("IRAN", "Iran sanctions umbrella"),
    ("IFSR", "Iranian Financial Sanctions Regulations"),
    ("IRGC", "Islamic Revolutionary Guard Corps"),
    ("IRAN-HR", "Iran human-rights abuses"),
    ("IRAN-EO13871", "Iran metals/mining EO 13871"),
    ("IRAN-EO13876", "Iran Supreme Leader's office EO 13876"),
    ("IRAN-CON-ARMS-EO13949", "Iran conventional arms EO 13949"),
    ("13599", "Iran Government List (Section 1245 NDAA, list of GOI entities)"),
    ("561 List", "Iranian financial institutions list (CISADA Section 561)"),
    ("FSE-IR", "Iran Foreign Sanctions Evader"),
    # OFAC Russia / Ukraine
    ("RUSSIA-EO14024", "Russia harmful foreign activities (post-2022)"),
    ("RUSSIA-EO14039", "Russia Nord Stream 2"),
    ("RUSSIA-EO14066", "Russia LNR/DNR import ban"),
    ("RUSSIA-EO14068", "Russia luxury goods / new investment ban"),
    ("RUSSIA-EO14071", "Russia services export ban"),
    ("UKRAINE-EO13660", "Ukraine territorial integrity"),
    ("UKRAINE-EO13661", "Ukraine government of Russia"),
    ("UKRAINE-EO13662", "Ukraine sectoral (energy, finance)"),
    ("UKRAINE-EO13685", "Ukraine Crimea region"),
    ("SSI-RU", "Russia sectoral sanctions identification"),
    ("SSI-UA", "Ukraine sectoral sanctions identification"),
    # OFAC Syria
    ("SYRIA", "Syria sanctions umbrella"),
    ("SYRIA-CAESAR", "Caesar Syria Civilian Protection Act"),
    ("FSE-SY", "Syria Foreign Sanctions Evader"),
    # OFAC other country programs
    ("CUBA", "Cuba sanctions"),
    ("DPRK", "North Korea sanctions"),
    ("DPRK2/DPRK3/DPRK4", "North Korea EO 13687/13722/13810"),
    ("VENEZUELA", "Venezuela sanctions"),
    ("VENEZUELA-EO13884", "Venezuela government blocking"),
    ("BURMA-EO14014", "Burma/Myanmar military"),
    ("BELARUS-EO14038", "Belarus 2021 EO"),
    ("LIBYA2", "Libya sanctions"),
    ("YEMEN", "Yemen sanctions"),
    ("SOMALIA", "Somalia sanctions"),
    ("BALKANS-EO14033", "Western Balkans destabilizers"),
    ("HK-EO13936", "Hong Kong autonomy"),
    # OFAC cyber / corruption / human rights
    ("CYBER2", "Malicious cyber-enabled activities (EO 13694/13757)"),
    ("MAGNIT", "Magnitsky human-rights/corruption"),
    ("GLOMAG", "Global Magnitsky human-rights/corruption (EO 13818)"),
    ("ELECTION-EO13848", "Foreign election interference"),
    ("TCO", "Transnational Criminal Organization (EO 13581)"),
    # OFAC China / strategic competition
    ("CMIC-EO13959", "Chinese Military-Industrial Complex (investment ban)"),
    ("NS-CMIC", "Non-SDN Chinese Military-Industrial Complex list"),
    # OFAC misc
    ("NS-PLC", "Non-SDN Palestinian Legislative Council list"),
    ("NS-MBS", "Non-SDN Menu-Based Sanctions list"),
    ("NS-ISA", "Non-SDN Iran Sanctions Act list"),
    ("FSE", "Foreign Sanctions Evader (generic)"),
    # BIS (Commerce)
    ("Entity List", "BIS export-controlled entity (license required)"),
    ("DPL", "BIS Denied Persons List (export privileges denied)"),
    ("UVL", "BIS Unverified List (end-use verification failed)"),
    ("MEU", "BIS Military End User list"),
    ("MIEU", "BIS Military-Intelligence End User list"),
    # State Department (DDTC / ISN)
    ("AECA Debarred", "Arms Export Control Act debarred (DDTC)"),
    ("Nonproliferation", "State Dept WMD/missile nonproliferation sanctions"),
    ("CAATSA Section 231", "Russia defense-sector secondary sanctions"),
    ("CAATSA Section 235", "CAATSA secondary sanctions menu"),
]


def _format_program_glossary() -> str:
    return "\n".join(f"  - {code}: {meaning}" for code, meaning in _PROGRAM_GLOSSARY)


# ---------------------------------------------------------------------------
# Structured response schema
# ---------------------------------------------------------------------------


class _CandidateJudgement(BaseModel):
    candidate_id: int = Field(
        description="Index of the candidate in the input list (0-based)."
    )
    verdict: Literal["confirmed", "rejected", "unclear"] = Field(
        description=(
            "confirmed = same entity as the listed entry; "
            "rejected = clearly a different entity; "
            "unclear = cannot decide without more info (preferred over rejected when uncertain)."
        )
    )
    reasoning: str = Field(
        description="One to three sentences, max 400 chars. Specific factual reasoning — "
        "cite alias overlap, country match/mismatch, BIC decode, program semantics, "
        "or shared-generic-token problem."
    )


class _BatchJudgement(BaseModel):
    judgements: list[_CandidateJudgement] = Field(
        description="One judgement per input candidate, in the same order."
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = f"""You are a senior OFAC / BIS / State Department sanctions-screening analyst.

You receive an invoice context block (seller, consignee, bank, currency, dates) and a list of fuzzy-matched sanctions-list candidates. For each candidate, decide whether the invoice party IS that listed entity.

# VERDICTS

- "confirmed": invoice party IS the listed entity (same organization, perhaps written differently). Examples: "Bank Melli" vs "BANK MELLI IRAN"; "BMI" (strong alias) vs "BANK MELLI IRAN"; native-script match e.g. "بانک ملی" vs primary "BANK MELLI IRAN".
- "rejected": clearly a different entity. Examples: "Revolut Bank UAB" (Lithuanian fintech) vs "HAVIN BANK LIMITED" (Cuban); "SAS Lassimo" (French logistics) vs "YAKERA Y LANE SAS" (Colombian, "SAS" is just a French/Colombian corp suffix).
- "unclear": cannot decide from available evidence — generic name, no distinguishing context, partial overlap with insufficient signal.

# RECALL-FIRST TIE-BREAK

When in doubt between "rejected" and "unclear", choose "unclear" so the human reviewer sees it. Reserve "rejected" for cases where the candidate is clearly a different entity (different country with no transliteration bridge, different entity type, generic-token-only overlap, mismatched BIC, etc.). Over-rejection is the dominant failure mode of LLM sanctions cascades — don't replicate it.

# ROLE-SPECIFIC EVIDENCE WEIGHTING

- seller / consignee / customer / payable_to: address country + phone country code + corporate-suffix conventions dominate. Country match (with consistent address) = strong confirming evidence; differing country with NO transliteration bridge (e.g. Latin "Acme Trading Riyadh" vs Cyrillic "Акме Москва") = strong rejection signal. `payable_to` is usually the seller itself — sanity-check accordingly.
- bank / beneficiary: the SWIFT BIC is deterministic. Positions 5-6 of the BIC encode ISO 3166 alpha-2 country (CHASUS33 -> US, EBILAEAD -> AE, ICBKCNBJ -> CN). If the invoice BIC's decoded country differs from every address country on the candidate, this is a STRONG rejection signal regardless of name similarity — banks don't lend their name across borders without that branch being explicitly listed. Treat the beneficiary as a seller-style name check, not a bank check.

# PROGRAM CODE GLOSSARY (common codes you will see in candidate cards)

{_format_program_glossary()}

If a candidate's programs list contains country-specific codes (IRAN, CUBA, DPRK, SYRIA, RUSSIA-*), use that to cross-check the invoice country: an OFAC entity on IRAN program is almost always physically located in Iran or operating on Iran's behalf — if the invoice party is squarely European/US with no Iran nexus, that's evidence toward rejection.

# RULES

1. Same legal name with different word order or suffix-only differences ("Acme Trading Ltd" vs "Acme Trading Co") -> confirmed.
2. Country mismatch is STRONG evidence of rejected, but not absolute — sanctioned entities sometimes operate via foreign subsidiaries listed at separate addresses on the same candidate card. Check the full addresses list.
3. Shared generic tokens only ("Bank", "International", "Trading", "SAS", "LLC", "Group") with no other overlap -> rejected.
4. Different entity types (Individual vs Entity, Vessel vs Bank) when the invoice context is clearly one type -> rejected.
5. Transliteration variant of same name (Latin "Melli"/"Meli", "Cuba"/"Kuba"; Latin <-> native-script alias) -> confirmed.
6. Strong vs Weak alias quality matters: a Weak (LowQuality) alias match alone, with no other signal, is rarely enough — push toward unclear unless the name is highly distinctive.
7. Date sanity: if `list_date` predates the invoice by years and the program is still active, the candidate is current; very recently-added entries (list_date within a few months of invoice_date) are still active.

# OUTPUT

Return JSON exactly matching the schema. One judgement per input candidate, in input order. Reasoning <= 400 chars, factual, citing the specific evidence (alias matched, country decoded from BIC, program semantics, transliteration bridge, etc.).
"""


# ---------------------------------------------------------------------------
# Invoice context helper (exported — caller in match.py builds this from Invoice)
# ---------------------------------------------------------------------------


def build_invoice_context(invoice: Any) -> dict:
    """Extract sanctions-relevant fields from an Invoice for the LLM prompt.

    Returns a plain dict (not a Pydantic model) so the formatter can iterate
    keys without caring about which fields are populated. Decodes the bank
    country from the SWIFT BIC positions 5-6 when available.
    """
    bank = getattr(invoice, "bank", None)
    swift = (getattr(bank, "swift_code", None) if bank is not None else None) or ""
    bank_country_from_swift: str | None = None
    if len(swift) >= 6:
        bank_country_from_swift = swift[4:6].upper()

    return {
        "seller_name": getattr(invoice, "company_name", None)
        or getattr(invoice, "exporter_name", None),
        "seller_address": getattr(invoice, "company_address", None),
        "seller_country": getattr(invoice, "company_country", None),
        "seller_phone": getattr(invoice, "company_phone", None),
        "consignee_name": getattr(invoice, "consignee_name", None),
        "consignee_address": getattr(invoice, "consignee_address", None),
        "customer_name": getattr(invoice, "customer_name", None),
        "payable_to": getattr(invoice, "payable_to", None),
        "bank_name": getattr(bank, "bank_name", None) if bank is not None else None,
        "bank_swift": swift or None,
        "bank_country_decoded": bank_country_from_swift,
        "bank_country_stated": getattr(bank, "bank_country", None) if bank is not None else None,
        "bank_address": getattr(bank, "address", None) if bank is not None else None,
        "beneficiary": getattr(bank, "beneficiary", None) if bank is not None else None,
        "invoice_currency": getattr(invoice, "currency", None),
        "invoice_date": getattr(invoice, "invoice_date", None),
        "invoice_total": getattr(invoice, "total_amount", None),
        "incoterm": getattr(invoice, "incoterm", None),
    }


def _format_invoice_context(ctx: dict | None) -> str:
    if not ctx:
        return "INVOICE CONTEXT: (not provided — caller did not plumb invoice context through)\n"
    lines = ["INVOICE CONTEXT:"]
    for key, value in ctx.items():
        if value in (None, "", []):
            continue
        lines.append(f"  {key}: {value}")
    if len(lines) == 1:
        return "INVOICE CONTEXT: (all fields empty)\n"
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Candidate card formatting
# ---------------------------------------------------------------------------


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Tolerant attribute access — candidate models may grow new fields."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _format_alias_list(aliases: Any) -> list[str]:
    """Accept the legacy `[(name, quality)]` shape and the richer
    `[(name, quality, script)]` shape Agent A's loader rewrite emits.
    Returns one human-readable bullet per alias."""
    if not aliases:
        return []
    out: list[str] = []
    for a in aliases:
        if isinstance(a, (list, tuple)):
            name = a[0] if len(a) > 0 else ""
            quality = a[1] if len(a) > 1 else "Strong"
            script = a[2] if len(a) > 2 else None
            suffix = f" [{quality}{', ' + script if script else ''}]"
            out.append(f'        - "{name}"{suffix}')
        elif isinstance(a, dict):
            name = a.get("name") or a.get("whole_name") or ""
            quality = a.get("quality") or ("Strong" if a.get("strong", True) else "Weak")
            script = a.get("script")
            suffix = f" [{quality}{', ' + script if script else ''}]"
            out.append(f'        - "{name}"{suffix}')
        else:
            out.append(f'        - "{a}"')
    return out


def _format_addresses(addresses: Any, fallback_countries: Any) -> list[str]:
    """Prefer Agent A's structured addresses list (dicts of address1/city/
    state_or_province/country). Fall back to a bare country list otherwise."""
    if addresses:
        out: list[str] = []
        for addr in addresses:
            if isinstance(addr, dict):
                parts = [
                    addr.get("address1"),
                    addr.get("city"),
                    addr.get("state_or_province"),
                    addr.get("postal_code"),
                    addr.get("country"),
                ]
                joined = ", ".join(p for p in parts if p)
                if joined:
                    out.append(f"        - {joined}")
            elif isinstance(addr, str):
                out.append(f"        - {addr}")
        if out:
            return out
    if fallback_countries:
        return [f"        - (country only) {c}" for c in fallback_countries]
    return []


def _format_ids(ids: Any) -> list[str]:
    if not ids:
        return []
    out: list[str] = []
    for ident in ids:
        if isinstance(ident, dict):
            id_type = ident.get("id_type") or ident.get("type") or "ID"
            id_number = ident.get("id_number") or ident.get("number") or ""
            country = ident.get("id_country") or ident.get("country") or ""
            tail = f" ({country})" if country else ""
            out.append(f"        - {id_type} {id_number}{tail}")
        elif isinstance(ident, (list, tuple)) and len(ident) >= 2:
            out.append(f"        - {ident[0]} {ident[1]}")
        else:
            out.append(f"        - {ident}")
    return out


def _format_relationships(relationships: Any) -> list[str]:
    if not relationships:
        return []
    out: list[str] = []
    for rel in relationships:
        if isinstance(rel, dict):
            rtype = rel.get("type") or rel.get("relation") or "related-to"
            other = rel.get("other_uid") or rel.get("uid") or rel.get("target") or ""
            name = rel.get("other_name") or rel.get("name") or ""
            out.append(f"        - {rtype}: {other} {name}".rstrip())
        elif isinstance(rel, (list, tuple)):
            out.append(f"        - {' '.join(str(x) for x in rel)}")
        else:
            out.append(f"        - {rel}")
    return out


def _format_programs(programs: Any) -> str:
    if not programs:
        return "(none)"
    # Annotate known program codes inline with their meaning.
    glossary = {code: meaning for code, meaning in _PROGRAM_GLOSSARY}
    decorated: list[str] = []
    for p in programs:
        s = str(p).strip()
        if not s:
            continue
        meaning = glossary.get(s)
        decorated.append(f"{s} ({meaning})" if meaning else s)
    return ", ".join(decorated) if decorated else "(none)"


def _format_candidate_card(i: int, c: SanctionsMatch) -> str:
    """Build the multi-line card for one candidate. Uses tolerant attr access
    so candidates carrying the new `source_list`, `addresses`, `matched_script`
    fields (Agent C's match.py rewrite) and the richer aliases/ids/relationships
    surfaced via SdnEntry (Agent A's loader rewrite) all render gracefully."""
    source_list = _attr(c, "source_list", "SDN")
    list_date = _attr(c, "list_date", None) or "?"
    matched_script = _attr(c, "matched_script", None)
    primary_native = _attr(c, "primary_name_native", None)
    aliases_latin = _attr(c, "aliases", None)
    aliases_native = _attr(c, "aliases_native", None)
    addresses = _attr(c, "addresses", None)
    fallback_countries = _attr(c, "address_countries", None) or []
    ids = _attr(c, "ids", None)
    relationships = _attr(c, "relationships", None)
    treasury_url = _attr(c, "treasury_url", "") or ""

    matched_note = ""
    if c.matched_name and c.matched_name != c.primary_name:
        script_tag = f", script={matched_script}" if matched_script else ""
        matched_note = f' [matched via alias "{c.matched_name}"{script_tag}]'

    lines = [
        f"[{i}] uid={c.uid}  source={source_list}  type={c.sdn_type}  "
        f"rapidfuzz_score={c.score:.1f}  list_date={list_date}",
        f"    primary_name: {c.primary_name}{matched_note}",
    ]
    if primary_native:
        lines.append(f"    primary_name_native: {primary_native}")

    latin_bullets = _format_alias_list(aliases_latin)
    if latin_bullets:
        lines.append("    aliases (latin):")
        lines.extend(latin_bullets)

    native_bullets = _format_alias_list(aliases_native)
    if native_bullets:
        lines.append("    aliases (native):")
        lines.extend(native_bullets)

    addr_bullets = _format_addresses(addresses, fallback_countries)
    if addr_bullets:
        lines.append("    addresses:")
        lines.extend(addr_bullets)

    id_bullets = _format_ids(ids)
    if id_bullets:
        lines.append("    ids:")
        lines.extend(id_bullets)

    rel_bullets = _format_relationships(relationships)
    if rel_bullets:
        lines.append("    relationships:")
        lines.extend(rel_bullets)

    lines.append(f"    programs: {_format_programs(c.programs)}")
    if treasury_url:
        lines.append(f"    treasury_url: {treasury_url}")

    return "\n".join(lines)


def _build_user_prompt(
    role: str,
    query_name: str,
    query_country: str | None,
    candidates: list[SanctionsMatch],
    invoice_context: dict | None,
) -> str:
    parts: list[str] = [_format_invoice_context(invoice_context)]
    parts.append(
        f"ROLE BEING SCREENED: {role}\n"
        f"  name: {query_name}\n"
        f"  country (extracted): {query_country or '(not specified)'}\n"
    )
    parts.append(f"SANCTIONS-LIST CANDIDATES (N={len(candidates)}):")
    for i, c in enumerate(candidates):
        parts.append(_format_candidate_card(i, c))
    parts.append("")
    parts.append(
        "Judge each candidate independently. Output JSON: "
        '{"judgements": [{"candidate_id": int, "verdict": "confirmed|rejected|unclear", '
        '"reasoning": "<=400 chars"}, ...]} — one entry per candidate, in input order.'
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def judge_candidates(
    role: str,
    query_name: str,
    query_country: str | None,
    candidates: list[SanctionsMatch],
    model: str | None = None,
    invoice_context: dict | None = None,
) -> tuple[list[SanctionsMatch], str | None]:
    """Annotate each candidate with `llm_verdict` + `llm_reasoning`.

    Args:
        role: free-form tag ("seller", "bank", "consignee", "beneficiary", ...).
        query_name: the invoice party name being screened.
        query_country: best-known country for that party (extracted or decoded).
        candidates: rapidfuzz-generated candidates to judge.
        model: judge model override; falls back to `SANCTIONS_MODEL`, then
            legacy `SANCTIONS_JUDGE_MODEL`, then `EXTRACT_MODEL`, then
            `openai/gpt-5-mini`. Endpoint is env-driven via `LLM_BASE_URL`.
        invoice_context: full invoice context dict (build via
            `build_invoice_context(invoice)`). Optional — omitting keeps
            back-compat with existing callers but loses recall.

    Returns:
        (annotated_candidates, error_message_or_None)

    On any LLM/network failure, every input candidate is annotated with
    `llm_verdict="error"` and the error message is returned as the second
    element of the tuple. The caller decides what to do (typically falls
    back to rapidfuzz-only scoring for status determination).
    """
    if not candidates:
        return candidates, None

    chosen_model = model or DEFAULT_JUDGE_MODEL

    # Endpoint is env-driven (LLM_BASE_URL); reuse the extract builder so the
    # base_url / dummy-key-for-local-Ollama logic stays in one place. Falls
    # back to a local client construction if extract can't be imported.
    try:
        from extract import _build_client  # shared env-driven OpenAI client
    except Exception as exc:  # noqa: BLE001 — degrade to local construction
        try:
            from openai import OpenAI
        except ImportError as imp_exc:
            err = f"openai package not available: {imp_exc}"
            return _annotate_all(candidates, "error", err), err
        base_url = os.environ.get("LLM_BASE_URL", "").strip() or "https://openrouter.ai/api/v1"
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip() or "ollama"
        client = OpenAI(api_key=api_key, base_url=base_url)
        logger.debug("extract._build_client unavailable (%s); built local client", exc)
    else:
        client = _build_client()

    extra_body: dict = {"reasoning": {"effort": "medium"}}
    if "qwen3" in chosen_model.lower() or os.environ.get(
        "LLM_DISABLE_THINKING", ""
    ).strip().lower() in {"1", "true", "yes", "on"}:
        extra_body["think"] = False

    user_prompt = _build_user_prompt(
        role, query_name, query_country, candidates, invoice_context
    )

    try:
        completion = client.chat.completions.parse(
            model=chosen_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format=_BatchJudgement,
            temperature=0.1,
            extra_headers={
                "HTTP-Referer": "https://example.com/invoice-ai",
                "X-Title": "invoice-ai/sanctions",
            },
            extra_body=extra_body,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM judge call failed")
        return _annotate_all(candidates, "error", str(exc)), str(exc)

    parsed = completion.choices[0].message.parsed
    if parsed is None:
        err = "LLM returned no parseable JSON"
        return _annotate_all(candidates, "error", err), err

    # Map judgements onto candidates by candidate_id.
    by_id = {j.candidate_id: j for j in parsed.judgements}
    annotated: list[SanctionsMatch] = []
    for i, cand in enumerate(candidates):
        j = by_id.get(i)
        if j is None:
            annotated.append(
                cand.model_copy(update={
                    "llm_verdict": "error",
                    "llm_reasoning": "LLM omitted this candidate from its response",
                })
            )
        else:
            annotated.append(
                cand.model_copy(update={
                    "llm_verdict": j.verdict,
                    "llm_reasoning": j.reasoning.strip()[:500],
                })
            )
    return annotated, None


def _annotate_all(
    candidates: list[SanctionsMatch],
    verdict: str,
    reasoning: str,
) -> list[SanctionsMatch]:
    return [
        c.model_copy(update={"llm_verdict": verdict, "llm_reasoning": reasoning})
        for c in candidates
    ]
