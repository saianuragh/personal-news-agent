"""Production CLI imports must not pull in optional database drivers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_config_check_runs_without_psycopg() -> None:
    script = """
import sys
sys.modules["psycopg"] = None
from app.cli import main
assert main(["config-check"]) == 0
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
