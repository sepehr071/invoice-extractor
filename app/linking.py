"""Field ↔ bounding-box fuzzy matcher.

Given a structured `Invoice` (from `extract.extract`) and the raw per-page
OCR output (the `pages` list inside `ocr.json`), produce:

    * `list[FieldLink]`        — one entry per leaf scalar field
    * `list[LineItemLink]`     — one entry per line-item row

The match uses rapidfuzz `partial_ratio` against each token's text, then
merges adjacent token polygons when the value spans multiple tokens.

No DB access here — pure data in / data out — so the worker can call this
and then persist whatever shape it wants.
"""

from __future__ import annotations

from typing import Any, Iterator

from rapidfuzz import fuzz

from app.models import FieldLink, Invoice, LineItem, LineItemLink, Polygon

__all__ = [
    "Polygon",
    "link_dict_to_bboxes",
    "link_fields_to_bboxes",
    "walk_dict_leaves",
    "walk_invoice_leaves",
    "merge_polygons",
]


# Match-quality threshold below which a field is considered "unlinked".
# Matches contract.md: "score < 60 → mark unlinked".
SCORE_THRESHOLD: float = 60.0

# Vertical proximity (source-image pixels) used when expanding a line-item
# row's bbox to cover every token on the same visual row.
ROW_Y_TOLERANCE: int = 20


# ---------------------------------------------------------------------------
# Leaf walker
# ---------------------------------------------------------------------------


