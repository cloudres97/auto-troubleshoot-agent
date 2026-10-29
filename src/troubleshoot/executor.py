"""Portable executor: registered read-only tools only; no command execution."""
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException

from .auth import bearer
from .contracts import ToolRequest, ToolResult
from .db import now


def load_registry():
    path = os.getenv("DIAG_ENVIRONMENTS_FILE")
    if not path:
        raise RuntimeError("Set DIAG_ENVIRONMENTS_FILE to a reviewed environment registry")
    registry = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(registry, dict):
        raise ValueError("Environment registry must be an object")
    for entry in registry.values():
        if entry.get("adapter") not in ("demo", "yarn"):
            raise ValueError("Unsupported adapter")
        if entry["adapter"] == "yarn":
            u = urlsplit(entry.get("base_url", ""))
            if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
                raise ValueError("YARN base_url must be a fixed origin without credentials or path")
    return registry


def create_executor(registry=None, token=None, client=None):
    registry = load_registry() if registry is None else registry
    token = token or os.getenv("DIAG_EXECUTOR_TOKEN", "")
    if not token:
        raise RuntimeError("Set DIAG_EXECUTOR_TOKEN")
    app = FastAPI(title="Read-only environment executor")
    # Bounds total concurrent acquisitions, including upstream latency.
    slots = threading.BoundedSemaphore(2)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/tools/run", response_model=ToolResult, dependencies=[Depends(bearer(token))])
    def run(request: ToolRequest):
        target = registry.get(request.environment_id)
        if target is None:
            raise HTTPException(404, "Unregistered environment")
        if request.application_id not in target.get("applications", []):
            raise HTTPException(403, "Application is outside the registered scope")
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "Collection concurrency budget exhausted")
        try:
            status, truncated = "ok", False
            if target["adapter"] == "demo":
                data = {"app": {"id": request.application_id, "state": "FAILED", "finalStatus": "FAILED", "diagnostics": "DEMO: Container exited with a non-zero exit code. Root cause remains unconfirmed."}}
                raw = json.dumps(data, ensure_ascii=False)
            else:
                try:
                    def collect(http):
                        nonlocal truncated
                        url = target["base_url"].rstrip("/") + "/ws/v1/cluster/apps/" + request.application_id
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
                except httpx.HTTPError:
                    # Never expose upstream URLs, authorization headers or error bodies.
                    status, raw = "error", "YARN collection failed; check executor connectivity and authentication."
            return ToolResult(**request.model_dump(), collected_at=now(), source=target["adapter"], status=status, raw=raw, truncated=truncated)
        finally:
            slots.release()

    return app
