"""Command-line entry point for the read-only lamination agent."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from catalog_common import CatalogError, DEFAULT_CATALOG

from .calibration import (
    DEFAULT_EXPORT_PATH,
    DEFAULT_IMPORT_PATH,
    DEFAULT_SOURCE,
    DEFAULT_UNLABELED_PATH,
    export_reviews,
    export_unlabeled,
    import_reviews,
    render_export_summary,
    render_import_summary,
)
from .evaluation import (
    EVAL_SCHEMA,
    evaluate_file,
    load_suite,
    render_summary,
    write_report,
)
from .models import canonical_json
from .jobs import get_job, get_result, list_jobs, run_worker, start_evaluation, stop_job
from .workflow import diagnose
from .web_search import SearXNGSearchAdapter
from .public_pack import DEFAULT_PACK_PATH, PublicPackRetriever, build_pack, load_pack, pack_summary


def _build_case(args) -> dict:
    if args.input:
        path = Path(args.input).expanduser()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CatalogError("case_file_not_found") from exc
        except json.JSONDecodeError as exc:
            raise CatalogError("invalid_case_json") from exc
        if not isinstance(value, dict):
            raise CatalogError("invalid_case")
        return value
    case = {"question": args.question or ""}
    for key in ("case_id", "defect", "stage", "location", "material", "stackup",
                "equipment", "batch", "timing", "domain", "notes"):
        value = getattr(args, key, None)
        if value:
            case[key] = value
    return case


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="pcb-agent",
        description="Read-only PCB lamination diagnosis agent over the local catalog.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    diagnose_parser = subparsers.add_parser(
        "diagnose", help="Diagnose one lamination case from JSON or a question"
    )
    source = diagnose_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="Path to a case JSON file")
    source.add_argument("--question", help="One free-text engineering question")
    diagnose_parser.add_argument("--case-id")
    diagnose_parser.add_argument("--defect")
    diagnose_parser.add_argument("--stage")
    diagnose_parser.add_argument("--location")
    diagnose_parser.add_argument("--material")
    diagnose_parser.add_argument("--stackup")
    diagnose_parser.add_argument("--equipment")
    diagnose_parser.add_argument("--batch")
    diagnose_parser.add_argument("--timing")
    diagnose_parser.add_argument("--domain")
    diagnose_parser.add_argument("--notes")
    diagnose_parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    diagnose_parser.add_argument("--public-pack", help="Use only this local public pack instead of the Catalog")
    diagnose_parser.add_argument("--limit", type=int, default=6)
    diagnose_parser.add_argument("--json", action="store_true", help="Print the full JSON result")
    diagnose_parser.add_argument(
        "--external-search", choices=("off", "searxng"), default="off",
        help="Explicitly enable public search fallback (default: off)",
    )
    diagnose_parser.add_argument(
        "--searxng-url", help="SearXNG base URL; alternatively set PCB_AGENT_SEARXNG_URL",
    )
    diagnose_parser.add_argument(
        "--search-timeout", type=float, default=5.0,
        help="Timeout in seconds per public search request (0 < timeout <= 30)",
    )

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="Run an unlabeled or labeled JSON/JSONL evaluation suite"
    )
    evaluate_parser.add_argument("--cases", required=True, help="Path to a JSON or JSONL suite")
    evaluate_parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    evaluate_parser.add_argument("--public-pack", help="Use only this local public pack instead of the Catalog")
    evaluate_parser.add_argument("--top-k", type=int, default=3)
    evaluate_parser.add_argument(
        "--check-determinism", action="store_true",
        help="Run each case twice and compare result fingerprints",
    )
    evaluate_parser.add_argument("--include-answers", action="store_true")
    evaluate_parser.add_argument(
        "--validate-only", action="store_true",
        help="Validate suite inputs and optional labels without running diagnosis",
    )
    evaluate_parser.add_argument("--output", help="Write the JSON report to this path")
    evaluate_parser.add_argument("--json", action="store_true", help="Print the full JSON report")
    evaluate_parser.add_argument("--background", action="store_true", help="Run as a persistent local task and return its ID")

    jobs_parser = subparsers.add_parser("jobs", help="Inspect and manage local Agent evaluation jobs")
    jobs_subparsers = jobs_parser.add_subparsers(dest="jobs_command", required=True)
    jobs_subparsers.add_parser("list", help="List recent evaluation jobs")
    job_status_parser = jobs_subparsers.add_parser("status", help="Show job state and progress")
    job_status_parser.add_argument("job_id")
    job_result_parser = jobs_subparsers.add_parser("result", help="Show a completed job report")
    job_result_parser.add_argument("job_id")
    job_stop_parser = jobs_subparsers.add_parser("stop", help="Stop a running evaluation job")
    job_stop_parser.add_argument("job_id")

    worker_parser = subparsers.add_parser("_run-eval-job", help=argparse.SUPPRESS)
    worker_parser.add_argument("job_id")
    worker_parser.add_argument("--job-dir")

    pack_parser = subparsers.add_parser("public-pack", help="Build or verify a local public reference pack")
    pack_subparsers = pack_parser.add_subparsers(dest="pack_command", required=True)
    pack_build = pack_subparsers.add_parser("build", help="Build from the existing v1/v2 public archives")
    pack_build.add_argument("--root", default=".")
    pack_build.add_argument("--output", default=str(DEFAULT_PACK_PATH))
    pack_build.add_argument("--json", action="store_true")
    pack_verify = pack_subparsers.add_parser("verify", help="Verify pack structure and checksums")
    pack_verify.add_argument("--pack", default=str(DEFAULT_PACK_PATH))
    pack_verify.add_argument("--json", action="store_true")

    calibrate_parser = subparsers.add_parser(
        "calibrate", help="Export and import real-case calibration packets"
    )
    calibrate_subparsers = calibrate_parser.add_subparsers(
        dest="calibrate_command", required=True
    )
    calibrate_export = calibrate_subparsers.add_parser(
        "export", help="Export real cases as an unlabeled suite or optional review packet"
    )
    calibrate_export.add_argument("--source", default=str(DEFAULT_SOURCE))
    calibrate_export.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    calibrate_export.add_argument("--limit", type=int, default=30)
    calibrate_export.add_argument("--output")
    calibrate_export.add_argument(
        "--unlabeled", action="store_true",
        help="Export observation inputs directly without expected labels or review",
    )
    calibrate_export.add_argument(
        "--include-without-reference", action="store_true",
        help="Include cases that have no analysis/action/verification text",
    )
    calibrate_export.add_argument("--overwrite", action="store_true")
    calibrate_export.add_argument("--json", action="store_true")

    calibrate_import = calibrate_subparsers.add_parser(
        "import", help="Import completed reviews as an evaluation suite"
    )
    calibrate_import.add_argument("--review", required=True)
    calibrate_import.add_argument("--output", default=str(DEFAULT_IMPORT_PATH))
    calibrate_import.add_argument("--overwrite", action="store_true")
    calibrate_import.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "public-pack":
        try:
            summary = (build_pack(root=args.root, output=args.output) if args.pack_command == "build"
                       else pack_summary(load_pack(args.pack)))
            print(canonical_json(summary, pretty=True) if args.json else (
                f"公开知识包校验通过：{summary['source_count']} 个来源，{summary['unit_count']} 个正文单元。\n"
                f"指纹：{summary['fingerprint']}"
            ))
            return 0
        except CatalogError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "_run-eval-job":
        try:
            return run_worker(args.job_id, root=Path(args.job_dir) if args.job_dir else None)
        except CatalogError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "jobs":
        try:
            if args.jobs_command == "list":
                records = list_jobs()
                if not records:
                    print("没有评测任务。")
                for item in records:
                    progress = item.get("progress") or {}
                    print(f"{item['job_id']}  {item['status']}  {progress.get('completed', 0)}/{progress.get('total', 0)}")
                return 0
            if args.jobs_command == "status":
                item = get_job(args.job_id)
                progress = item.get("progress") or {}
                print(canonical_json(item, pretty=True))
                print(f"进度：{progress.get('completed', 0)}/{progress.get('total', 0)}")
                return 0
            if args.jobs_command == "stop":
                print(canonical_json(stop_job(args.job_id), pretty=True))
                return 0
            item, report = get_result(args.job_id)
            print(render_summary(report))
            print(f"完整报告：{item['report_path']}")
            return 0
        except CatalogError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "calibrate":
        try:
            if args.calibrate_command == "export":
                export_function = export_unlabeled if args.unlabeled else export_reviews
                export_options = {} if args.unlabeled else {"catalog": Path(args.catalog)}
                default_output = DEFAULT_UNLABELED_PATH if args.unlabeled else DEFAULT_EXPORT_PATH
                summary = export_function(
                    source=Path(args.source),
                    limit=args.limit,
                    output=Path(args.output or default_output),
                    overwrite=args.overwrite,
                    include_without_reference=args.include_without_reference,
                    **export_options,
                )
                print(
                    canonical_json(summary, pretty=True)
                    if args.json else render_export_summary(summary)
                )
            else:
                summary = import_reviews(
                    review=Path(args.review),
                    output=Path(args.output),
                    overwrite=args.overwrite,
                )
                print(
                    canonical_json(summary, pretty=True)
                    if args.json else render_import_summary(summary)
                )
            return 0
        except CatalogError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "evaluate":
        try:
            pack_options = {"public_pack": Path(args.public_pack)} if args.public_pack else {}
            if args.public_pack:
                load_pack(args.public_pack)
            if args.validate_only:
                suite = load_suite(args.cases)
                payload = {
                    "schema": EVAL_SCHEMA,
                    "valid": True,
                    "case_count": len(suite),
                    "labeled_case_count": sum(1 for item in suite if item.labeled),
                }
                if args.json:
                    print(canonical_json(payload, pretty=True))
                else:
                    print(
                        f"评测集有效：{payload['case_count']} 条用例，"
                        f"其中 {payload['labeled_case_count']} 条含期望。"
                    )
                return 0
            if args.background:
                if args.output:
                    raise CatalogError("background_report_uses_private_job_directory")
                item = start_evaluation(
                    args.cases, catalog=args.catalog, top_k=args.top_k,
                    check_determinism=args.check_determinism, include_answers=args.include_answers,
                    **pack_options,
                )
                print(canonical_json(item, pretty=True) if args.json else f"已启动评测任务 {item['job_id']}（{item['progress']['total']} 条用例）")
                return 0
            report = evaluate_file(
                args.cases,
                catalog=Path(args.catalog),
                top_k=args.top_k,
                check_determinism=args.check_determinism,
                include_answers=args.include_answers,
                **pack_options,
            )
            if args.output:
                write_report(report, args.output)
            if args.json:
                print(canonical_json(report, pretty=True))
            else:
                print(render_summary(report))
            return 0
        except CatalogError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        case = _build_case(args)
        search_options = {}
        if args.public_pack:
            search_options["retriever"] = PublicPackRetriever(args.public_pack)
        if args.external_search == "searxng":
            endpoint = args.searxng_url or os.environ.get("PCB_AGENT_SEARXNG_URL")
            if not endpoint:
                raise CatalogError("searxng_url_required")
            search_options.update({
                "external_search": SearXNGSearchAdapter(endpoint, timeout=args.search_timeout),
                "allow_external_search": True,
            })
        result = diagnose(
            case,
            catalog=Path(args.catalog),
            search_limit=args.limit,
            **search_options,
        )
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(canonical_json(result, pretty=True))
    else:
        print(result.get("answer") or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
