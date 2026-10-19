from __future__ import annotations

import csv
import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from car_scraper.constants import MANIFEST_CSV_NAME
from car_scraper.io_utils import atomic_write_text, ensure_directory, utc_now_iso


@dataclass(slots=True)
class ManifestRow:
    cod_imovel: str
    row_num: int
    status: str
    attempt_count: int
    json_path: str
    pdf_path: str
    last_error: str
    updated_at: str


class ManifestStore:
    SNAPSHOT_EVERY = 25
    JOURNAL_NAME = "status_updates.jsonl"

    def __init__(self, manifest_dir: Path) -> None:
        self.manifest_dir = manifest_dir
        ensure_directory(manifest_dir)
        self.csv_path = manifest_dir / MANIFEST_CSV_NAME
        self.journal_path = manifest_dir / self.JOURNAL_NAME
        self._lock = threading.Lock()
        self._pending_snapshot_updates = 0
        self._rows: dict[str, ManifestRow] = {}
        self._load_existing_state()

    def initialize_records(self, codes: list[str]) -> None:
        with self._lock:
            for index, code in enumerate(codes, start=1):
                existing = self._rows.get(code)
                if existing is None:
                    self._rows[code] = ManifestRow(
                        cod_imovel=code,
                        row_num=index,
                        status="pending",
                        attempt_count=0,
                        json_path="",
                        pdf_path="",
                        last_error="",
                        updated_at=utc_now_iso(),
                    )
                else:
                    existing.row_num = index
        self.export_csv_snapshot()

    def reset_in_progress_records(self) -> int:
        reset_count = 0
        with self._lock:
            for row in self._rows.values():
                if row.status != "in_progress":
                    continue
                row.status = "json_saved" if row.json_path else "pending"
                if not row.last_error:
                    row.last_error = "Recovered after interrupted run"
                row.updated_at = utc_now_iso()
                reset_count += 1
                self._append_journal_unlocked(row)
        if reset_count:
            self.export_csv_snapshot()
        return reset_count

    def reconcile_artifacts(self, output_root: Path) -> int:
        repaired = 0
        with self._lock:
            for row in self._rows.values():
                if row.status not in {"json_saved", "success"}:
                    continue
                json_exists = bool(row.json_path) and (output_root / row.json_path).exists()
                if json_exists:
                    json_exists = self._is_valid_json_artifact(output_root / row.json_path)
                pdf_exists = bool(row.pdf_path) and (output_root / row.pdf_path).exists()
                if pdf_exists:
                    pdf_exists = self._is_valid_pdf_artifact(output_root / row.pdf_path)
                expected_status = "success" if json_exists and pdf_exists else "json_saved" if json_exists else "pending"
                expected_json_path = row.json_path if json_exists else ""
                expected_pdf_path = row.pdf_path if pdf_exists else ""
                if (
                    row.status == expected_status
                    and row.json_path == expected_json_path
                    and row.pdf_path == expected_pdf_path
                ):
                    continue
                row.status = expected_status
                row.json_path = expected_json_path
                row.pdf_path = expected_pdf_path
                row.last_error = "Reconciled missing output artifact"
                row.updated_at = utc_now_iso()
                repaired += 1
                self._append_journal_unlocked(row)
        if repaired:
            self.export_csv_snapshot()
        return repaired

    def get_counts(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {"total": len(self._rows)}
            for row in self._rows.values():
                counts[row.status] = counts.get(row.status, 0) + 1
        return counts

    def get_row(self, cod_imovel: str) -> ManifestRow:
        with self._lock:
            row = self._rows.get(cod_imovel)
            if row is None:
                raise KeyError(cod_imovel)
            return self._clone(row)

    def get_candidates(
        self,
        *,
        start_at: str | None = None,
        limit: int | None = None,
        retry_failures: bool = False,
        max_attempts: int | None = None,
        codes: set[str] | None = None,
    ) -> list[ManifestRow]:
        allowed = {"pending", "json_saved"}
        if retry_failures or max_attempts is not None:
            allowed.add("failed")
        with self._lock:
            rows = sorted(self._rows.values(), key=lambda item: item.row_num)
            if start_at:
                matching = [row.row_num for row in rows if row.cod_imovel == start_at]
                if not matching:
                    raise KeyError(f"start_at code not found: {start_at}")
                start_row_num = matching[0]
                rows = [row for row in rows if row.row_num >= start_row_num]
            rows = [
                self._clone(row) for row in rows
                if row.status in allowed
                and (codes is None or row.cod_imovel in codes)
                and (
                    retry_failures or max_attempts is None or row.attempt_count < max_attempts
                    or (row.status == "json_saved" and not row.last_error)
                )
            ]
        if limit is not None:
            rows = rows[:limit]
        return rows

    def mark_started(self, cod_imovel: str) -> ManifestRow:
        with self._lock:
            row = self._rows[cod_imovel]
            row.status = "in_progress"
            row.attempt_count += 1
            row.updated_at = utc_now_iso()
            row.last_error = ""
            self._append_journal_unlocked(row)
            cloned = self._clone(row)
        self._maybe_schedule_snapshot()
        return cloned

    def update_record(
        self,
        cod_imovel: str,
        *,
        status: str | None = None,
        json_path: str | None = None,
        pdf_path: str | None = None,
        last_error: str | None = None,
    ) -> None:
        with self._lock:
            row = self._rows[cod_imovel]
            if status is not None:
                row.status = status
            if json_path is not None:
                row.json_path = json_path
            if pdf_path is not None:
                row.pdf_path = pdf_path
            if last_error is not None:
                row.last_error = last_error
            row.updated_at = utc_now_iso()
            self._append_journal_unlocked(row)
        self._maybe_schedule_snapshot()

    def export_csv_snapshot(self) -> None:
        with self._lock:
            rows = sorted(self._rows.values(), key=lambda item: item.row_num)
            lines = [
                [
                    "cod_imovel",
                    "status",
                    "attempt_count",
                    "json_path",
                    "pdf_path",
                    "last_error",
                    "updated_at",
                ]
            ]
            for row in rows:
                lines.append(
                    [
                        row.cod_imovel,
                        row.status,
                        str(row.attempt_count),
                        row.json_path,
                        row.pdf_path,
                        row.last_error,
                        row.updated_at,
                    ]
                )
            csv_text = _rows_to_csv_text(lines)
            atomic_write_text(self.csv_path, csv_text)
            atomic_write_text(self.journal_path, "")
            self._pending_snapshot_updates = 0

    def close(self) -> None:
        self.export_csv_snapshot()

    def export_skipped_records(self, *, max_attempts: int) -> Path:
        path = self.manifest_dir / "skipped_records.csv"
        with self._lock:
            rows = sorted(self._rows.values(), key=lambda row: row.row_num)
            lines = [[
                "cod_imovel", "status", "attempt_count", "retry_count", "missing_artifacts",
                "json_path", "pdf_path", "last_error", "updated_at",
            ]]
            for row in rows:
                if row.status not in {"failed", "json_saved", "pending"}:
                    continue
                if row.attempt_count < max_attempts or not row.last_error:
                    continue
                missing = []
                if not row.json_path:
                    missing.append("json")
                if not row.pdf_path:
                    missing.append("pdf")
                lines.append([
                    row.cod_imovel, row.status, str(row.attempt_count), str(max(0, row.attempt_count - 1)),
                    ";".join(missing), row.json_path, row.pdf_path, row.last_error, row.updated_at,
                ])
            atomic_write_text(path, _rows_to_csv_text(lines))
        return path

    def _load_existing_state(self) -> None:
        if self.csv_path.exists():
            with self.csv_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                for index, row in enumerate(reader, start=1):
                    cod_imovel = (row.get("cod_imovel") or "").strip()
                    if not cod_imovel:
                        continue
                    self._rows[cod_imovel] = ManifestRow(
                        cod_imovel=cod_imovel,
                        row_num=index,
                        status=(row.get("status") or "pending").strip() or "pending",
                        attempt_count=int((row.get("attempt_count") or "0").strip() or 0),
                        json_path=(row.get("json_path") or "").strip(),
                        pdf_path=(row.get("pdf_path") or "").strip(),
                        last_error=(row.get("last_error") or "").strip(),
                        updated_at=(row.get("updated_at") or "").strip(),
                    )
        if self.journal_path.exists():
            with self.journal_path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()
                for index, line in enumerate(lines):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        # A forced stop can interrupt the final journal append.
                        if not any(rest.strip() for rest in lines[index + 1:]):
                            break
                        raise
                    row = ManifestRow(**payload)
                    self._rows[row.cod_imovel] = row

    def _maybe_schedule_snapshot(self) -> None:
        should_export = False
        with self._lock:
            self._pending_snapshot_updates += 1
            if self._pending_snapshot_updates >= self.SNAPSHOT_EVERY:
                should_export = True
        if should_export:
            self.export_csv_snapshot()

    def _append_journal_unlocked(self, row: ManifestRow) -> None:
        with self.journal_path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")

    @staticmethod
    def _clone(row: ManifestRow) -> ManifestRow:
        return ManifestRow(**asdict(row))

    @staticmethod
    def _is_valid_json_artifact(path: Path) -> bool:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get("status") != "s":
            return False
        return bool(payload.get("dados"))

    @staticmethod
    def _is_valid_pdf_artifact(path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    return False
                handle.seek(max(0, path.stat().st_size - 4096))
                return b"%%EOF" in handle.read()
        except OSError:
            return False


def _rows_to_csv_text(rows: list[list[str]]) -> str:
    output: list[str] = []
    for row in rows:
        escaped = []
        for cell in row:
            cell = cell.replace('"', '""')
            if any(token in cell for token in [",", "\n", "\r", '"']):
                escaped.append(f'"{cell}"')
            else:
                escaped.append(cell)
        output.append(",".join(escaped))
    return "\n".join(output) + "\n"
