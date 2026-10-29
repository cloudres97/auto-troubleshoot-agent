"""Run an actual HTTP workflow, then stop only the services started here."""
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    api_port, executor_port = port(), port()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as folder:
        config = dict(os.environ)
        config.update(DIAG_API_TOKEN=secrets.token_urlsafe(32), DIAG_REVIEWER_TOKEN=secrets.token_urlsafe(32), DIAG_EXECUTOR_TOKEN=secrets.token_urlsafe(32), DIAG_DATABASE_URL="sqlite:///" + str(Path(folder) / "smoke.db"), DIAG_EXECUTOR_URL=f"http://127.0.0.1:{executor_port}", DIAG_ENVIRONMENTS_FILE=str(root / "environments.example.json"))
        config["NO_PROXY"] = config.get("NO_PROXY", "") + ",127.0.0.1,localhost"
        children = []
        with (Path(folder) / "services.log").open("w+") as log:
            try:
                for component, selected in (("executor:create_executor", executor_port), ("api:create_app", api_port)):
                    children.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "troubleshoot." + component, "--factory", "--host", "127.0.0.1", "--port", str(selected)], cwd=root, env=config, stdout=log, stderr=log))
                with httpx.Client(trust_env=False, timeout=5) as client:
                    for selected in (executor_port, api_port):
                        for _ in range(100):
                            if any(p.poll() is not None for p in children):
                                raise RuntimeError("Service exited before readiness")
                            try:
                                if client.get(f"http://127.0.0.1:{selected}/health").status_code == 200:
                                    break
                            except httpx.HTTPError:
                                pass
                            time.sleep(0.1)
                        else:
                            raise RuntimeError("Service readiness deadline exceeded")
                    base = f"http://127.0.0.1:{api_port}"
                    assert "自动诊断工作台" in client.get(base).text
                    op = {"Authorization": "Bearer " + config["DIAG_API_TOKEN"]}
                    review = {"Authorization": "Bearer " + config["DIAG_REVIEWER_TOKEN"]}
                    catalog = client.get(base + "/v1/targets", headers=op)
                    catalog.raise_for_status()
                    assert len(catalog.json()) == 7
                    created = client.post(base + "/v1/cases", headers=op, json={"environment_info": "demo（演示环境）", "symptom": "订单服务不可用，请帮忙排查"})
                    created.raise_for_status()
                    path = base + "/v1/cases/" + created.json()["id"]
                    subprocess.run([sys.executable, "-m", "troubleshoot.worker", "--once"], cwd=root, env=config, check=True, timeout=40)
                    detail = client.get(path, headers=op).json()
                    assert detail["state"] == "awaiting_review"
                    assert detail["report"]["root_cause"] is None
                    assert detail["evidence"][0]["source"] == "demo"
                    assert detail["evidence"][0]["target_id"] == "demo-java"
                    assert detail["plan"]["skill_id"] == "service-status-triage"
                    response = client.post(path + "/review", headers=review, json={"revision": detail["revision"], "note": "Smoke: demo report reviewed"})
                    response.raise_for_status()
                    assert client.get(path, headers=op).json()["state"] == "completed"
                print("PASS: HTTP services -> queued case -> worker -> demo evidence -> reviewed report")
            finally:
                for child in children:
                    child.terminate()
                for child in children:
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()


if __name__ == "__main__":
    main()
