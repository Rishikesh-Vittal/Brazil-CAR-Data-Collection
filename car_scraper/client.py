from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests
import urllib3

from car_scraper.constants import JSON_ENDPOINT, PDF_ENDPOINT
from car_scraper.io_utils import ensure_directory


class CarRequestError(RuntimeError):
    """Raised when the CAR endpoint returns an invalid response."""


@dataclass(slots=True)
class FetchResult:
    payload: Any
    elapsed_seconds: float
    content_type: str


class CarClient:
    def __init__(
        self,
        *,
        verify_ssl: bool = False,
        timeout_seconds: float = 60.0,
        session: requests.Session | None = None,
        prefer_curl: bool = True,
    ) -> None:
        self.verify_ssl = verify_ssl
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.curl_path = shutil.which("curl.exe") or shutil.which("curl")
        self.prefer_curl = prefer_curl and bool(self.curl_path)
        self.runtime_dir = Path.cwd() / ".car_scraper_runtime"
        ensure_directory(self.runtime_dir)
        self.cookie_jar_path = self.runtime_dir / f"cookies_{uuid.uuid4().hex}.txt"
        self._curl_cookies_ready = False
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/148.0.0.0 Safari/537.36"
                )
            }
        )
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def fetch_json(self, cod_imovel: str, recaptcha_token: str) -> FetchResult:
        url = self._build_url(JSON_ENDPOINT.format(cod_imovel=cod_imovel), recaptcha_token)
        if self.prefer_curl:
            payload, content_type, elapsed = self._fetch_with_curl(
                url,
                headers=[
                    "Accept: application/json, text/plain, */*",
                    "Referer: https://www.car.gov.br/",
                ],
            )
            try:
                json_payload = requests.models.complexjson.loads(payload.decode("utf-8"))
            except ValueError as exc:
                raise CarRequestError(
                    f"Expected JSON for {cod_imovel}, got {content_type or 'unknown'}"
                ) from exc
            if not isinstance(json_payload, dict) or "dados" not in json_payload:
                raise CarRequestError(
                    f"Unexpected JSON payload for {cod_imovel}: {json_payload!r}"
                )
            if json_payload.get("status") != "s" or not json_payload.get("dados"):
                raise CarRequestError(
                    f"CAR JSON request failed for {cod_imovel}: {json_payload.get('mensagem') or json_payload!r}"
                )
            return FetchResult(
                payload=json_payload,
                elapsed_seconds=elapsed,
                content_type=content_type,
            )
        started_at = time.monotonic()
        response = self.session.get(
            url,
            headers={"Accept": "application/json, text/plain, */*"},
            timeout=self.timeout_seconds,
            verify=self.verify_ssl,
        )
        elapsed = time.monotonic() - started_at
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CarRequestError(
                f"Expected JSON for {cod_imovel}, got {content_type or 'unknown'}"
            ) from exc
        if not isinstance(payload, dict) or "dados" not in payload:
            raise CarRequestError(f"Unexpected JSON payload for {cod_imovel}: {payload!r}")
        if payload.get("status") != "s" or not payload.get("dados"):
            raise CarRequestError(
                f"CAR JSON request failed for {cod_imovel}: {payload.get('mensagem') or payload!r}"
            )
        return FetchResult(payload=payload, elapsed_seconds=elapsed, content_type=content_type)

    def fetch_pdf(self, cod_imovel: str, recaptcha_token: str) -> FetchResult:
        url = self._build_url(PDF_ENDPOINT.format(cod_imovel=cod_imovel), recaptcha_token)
        if self.prefer_curl:
            content, content_type, elapsed = self._fetch_with_curl(
                url,
                headers=[
                    "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8,"
                    "application/signed-exchange;v=b3;q=0.7",
                    "Referer: https://www.car.gov.br/",
                ],
            )
            if not content:
                raise CarRequestError(f"Empty PDF response for {cod_imovel}")
            if not content.startswith(b"%PDF"):
                snippet = content[:120].decode("utf-8", errors="replace")
                raise CarRequestError(
                    f"Unexpected PDF response for {cod_imovel}: {content_type or 'unknown'} {snippet!r}"
                )
            return FetchResult(
                payload=content,
                elapsed_seconds=elapsed,
                content_type=content_type,
            )
        started_at = time.monotonic()
        response = self.session.get(
            url,
            headers={
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8,"
                    "application/signed-exchange;v=b3;q=0.7"
                )
            },
            timeout=self.timeout_seconds,
            verify=self.verify_ssl,
        )
        elapsed = time.monotonic() - started_at
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        content = response.content
        if not content:
            raise CarRequestError(f"Empty PDF response for {cod_imovel}")
        if not content.startswith(b"%PDF"):
            snippet = content[:120].decode("utf-8", errors="replace")
            raise CarRequestError(
                f"Unexpected PDF response for {cod_imovel}: {content_type or 'unknown'} {snippet!r}"
            )
        return FetchResult(payload=content, elapsed_seconds=elapsed, content_type=content_type)

    @staticmethod
    def _build_url(base_url: str, recaptcha_token: str) -> str:
        return f"{base_url}?{urlencode({'recaptchaResponse': recaptcha_token})}"

    def close(self) -> None:
        self.session.close()
        try:
            self.cookie_jar_path.unlink(missing_ok=True)
        except OSError:
            # A locked temporary cookie jar must not turn a completed download into a failure.
            pass

    def _fetch_with_curl(self, url: str, *, headers: list[str]) -> tuple[bytes, str, float]:
        if not self.curl_path:
            raise CarRequestError("curl is not available on this system")
        self._prime_cookies_with_curl()
        command = [
            self.curl_path,
            "--silent",
            "--show-error",
            "--location",
            "--include",
            "--compressed",
            "--max-time",
            str(int(self.timeout_seconds)),
            "--cookie",
            str(self.cookie_jar_path),
            "--cookie-jar",
            str(self.cookie_jar_path),
            "--user-agent",
            self.session.headers["User-Agent"],
        ]
        if not self.verify_ssl:
            command.append("--insecure")
        for header in headers:
            command.extend(["--header", header])
        command.append(url)

        started_at = time.monotonic()
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
        )
        elapsed = time.monotonic() - started_at
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            endpoint = urlsplit(url).path.rsplit("/", 1)[-1]
            raise CarRequestError(f"curl failed for {endpoint}: {stderr or result.returncode}")
        header_bytes, body = self._split_headers_and_body(result.stdout)
        status_code, content_type = self._parse_header_block(header_bytes)
        if status_code >= 400:
            snippet = body[:120].decode("utf-8", errors="replace")
            raise CarRequestError(
                f"HTTP {status_code} from CAR endpoint ({content_type or 'unknown'}): {snippet!r}"
            )
        return body, content_type, elapsed

    def _prime_cookies_with_curl(self) -> None:
        if self._curl_cookies_ready or not self.curl_path:
            return
        command = [
            self.curl_path,
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            str(int(self.timeout_seconds)),
            "--cookie-jar",
            str(self.cookie_jar_path),
            "--cookie",
            str(self.cookie_jar_path),
            "--user-agent",
            self.session.headers["User-Agent"],
            "https://www.car.gov.br/",
        ]
        if not self.verify_ssl:
            command.append("--insecure")
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise CarRequestError(f"curl cookie bootstrap failed: {stderr or result.returncode}")
        self._curl_cookies_ready = True

    @staticmethod
    def _split_headers_and_body(payload: bytes) -> tuple[bytes, bytes]:
        offset = 0
        header_block = b""
        # Strip consecutive proxy/redirect headers without changing binary PDF bytes.
        while payload.startswith(b"HTTP/", offset):
            separators = [
                (position, len(marker))
                for marker in (b"\r\n\r\n", b"\n\n")
                if (position := payload.find(marker, offset)) != -1
            ]
            if not separators:
                raise CarRequestError("curl response did not contain complete HTTP headers")
            end, marker_length = min(separators)
            header_block = payload[offset:end].replace(b"\r\n", b"\n")
            offset = end + marker_length
        if not header_block:
            raise CarRequestError("curl response did not contain HTTP headers")
        return header_block, payload[offset:]

    @staticmethod
    def _parse_header_block(header_block: bytes) -> tuple[int, str]:
        lines = [line.decode("utf-8", errors="replace") for line in header_block.split(b"\n") if line]
        if not lines:
            raise CarRequestError("Missing HTTP status line from curl response")
        status_line = lines[0]
        try:
            status_code = int(status_line.split()[1])
        except (IndexError, ValueError) as exc:
            raise CarRequestError(f"Unparseable HTTP status line: {status_line!r}") from exc
        content_type = ""
        for line in lines[1:]:
            if line.lower().startswith("content-type:"):
                content_type = line.split(":", 1)[1].strip()
                break
        return status_code, content_type
