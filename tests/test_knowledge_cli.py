import json

import pytest

import FRS.knowledge.__main__ as cli


def test_default_cli_is_retrieval_only_without_model_or_export(monkeypatch, capsys):
    def forbidden():
        raise AssertionError("model should not start")

    monkeypatch.setattr(cli, "build_local_service", forbidden)
    monkeypatch.setattr("sys.argv", ["knowledge", "--question", "지원 포메이션은?"])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["model_requests"] == result["trace_requests"] == 0
    assert result["mode"] == "retrieval_plan_only" and result["hits"]


def test_upload_requires_explicit_execution(monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["knowledge", "--question", "hello", "--upload-trace"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_manual_run_records_stop_without_reusing_id(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "knowledge",
            "--question",
            "지원 포메이션은?",
            "--run",
            "--run-id",
            "test-run",
        ],
    )

    def unavailable():
        raise ValueError("not configured")

    monkeypatch.setattr(cli, "build_local_service", unavailable)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    directory = tmp_path / "artifacts/knowledge-runs/test-run"
    assert (directory / "stopped.json").exists()
    with pytest.raises(FileExistsError):
        cli.main()
