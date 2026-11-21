"""Evaluate similar-case recommendation consistency without exposing source prose."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from catalog_common import DEFAULT_CATALOG
from catalog_evaluation import evaluate_similar_cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--sample-size", type=int, default=200)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261022)
    parser.add_argument("--report", type=Path, help="Create a new aggregate report under docs/")
    args = parser.parse_args()
    report_path = (ROOT / args.report).resolve() if args.report else None
    if report_path and (not report_path.is_relative_to(ROOT / "docs") or report_path.exists()):
        parser.error("--report must be a new file under docs/")
    report = evaluate_similar_cases(args.catalog, sample_size=args.sample_size,
                                    limit=args.limit, seed=args.seed)
    if report_path:
        with report_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["invariants"]["failures"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())