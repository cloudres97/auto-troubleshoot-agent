"""Local development process supervisor; no shell sourcing or secret logging."""
import argparse
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import Settings


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


def supervise(config, root, api_port, stop=None):
    settings = Settings.load(config)
    address = urlsplit(settings.executor_url)
    if address.scheme != "http" or address.hostname not in ("127.0.0.1", "localhost") or address.path not in ("", "/") or address.username or address.password or address.query or address.fragment:
        raise ValueError("Local launcher requires a loopback DIAG_EXECUTOR_URL; use separate services for a remote Windows executor")
    executor_port = address.port or 80
    if api_port == executor_port:
        raise ValueError("API and executor ports must differ")
    for port in (api_port, executor_port):
        check_port(port)
    config = dict(config)
    exclusions = config.get("NO_PROXY", config.get("no_proxy", "")) + ",127.0.0.1,localhost"
    config.update(NO_PROXY=exclusions, no_proxy=exclusions)
    stop = stop or threading.Event()
    logs = root / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    children, files = [], []

    def start(name, arguments):
        descriptor = os.open(logs / (name + ".log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
        files.append(stream)
        child = subprocess.Popen([sys.executable, *arguments], cwd=root, env=config, stdout=stream, stderr=stream, start_new_session=os.name != "nt")
        children.append(child)

    def ready(client, port):
        for _ in range(150):
            if stop.is_set():
                raise InterruptedError("Startup cancelled")
            if any(child.poll() is not None for child in children):
                raise RuntimeError("A service exited; inspect data/logs")
            try:
                if client.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            stop.wait(0.2)
        raise RuntimeError("Service readiness timed out; inspect data/logs")

    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            start("executor", ["-m", "uvicorn", "troubleshoot.executor:create_executor", "--factory", "--host", "127.0.0.1", "--port", str(executor_port)])
            ready(client, executor_port)
            start("api", ["-m", "uvicorn", "troubleshoot.api:create_app", "--factory", "--host", "127.0.0.1", "--port", str(api_port)])
            ready(client, api_port)
            # Require actual authenticated registry access, not just an open port.
            response = client.get(f"http://127.0.0.1:{api_port}/v1/targets", headers={"Authorization": "Bearer " + settings.api_token}, timeout=10)
            response.raise_for_status()
            start("worker", ["-m", "troubleshoot.worker"])
            if stop.wait(0.5):
                return
            if any(child.poll() is not None for child in children):
                raise RuntimeError("A service exited; inspect data/logs")
            print(f"READY: http://localhost:{api_port} | API + executor + worker", flush=True)
            print("Use DIAG_API_TOKEN from .env in the page. Logs: data/logs. Ctrl+C stops all three services.", flush=True)
            while not stop.wait(0.5):
                if any(child.poll() is not None for child in children):
                    raise RuntimeError("A service exited; stopping the remaining services. Inspect data/logs")
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
    args = parser.parse_args()
    root = Path.cwd()
    if initialize_env(root / ".env"):
        print("Created .env with three distinct random credentials; values are not printed.", flush=True)
    stop = threading.Event()
    for event in (signal.SIGINT, signal.SIGTERM):
        signal.signal(event, lambda *_: stop.set())
    try:
        supervise(load_env(root / ".env"), root, args.port, stop)
    except InterruptedError:
        pass
    except (OSError, RuntimeError, ValueError, httpx.HTTPError):
        print("Startup failed: check .env, port availability and data/logs. Existing .env is never overwritten; its three credentials must be nonempty and distinct.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
