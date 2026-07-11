"""Store provider credentials in the macOS login Keychain."""

from __future__ import annotations

import os
import platform
import subprocess

SERVICE_NAME = "hermes-support-agency"
PROVIDERS = {
    "github": ("github-token", "GITHUB_TOKEN"),
    "linear": ("linear-api-key", "LINEAR_API_KEY"),
}


class CredentialStoreError(Exception):
    """Raised when a provider credential cannot be stored safely."""


def _provider(provider: str) -> tuple[str, str]:
    try:
        return PROVIDERS[provider]
    except KeyError as error:
        raise CredentialStoreError(f"Unsupported credential provider: {provider}") from error


def save_provider_secret(provider: str, secret: str) -> None:
    """Save a non-empty secret to the current user's macOS login Keychain."""
    account, _environment_name = _provider(provider)
    if not secret:
        return
    if platform.system() != "Darwin":
        raise CredentialStoreError(
            "Dashboard credential storage currently requires macOS Keychain."
        )
    result = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-a",
            account,
            "-s",
            SERVICE_NAME,
            "-w",
            secret,
        ],
        capture_output=True,
        text=True,
        shell=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "Keychain command failed").strip()
        raise CredentialStoreError(f"Could not save {provider} credential: {detail}")


def load_provider_secret(provider: str) -> str:
    """Read an environment override, then the macOS Keychain, without logging it."""
    account, environment_name = _provider(provider)
    environment_value = os.environ.get(environment_name, "")
    if environment_value:
        return environment_value
    if platform.system() != "Darwin":
        return ""
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-a",
            account,
            "-s",
            SERVICE_NAME,
            "-w",
        ],
        capture_output=True,
        text=True,
        shell=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def has_provider_secret(provider: str) -> bool:
    return bool(load_provider_secret(provider))
