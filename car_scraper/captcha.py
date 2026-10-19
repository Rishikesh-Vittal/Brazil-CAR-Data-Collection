from __future__ import annotations

import time
from dataclasses import dataclass

import requests


class CaptchaError(RuntimeError):
    """Raised when the captcha provider cannot provide a token."""


@dataclass(slots=True)
class CaptchaResult:
    token: str
    elapsed_seconds: float
    request_id: str


class TwoCaptchaClient:
    SUBMIT_URL = "https://2captcha.com/in.php"
    RESULT_URL = "https://2captcha.com/res.php"

    def __init__(
        self,
        api_key: str,
        *,
        polling_interval: float = 5.0,
        timeout_seconds: float = 180.0,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key
        self.polling_interval = polling_interval
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def solve_recaptcha(self, *, site_key: str, page_url: str) -> CaptchaResult:
        started_at = time.monotonic()
        submit_response = self.session.post(
            self.SUBMIT_URL,
            data={
                "key": self.api_key,
                "method": "userrecaptcha",
                "googlekey": site_key,
                "pageurl": page_url,
                "json": 1,
            },
            timeout=30,
        )
        submit_response.raise_for_status()
        payload = submit_response.json()
        if payload.get("status") != 1:
            raise CaptchaError(f"2captcha submit failed: {payload!r}")

        request_id = str(payload["request"])
        deadline = started_at + self.timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(self.polling_interval)
            result_response = self.session.get(
                self.RESULT_URL,
                params={
                    "key": self.api_key,
                    "action": "get",
                    "id": request_id,
                    "json": 1,
                },
                timeout=30,
            )
            result_response.raise_for_status()
            result_payload = result_response.json()
            if result_payload.get("status") == 1:
                return CaptchaResult(
                    token=str(result_payload["request"]),
                    elapsed_seconds=time.monotonic() - started_at,
                    request_id=request_id,
                )
            if result_payload.get("request") != "CAPCHA_NOT_READY":
                raise CaptchaError(f"2captcha solve failed: {result_payload!r}")

        raise CaptchaError(
            f"Timed out waiting for captcha token after {self.timeout_seconds} seconds"
        )


class CapSolverClient:
    CREATE_TASK_URL = "https://api.capsolver.com/createTask"
    GET_TASK_RESULT_URL = "https://api.capsolver.com/getTaskResult"

    def __init__(
        self,
        api_key: str,
        *,
        polling_interval: float = 2.0,
        timeout_seconds: float = 180.0,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key
        self.polling_interval = polling_interval
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def solve_recaptcha(self, *, site_key: str, page_url: str) -> CaptchaResult:
        started_at = time.monotonic()
        submit_response = self.session.post(
            self.CREATE_TASK_URL,
            json={
                "clientKey": self.api_key,
                "task": {
                    "type": "ReCaptchaV2TaskProxyLess",
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                },
            },
            timeout=30,
        )
        submit_response.raise_for_status()
        payload = submit_response.json()
        if payload.get("errorId") != 0:
            raise CaptchaError(f"CapSolver submit failed: {payload!r}")

        request_id = str(payload["taskId"])
        deadline = started_at + self.timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(self.polling_interval)
            result_response = self.session.post(
                self.GET_TASK_RESULT_URL,
                json={
                    "clientKey": self.api_key,
                    "taskId": request_id,
                },
                timeout=30,
            )
            result_response.raise_for_status()
            result_payload = result_response.json()
            if result_payload.get("errorId") != 0:
                raise CaptchaError(f"CapSolver solve failed: {result_payload!r}")
            if result_payload.get("status") == "ready":
                solution = result_payload.get("solution") or {}
                token = solution.get("gRecaptchaResponse")
                if not token:
                    raise CaptchaError(f"CapSolver returned no reCAPTCHA token: {result_payload!r}")
                return CaptchaResult(
                    token=str(token),
                    elapsed_seconds=time.monotonic() - started_at,
                    request_id=request_id,
                )
            if result_payload.get("status") not in {"idle", "processing"}:
                raise CaptchaError(f"CapSolver returned unexpected status: {result_payload!r}")

        raise CaptchaError(
            f"Timed out waiting for CapSolver token after {self.timeout_seconds} seconds"
        )
