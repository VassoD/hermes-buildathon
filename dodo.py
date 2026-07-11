"""Small Dodo Payments client for safe refund execution.

Test mode is the default. Live mode requires DODO_ALLOW_LIVE=true in addition
to a live API key so a local demo cannot accidentally move real money.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


TEST_BASE_URL = "https://test.dodopayments.com"
LIVE_BASE_URL = "https://live.dodopayments.com"
PAYMENT_ID_PATTERN = re.compile(r"\bpay_[A-Za-z0-9_-]+\b")
REQUEST_TIMEOUT_SECONDS = 30


class DodoError(Exception):
    """Raised when Dodo configuration or an API request fails."""


@dataclass(frozen=True)
class DodoRefund:
    refund_id: str
    payment_id: str
    status: str
    amount: int | None = None
    currency: str | None = None


def extract_payment_id(text: str) -> str | None:
    """Return the first Dodo payment ID included in user-provided text."""
    match = PAYMENT_ID_PATTERN.search(text or "")
    return match.group(0) if match else None


def _read_env_file(name: str) -> str | None:
    path = Path.home() / ".hermes" / ".env"
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{name}=") and not stripped.startswith("#"):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def load_dodo_api_key() -> str:
    key = os.environ.get("DODO_PAYMENTS_API_KEY") or _read_env_file(
        "DODO_PAYMENTS_API_KEY"
    )
    if not key:
        raise DodoError(
            "DODO_PAYMENTS_API_KEY is not configured. Add a test-mode key to "
            "the environment or ~/.hermes/.env."
        )
    return key


class DodoClient:
    def __init__(
        self,
        api_key: str,
        environment: str = "test",
        opener: Callable = urllib.request.urlopen,
    ):
        if environment not in {"test", "live"}:
            raise DodoError("DODO_ENVIRONMENT must be 'test' or 'live'.")
        if environment == "live" and os.environ.get("DODO_ALLOW_LIVE") != "true":
            raise DodoError(
                "Live refunds are disabled. Set DODO_ALLOW_LIVE=true explicitly."
            )
        self.api_key = api_key
        self.environment = environment
        self.base_url = TEST_BASE_URL if environment == "test" else LIVE_BASE_URL
        self.opener = opener

    @classmethod
    def from_environment(cls) -> "DodoClient":
        return cls(
            load_dodo_api_key(),
            environment=os.environ.get("DODO_ENVIRONMENT", "test").lower(),
        )

    def create_refund(
        self,
        payment_id: str,
        reason: str,
        metadata: dict[str, str] | None = None,
    ) -> DodoRefund:
        payload = {
            "payment_id": payment_id,
            "reason": reason[:3000],
            "metadata": metadata or {},
        }
        request = urllib.request.Request(
            f"{self.base_url}/refunds",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "HermesSupportCrew/1.0",
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise DodoError(f"Dodo refund failed ({error.code}): {detail}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise DodoError(f"Dodo refund request failed: {error}") from error

        try:
            return DodoRefund(
                refund_id=body["refund_id"],
                payment_id=body["payment_id"],
                status=body["status"],
                amount=body.get("amount"),
                currency=body.get("currency"),
            )
        except KeyError as error:
            raise DodoError(f"Dodo returned an incomplete refund response: {body}") from error
