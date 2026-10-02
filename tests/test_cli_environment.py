import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.cli import (
    _default_config_directory,
    _default_preview_directory,
    main,
)
from app.pipeline.runner import PipelineCounts, PipelineRunResult, StageFailure


def test_runtime_paths_can_be_overridden_by_environment(monkeypatch, tmp_path):
    config_path = tmp_path / "config"
    preview_path = tmp_path / "previews"
    monkeypatch.setenv("APP_CONFIG_DIR", str(config_path))
    monkeypatch.setenv("PREVIEW_DIRECTORY", str(preview_path))

    assert _default_config_directory() == config_path
    assert _default_preview_directory() == preview_path


def test_email_credential_cli_uses_hidden_prompt_and_never_displays_value(
    monkeypatch, capsys
) -> None:
    secret = "hidden-smtp-test-secret"
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("SMTP_USERNAME", "sender@example.test")
    monkeypatch.setattr("app.cli.keyring_dependency_available", lambda: True)
    monkeypatch.setattr("app.cli.getpass.getpass", lambda _prompt: secret)
    saved: list[tuple[str, str]] = []

    def store(username: str, *, prompt) -> None:
        saved.append((username, prompt("hidden")))

    monkeypatch.setattr("app.cli.prompt_and_store_smtp_credential", store)

    assert main(["email-credential-set"]) == 0
    output = capsys.readouterr().out

    assert saved == [("sender@example.test", secret)]
    assert "smtp_credential_stored" in output
    assert secret not in output


def test_runs_cli_prints_bounded_safe_history_summary(monkeypatch, capsys) -> None:
    secret = "history-secret-must-not-be-printed"

    class FakeRepository:
        def list_runs(self, limit: int):
            assert limit == 5
            return (
                {
                    "run_id": "run-123",
                    "status": "partial",
                    "mode": "preview",
                    "sources_failed": 1,
                    "warnings_json": secret,
                    "error": secret,
                    "database_url": secret,
                },
            )

    monkeypatch.setattr("app.cli.load_local_env", lambda: None)
    monkeypatch.setattr("app.cli.create_run_repository", lambda: FakeRepository())

    assert main(["runs", "--limit", "5"]) == 0

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["status"] == "runs_listed"
    assert payload["count"] == 1
    assert payload["runs"] == [
        {
            "run_id": "run-123",
            "status": "partial",
            "mode": "preview",
            "sources_failed": 1,
            "started_at": None,
            "completed_at": None,
            "as_of": None,
            "sources_attempted": None,
            "sources_succeeded": None,
            "raw_entries": None,
            "article_count": None,
            "duplicate_count": None,
            "story_count": None,
            "categorized_count": None,
            "ranked_count": None,
            "selected_count": None,
            "summarized_count": None,
            "fallback_count": None,
            "summary_failure_count": None,
            "omitted_count": None,
            "delivery_outcome": None,
        }
    ]
    assert secret not in output


def test_runs_cli_reports_database_read_failure_without_exception_details(
    monkeypatch, capsys
) -> None:
    secret = "postgres-password-must-not-be-printed"

    class FailingRepository:
        def list_runs(self, limit: int):
            raise RuntimeError(f"database connection failed: {secret}")

    monkeypatch.setattr("app.cli.load_local_env", lambda: None)
    monkeypatch.setattr("app.cli.create_run_repository", lambda: FailingRepository())

    assert main(["runs"]) == 1

    output = capsys.readouterr().out
    assert json.loads(output) == {
        "status": "run_history_unavailable",
        "error": "Pipeline run history could not be read safely.",
    }
    assert secret not in output


def test_delivery_reset_cli_targets_configured_recipient_and_explicit_date_without_pipeline(
    monkeypatch, capsys
) -> None:
    from app.email.base import newsletter_idempotency_key

    recipient = "reader@example.test"
    secret = "smtp-secret-must-not-be-printed"
    observed = []

    class FakeRepository:
        def reset_delivery_claim(self, delivery_key: str) -> bool:
            observed.append(delivery_key)
            return True

    def unexpected_pipeline(*_args, **_kwargs):
        raise AssertionError("delivery-reset must not invoke the pipeline")

    monkeypatch.setattr("app.cli.load_local_env", lambda: None)
    monkeypatch.setattr("app.cli.create_run_repository", lambda: FakeRepository())
    monkeypatch.setattr("app.cli.PipelineRunner", unexpected_pipeline)
    monkeypatch.setenv("NEWSLETTER_RECIPIENT", recipient)
    monkeypatch.setenv("SMTP_PASSWORD", secret)

    assert main(["delivery-reset", "--date", "2026-10-02"]) == 0

    output = capsys.readouterr().out
    assert json.loads(output) == {
        "mode": "delivery-reset",
        "date": "2026-10-02",
        "status": "delivery_claim_reset",
    }
    assert observed == [newsletter_idempotency_key(recipient, datetime(2026, 10, 2).date())]
    assert secret not in output


