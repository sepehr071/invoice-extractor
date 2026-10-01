"""PP-StructureV3 — full invoice pipeline: layout + tables + KIE."""

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from paddleocr import PPStructureV3


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python structure_test.py <image_or_pdf>")
        return 1

    src = sys.argv[1]
    if not Path(src).exists():
        print(f"ERROR: {src} not found")
        return 1

    pipeline = PPStructureV3(
        use_doc_orientation_classify=True,
        use_doc_unwarping=False,
        use_textline_orientation=True,
    )
    print(f"[*] PP-StructureV3 on {src}")
    results = pipeline.predict(src)

    out_dir = Path("output")
    out_dir.mkdir(exist_ok=True)

    for i, res in enumerate(results):
        res.save_to_json(str(out_dir / f"page_{i}.json"))
        res.save_to_markdown(str(out_dir / f"page_{i}.md"))
        print(f"[+] page_{i}.json + page_{i}.md")

    print(f"[+] Done. See {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
