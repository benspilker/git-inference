"""Run Duck.ai structural, image, footer, mobile-nav, and screenshot-backed visual checks."""

import argparse
import json
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from site_quality import assess_website  # noqa: E402


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_folder", help="Folder containing files/ or direct index.html and styles.css")
    parser.add_argument("prompt", help="Original website request")
    args = parser.parse_args()
    result = assess_website(args.prompt, args.run_folder)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("final_success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
