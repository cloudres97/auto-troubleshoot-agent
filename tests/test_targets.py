import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from troubleshoot.api import create_app
from troubleshoot.config import Settings
from troubleshoot.contracts import CaseInput
from troubleshoot.db import Case, Evidence, database
from troubleshoot.diagnosis import verify_evidence
from troubleshoot.executor import create_executor
from troubleshoot.registry import normalize_registry
from troubleshoot.worker import run_once


OP = {"Authorization": "Bearer operator-test"}
REVIEW = {"Authorization": "Bearer reviewer-test"}
EX = {"Authorization": "Bearer executor-test"}
REGISTRY = json.loads((Path(__file__).resolve().parents[1] / "environments.example.json").read_text(encoding="utf-8"))


@pytest.fixture
def generic(tmp_path):
    settings = Settings(f"sqlite:///{tmp_path / 'test.db'}", "operator-test", "reviewer-test", "http://executor", "executor-test")
    sessions = database(settings.database_url)
    executor = TestClient(create_executor(REGISTRY, settings.executor_token))
    api = TestClient(create_app(settings, sessions, executor))
    return settings, sessions, api, executor


def tool_request(target_id, target_type):
    return {"case_id": str(uuid4()), "request_id": str(uuid4()), "environment_id": "demo", "target_id": target_id, "target_type": target_type, "tool": "get_target_status"}


@pytest.mark.parametrize("target_id", list(REGISTRY["demo"]["targets"]))
def test_each_target_type_can_complete_without_application_id(generic, target_id):
    settings, sessions, api, executor = generic
    kind = REGISTRY["demo"]["targets"][target_id]["target_type"]
    created = api.post("/v1/cases", headers=OP, json={"environment_id": "demo", "target_id": target_id, "target_type": kind, "symptom": "对象发生异常"})
    assert created.status_code == 201, created.text
    assert created.json()["application_id"] == ""
    assert run_once(sessions, settings, executor)
    case_id = created.json()["id"]
    detail = api.get(f"/v1/cases/{case_id}", headers=OP).json()
    assert detail["target_id"] == target_id
    assert detail["target_type"] == kind
    assert detail["state"] == "awaiting_review"
    assert detail["report"]["root_cause"] is None
    evidence = detail["evidence"][0]
    assert (evidence["target_id"], evidence["target_type"]) == (target_id, kind)
    if kind != "yarn_application":
        assert "尚未采集" in detail["report"]["observations"][0]["text"]
    assert api.post(f"/v1/cases/{case_id}/review", headers=REVIEW, json={"revision": detail["revision"], "note": "仅为演示"}).status_code == 200


def test_catalog_is_authenticated_and_hides_connection_configuration(generic):
    _, _, api, executor = generic
    assert api.get("/v1/targets").status_code == 401
    assert executor.get("/v1/targets", headers=OP).status_code == 401
    response = api.get("/v1/targets", headers=OP)
    assert response.status_code == 200
    assert len(response.json()) == 7
    assert {t["source"] for t in response.json()} == {"demo"}
    for target in response.json():
        assert set(target) == {"environment_id", "target_id", "target_type", "label", "source"}


@pytest.mark.parametrize("target_id,target_type", [("unregistered", "java_service"), ("demo-java", "network"), ("demo-go", "storage")])
def test_target_and_type_allowlist_enforced_at_both_boundaries(generic, target_id, target_type):
    _, _, api, executor = generic
    body = {"environment_id": "demo", "target_id": target_id, "target_type": target_type, "symptom": "error"}
    assert api.post("/v1/cases", headers=OP, json=body).status_code == 403
    assert executor.post("/v1/tools/run", headers=EX, json=tool_request(target_id, target_type)).status_code == 403


@pytest.mark.parametrize("changes", [{"target_id": "../../etc/passwd"}, {"target_id": "http://server"}, {"target_type": "shell"}, {"application_id": "application_1_0001"}, {"url": "https://server"}])
def test_generic_contract_rejects_injection_and_ambiguous_legacy_input(generic, changes):
    _, _, api, _ = generic
    body = {"environment_id": "demo", "target_id": "demo-java", "target_type": "java_service", "symptom": "error", **changes}
    assert api.post("/v1/cases", headers=OP, json=body).status_code == 422


def test_executor_unavailable_does_not_offer_fake_targets_or_accept_generic_cases(generic):
    settings, sessions, _, _ = generic
    class Unavailable:
        def get(self, *args, **kwargs):
            raise httpx.ConnectError("sensitive backend details")
    api = TestClient(create_app(settings, sessions, Unavailable()))
    for response in (api.get("/v1/targets", headers=OP), api.post("/v1/cases", headers=OP, json={"environment_id": "demo", "target_id": "demo-java", "target_type": "java_service", "symptom": "error"})):
        assert response.status_code == 503
        assert "sensitive" not in response.text


def test_evidence_type_is_part_of_integrity_scope(generic):
    settings, sessions, api, executor = generic
    api.post("/v1/cases", headers=OP, json={"environment_id": "demo", "target_id": "demo-java", "target_type": "java_service", "symptom": "error"})
    run_once(sessions, settings, executor)
    with sessions() as session:
        evidence = session.scalar(select(Evidence))
        case = session.get(Case, evidence.case_id)
        evidence.target_type = "go_service"
        with pytest.raises(ValueError, match="scope"):
            verify_evidence(case, [evidence])


