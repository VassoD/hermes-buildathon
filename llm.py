"""Minimal LLM client using the provider configured in ~/.hermes.

Reads the model from ~/.hermes/config.yaml (model.default) and the API key
from OPENAI_API_KEY (environment first, then ~/.hermes/.env). Stdlib only,
no external dependencies.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERMES_DIR = Path.home() / ".hermes"
API_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 2

# Cost per million tokens (input, output). Unknown models get a NULL
# cost_estimate in the runs table.
# gpt-5.6-sol pricing as of July 2026 (GA on July 9, 2026), sourced from
# OpenAI pricing docs and OpenRouter listings.
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-5.6-sol": (5.00, 30.00),
}


class LLMError(Exception):
    """Raised when the LLM call fails after retries or returns bad output."""


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_estimate(self) -> float | None:
        pricing = PRICING_PER_MTOK.get(self.model)
        if pricing is None:
            return None
        input_price, output_price = pricing
        return (
            self.prompt_tokens * input_price
            + self.completion_tokens * output_price
        ) / 1_000_000


def load_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    env_file = HERMES_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("OPENAI_API_KEY=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise LLMError(
        "OPENAI_API_KEY not found in environment or ~/.hermes/.env. "
        "Add it there or export it before running."
    )


def load_model() -> str:
    override = os.environ.get("HERMES_MODEL")
    if override:
        return override
    config_file = HERMES_DIR / "config.yaml"
    if config_file.exists():
        # Targeted parse of the top-level model block, avoids a yaml dependency.
        in_model_block = False
        for line in config_file.read_text().splitlines():
            if re.match(r"^model:\s*$", line):
                in_model_block = True
                continue
            if in_model_block:
                if re.match(r"^\S", line):
                    break
                match = re.match(r'^\s+(?:default|model):\s*"?([^"\s#]+)"?', line)
                if match:
                    return match.group(1)
    raise LLMError(
        "Could not read model.default from ~/.hermes/config.yaml. "
        "Set HERMES_MODEL to override."
    )


def complete(system_prompt: str, user_prompt: str) -> LLMResult:
    """Single-turn chat completion against the configured provider."""
    api_key = load_api_key()
    model = load_model()
    # No temperature override: the configured model only accepts its default.
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
    ).encode()

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        request = urllib.request.Request(
            f"{API_BASE_URL}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read())
            usage = body.get("usage", {})
            return LLMResult(
                text=body["choices"][0]["message"]["content"],
                model=body.get("model", model),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            )
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            last_error = LLMError(f"API returned {error.code}: {detail}")
            # Client errors other than rate limiting will not succeed on retry.
            if error.code != 429 and error.code < 500:
                raise last_error from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as error:
            last_error = LLMError(f"LLM call failed: {error}")
        time.sleep(RETRY_BASE_DELAY_SECONDS * 2**attempt)

    raise LLMError(f"LLM call failed after {MAX_RETRIES} attempts: {last_error}")


def extract_json(text: str) -> dict:
    """Extract the first JSON object from a model response."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        raise LLMError(f"No JSON object found in model response: {text[:200]}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as error:
        raise LLMError(f"Model returned invalid JSON: {error}") from error
