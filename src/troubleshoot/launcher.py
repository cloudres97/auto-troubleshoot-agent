"""Local development process supervisor; no shell sourcing or secret logging."""
import argparse
import errno
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .registry import normalize_registry


class StartupError(RuntimeError):
    """Safe diagnostic text that never includes configuration values or raw logs."""


def load_env(path, inherited=None):
    config = dict(os.environ if inherited is None else inherited)
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key):
            raise ValueError(f"Invalid .env assignment on line {line_number}")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"Unclosed .env quote on line {line_number}")
            value = value[1:-1]
        # Literal values: no shell expansion, escape processing, or inline comments.
        config[key] = value
    return config


def initialize_env(path):
    if path.exists():
        return False
    lines = [f"{name}={secrets.token_urlsafe(32)}" for name in ("DIAG_API_TOKEN", "DIAG_REVIEWER_TOKEN", "DIAG_EXECUTOR_TOKEN")]
    lines += ["DIAG_DATABASE_URL=sqlite:///./data/diagnosis.db", "DIAG_EXECUTOR_URL=http://127.0.0.1:18081", "DIAG_ENVIRONMENTS_FILE=./environments.example.json"]
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n")
    return True


def check_port(port):
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def preflight(config, root, api_port):
    try:
        settings = Settings.load(config)
    except RuntimeError as exc:
        raise StartupError("Credentials: " + str(exc)) from exc
    print("[check] Credentials: OK (values hidden)", flush=True)
    try:
        address = urlsplit(settings.executor_url)
        executor_port = 80 if address.port is None else address.port
    except ValueError as exc:
        raise StartupError("DIAG_EXECUTOR_URL is malformed; use http://127.0.0.1:18081 for local startup") from exc
    if address.scheme != "http" or address.hostname not in ("127.0.0.1", "localhost") or address.path not in ("", "/") or address.username or address.password or address.query or address.fragment:
        raise StartupError("Local launcher requires a loopback DIAG_EXECUTOR_URL; use http://127.0.0.1:18081 locally, or separate services for a remote Windows executor")
    if api_port == executor_port:
        raise StartupError("API and executor ports must differ")
    for name, port in (("API", api_port), ("executor", executor_port)):
        try:
            check_port(port)
        except ValueError as exc:
            raise StartupError(f"{name} port must be between 1 and 65535") from exc
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
                reason = "is already in use; stop the old service with Ctrl+C, then retry"
            elif exc.errno in (errno.EACCES, errno.EPERM):
                reason = "cannot be bound: permission denied"
            else:
                reason = "cannot be bound on 127.0.0.1"
            raise StartupError(f"{name} port {port} {reason}. No existing process was stopped.") from exc
        print(f"[check] {name} port {port}: available", flush=True)
    registry_file = config.get("DIAG_ENVIRONMENTS_FILE")
    if not registry_file:
        raise StartupError("DIAG_ENVIRONMENTS_FILE is missing; set it to ./environments.example.json for the demo")
    registry_path = Path(registry_file)
    if not registry_path.is_absolute():
        registry_path = root / registry_path
    try:
        registry = normalize_registry(json.loads(registry_path.read_text(encoding="utf-8-sig")))
    except FileNotFoundError as exc:
        raise StartupError("Target registry file not found; check DIAG_ENVIRONMENTS_FILE relative to the repository directory") from exc
    except OSError as exc:
        raise StartupError("Target registry file cannot be read; check its permissions") from exc
    except (ValueError, TypeError) as exc:
        raise StartupError("Invalid target registry; check JSON, target types and adapter settings (values hidden)") from exc
    print(f"[check] Target registry: OK ({sum(len(e['targets']) for e in registry.values())} targets)", flush=True)
    return settings, executor_port


def failure_hint(log_path, offset):
    """Classify only this run's bounded log tail; never print raw exception text."""
    try:
        with log_path.open("rb") as stream:
            stream.seek(max(offset, log_path.stat().st_size - 16384))
            tail = stream.read(16384).decode("utf-8", errors="replace")
    except OSError:
        return ""
    for marker, hint in (
        ("No module named", "a required Python module is missing; retry dependency installation"),
        ("unable to open database file", "database file cannot be opened; check DIAG_DATABASE_URL and directory permissions"),
        ("database is locked", "SQLite database is locked; stop the previous API and Worker before restarting"),
        ("PermissionError", "a required file or directory is not writable/readable"),
        ("Address already in use", "a configured port was occupied during startup"),
        ("address already in use", "a configured port was occupied during startup"),
        ("FileNotFoundError", "a configured file is missing"),
        ("OperationalError", "database initialization/connection failed; check database configuration and access"),
    ):
        if marker in tail:
            return "; " + hint
    return ""