def test_delivery_reset_cli_reports_missing_claim_and_never_calls_pipeline(
    monkeypatch, capsys
) -> None:
    class FakeRepository:
        def reset_delivery_claim(self, _delivery_key: str) -> bool:
            return False

    monkeypatch.setattr("app.cli.load_local_env", lambda: None)
    monkeypatch.setattr("app.cli.create_run_repository", lambda: FakeRepository())
    monkeypatch.setattr(
        "app.cli.PipelineRunner",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("pipeline invoked")),
    )
    monkeypatch.setenv("NEWSLETTER_RECIPIENT", "reader@example.test")

    assert main(["delivery-reset", "--date", "2026-10-01"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "mode": "delivery-reset",
        "date": "2026-10-01",
        "status": "no_delivery_claim",
    }


@pytest.mark.parametrize("value", [None, "today", "2026-2-02", "2026-02-30", "20261002"])
def test_delivery_reset_requires_strict_explicit_iso_date(monkeypatch, capsys, value) -> None:
    monkeypatch.setattr("app.cli.load_local_env", lambda: None)
    args = ["delivery-reset"] + ([] if value is None else ["--date", value])

    with pytest.raises(SystemExit) as error:
        main(args)

    assert error.value.code == 2
    assert "delivery-reset requires --date" in capsys.readouterr().err if value is None else True
    if value is not None:
        assert "YYYY-MM-DD" in capsys.readouterr().err


def test_delivery_reset_persistence_error_is_safe(monkeypatch, capsys) -> None:
    secret = "postgres://user:password@host/database"

    class FailingRepository:
        def reset_delivery_claim(self, _delivery_key: str) -> bool:
            raise RuntimeError(secret)

    monkeypatch.setattr("app.cli.load_local_env", lambda: None)
    monkeypatch.setattr("app.cli.create_run_repository", lambda: FailingRepository())
    monkeypatch.setenv("NEWSLETTER_RECIPIENT", "reader@example.test")

    assert main(["delivery-reset", "--date", "2026-10-02"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "delivery_reset_failed"
    assert secret not in output


@pytest.mark.parametrize(
    ("status", "expected_exit_code"),
    [
        ("preview", 0),
        ("partial", 0),
        ("all_sources_failed", 1),
        ("persistence_failed", 1),
    ],
)
def test_pipeline_cli_preserves_partial_and_failure_statuses(
    monkeypatch, capsys, status: str, expected_exit_code: int
) -> None:
    failure_ids = (
        ("source-b",)
        if status == "partial"
        else ("source-a", "source-b")
        if status == "all_sources_failed"
        else ()
    )
    failures = tuple(
        StageFailure("source_fetch", "Timeout", "Source fetch failed (Timeout).", source_id)
        for source_id in failure_ids
    )
    result = PipelineRunResult(
        uuid4(),
        "preview",
        datetime(2026, 10, 2, tzinfo=UTC),
        status,  # type: ignore[arg-type]
        PipelineCounts(sources_attempted=2, sources_failed=len(failures)),
        source_failures=failures,
    )

    class StubRunner:
        def __init__(self, dependencies) -> None:
            assert dependencies.run_repository is None

        def run(self, *args, **kwargs):
            return result

    monkeypatch.setattr("app.cli.load_local_env", lambda: None)
    monkeypatch.setattr("app.cli.create_run_repository", lambda: None)
    monkeypatch.setattr("app.cli.PipelineRunner", StubRunner)

    assert main(["preview"]) == expected_exit_code
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == status
    assert payload["source_failures"] == [
        {
            "stage": failure.stage,
            "source_id": failure.source_id,
            "error_type": failure.error_type,
            "message": failure.message,
        }
        for failure in failures
    ]
