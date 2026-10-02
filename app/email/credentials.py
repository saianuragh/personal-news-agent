"""Secure SMTP credential access for cloud environments and local OS keyrings."""

from __future__ import annotations

import os
from collections.abc import Mapping
from getpass import getpass
from importlib.util import find_spec
from typing import Protocol

_SERVICE_NAME = "personal-news-agent/smtp"


class KeyringBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...


def store_smtp_credential(
    username: str, password: str, *, backend: KeyringBackend | None = None
) -> None:
    """Store an SMTP app password without placing it in a file or command argument."""
    if not isinstance(username, str) or not username.strip():
        raise ValueError("SMTP_USERNAME must be configured before storing a credential")
    if not isinstance(password, str) or not password.strip():
        raise ValueError("Credential input was empty; nothing was stored")
    active_backend = backend or _system_keyring()
    normalized_username = username.strip()
    normalized_password = password.strip()
    try:
        active_backend.set_password(_SERVICE_NAME, normalized_username, normalized_password)
        stored_password = active_backend.get_password(_SERVICE_NAME, normalized_username)
        if stored_password is None or stored_password != normalized_password:
            raise RuntimeError
    except Exception:
        raise RuntimeError(
            "The SMTP credential could not be verified in the secure credential store."
        ) from None


def load_smtp_credential(
    username: str,
    *,
    backend: KeyringBackend | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Read cloud-injected SMTP_PASSWORD, otherwise use the local OS keyring.

    The returned value is only for immediate authentication and must never be logged.
    """
    values = os.environ if environ is None else environ
    environment_password = values.get("SMTP_PASSWORD", "").strip()
    if environment_password:
        return environment_password
    active_backend = backend or _system_keyring()
    return active_backend.get_password(_SERVICE_NAME, username)


def keyring_dependency_available() -> bool:
    """Report whether the optional OS-keyring package is installed, without loading a secret."""
    return find_spec("keyring") is not None


def smtp_credential_configured(
    username: str, *, environ: Mapping[str, str] | None = None
) -> bool:
    """Check cloud or keyring configuration without exposing a credential value."""
    values = os.environ if environ is None else environ
    if values.get("SMTP_PASSWORD", "").strip():
        return True
    if not username.strip() or not keyring_dependency_available():
        return False
    try:
        return bool(load_smtp_credential(username.strip(), environ=values))
    except Exception:
        return False


def prompt_and_store_smtp_credential(
    username: str,
    *,
    prompt=getpass,
    backend: KeyringBackend | None = None,
) -> None:
    """Prompt without echo and store the credential in the OS keyring."""
    password = prompt("SMTP app password (input hidden): ")
    store_smtp_credential(username, password, backend=backend)


def _system_keyring() -> KeyringBackend:
    try:
        import keyring
    except ImportError as error:
        raise RuntimeError(
            "Install the optional Windows email extra before configuring SMTP credentials."
        ) from error
    return keyring
