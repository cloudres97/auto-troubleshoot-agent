import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

from troubleshoot.config import Settings
from troubleshoot.launcher import StartupError, failure_hint, initialize_env, load_env, main, preflight, supervise


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_env_reader_preserves_dollar_hash_quotes_and_windows_line_endings(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b'\xef\xbb\xbf# config\r\nDIAG_API_TOKEN=abc$def#123\r\nDIAG_REVIEWER_TOKEN=\'review$#token\'\r\nDIAG_EXECUTOR_TOKEN="executor$#token"\r\n')
    config = load_env(path, {"DIAG_API_TOKEN": "expanded-wrong-value"})
    assert config["DIAG_API_TOKEN"] == "abc$def#123"
    assert config["DIAG_REVIEWER_TOKEN"] == "review$#token"
    assert config["DIAG_EXECUTOR_TOKEN"] == "executor$#token"
    Settings.load(config)


def test_first_start_initializes_credentials_without_overwriting(tmp_path):
    path = tmp_path / ".env"
    assert initialize_env(path)
    before = path.read_bytes()
    settings = Settings.load(load_env(path, {}))
    assert len({settings.api_token, settings.reviewer_token, settings.executor_token}) == 3
    assert not initialize_env(path)
    assert path.read_bytes() == before
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_existing_invalid_configuration_is_not_silently_replaced(tmp_path):
    path = tmp_path / ".env"
    path.write_text("DIAG_API_TOKEN=same\nDIAG_REVIEWER_TOKEN=same\nDIAG_EXECUTOR_TOKEN=same\n")
    assert not initialize_env(path)
    with pytest.raises(RuntimeError, match="distinct"):
        Settings.load(load_env(path, {}))


def test_supervisor_runs_full_generic_workflow_and_cleans_up(tmp_path, capsys):
    api_port, executor_port = free_port(), free_port()
    while api_port == executor_port:
        executor_port = free_port()
    initialize_env(tmp_path / ".env")
    config = load_env(tmp_path / ".env")
    registry = Path(__file__).resolve().parents[1] / "environments.example.json"
    config.update(DIAG_ENVIRONMENTS_FILE=str(registry), DIAG_EXECUTOR_URL=f"http://127.0.0.1:{executor_port}")
    stop = threading.Event()
    errors = []
    def run():
        try:
            supervise(config, tmp_path, api_port, stop)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    output = ""
    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            base = f"http://127.0.0.1:{api_port}"
            for _ in range(150):
                if errors:
                    raise errors[0]
                try:
                    response = client.get(base + "/health")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                pytest.fail("API did not become ready")
            headers = {"Authorization": "Bearer " + config["DIAG_API_TOKEN"]}
            created = client.post(base + "/v1/cases", headers=headers, json={"environment_id": "demo", "target_id": "demo-go", "target_type": "go_service", "symptom": "服务无响应"})
            assert created.status_code == 201
            for _ in range(150):
                detail = client.get(base + "/v1/cases/" + created.json()["id"], headers=headers).json()
                if detail["state"] == "awaiting_review":
                    break
                time.sleep(.1)
            else:
                pytest.fail("Supervised Worker did not process case")
            assert detail["evidence"][0]["target_type"] == "go_service"
            for _ in range(100):
                output += capsys.readouterr().out
                if "READY:" in output:
                    break
                time.sleep(.05)
    finally:
        stop.set()
        thread.join(timeout=20)
    assert not thread.is_alive()
    assert errors == []
    output += capsys.readouterr().out
    assert "READY:" in output
    for key in ("DIAG_API_TOKEN", "DIAG_REVIEWER_TOKEN", "DIAG_EXECUTOR_TOKEN"):
        assert config[key] not in output
    for port in (api_port, executor_port):
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_occupied_port_is_not_killed_or_reused(tmp_path):
    initialize_env(tmp_path / ".env")
    config = load_env(tmp_path / ".env")
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        config["DIAG_EXECUTOR_URL"] = f"http://127.0.0.1:{port}"
        with pytest.raises(StartupError, match=f"executor port {port} is already in use"):
            supervise(config, tmp_path, free_port(), threading.Event())


def test_missing_registry_explains_failure_without_printing_path_or_secret(tmp_path):
    initialize_env(tmp_path / ".env")
    config = load_env(tmp_path / ".env")
    config["DIAG_ENVIRONMENTS_FILE"] = "private-missing-file.json"
    config["DIAG_EXECUTOR_URL"] = f"http://127.0.0.1:{free_port()}"
    with pytest.raises(StartupError, match="Target registry file not found") as error:
        preflight(config, tmp_path, free_port())
    assert "private-missing" not in str(error.value)
    assert config["DIAG_API_TOKEN"] not in str(error.value)


def test_check_mode_does_not_create_configuration(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["launcher", "--check"])
    assert main() == 1
    assert ".env does not exist" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_check_mode_validates_without_starting_services(tmp_path, monkeypatch, capsys):
    initialize_env(tmp_path / ".env")
    registry = Path(__file__).resolve().parents[1] / "environments.example.json"
    with (tmp_path / ".env").open("a", encoding="utf-8") as stream:
        stream.write(f"DIAG_ENVIRONMENTS_FILE={registry}\nDIAG_EXECUTOR_URL=http://127.0.0.1:{free_port()}\n")
    before = (tmp_path / ".env").read_bytes()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["launcher", "--check", "--port", str(free_port())])
    assert main() == 0
    output = capsys.readouterr().out
    assert "CHECK OK" in output
    assert not (tmp_path / "data").exists()
    assert (tmp_path / ".env").read_bytes() == before


def test_log_hint_ignores_previous_runs_and_never_emits_raw_log(tmp_path):
    path = tmp_path / "api.log"
    previous = b"No module named secret-OLD\n"
    path.write_bytes(previous + b"OperationalError: unable to open database file at secret-NEW\n")
    hint = failure_hint(path, len(previous))
    assert "database file cannot be opened" in hint
    assert "module" not in hint
    assert "secret" not in hint


def test_failed_api_names_service_and_cleans_up_executor(tmp_path):
    initialize_env(tmp_path / ".env")
    config = load_env(tmp_path / ".env")
    executor_port, api_port = free_port(), free_port()
    while api_port == executor_port:
        api_port = free_port()
    invalid_database = tmp_path / "not-a-database-file"
    invalid_database.mkdir()
    config.update(DIAG_DATABASE_URL="sqlite:///" + str(invalid_database), DIAG_EXECUTOR_URL=f"http://127.0.0.1:{executor_port}", DIAG_ENVIRONMENTS_FILE=str(Path(__file__).resolve().parents[1] / "environments.example.json"))
    with pytest.raises(StartupError, match="api exited.*database file cannot be opened"):
        supervise(config, tmp_path, api_port)
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", executor_port)) != 0
