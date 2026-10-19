from __future__ import annotations

import argparse
import os
from pathlib import Path

from car_scraper.pipeline import AcrePipeline, RunConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="car_scraper",
        description="Resumable direct scraper for Brazil CAR parcel JSON and PDFs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run-acre", help="Run or resume the Acre pipeline.")
    run_parser.add_argument("--input", required=True, type=Path, help="CSV with a cod_imovel column.")
    run_parser.add_argument("--output", required=True, type=Path, help="Output root directory.")
    run_parser.add_argument("--workers", type=int, default=5, help="Concurrent worker count.")
    run_parser.add_argument("--limit", type=int, default=None, help="Maximum records to schedule.")
    run_parser.add_argument(
        "--first-records", type=int, default=None,
        help="Restrict processing and resume to the first N unique input IDs, including completed ones.",
    )
    run_parser.add_argument(
        "--max-retries", type=int, default=4,
        help="Automatic retries after the initial attempt (default: 4; five attempts total).",
    )
    run_parser.add_argument(
        "--retry-delay-seconds", type=float, default=5.0,
        help="Initial retry delay; doubles after each failure up to 60 seconds.",
    )
    run_parser.add_argument(
        "--retry-failures",
        action="store_true",
        help="Give exhausted records a fresh retry budget; completed records remain skipped.",
    )
    run_parser.add_argument(
        "--start-at",
        default=None,
        help="Start scheduling from this cod_imovel (inclusive).",
    )
    run_parser.add_argument(
        "--benchmark",
        type=int,
        default=None,
        help="Run only the first N eligible records and write timing metrics.",
    )
    run_parser.add_argument(
        "--captcha-provider",
        choices=["2captcha", "capsolver"],
        default=os.getenv("CAPTCHA_PROVIDER", "2captcha"),
        help="Captcha provider to use. Defaults to CAPTCHA_PROVIDER or 2captcha.",
    )
    run_parser.add_argument(
        "--two-captcha-api-key",
        default=os.getenv("TWO_CAPTCHA_API_KEY") or os.getenv("TWO_CAPTCHA_API", ""),
        help="2captcha API key. Defaults to TWO_CAPTCHA_API_KEY or TWO_CAPTCHA_API env vars.",
    )
    run_parser.add_argument(
        "--capsolver-api-key",
        default=os.getenv("CAPSOLVER_API_KEY") or os.getenv("CAPSOLVER_API", ""),
        help="CapSolver API key. Defaults to CAPSOLVER_API_KEY or CAPSOLVER_API env vars.",
    )
    run_parser.add_argument(
        "--verify-ssl",
        action="store_true",
        help="Verify SSL certificates for CAR requests. Disabled by default for compatibility.",
    )
    run_parser.add_argument(
        "--captcha-poll-interval",
        type=float,
        default=5.0,
        help="Polling interval in seconds for CAPTCHA result checks.",
    )
    run_parser.add_argument(
        "--captcha-timeout-seconds",
        type=float,
        default=180.0,
        help="Maximum wait for a CAPTCHA token.",
    )
    run_parser.add_argument(
        "--car-timeout-seconds",
        type=float,
        default=60.0,
        help="HTTP timeout for CAR endpoint requests.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run-acre":
        if args.workers < 1 or args.max_retries < 0 or args.retry_delay_seconds < 0:
            parser.error("Workers must be positive; retries and retry delay cannot be negative.")
        if args.first_records is not None and args.first_records < 1:
            parser.error("--first-records must be positive.")
        if args.captcha_provider == "2captcha" and not args.two_captcha_api_key:
            parser.error(
                "A 2captcha API key is required. Pass --two-captcha-api-key or set "
                "TWO_CAPTCHA_API_KEY."
            )
        if args.captcha_provider == "capsolver" and not args.capsolver_api_key:
            parser.error(
                "A CapSolver API key is required. Pass --capsolver-api-key or set "
                "CAPSOLVER_API_KEY."
            )
        config = RunConfig(
            input_csv=args.input,
            output_root=args.output,
            state_slug="acre",
            workers=args.workers,
            limit=args.limit,
            retry_failures=args.retry_failures,
            start_at=args.start_at,
            benchmark=args.benchmark,
            verify_ssl=args.verify_ssl,
            captcha_poll_interval=args.captcha_poll_interval,
            captcha_timeout_seconds=args.captcha_timeout_seconds,
            car_timeout_seconds=args.car_timeout_seconds,
            captcha_provider=args.captcha_provider,
            two_captcha_api_key=args.two_captcha_api_key,
            capsolver_api_key=args.capsolver_api_key,
            max_retries=args.max_retries,
            retry_delay_seconds=args.retry_delay_seconds,
            first_records=args.first_records,
        )
        summary = AcrePipeline(config).run()
        print(
            "Scheduled={scheduled} Completed={completed} Succeeded={succeeded} "
            "Partial={partial} Failed={failed}".format(
                scheduled=summary.scheduled,
                completed=summary.completed,
                succeeded=summary.succeeded,
                partial=summary.partial,
                failed=summary.failed,
            )
        )
        if summary.benchmark_mode and summary.metrics:
            print("Benchmark mode complete. See outputs/acre/logs/last_run_summary.json for timings.")
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2