def _is_empty(value: Any) -> bool:
    """A leaf is 'empty' when it has no information worth linking."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def walk_dict_leaves(
    data: dict, *, skip_keys: frozenset[str] = frozenset()
) -> Iterator[tuple[str, Any]]:
    """Yield `(field_path, value)` for every non-empty leaf in an arbitrary dict.

    Fully recursive to any depth. Path convention (matches `contract.md`):
      * dict key      → child path `f"{path}.{k}"`
      * list index    → child path `f"{path}[{i}]"`
      * scalar leaf   → emitted as `(path, value)`
    `skip_keys` is honored only at the TOP level (e.g. skip "line_items" so an
    Invoice's rows are linked separately with row-strip bboxes).
    """

    def _walk(value: Any, path: str) -> Iterator[tuple[str, Any]]:
        if isinstance(value, dict):
            for k, v in value.items():
                child = f"{path}.{k}" if path else str(k)
                yield from _walk(v, child)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                child = f"{path}[{i}]"
                if isinstance(item, (dict, list)):
                    yield from _walk(item, child)
                elif not _is_empty(item):
                    yield child, item
        elif not _is_empty(value):
            yield path, value

    for key, value in data.items():
        if key in skip_keys:
            continue
        yield from _walk(value, str(key))


def walk_invoice_leaves(invoice: Invoice) -> Iterator[tuple[str, Any]]:
    """Yield `(field_path, value)` for every non-empty leaf field.

    Follows the dot-notation convention in `contract.md`:
      * top-level scalar         → `invoice_number`
      * nested model field       → `bank.swift_code`
      * top-level string list    → `remarks[0]`, `extra_notes[0]`, …
      * nested-model list item   → `bank.accounts[0].currency`,
                                   `bank.accounts[0].account_number`, …
      * nested string list       → `bank.notices[0]`, …
    The `line_items` list is intentionally skipped — those are linked
    separately via `link_fields_to_bboxes` so callers can attach a row bbox.
    """
    yield from walk_dict_leaves(invoice.model_dump(), skip_keys=frozenset({"line_items"}))


# ---------------------------------------------------------------------------
# Polygon helpers
# ---------------------------------------------------------------------------


def merge_polygons(polygons: list[Polygon]) -> Polygon:
    """Return the axis-aligned bounding rectangle covering every input polygon.

    Output polygon has the canonical 4-point order TL → TR → BR → BL.
    """
    xs: list[int] = []
    ys: list[int] = []
    for poly in polygons:
        for point in poly:
            xs.append(int(point[0]))
            ys.append(int(point[1]))

    if not xs or not ys:
        # Should not happen in normal usage; return a degenerate point.
        return [[0, 0], [0, 0], [0, 0], [0, 0]]

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    return [
        [min_x, min_y],
        [max_x, min_y],
        [max_x, max_y],
        [min_x, max_y],
    ]


def _poly_y_center(poly: Polygon) -> float:
    """Vertical midpoint of a polygon (avg y of its 4 points)."""
    return sum(int(p[1]) for p in poly) / max(1, len(poly))


# ---------------------------------------------------------------------------
# Field → bbox matching
# ---------------------------------------------------------------------------


def _length_aware_score(needle: str, candidate: str) -> float:
    """Length-penalized partial_ratio.

    `fuzz.partial_ratio` returns 100 whenever the shorter string is a
    substring of the longer — so a single character like "2" scores 100
    against "CZCBCN2X". That false-positive lets tiny tokens win matches
    against long needles. Penalize by the candidate-to-needle length ratio
    when the candidate is shorter than the needle.
    """
    if not needle or not candidate:
        return 0.0
    base = float(fuzz.partial_ratio(needle, candidate))
    nlen = len(needle)
    clen = len(candidate)
    if clen >= nlen:
        return base
    return base * (clen / nlen)


def _best_match_on_page(value: str, texts: list[str]) -> tuple[int, float] | None:
    """Length-aware partial-ratio scan over every token on the page.

    Returns `(token_index, score)` for the best match, or None if the page
    has no tokens. Also tries 2- and 3-token sliding windows so multi-token
    values (e.g. "John Smith" split across two OCR boxes) get caught at
    the matching stage rather than only via post-hoc expansion.
    """
    if not texts:
        return None

    best_idx = -1
    best_score = -1.0

    # Single-token comparisons.
    for i, token in enumerate(texts):
        s = _length_aware_score(value, token)
        if s > best_score:
            best_score = s
            best_idx = i

    # 2- and 3-token windows. Anchor index = start of window so the
    # adjacency-expander downstream can grow from there.
    for window in (2, 3):
        if len(texts) < window:
            continue
        for start in range(len(texts) - window + 1):
            joined = " ".join(texts[start : start + window])
            s = _length_aware_score(value, joined)
            if s > best_score:
                best_score = s
                best_idx = start

    if best_idx < 0:
        return None
    return best_idx, best_score


def _find_adjacent_token_run(
    value: str, texts: list[str], anchor_idx: int
) -> list[int]:
    """Expand from `anchor_idx` to cover adjacent tokens that improve the match.

    Walks left and right of the anchor as long as concatenating the neighbour's
    text into the run keeps the partial_ratio score within a small margin of
    the anchor's score. This catches values like "ACME TRADING CO." that
    PaddleOCR splits across 3-4 tokens.
    """
    if not value or not texts:
        return [anchor_idx]

    target = str(value)
    anchor_score = fuzz.partial_ratio(target, texts[anchor_idx])

    # Token expansion only helps for multi-word values. For very short values
    # (single number, single word) be conservative and don't expand.
    if len(target.split()) <= 1 and len(target) <= 6:
        return [anchor_idx]

    indices = [anchor_idx]

    def joined(idx_list: list[int]) -> str:
        return " ".join(texts[i] for i in sorted(idx_list))

    # Walk right.
    right = anchor_idx + 1
    while right < len(texts):
        candidate = indices + [right]
        new_score = fuzz.partial_ratio(target, joined(candidate))
        if new_score + 1 < anchor_score:
            break
        indices = candidate
        anchor_score = max(anchor_score, new_score)
        if new_score >= 99:
            break
        right += 1

    # Walk left.
    left = anchor_idx - 1
    while left >= 0:
        candidate = [left] + indices
        new_score = fuzz.partial_ratio(target, joined(candidate))
        if new_score + 1 < anchor_score:
            break
        indices = candidate
        anchor_score = max(anchor_score, new_score)
        if new_score >= 99:
            break
        left -= 1

    return sorted(indices)


def _link_value(
    value: Any, ocr_pages: list[dict]
) -> tuple[int | None, Polygon | None, float]:
    """Find best page + bbox + score for `value` across all OCR pages.

    Returns `(page_number_or_None, polygon_or_None, score)`. Pages are
    1-indexed (matching the `page` field on each ocr_pages entry).
    """
    if value is None:
        return None, None, 0.0

    needle = str(value).strip()
    if not needle:
        return None, None, 0.0

    best_page: int | None = None
    best_idx: int | None = None
    best_score: float = -1.0
    best_texts: list[str] | None = None
    best_boxes: list[Polygon] | None = None

    for page in ocr_pages:
        texts = page.get("texts") or []
        if not texts:
            continue

        match = _best_match_on_page(needle, texts)
        if match is None:
            continue
        idx, score = match
        if score > best_score:
            best_score = score
            best_idx = idx
            best_page = int(page.get("page", 0)) or None
            best_texts = texts
            best_boxes = page.get("boxes") or []

    if (
        best_score < SCORE_THRESHOLD
        or best_idx is None
        or best_page is None
        or best_texts is None
        or best_boxes is None
    ):
        return None, None, max(0.0, best_score if best_score > 0 else 0.0)

    # Expand to adjacent tokens for multi-token values, then merge polygons.
    run = _find_adjacent_token_run(needle, best_texts, best_idx)
    run = [i for i in run if 0 <= i < len(best_boxes)]
    polys = [best_boxes[i] for i in run]
    bbox = merge_polygons(polys) if polys else None

    return best_page, bbox, float(best_score)


def _link_line_item_row(
    row: LineItem, ocr_pages: list[dict]
) -> tuple[int | None, Polygon | None]:
    """Find the page + horizontal-strip bbox for a single line item.

    Anchor token is product_name (else size, else any non-empty leaf field).
    Once the anchor token is found, expand the bbox horizontally to every
    token whose vertical centre is within `ROW_Y_TOLERANCE` pixels — that
    captures the full row.
    """
    anchor_value: Any = row.product_name
    if anchor_value is None or (isinstance(anchor_value, str) and not anchor_value.strip()):
        anchor_value = row.size

    if anchor_value is None or (isinstance(anchor_value, str) and not anchor_value.strip()):
        # Fall back to the first non-empty scalar on the row. Skip purely
        # derived / generic fields that make poor OCR anchors.
        _BAD_ANCHORS = {"no", "category", "count_type", "unit", "line_currency"}
        for sub_key, sub_value in row.model_dump().items():
            if sub_key in _BAD_ANCHORS:
                continue
            if not _is_empty(sub_value):
                anchor_value = sub_value
                break

    if anchor_value is None:
        return None, None

    needle = str(anchor_value).strip()
    if not needle:
        return None, None

    best_page_obj: dict | None = None
    best_idx: int | None = None
    best_score: float = -1.0

    for page in ocr_pages:
        texts = page.get("texts") or []
        if not texts:
            continue
        match = _best_match_on_page(needle, texts)
        if match is None:
            continue
        idx, score = match
        if score > best_score:
            best_score = score
            best_idx = idx
            best_page_obj = page

    if best_score < SCORE_THRESHOLD or best_idx is None or best_page_obj is None:
        return None, None

    boxes = best_page_obj.get("boxes") or []
    if best_idx >= len(boxes):
        return None, None

    anchor_box = boxes[best_idx]
    anchor_y = _poly_y_center(anchor_box)

    # Collect every token on the same horizontal row.
    row_polys: list[Polygon] = []
    for poly in boxes:
        if abs(_poly_y_center(poly) - anchor_y) <= ROW_Y_TOLERANCE:
            row_polys.append(poly)

    if not row_polys:
        row_polys = [anchor_box]

    page_num = int(best_page_obj.get("page", 0)) or None
    return page_num, merge_polygons(row_polys)


def link_fields_to_bboxes(
    invoice: Invoice, ocr_pages: list[dict]
) -> tuple[list[FieldLink], list[LineItemLink]]:
    """Top-level entry point used by `app.jobs.run_job`.

    Args:
        invoice:     parsed `Invoice` Pydantic model from `extract.extract()`.
        ocr_pages:   raw `ocr.json["pages"]` list — each element has keys
                     `page`, `texts`, `boxes`, `scores`.

    Returns:
        A pair `(field_links, line_item_links)` ready to persist or hand to
        the frontend.
    """
    field_links: list[FieldLink] = []
    for field_path, value in walk_invoice_leaves(invoice):
        page, bbox, score = _link_value(value, ocr_pages)
        field_links.append(
            FieldLink(
                field_path=field_path,
                value=value,
                page=page,
                bbox=bbox,
                score=score,
                edited=False,
            )
        )

    line_item_links: list[LineItemLink] = []
    for idx, row in enumerate(invoice.line_items):
        page, bbox = _link_line_item_row(row, ocr_pages)
        line_item_links.append(
            LineItemLink(
                row_index=idx,
                data=row,
                page=page,
                bbox=bbox,
                edited=False,
            )
        )

    return field_links, line_item_links


def link_dict_to_bboxes(data: dict, ocr_pages: list[dict]) -> list[FieldLink]:
    """Link every scalar leaf of an arbitrary dict to its best OCR bbox.

    Used by the prompt-driven (JSON output) path where there is no fixed
    `Invoice` schema — the inferred dict is walked generically and each scalar
    is matched best-effort. Sub-threshold leaves keep null page/bbox (the same
    "unlinked" behavior `_link_value` returns), so this never drops fields.
    """
    field_links: list[FieldLink] = []
    for field_path, value in walk_dict_leaves(data):
        if not isinstance(value, (str, int, float, bool, type(None))):
            continue
        page, bbox, score = _link_value(value, ocr_pages)
        field_links.append(
            FieldLink(
                field_path=field_path,
                value=value,
                page=page,
                bbox=bbox,
                score=score,
                edited=False,
            )
        )
    return field_links
