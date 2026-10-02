from __future__ import annotations

from app.env_file import load_local_env


def test_local_environment_file_loads_values_without_overriding_process_environment(
    tmp_path,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# local config\nA=from-file\nB='quoted value'\nC=value # comment\n",
        encoding="utf-8",
    )
    environment = {"A": "from-process"}

    load_local_env(path, environ=environment)

    assert environment == {"A": "from-process", "B": "quoted value", "C": "value"}


def test_local_environment_file_reports_malformed_line_without_showing_value(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text("LLM_API_KEY=secret-value\nthis is malformed secret-value\n", encoding="utf-8")

    try:
        load_local_env(path, environ={})
    except ValueError as error:
        assert str(error) == "Malformed .env assignment on line 2."
        assert "secret-value" not in str(error)
    else:
        raise AssertionError("malformed file should be rejected")
