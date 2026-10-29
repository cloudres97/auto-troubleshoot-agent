import argparse
import hashlib
import time
from uuid import uuid4

import httpx
from sqlalchemy import select, update

from .config import Settings
from .contracts import ToolRequest, ToolResult
from .db import Audit, Case, Evidence, database, now
from .diagnosis import make_report


def run_once(sessions, settings, client=None):
    claim = str(uuid4())
    # Compare-and-swap claim; works for SQLite and PostgreSQL without holding a
    # database transaction open across an HTTP request.
    with sessions.begin() as session:
        case_id = session.scalar(select(Case.id).where(Case.state == "queued").order_by(Case.created_at).limit(1))
        if case_id is None:
            return False
        changed = session.execute(update(Case).where(Case.id == case_id, Case.state == "queued").values(state="collecting", claim_id=claim, updated_at=now()))
        if changed.rowcount != 1:
            return False
        session.add(Audit(case_id=case_id, actor="worker", action="collection_started"))
    with sessions() as session:
        case = session.get(Case, case_id)
        request = ToolRequest(case_id=case.id, request_id=str(uuid4()), environment_id=case.environment_id, target_id=case.target_id, target_type=case.target_type, application_id=case.application_id, tool="get_target_status")
    try:
        def invoke(http):
            response = http.post(settings.executor_url.rstrip("/") + "/v1/tools/run", json=request.model_dump(), headers={"Authorization": "Bearer " + settings.executor_token}, timeout=25)
            response.raise_for_status()
            if len(response.content) > 400000:
                raise ValueError("Executor response exceeds budget")
            return ToolResult.model_validate(response.json())
        if client is not None:
            result = invoke(client)
        else:
            with httpx.Client(verify=True, follow_redirects=False) as http:
                result = invoke(http)
        for key, value in request.model_dump().items():
            if getattr(result, key) != value:
                raise ValueError("Executor result scope mismatch")
        with sessions.begin() as session:
            current = session.get(Case, case_id)
            if current.state != "collecting" or current.claim_id != claim:
                return True  # cancelled/recovered claim cannot publish late output
            evidence = Evidence(case_id=case_id, **result.model_dump(exclude={"case_id"}), sha256=hashlib.sha256(result.raw.encode()).hexdigest())
            session.add(evidence)
            session.flush()
            report = make_report(current, [evidence])
            changed = session.execute(update(Case).where(Case.id == case_id, Case.state == "collecting", Case.claim_id == claim).values(report=report, state="awaiting_review", revision=Case.revision + 1, updated_at=now()))
            if changed.rowcount != 1:
                session.rollback()
                return True
            session.add(Audit(case_id=case_id, actor="worker", action="report_drafted"))
    except (httpx.HTTPError, ValueError):
        with sessions.begin() as session:
            current = session.get(Case, case_id)
            if current.state == "collecting" and current.claim_id == claim:
                current.state = "collection_unknown"
                current.error = "Collection result unknown or invalid; operator must inspect before retrying."
                current.updated_at = now()
                session.add(Audit(case_id=case_id, actor="worker", action="collection_unknown"))
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings.load()
    sessions = database(settings.database_url)
    while True:
        worked = run_once(sessions, settings)
        if args.once:
            return
        if not worked:
            time.sleep(2)


if __name__ == "__main__":
    main()
