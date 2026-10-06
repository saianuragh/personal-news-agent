from pathlib import Path

import yaml


def _load(name: str) -> dict:
    path = Path(__file__).parents[1] / ".github" / "workflows" / name
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_cloud_workflow_schedules_ist_and_supports_manual_production_runs() -> None:
    workflow = _load("daily-newsletter.yml")

    assert workflow["on"]["schedule"][0]["cron"] == "37 1 * * *"
    assert "workflow_dispatch" in workflow["on"]
    job = workflow["jobs"]["send-newsletter"]
    assert "${{ secrets.SMTP_PASSWORD }}" == job["env"]["SMTP_PASSWORD"]
    assert "${{ secrets.NEWSLETTER_RECIPIENT }}" == job["env"]["NEWSLETTER_RECIPIENT"]
    assert "${{ secrets.SMTP_USERNAME }}" == job["env"]["SMTP_USERNAME"]
    assert "${{ secrets.EMAIL_FROM }}" == job["env"]["EMAIL_FROM"]
    assert not any("DATABASE" in name for name in job["env"])
    assert "${{ secrets.NEWS_AGENT_DATABASE_URL }}" not in job["env"].values()
    assert "default_branch" in job["if"]
    assert "github.event_name == 'schedule'" in job["if"]


def test_manual_runs_send_only_when_send_input_is_true() -> None:
    workflow = _load("daily-newsletter.yml")
    send_input = workflow["on"]["workflow_dispatch"]["inputs"]["send"]
    assert send_input["type"] == "boolean"
    assert send_input["default"] == "false"

    steps = {step.get("name"): step for step in workflow["jobs"]["send-newsletter"]["steps"]}
    decide = steps["Decide whether to send"]
    assert "github.event.inputs.send" in decide["run"]
    send_step = steps["Run the production newsletter pipeline"]
    assert send_step["if"] == "steps.decide.outputs.send == 'true'"
    assert "personal-news-agent send" in send_step["run"]

    commands = [s["run"] for s in workflow["jobs"]["send-newsletter"]["steps"] if "run" in s]
    assert commands[0] == "python -m pip install ."
    assert commands[1] == "personal-news-agent config-check"
    sends = [c for c in commands if "personal-news-agent send" in c]
    assert len(sends) == 1


def test_safe_summary_never_claims_inbox_delivery() -> None:
    workflow = _load("daily-newsletter.yml")
    steps = {step.get("name"): step for step in workflow["jobs"]["send-newsletter"]["steps"]}
    summary = steps["Print safe summary"]["run"]
    assert "Inbox delivery = not verified" in summary
    assert "delivered" not in summary.lower()


def test_keepalive_workflow_is_minimal_and_checks_api_errors() -> None:
    workflow = _load("keepalive.yml")
    assert workflow["permissions"] == {"actions": "write"}
    run = workflow["jobs"]["keepalive"]["steps"][0]["run"]
    assert "--fail-with-body" in run
    assert "actions/workflows/daily-newsletter.yml/enable" in run
    assert "secrets." not in str(workflow)
