"""One-shot: PDF -> per-page PNG -> OCR -> LLM extraction -> structured JSON."""

import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from paddleocr import PaddleOCR

from extract import DEFAULT_MODEL, extract, ocr_to_text
from pdf_ocr import ocr_page, pdf_to_images


def fmt(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    m, s = divmod(seconds, 60)
    return f"{int(m)}m{s:.1f}s"


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python process.py <pdf_path> [lang=en] [dpi=400] [model] [min_score=0.5]")
        print("  lang: en (default), arabic (Persian/Farsi), ch, japan, korean, ...")
        print("  dpi: 400 (default)")
        print(f"  model: {DEFAULT_MODEL} (default, from .env EXTRACT_MODEL)")
        print("  min_score: OCR confidence threshold 0.0-1.0 (default 0.5). Lines below are dropped before LLM call.")
        return 1

    pdf_path = Path(sys.argv[1])
    lang = sys.argv[2] if len(sys.argv) > 2 else "en"
    dpi = int(sys.argv[3]) if len(sys.argv) > 3 else 400
    model = sys.argv[4] if len(sys.argv) > 4 else DEFAULT_MODEL
    min_score = float(sys.argv[5]) if len(sys.argv) > 5 else 0.5
    ocr_version = "PP-OCRv5"

    if not 0.0 <= min_score <= 1.0:
        print(f"ERROR: min_score must be in [0.0, 1.0], got {min_score}")
        return 1

    if not pdf_path.exists():
        print(f"ERROR: {pdf_path} not found")
        return 1

    out_root = Path("output") / pdf_path.stem
    pages_dir = out_root / "pages"
    print(f"[*] PDF: {pdf_path}")
    print(f"[*] lang={lang}  dpi={dpi}  model={model}  min_score={min_score}")
    print(f"[*] Output dir: {out_root}")

    timings: dict[str, float] = {}
    t0 = time.perf_counter()

    print("\n[1/3] Rendering PDF pages...")
    t = time.perf_counter()
    image_paths = pdf_to_images(pdf_path, pages_dir, dpi=dpi)
    timings["render"] = time.perf_counter() - t
    print(f"  -> {len(image_paths)} page(s) rendered in {fmt(timings['render'])}")

    print(f"\n[2/3] Loading PaddleOCR ({ocr_version}, lang={lang}, device=gpu, max accuracy fp32, fixed orientation)...")
    t = time.perf_counter()
    ocr = PaddleOCR(
        device="gpu:0",
        ocr_version=ocr_version,
        lang=lang,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )
    timings["ocr_load"] = time.perf_counter() - t
    print(f"  -> PaddleOCR ready in {fmt(timings['ocr_load'])}")

    merged = {
        "source": str(pdf_path),
        "lang": lang,
        "dpi": dpi,
        "page_count": len(image_paths),
        "pages": [],
    }
    per_page_times = []
    for i, img_path in enumerate(image_paths, start=1):
        t = time.perf_counter()
        page_result = ocr_page(ocr, img_path)
        page_result["page"] = i
        page_result["image"] = str(img_path.relative_to(out_root))
        merged["pages"].append(page_result)
        dt = time.perf_counter() - t
        per_page_times.append(dt)
        print(f"  [*] OCR page {i}/{len(image_paths)}: {img_path.name}  ({fmt(dt)}, {len(page_result['texts'])} lines)")

    timings["ocr_pages"] = sum(per_page_times)
    timings["ocr_avg_per_page"] = sum(per_page_times) / len(per_page_times) if per_page_times else 0

    ocr_json_path = out_root / "ocr.json"
    ocr_json_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    total_lines = sum(len(p["texts"]) for p in merged["pages"])
    print(f"[+] OCR done: {len(image_paths)} pages, {total_lines} lines in {fmt(timings['ocr_pages'])} (avg {fmt(timings['ocr_avg_per_page'])}/page)")

    print(f"\n[3/3] Calling LLM ({model}) for structured extraction...")
    t = time.perf_counter()
    text = ocr_to_text(merged, min_score=min_score)
    result = extract(text, model=model)
    timings["llm"] = time.perf_counter() - t
    print(f"  -> LLM returned in {fmt(timings['llm'])}")

    extracted_path = out_root / "extracted.json"
    extracted_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    timings["total"] = time.perf_counter() - t0

    print(f"[+] Wrote {extracted_path}")
    print()
    print(json.dumps(result, ensure_ascii=False, indent=2))

    print()
    print("=" * 60)
    print("TIMING BREAKDOWN")
    print("=" * 60)
    print(f"  [1] PDF render        : {fmt(timings['render']):>10}")
    print(f"  [2] PaddleOCR load    : {fmt(timings['ocr_load']):>10}")
    print(f"  [2] OCR ({len(image_paths)} pages)   : {fmt(timings['ocr_pages']):>10}  (avg {fmt(timings['ocr_avg_per_page'])}/page)")
    print(f"  [3] LLM extract       : {fmt(timings['llm']):>10}")
    print(f"  {'─' * 35}")
    print(f"  TOTAL                 : {fmt(timings['total']):>10}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
