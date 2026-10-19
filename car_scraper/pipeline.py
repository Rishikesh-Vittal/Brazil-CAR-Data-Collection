from __future__ import annotations

import csv
import json
import signal
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from car_scraper.captcha import CapSolverClient, TwoCaptchaClient
from car_scraper.client import CarClient, CarRequestError
from car_scraper.constants import EVENT_LOG_NAME, JSON_PAGE_URL, JSON_SITE_KEY
from car_scraper.io_utils import EventLogger, atomic_write_bytes, atomic_write_json, ensure_directory, utc_now_iso
from car_scraper.manifest import ManifestRow, ManifestStore
from car_scraper.normalize import build_normalized_payload, repair_mojibake


@dataclass(slots=True)
class RunConfig:
    input_csv: Path
    output_root: Path
    state_slug: str = "acre"
    workers: int = 5
    limit: int | None = None
    retry_failures: bool = False
    start_at: str | None = None
    benchmark: int | None = None
    verify_ssl: bool = False
    captcha_poll_interval: float = 5.0
    captcha_timeout_seconds: float = 180.0
    car_timeout_seconds: float = 60.0
    captcha_provider: str = "2captcha"
    two_captcha_api_key: str = ""
    capsolver_api_key: str = ""
    max_retries: int = 4
    retry_delay_seconds: float = 5.0
    first_records: int | None = None


@dataclass(slots=True)
class RecordMetrics:
    cod_imovel: str
    status: str
    total_seconds: float
    json_captcha_seconds: float = 0.0
    json_request_seconds: float = 0.0
    pdf_captcha_seconds: float = 0.0
    pdf_request_seconds: float = 0.0
    error: str = ""
    attempts: int = 0


@dataclass(slots=True)
class RunSummary:
    scheduled: int = 0
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    partial: int = 0
    benchmark_mode: bool = False
    metrics: list[RecordMetrics] = field(default_factory=list)


