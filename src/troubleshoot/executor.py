"""Portable executor: registered read-only tools only; no command execution."""
import json
import os
import threading
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException

from .auth import bearer
from .contracts import ToolRequest, ToolResult, TargetInfo
from .db import now
from .registry import normalize_registry


def load_registry():
    path = os.getenv("DIAG_ENVIRONMENTS_FILE")
    if not path:
        raise RuntimeError("Set DIAG_ENVIRONMENTS_FILE to a reviewed environment registry")
    return normalize_registry(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def create_executor(registry=None, token=None, client=None):
    registry = load_registry() if registry is None else normalize_registry(registry)
    token = token or os.getenv("DIAG_EXECUTOR_TOKEN", "")
    if not token:
        raise RuntimeError("Set DIAG_EXECUTOR_TOKEN")
    app = FastAPI(title="Read-only environment executor")
    # Bounds total concurrent acquisitions, including upstream latency.
    slots = threading.BoundedSemaphore(2)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/v1/targets", response_model=list[TargetInfo], dependencies=[Depends(bearer(token))])
    def targets():
        return [TargetInfo(environment_id=env, target_id=key, target_type=value["target_type"], label=value["label"] or key, source=value["adapter"], aliases=value["aliases"]) for env, entry in registry.items() for key, value in entry["targets"].items()]

    @app.post("/v1/tools/run", response_model=ToolResult, dependencies=[Depends(bearer(token))])
    def run(request: ToolRequest):
        environment = registry.get(request.environment_id)
        if environment is None:
            raise HTTPException(404, "Unregistered environment")
        target = environment["targets"].get(request.target_id)
        if target is None or target["target_type"] != request.target_type:
            raise HTTPException(403, "Target is outside the registered scope")
        if request.application_id and request.application_id != target["application_id"]:
            raise HTTPException(403, "Legacy application does not match registered target")
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "Collection concurrency budget exhausted")
        try:
            status, truncated = "ok", False
            if target["adapter"] == "demo":
                if target["target_type"] == "yarn_application":
                    data = {"app": {"id": target["application_id"], "state": "FAILED", "finalStatus": "FAILED", "diagnostics": "DEMO: Root cause remains unconfirmed."}}
                else:
                    data = {"target_id": request.target_id, "target_type": request.target_type, "state": "DEMO_ONLY"}
                raw = json.dumps(data, ensure_ascii=False)
            else:
                try:
                    def collect(http):
                        nonlocal truncated
                        url = target["base_url"].rstrip("/") + "/ws/v1/cluster/apps/" + target["application_id"]
                        chunks = bytearray()
                        with http.stream("GET", url, follow_redirects=False, timeout=15) as response:
                            response.raise_for_status()
                            for chunk in response.iter_bytes():
                                remaining = 65536 - len(chunks)
                                chunks.extend(chunk[:remaining])
                                if len(chunk) > remaining:
                                    truncated = True
                                    break
                        return bytes(chunks).decode("utf-8", errors="replace")
                    if client is not None:
                        raw = collect(client)
                    else:
                        with httpx.Client(verify=True) as http:
                            raw = collect(http)
                    if not truncated and json.loads(raw)["app"]["id"] != target["application_id"]:
                        raise ValueError("Upstream application mismatch")
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    # Never expose upstream URLs, authorization headers or error bodies.
                    status, raw = "error", "YARN collection failed; check executor connectivity and authentication."
            return ToolResult(**request.model_dump(), collected_at=now(), source=target["adapter"], status=status, raw=raw, truncated=truncated)
        finally:
            slots.release()

    return app
