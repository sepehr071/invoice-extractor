"""Filesystem layout for job artifacts.

Layout:
    output/<job_id>/source.<ext>                   # uploaded source (pdf or raster image)
    output/<job_id>/pages/page_NNN.png             # original rendered pages
    output/<job_id>/pages_masked/page_NNN.png      # ROI-masked pages (only when ROIs drawn)
    output/<job_id>/rois.json                      # user-drawn region rects per page
    output/<job_id>/ocr.json
    output/<job_id>/extracted.json
    output/<job_id>/result.txt                     # free-form prompt-mode result
    output/<job_id>/approved.json
    data/app.db
"""

from pathlib import Path

OUTPUT_ROOT = Path("output")
DATA_ROOT = Path("data")


def ensure_dirs() -> None:
    """Create root directories if they don't exist. Idempotent."""
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)


def job_dir(job_id: str) -> Path:
    return OUTPUT_ROOT / job_id


def job_pages_dir(job_id: str) -> Path:
    return OUTPUT_ROOT / job_id / "pages"


def job_pdf_path(job_id: str) -> Path:
    return OUTPUT_ROOT / job_id / "source.pdf"


def job_source_path(job_id: str, ext: str) -> Path:
    """Path for an uploaded source of arbitrary type, e.g. ``source.png``.

    Generalizes ``job_pdf_path`` so images and PDFs share one slot; ``ext`` is
    the extension without a leading dot.
    """
    return OUTPUT_ROOT / job_id / f"source.{ext.lstrip('.')}"


def find_job_source(job_id: str) -> Path | None:
    """Return the stored source file regardless of extension, or None.

    Lets downstream code locate the upload without knowing whether it was a
    PDF or an image.
    """
    return next(iter(sorted((OUTPUT_ROOT / job_id).glob("source.*"))), None)


def job_result_text(job_id: str) -> Path:
    return OUTPUT_ROOT / job_id / "result.txt"


def job_page_image(job_id: str, page: int) -> Path:
    return OUTPUT_ROOT / job_id / "pages" / f"page_{page:03d}.png"


def job_ocr_json(job_id: str) -> Path:
    return OUTPUT_ROOT / job_id / "ocr.json"


def job_extracted_json(job_id: str) -> Path:
    return OUTPUT_ROOT / job_id / "extracted.json"


def job_approved_json(job_id: str) -> Path:
    return OUTPUT_ROOT / job_id / "approved.json"


def job_rois_json(job_id: str) -> Path:
    return OUTPUT_ROOT / job_id / "rois.json"


def job_pages_masked_dir(job_id: str) -> Path:
    return OUTPUT_ROOT / job_id / "pages_masked"


def job_page_masked_image(job_id: str, page: int) -> Path:
    return OUTPUT_ROOT / job_id / "pages_masked" / f"page_{page:03d}.png"
