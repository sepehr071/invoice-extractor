"""Quick PaddleOCR smoke test — invoice → structured JSON."""

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from paddleocr import PaddleOCR


def run(image_path: str, lang: str = "ch") -> dict:
    """Run OCR on one image. lang='ch' handles Chinese+English."""
    ocr = PaddleOCR(
        use_doc_orientation_classify=True,
        use_doc_unwarping=False,
        use_textline_orientation=True,
        lang=lang,
    )
    result = ocr.predict(image_path)
    return result[0] if result else {}


def to_json(result, out_path: str) -> None:
    """Serialize OCR result to JSON file."""
    if hasattr(result, "save_to_json"):
        result.save_to_json(out_path)
    else:
        Path(out_path).write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python ocr_test.py <image_path> [lang]")
        print("  lang: ch (default, Chinese+English), en, japan, korean, ...")
        return 1

    image = sys.argv[1]
    lang = sys.argv[2] if len(sys.argv) > 2 else "ch"

    if not Path(image).exists():
        print(f"ERROR: {image} not found")
        return 1

    print(f"[*] OCR on {image} (lang={lang})")
    result = run(image, lang=lang)

    out_json = Path(image).with_suffix(".json")
    to_json(result, str(out_json))
    print(f"[+] Saved: {out_json}")

    if hasattr(result, "save_to_img"):
        out_img = Path(image).stem + "_ocr.jpg"
        result.save_to_img(out_img)
        print(f"[+] Annotated image: {out_img}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