def test_yarn_resource_id_is_configured_only_in_registry(generic):
    settings, sessions, _, _ = generic
    registry = {"demo": {"targets": {"flink-orders": {"target_type": "yarn_application", "adapter": "yarn", "application_id": "application_1_0001", "base_url": "https://yarn.example"}}}}
    def respond(req):
        assert req.url.path.endswith("/application_1_0001")
        return httpx.Response(200, json={"app": {"id": "application_1_0001", "state": "RUNNING"}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as upstream:
        executor = TestClient(create_executor(registry, settings.executor_token, upstream))
        api = TestClient(create_app(settings, sessions, executor))
        created = api.post("/v1/cases", headers=OP, json={"environment_id": "demo", "target_id": "flink-orders", "target_type": "yarn_application", "symptom": "error"})
        assert created.status_code == 201
        run_once(sessions, settings, executor)
        detail = api.get("/v1/cases/" + created.json()["id"], headers=OP).json()
        assert detail["report"]["observations"][0]["text"].endswith("RUNNING")


def test_v01_database_upgrade_is_additive_and_repeatable(tmp_path):
    path = tmp_path / "legacy.db"
    case_id, evidence_id = str(uuid4()), str(uuid4())
    raw = json.dumps({"app": {"id": "application_1_0001", "state": "FAILED"}})
    report = json.dumps({"evidence_ids": [evidence_id], "review_status": "pending", "location_status": "未定位"})
    # Actual previous schema: no generic target columns.
    with sqlite3.connect(path) as connection:
        connection.executescript('''
          CREATE TABLE cases (id VARCHAR(36) PRIMARY KEY, environment_id VARCHAR(64) NOT NULL, application_id VARCHAR(80) NOT NULL, symptom TEXT NOT NULL, state VARCHAR(32) NOT NULL, created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL, claim_id VARCHAR(36), error TEXT, report JSON, revision INTEGER NOT NULL);
          CREATE TABLE evidence (id VARCHAR(36) PRIMARY KEY, case_id VARCHAR(36) NOT NULL REFERENCES cases(id), environment_id VARCHAR(64) NOT NULL, application_id VARCHAR(80) NOT NULL, request_id VARCHAR(36) NOT NULL UNIQUE, tool VARCHAR(80) NOT NULL, tool_version VARCHAR(20) NOT NULL, collected_at VARCHAR(40) NOT NULL, status VARCHAR(32) NOT NULL, source VARCHAR(20) NOT NULL, raw TEXT NOT NULL, sha256 VARCHAR(64) NOT NULL, truncated BOOLEAN NOT NULL);
          CREATE TABLE audit (id VARCHAR(36) PRIMARY KEY, case_id VARCHAR(36) NOT NULL REFERENCES cases(id), at VARCHAR(40) NOT NULL, actor VARCHAR(40) NOT NULL, action VARCHAR(80) NOT NULL, note TEXT);
        ''')
        connection.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?)", (case_id, "demo", "application_1_0001", "旧工单", "awaiting_review", "2026-10-29T00:00:00+00:00", "2026-10-29T00:00:00+00:00", None, None, report, 2))
        connection.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (evidence_id, case_id, "demo", "application_1_0001", str(uuid4()), "get_application_status", "1", "2026-10-29T00:00:00+00:00", "ok", "demo", raw, hashlib.sha256(raw.encode()).hexdigest(), False))
        connection.execute("INSERT INTO audit VALUES (?,?,?,?,?,?)", (str(uuid4()), case_id, "2026-10-29T00:00:00+00:00", "worker", "report_drafted", None))
    url = f"sqlite:///{path}"
    database(url)
    sessions = database(url)
    settings = Settings(url, "operator-test", "reviewer-test", "http://executor", "executor-test")
    api = TestClient(create_app(settings, sessions))
    detail = api.get(f"/v1/cases/{case_id}", headers=OP).json()
    assert detail["target_id"] == "application_1_0001"
    assert detail["target_type"] == "yarn_application"
    assert detail["evidence"][0]["target_id"] == "application_1_0001"
    assert detail["audit"][0]["action"] == "report_drafted"
    assert api.post(f"/v1/cases/{case_id}/review", headers=REVIEW, json={"revision": 2, "note": "升级后复核"}).status_code == 200
    raw_response = api.get(f"/v1/cases/{case_id}/evidence/{evidence_id}/raw", headers=REVIEW)
    assert raw_response.text == raw


def test_legacy_contract_and_registry_remain_supported():
    body = CaseInput(environment_id="demo", application_id="application_1_0001", symptom="旧客户端")
    assert body.target_id == "application_1_0001"
    assert body.target_type == "yarn_application"
    registry = normalize_registry({"demo": {"adapter": "demo", "applications": ["application_1_0001"]}})
    assert registry["demo"]["targets"][body.target_id]["target_type"] == "yarn_application"


def test_non_yarn_resource_cannot_use_yarn_adapter():
    with pytest.raises(ValueError):
        normalize_registry({"demo": {"targets": {"vm": {"target_type": "virtual_machine", "adapter": "yarn", "base_url": "https://yarn.example", "application_id": "application_1_0001"}}}})