class AcrePipeline:
    def __init__(self, config: RunConfig) -> None:
        if config.workers < 1 or config.max_retries < 0 or config.retry_delay_seconds < 0:
            raise ValueError("Workers must be positive; retries and retry delay cannot be negative")
        if config.first_records is not None and config.first_records < 1:
            raise ValueError("first_records must be positive")
        self.config = config
        self.output_root = config.output_root
        self.json_dir = self.output_root / "json"
        self.pdf_dir = self.output_root / "pdf" / config.state_slug
        self.manifest = ManifestStore(self.output_root / "manifests")
        self.logger = EventLogger(self.output_root / "logs" / EVENT_LOG_NAME)
        self.stop_event = threading.Event()
        self.summary = RunSummary(benchmark_mode=config.benchmark is not None)
        ensure_directory(self.json_dir)
        ensure_directory(self.pdf_dir)
        ensure_directory(self.output_root / "logs")

    def run(self) -> RunSummary:
        original_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handle_sigint)
        try:
            codes = self._load_codes(self.config.input_csv)
            self.manifest.initialize_records(codes)
            recovered = self.manifest.reset_in_progress_records()
            repaired = self.manifest.reconcile_artifacts(self.output_root)
            self.logger.log(
                "run_started",
                input_csv=str(self.config.input_csv),
                output_root=str(self.output_root),
                workers=self.config.workers,
                captcha_provider=self.config.captcha_provider,
                recovered_in_progress=recovered,
                reconciled_records=repaired,
            )
            candidate_limit = self.config.benchmark if self.config.benchmark is not None else self.config.limit
            candidates = self.manifest.get_candidates(
                start_at=self.config.start_at,
                limit=candidate_limit,
                retry_failures=self.config.retry_failures,
                max_attempts=self.config.max_retries + 1,
                codes=set(codes[:self.config.first_records]) if self.config.first_records else None,
            )
            self.summary.scheduled = len(candidates)
            self.logger.log("queue_built", scheduled=len(candidates))
            self._run_candidates(candidates)
            self.manifest.export_csv_snapshot()
            self._write_summary()
            return self.summary
        finally:
            signal.signal(signal.SIGINT, original_sigint)
            self.manifest.export_skipped_records(max_attempts=self.config.max_retries + 1)
            self.manifest.close()

    def _run_candidates(self, candidates: list[ManifestRow]) -> None:
        iterator = iter(candidates)
        active: dict[Future[RecordMetrics], str] = {}
        with ThreadPoolExecutor(max_workers=self.config.workers) as executor:
            while True:
                while not self.stop_event.is_set() and len(active) < self.config.workers:
                    try:
                        row = next(iterator)
                    except StopIteration:
                        break
                    future = executor.submit(self._process_record, row)
                    active[future] = row.cod_imovel
                if not active:
                    break
                done, _ = wait(active.keys(), timeout=1.0, return_when=FIRST_COMPLETED)
                for future in done:
                    cod_imovel = active.pop(future)
                    try:
                        metrics = future.result()
                    except Exception as exc:  # pragma: no cover - defensive fallback
                        self.summary.failed += 1
                        self.summary.completed += 1
                        self.logger.log(
                            "record_crashed",
                            cod_imovel=cod_imovel,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        self.manifest.update_record(
                            cod_imovel,
                            status="failed",
                            last_error=f"{type(exc).__name__}: {exc}",
                        )
                        continue
                    self.summary.completed += 1
                    self.summary.metrics.append(metrics)
                    if metrics.status == "success":
                        self.summary.succeeded += 1
                    elif metrics.status == "json_saved":
                        self.summary.partial += 1
                    else:
                        self.summary.failed += 1
            if self.stop_event.is_set():
                self.logger.log("stop_requested", active_workers=len(active))
                for future in active:
                    metrics = future.result()
                    self.summary.completed += 1
                    self.summary.metrics.append(metrics)
                    if metrics.status == "success":
                        self.summary.succeeded += 1
                    elif metrics.status == "json_saved":
                        self.summary.partial += 1
                    else:
                        self.summary.failed += 1

    def _process_record(self, row: ManifestRow) -> RecordMetrics:
        started_at = time.monotonic()
        combined = RecordMetrics(cod_imovel=row.cod_imovel, status=row.status, total_seconds=0.0)
        max_attempts = self.config.max_retries + 1
        remaining_attempts = max_attempts if self.config.retry_failures else max_attempts - row.attempt_count
        if row.status == "json_saved" and not row.last_error:
            remaining_attempts = max(1, remaining_attempts)
        for attempt in range(max(0, remaining_attempts)):
            if self.stop_event.is_set():
                break
            current = self.manifest.get_row(row.cod_imovel)
            self.manifest.mark_started(row.cod_imovel)
            result = self._process_record_once(current)
            combined.attempts += 1
            combined.status = result.status
            combined.error = result.error
            for name in (
                "json_captcha_seconds", "json_request_seconds", "pdf_captcha_seconds", "pdf_request_seconds",
            ):
                setattr(combined, name, getattr(combined, name) + getattr(result, name))
            if result.status == "success" or self.stop_event.is_set():
                break
            if attempt + 1 == remaining_attempts:
                self.logger.log(
                    "record_skipped", cod_imovel=row.cod_imovel,
                    attempt_count=self.manifest.get_row(row.cod_imovel).attempt_count,
                    status=result.status, error=result.error,
                )
                self.manifest.export_skipped_records(max_attempts=max_attempts)
                break
            delay = min(self.config.retry_delay_seconds * (2 ** min(attempt, 10)), 60.0)
            self.logger.log(
                "retry_scheduled", cod_imovel=row.cod_imovel,
                next_attempt=self.manifest.get_row(row.cod_imovel).attempt_count + 1,
                resume_step="pdf" if result.status == "json_saved" else "json",
                delay_seconds=delay,
            )
            if self.stop_event.wait(delay):
                break
        combined.total_seconds = time.monotonic() - started_at
        return combined

    def _process_record_once(self, row: ManifestRow) -> RecordMetrics:
        started_at = time.monotonic()
        solver = self._build_captcha_solver()
        client = CarClient(
            verify_ssl=self.config.verify_ssl,
            timeout_seconds=self.config.car_timeout_seconds,
        )
        current_status = row.status
        record_path = self.json_dir / f"{row.cod_imovel}.json"
        pdf_path = self.pdf_dir / f"{row.cod_imovel}.pdf"
        errors: list[str] = []
        if current_status == "json_saved" and record_path.exists():
            try:
                errors = json.loads(record_path.read_text(encoding="utf-8")).get("errors") or []
            except (OSError, ValueError):
                current_status = "pending"
        metrics = RecordMetrics(cod_imovel=row.cod_imovel, status="failed", total_seconds=0.0)

        try:
            if current_status != "json_saved":
                json_token = solver.solve_recaptcha(site_key=JSON_SITE_KEY, page_url=JSON_PAGE_URL)
                metrics.json_captcha_seconds = json_token.elapsed_seconds
                json_result = client.fetch_json(row.cod_imovel, json_token.token)
                metrics.json_request_seconds = json_result.elapsed_seconds
                repaired_payload = repair_mojibake(json_result.payload)
                normalized = build_normalized_payload(repaired_payload)
                relative_pdf_path = row.pdf_path or ""
                record_payload = {
                    "cod_imovel": row.cod_imovel,
                    "captured_at": utc_now_iso(),
                    "status": repaired_payload.get("status"),
                    "mensagem": repaired_payload.get("mensagem"),
                    "dados": repaired_payload.get("dados"),
                    "normalized": normalized,
                    "pdf_path": relative_pdf_path,
                    "errors": errors,
                }
                atomic_write_json(record_path, record_payload)
                current_status = "json_saved"
                self.manifest.update_record(
                    row.cod_imovel,
                    status="json_saved",
                    json_path=self._relative_output_path(record_path),
                    last_error="",
                )
                self.logger.log(
                    "json_saved",
                    cod_imovel=row.cod_imovel,
                    json_path=self._relative_output_path(record_path),
                )

            if self.stop_event.is_set():
                metrics.status = "json_saved"
                metrics.total_seconds = time.monotonic() - started_at
                return metrics

            pdf_token = solver.solve_recaptcha(site_key=JSON_SITE_KEY, page_url=JSON_PAGE_URL)
            metrics.pdf_captcha_seconds = pdf_token.elapsed_seconds
            pdf_result = client.fetch_pdf(row.cod_imovel, pdf_token.token)
            metrics.pdf_request_seconds = pdf_result.elapsed_seconds
            atomic_write_bytes(pdf_path, pdf_result.payload)
            if not self.manifest._is_valid_pdf_artifact(pdf_path):
                raise CarRequestError(f"Saved PDF is invalid or incomplete for {row.cod_imovel}")

            record_payload = json.loads(record_path.read_text(encoding="utf-8"))
            record_payload["pdf_path"] = self._relative_output_path(pdf_path)
            record_payload["errors"] = errors
            atomic_write_json(record_path, record_payload)
            self.manifest.update_record(
                row.cod_imovel,
                status="success",
                json_path=self._relative_output_path(record_path),
                pdf_path=self._relative_output_path(pdf_path),
                last_error="",
            )
            self.logger.log(
                "record_success",
                cod_imovel=row.cod_imovel,
                json_path=self._relative_output_path(record_path),
                pdf_path=self._relative_output_path(pdf_path),
            )
            metrics.status = "success"
            return metrics
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            errors.append(message)
            metrics.error = message
            if current_status == "json_saved":
                if record_path.exists():
                    record_payload = json.loads(record_path.read_text(encoding="utf-8"))
                    record_payload["errors"] = errors
                    atomic_write_json(record_path, record_payload)
                self.manifest.update_record(row.cod_imovel, status="json_saved", last_error=message)
                metrics.status = "json_saved"
            else:
                self.manifest.update_record(row.cod_imovel, status="failed", last_error=message)
                metrics.status = "failed"
            self.logger.log("record_error", cod_imovel=row.cod_imovel, error=message)
            return metrics
        finally:
            metrics.total_seconds = time.monotonic() - started_at
            client.close()
            solver.session.close()

    def _handle_sigint(self, _signum: int, _frame: Any) -> None:
        self.stop_event.set()

    def _build_captcha_solver(self) -> TwoCaptchaClient | CapSolverClient:
        if self.config.captcha_provider == "capsolver":
            return CapSolverClient(
                self.config.capsolver_api_key,
                polling_interval=self.config.captcha_poll_interval,
                timeout_seconds=self.config.captcha_timeout_seconds,
            )
        return TwoCaptchaClient(
            self.config.two_captcha_api_key,
            polling_interval=self.config.captcha_poll_interval,
            timeout_seconds=self.config.captcha_timeout_seconds,
        )

    def _write_summary(self) -> None:
        metrics = self.summary.metrics
        if metrics:
            benchmark_payload = {
                "captcha_provider": self.config.captcha_provider,
                "scheduled": self.summary.scheduled,
                "completed": self.summary.completed,
                "succeeded": self.summary.succeeded,
                "failed": self.summary.failed,
                "partial": self.summary.partial,
                "max_retries": self.config.max_retries,
                "automatic_retries": sum(max(0, item.attempts - 1) for item in metrics),
                "average_total_seconds": sum(item.total_seconds for item in metrics) / len(metrics),
                "average_json_captcha_seconds": (
                    sum(item.json_captcha_seconds for item in metrics) / len(metrics)
                ),
                "average_json_request_seconds": (
                    sum(item.json_request_seconds for item in metrics) / len(metrics)
                ),
                "average_pdf_captcha_seconds": (
                    sum(item.pdf_captcha_seconds for item in metrics) / len(metrics)
                ),
                "average_pdf_request_seconds": (
                    sum(item.pdf_request_seconds for item in metrics) / len(metrics)
                ),
            }
            atomic_write_json(self.output_root / "logs" / "last_run_summary.json", benchmark_payload)
        self.logger.log(
            "run_finished",
            scheduled=self.summary.scheduled,
            completed=self.summary.completed,
            succeeded=self.summary.succeeded,
            failed=self.summary.failed,
            partial=self.summary.partial,
            manifest_counts=self.manifest.get_counts(),
        )

    def _relative_output_path(self, path: Path) -> str:
        return path.relative_to(self.output_root).as_posix()

    @staticmethod
    def _load_codes(path: Path) -> list[str]:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if "cod_imovel" not in (reader.fieldnames or []):
                raise ValueError(f"CSV must contain a cod_imovel column: {path}")
            seen: set[str] = set()
            codes: list[str] = []
            for row in reader:
                code = (row.get("cod_imovel") or "").strip()
                if not code or code in seen:
                    continue
                seen.add(code)
                codes.append(code)
        return codes
