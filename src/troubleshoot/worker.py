import argparse
import hashlib
import time
from uuid import uuid4

import httpx
from sqlalchemy import select, update
from pydantic import TypeAdapter

from .config import Settings
from .contracts import TargetInfo, ToolRequest, ToolResult
from .db import Audit, Case, Evidence, database, now
from .diagnosis import make_report
from .planning import Plan, RulePlanner, registered_skill, skill_for, validate_plan


def run_once(sessions, settings, client=None, planner=None):
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
    tool_started = False
    try:
        def invoke(http):
            nonlocal tool_started
            if case.target_id:
                plan = Plan(status="ready", mode="explicit-target", environment_id=case.environment_id, target_id=case.target_id, target_type=case.target_type, skill_id=skill_for(case.target_type).id)
            else:
                response = http.get(settings.executor_url.rstrip("/") + "/v1/targets", headers={"Authorization": "Bearer " + settings.executor_token}, timeout=5)
                response.raise_for_status()
                targets = TypeAdapter(list[TargetInfo]).validate_python(response.json())
                problem = "\n".join([case.symptom, *[m["content"] for m in (case.conversation or []) if m["role"] == "user"]])
                context = case.environment_info + "\n" + problem
                plan = validate_plan((planner or RulePlanner()).plan(problem, context, case.environment_id, targets), targets)
                if case.environment_id and plan.status == "ready" and plan.environment_id != case.environment_id:
                    raise ValueError("Planner changed the specified environment")
            skill = registered_skill(plan.skill_id) if plan.status == "ready" else None
            saved_plan = plan.model_dump()
            if skill:
                saved_plan.update(skill_title=skill.title, skill_version=skill.version, steps=skill.steps, tool=skill.tool)
            with sessions.begin() as session:
                current = session.get(Case, case_id)
                values = {"plan": saved_plan, "updated_at": now()}
                if plan.status == "needs_information":
                    values.update(state="waiting_information", claim_id=None, revision=Case.revision + 1, conversation=[*(current.conversation or []), {"role": "assistant", "content": plan.question, "at": now()}])
                else:
                    values.update(environment_id=plan.environment_id, target_id=plan.target_id, target_type=plan.target_type)
                changed = session.execute(update(Case).where(Case.id == case_id, Case.state == "collecting", Case.claim_id == claim).values(**values))
                if changed.rowcount != 1:
                    return None, None
                session.add(Audit(case_id=case_id, actor="worker", action="information_requested" if not skill else "plan_selected"))
            if skill is None:
                return None, None
            request = ToolRequest(case_id=case.id, request_id=str(uuid4()), environment_id=plan.environment_id, target_id=plan.target_id, target_type=plan.target_type, application_id=case.application_id, tool=skill.tool)
            # Check the claim again before dispatch; cancellation cannot retract
            # an HTTP request that is already on the wire.
            with sessions() as session:
                current = session.get(Case, case_id)
                if current.state != "collecting" or current.claim_id != claim:
                    return None, None
            tool_started = True
            response = http.post(settings.executor_url.rstrip("/") + "/v1/tools/run", json=request.model_dump(), headers={"Authorization": "Bearer " + settings.executor_token}, timeout=25)
            response.raise_for_status()
            if len(response.content) > 400000:
                raise ValueError("Executor response exceeds budget")
            return request, ToolResult.model_validate(response.json())
        if client is not None:
            request, result = invoke(client)
        else:
            with httpx.Client(verify=True, follow_redirects=False) as http:
                request, result = invoke(http)
        if result is None:
            return True
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
                state = "collection_unknown" if tool_started else "planning_failed"
                error = "Collection result unknown or invalid; operator must inspect before retrying." if tool_started else "Planning failed or produced an invalid scope; no environment tool was dispatched."
                session.execute(update(Case).where(Case.id == case_id, Case.state == "collecting", Case.claim_id == claim).values(state=state, error=error, updated_at=now()))
                session.add(Audit(case_id=case_id, actor="worker", action=state))
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