def supervise(config, root, api_port, stop=None):
    settings, executor_port = preflight(config, root, api_port)
    config = dict(config)
    exclusions = config.get("NO_PROXY", config.get("no_proxy", "")) + ",127.0.0.1,localhost"
    config.update(NO_PROXY=exclusions, no_proxy=exclusions)
    stop = stop or threading.Event()
    logs = root / "data" / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StartupError("Cannot create data/logs; check repository directory permissions") from exc
    children, files = [], []
    child_logs = []

    def start(name, arguments):
        log_path = logs / (name + ".log")
        try:
            descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            stream = os.fdopen(descriptor, "w", encoding="utf-8")
        except OSError as exc:
            raise StartupError(f"Cannot write data/logs/{name}.log; check file permissions") from exc
        files.append(stream)
        offset = log_path.stat().st_size
        print(f"[start] {name}; log: data/logs/{name}.log", flush=True)
        try:
            child = subprocess.Popen([sys.executable, *arguments], cwd=root, env=config, stdout=stream, stderr=stream, start_new_session=os.name != "nt")
        except OSError as exc:
            raise StartupError(f"Could not launch {name}; check the Python interpreter and process permissions") from exc
        children.append(child)
        child_logs.append((child, name, log_path, offset))

    def check_children():
        for child, name, log_path, offset in child_logs:
            code = child.poll()
            if code is not None:
                hint = failure_hint(log_path, offset)
                raise StartupError(f"{name} exited (code {code}){hint}. See data/logs/{name}.log locally; do not share credentials or raw logs without review.")

    def ready(client, name, port):
        for _ in range(150):
            if stop.is_set():
                raise InterruptedError("Startup cancelled")
            check_children()
            try:
                if client.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            stop.wait(0.2)
        raise StartupError(f"{name} readiness timed out on port {port}; inspect data/logs/{name}.log")

    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            start("executor", ["-m", "uvicorn", "troubleshoot.executor:create_executor", "--factory", "--host", "127.0.0.1", "--port", str(executor_port)])
            ready(client, "executor", executor_port)
            start("api", ["-m", "uvicorn", "troubleshoot.api:create_app", "--factory", "--host", "127.0.0.1", "--port", str(api_port)])
            ready(client, "api", api_port)
            # Require actual authenticated registry access, not just an open port.
            try:
                response = client.get(f"http://127.0.0.1:{api_port}/v1/targets", headers={"Authorization": "Bearer " + settings.api_token}, timeout=10)
            except httpx.HTTPError as exc:
                raise StartupError("API started but the authenticated target-catalog request failed; inspect data/logs/api.log") from exc
            if response.status_code != 200:
                raise StartupError(f"Target catalog returned HTTP {response.status_code}; inspect data/logs/api.log and data/logs/executor.log, and verify DIAG_EXECUTOR_TOKEN is consistent")
            start("worker", ["-m", "troubleshoot.worker"])
            if stop.wait(0.5):
                return
            check_children()
            print(f"READY: http://localhost:{api_port} | API + executor + worker", flush=True)
            print("Use DIAG_API_TOKEN from .env in the page. Logs: data/logs. Ctrl+C stops all three services.", flush=True)
            while not stop.wait(0.5):
                check_children()
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for stream in files:
            stream.close()


def main():
    parser = argparse.ArgumentParser(description="Start the local diagnosis workbench")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--check", action="store_true", help="Check configuration, registry and ports without starting services or modifying files")
    args = parser.parse_args()
    root = Path.cwd()
    stop = threading.Event()
    try:
        if args.check:
            if not (root / ".env").is_file():
                raise StartupError(".env does not exist; run bash start.sh to initialize it first")
            preflight(load_env(root / ".env"), root, args.port)
            print("CHECK OK: static checks passed. No services started; database access is verified during startup.", flush=True)
        else:
            for event in (signal.SIGINT, signal.SIGTERM):
                signal.signal(event, lambda *_: stop.set())
            if initialize_env(root / ".env"):
                print("Created .env with three distinct random credentials; values are not printed.", flush=True)
            supervise(load_env(root / ".env"), root, args.port, stop)
    except InterruptedError:
        pass
    except (StartupError, ValueError) as exc:
        # ValueError outside preflight comes from our .env parser; only emit its
        # own line-number diagnostics, never a third-party exception's values.
        message = str(exc) if isinstance(exc, StartupError) or str(exc).startswith(("Invalid .env assignment on line ", "Unclosed .env quote on line ")) else "Configuration syntax/encoding is invalid (values hidden)"
        print("Startup failed: " + message, file=sys.stderr)
        return 1
    except (OSError, RuntimeError, httpx.HTTPError):
        print("Startup failed while accessing local configuration/files; check .env and data/logs permissions (values hidden).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
