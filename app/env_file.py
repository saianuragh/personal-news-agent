"""Small, non-interpolating loader for this application's local .env file."""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from pathlib import Path

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_local_env(
    env_file: str | Path | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Load simple KEY=value lines without overriding the process environment.

    Values are never logged or returned. Variable expansion is deliberately not
    supported; the file is local-only and must remain excluded by .gitignore.
    """
    target = (
        Path(env_file)
        if env_file is not None
        else Path(__file__).resolve().parents[1] / ".env"
    )
    values = os.environ if environ is None else environ
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValueError("Unable to read the local environment file.") from error

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ASSIGNMENT.fullmatch(raw_line)
        if match is None:
            raise ValueError(f"Malformed .env assignment on line {line_number}.")
        name, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", maxsplit=1)[0].rstrip()
        values.setdefault(name, value)
