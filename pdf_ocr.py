"""PDF → per-page PNG → OCR → merged JSON."""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import pypdfium2 as pdfium
from PIL import Image, ImageOps
from paddleocr import PaddleOCR

# Decompression-bomb guards: cap total page count and clamp each page's
# rendered resolution to a pixel budget, so a crafted PDF (thousands of pages,
# or pages declaring enormous dimensions) cannot exhaust memory or pin the
# worker. Both overridable via env.
MAX_PDF_PAGES = int(os.environ.get("MAX_PDF_PAGES", "100"))
MAX_PAGE_MEGAPIXELS = float(os.environ.get("MAX_PAGE_MEGAPIXELS", "40"))


def pdf_to_images(pdf_path: Path, out_dir: Path, dpi: int = 300) -> list[Path]:
    """Render every page to PNG. Return list of paths.

    Hardened against decompression bombs: rejects PDFs over ``MAX_PDF_PAGES``,
    and clamps any page whose rendered size would exceed ``MAX_PAGE_MEGAPIXELS``
    down to that budget (effectively a lower per-page dpi) rather than
    allocating an unbounded bitmap.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    base_scale = dpi / 72.0
    max_px = int(MAX_PAGE_MEGAPIXELS * 1_000_000)
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        n_pages = len(pdf)
        if n_pages > MAX_PDF_PAGES:
            raise ValueError(f"PDF has {n_pages} pages; maximum allowed is {MAX_PDF_PAGES}")
        paths = []
        for i, page in enumerate(pdf):
            w_pt, h_pt = page.get_size()
            scale = base_scale
            est_px = (w_pt * scale) * (h_pt * scale)
            if max_px > 0 and est_px > max_px:
                # shrink linearly so width*height lands on the pixel budget
                scale = base_scale * (max_px / est_px) ** 0.5
            img = page.render(scale=scale).to_pil()
            page_path = out_dir / f"page_{i + 1:03d}.png"
            img.save(page_path, "PNG")
            paths.append(page_path)
            print(f"[*] page {i + 1}/{n_pages} -> {page_path.name}")
    finally:
        pdf.close()
    return paths


def image_to_page(image_path: Path, out_dir: Path) -> list[Path]:
    """Normalize a single raster image (jpg/png/webp/bmp) into pages/page_001.png
    so images and PDFs flow through the identical downstream OCR pipeline."""
    out_dir.mkdir(parents=True, exist_ok=True)
    page_path = out_dir / "page_001.png"
    with Image.open(image_path) as img:
        # exif_transpose first so phone photos land upright; convert to RGB to
        # drop alpha/palette, and save PNG 1:1 — OCR coords must match this PNG.
        ImageOps.exif_transpose(img).convert("RGB").save(page_path, "PNG")
    return [page_path]


def ocr_page(ocr: PaddleOCR, image_path: Path) -> dict:
    """Run OCR on one image. Extract texts + boxes + scores.

    Output contract is the classic ``{texts, boxes, scores}`` shape regardless
    of OCR version. PP-OCRv5's ``predict()`` exposes recognition polygons as
    ``rec_polys`` (1:1 with ``rec_texts``); ``dt_polys`` is the detection-stage
    fallback. Boxes are 4-point integer polygons.
    """
    result = ocr.predict(str(image_path))
    if not result:
        return {"texts": [], "boxes": [], "scores": []}
    r = result[0]
    polys = r.get("rec_polys")
    if polys is None:
        polys = r.get("dt_polys", [])
    return {
        "texts": list(r.get("rec_texts", [])),
        "boxes": [[[int(x), int(y)] for x, y in poly] for poly in polys],
        "scores": [float(s) for s in r.get("rec_scores", [])],
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python pdf_ocr.py <pdf_path> [lang] [dpi]")
        print("  lang: en (default), fa (Persian/Farsi), ar, ch, japan, korean, ...")
        print("  dpi: 400 (default)")
        return 1

    pdf_path = Path(sys.argv[1])
    lang = sys.argv[2] if len(sys.argv) > 2 else "en"
    dpi = int(sys.argv[3]) if len(sys.argv) > 3 else 400
    ocr_version = "PP-OCRv5"

    if not pdf_path.exists():
        print(f"ERROR: {pdf_path} not found")
        return 1

    out_root = Path("output") / pdf_path.stem
    pages_dir = out_root / "pages"
    print(f"[*] PDF: {pdf_path}  lang={lang}  dpi={dpi}")
    print(f"[*] Output: {out_root}")

    image_paths = pdf_to_images(pdf_path, pages_dir, dpi=dpi)

    print(f"[*] Loading PaddleOCR ({ocr_version}, lang={lang}, max accuracy, fixed orientation)...")
    ocr = PaddleOCR(
        device="gpu:0",
        ocr_version=ocr_version,
        lang=lang,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )

    merged = {
        "source": str(pdf_path),
        "lang": lang,
        "dpi": dpi,
        "page_count": len(image_paths),
        "pages": [],
    }
    for i, img_path in enumerate(image_paths, start=1):
        print(f"[*] OCR page {i}/{len(image_paths)}: {img_path.name}")
        page_result = ocr_page(ocr, img_path)
        page_result["page"] = i
        page_result["image"] = str(img_path.relative_to(out_root))
        merged["pages"].append(page_result)

    out_json = out_root / "ocr.json"
    out_json.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[+] Wrote {out_json}")
    total_lines = sum(len(p["texts"]) for p in merged["pages"])
    print(f"[+] {len(image_paths)} pages, {total_lines} text lines total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
