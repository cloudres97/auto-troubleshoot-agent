from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import select, text, update
from pydantic import TypeAdapter

from .auth import bearer
from .config import Settings
from .contracts import CaseInput, Followup, Review, TargetInfo
from .db import Audit, Case, Evidence, database, now
from .diagnosis import verify_evidence


def create_app(settings=None, sessions=None, executor_client=None):
    settings = settings or Settings.load()
    sessions = sessions or database(settings.database_url)
    app = FastAPI(title="自动诊断工作台", version="0.3.0")
    operator = [Depends(bearer(settings.api_token))]
    reviewer = [Depends(bearer(settings.reviewer_token))]

    def get_case(session, case_id):
        case = session.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "Case not found")
        return case

    def serialize(case):
        return {key: getattr(case, key) for key in ("id", "environment_id", "environment_info", "target_id", "target_type", "application_id", "symptom", "state", "created_at", "updated_at", "error", "plan", "conversation", "report", "revision")}

    def catalog():
        def fetch(client):
            response = client.get(settings.executor_url.rstrip("/") + "/v1/targets", headers={"Authorization": "Bearer " + settings.executor_token}, timeout=5)
            response.raise_for_status()
            return TypeAdapter(list[TargetInfo]).validate_python(response.json())
        try:
            if executor_client is not None:
                return fetch(executor_client)
            with httpx.Client(verify=True, follow_redirects=False) as client:
                return fetch(client)
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(503, "无法加载诊断对象，请检查执行器是否已启动及服务令牌是否一致。") from exc

    @app.get("/v1/targets", response_model=list[TargetInfo], dependencies=operator)
    def targets():
        return catalog()

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/health")
    def health():
        with sessions() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.post("/v1/cases", status_code=201, dependencies=operator)
    def create_case(body: CaseInput):
        # Legacy clients remain accepted; the executor still enforces their scope.
        if body.target_id and not body.application_id and not any((t.environment_id, t.target_id, t.target_type) == (body.environment_id, body.target_id, body.target_type) for t in catalog()):
            raise HTTPException(403, "请选择当前环境已登记的诊断对象。")
        with sessions.begin() as session:
            values = body.model_dump()
            values.update(target_id=body.target_id or "", target_type=body.target_type or "")
            case = Case(**values, conversation=[{"role": "user", "content": body.symptom, "at": now()}])
            session.add(case)
            session.flush()
            session.add(Audit(case_id=case.id, actor="operator", action="case_created"))
            return serialize(case)

    @app.get("/v1/cases", dependencies=operator)
    def list_cases():
        with sessions() as session:
            return [serialize(case) for case in session.scalars(select(Case).order_by(Case.created_at.desc()).limit(100))]

    @app.post("/v1/cases/{case_id}/messages", dependencies=operator)
    def supplement(case_id: str, body: Followup):
        with sessions.begin() as session:
            case = get_case(session, case_id)
            if case.state != "waiting_information" or case.revision != body.revision:
                raise HTTPException(409, "Case is not awaiting information or its revision changed")
            conversation = [*(case.conversation or []), {"role": "user", "content": body.message, "at": now()}]
            if len(conversation) > 40:
                raise HTTPException(409, "Conversation budget exhausted; create a new case with the consolidated information")
            changed = session.execute(update(Case).where(Case.id == case_id, Case.state == "waiting_information", Case.revision == body.revision).values(state="queued", plan=None, conversation=conversation, claim_id=None, revision=Case.revision + 1, updated_at=now()))
            if changed.rowcount != 1:
                raise HTTPException(409, "Case changed while adding information")
            session.add(Audit(case_id=case_id, actor="operator", action="information_provided"))
            return {"state": "queued"}

    @app.get("/v1/cases/{case_id}", dependencies=operator)
    def case_detail(case_id: str):
        with sessions() as session:
            case = get_case(session, case_id)
            result = serialize(case)
            result["evidence"] = [{key: getattr(e, key) for key in ("id", "case_id", "environment_id", "target_id", "target_type", "application_id", "request_id", "tool", "tool_version", "collected_at", "source", "status", "sha256", "truncated")} for e in session.scalars(select(Evidence).where(Evidence.case_id == case_id))]
            result["audit"] = [{"at": a.at, "actor": a.actor, "action": a.action, "note": a.note} for a in session.scalars(select(Audit).where(Audit.case_id == case_id).order_by(Audit.at))]
            return result

    @app.get("/v1/cases/{case_id}/evidence/{evidence_id}/raw", response_class=PlainTextResponse, dependencies=reviewer)
    def raw_evidence(case_id: str, evidence_id: str):
        with sessions() as session:
            get_case(session, case_id)
            evidence = session.get(Evidence, evidence_id)
            if evidence is None or evidence.case_id != case_id:
                raise HTTPException(404, "Evidence not found")
            return PlainTextResponse(evidence.raw, headers={"Content-Disposition": "attachment; filename=evidence.txt", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.post("/v1/cases/{case_id}/review", dependencies=reviewer)
    def review(case_id: str, body: Review):
        with sessions.begin() as session:
            case = get_case(session, case_id)
            if case.state != "awaiting_review" or case.revision != body.revision:
                raise HTTPException(409, "Report state or revision changed")
            evidence = list(session.scalars(select(Evidence).where(Evidence.case_id == case_id)))
            try:
                verify_evidence(case, evidence)
                if set(case.report["evidence_ids"]) != {e.id for e in evidence}:
                    raise ValueError("Report references do not match evidence")
            except (ValueError, KeyError, TypeError) as exc:
                raise HTTPException(409, "Evidence integrity check failed") from exc
            report = {**case.report, "review_status": "approved", "review_note": body.note, "reviewed_at": now()}
            changed = session.execute(update(Case).where(Case.id == case_id, Case.state == "awaiting_review", Case.revision == body.revision).values(state="completed", report=report, revision=body.revision + 1, updated_at=now()))
            if changed.rowcount != 1:
                raise HTTPException(409, "Report changed during review")
            session.add(Audit(case_id=case_id, actor="reviewer", action="report_approved", note=body.note))
            return {"state": "completed"}

    @app.post("/v1/cases/{case_id}/cancel", dependencies=operator)
    def cancel(case_id: str):
        with sessions.begin() as session:
            get_case(session, case_id)
            changed = session.execute(update(Case).where(Case.id == case_id, Case.state.in_(["queued", "collecting", "collection_unknown", "planning_failed", "waiting_information"])).values(state="cancelled", claim_id=None, updated_at=now(), revision=Case.revision + 1))
            if changed.rowcount != 1:
                raise HTTPException(409, "Case cannot be cancelled in its current state")
            session.add(Audit(case_id=case_id, actor="operator", action="case_cancelled"))
            return {"state": "cancelled"}

    @app.post("/v1/cases/{case_id}/recover", dependencies=reviewer)
    def recover(case_id: str, body: Review):
        # Explicit operator recovery only: never automatically replay interrupted I/O.
        with sessions.begin() as session:
            get_case(session, case_id)
            changed = session.execute(update(Case).where(Case.id == case_id, Case.state.in_(["collecting", "collection_unknown", "planning_failed"]), Case.revision == body.revision).values(state="queued", claim_id=None, error=None, revision=body.revision + 1, updated_at=now()))
            if changed.rowcount != 1:
                raise HTTPException(409, "Case state or revision changed")
            session.add(Audit(case_id=case_id, actor="reviewer", action="manual_read_only_retry", note=body.note))
            return {"state": "queued", "note": body.note}

    return app
