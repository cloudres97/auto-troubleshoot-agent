import hashlib
import json


def verify_evidence(case, evidence):
    if not evidence:
        raise ValueError("No evidence")
    for item in evidence:
        if (item.case_id, item.environment_id, item.target_id, item.target_type, item.application_id) != (case.id, case.environment_id, case.target_id, case.target_type, case.application_id):
            raise ValueError("Evidence scope mismatch")
        if hashlib.sha256(item.raw.encode()).hexdigest() != item.sha256:
            raise ValueError("Evidence digest mismatch")


def make_report(case, evidence):
    verify_evidence(case, evidence)
    observations = []
    unresolved = ["当前流程为确定性只读检查，尚未接入公司模型；不自动作出因果判断。"]
    for item in evidence:
        if item.source == "demo":
            unresolved.append("演示数据不能证明真实环境状态。")
        if item.status != "ok" or item.truncated:
            unresolved.append(f"证据 {item.id} 采集失败或被截断，不能据此判断正常。")
            continue
        try:
            payload = json.loads(item.raw)
            if case.target_type == "yarn_application":
                data = payload["app"]
                if case.application_id and data["id"] != case.application_id:
                    raise ValueError("Target mismatch")
                state = data["state"]
                if state not in {"NEW", "NEW_SAVING", "SUBMITTED", "ACCEPTED", "RUNNING", "FINISHED", "FAILED", "KILLED"}:
                    raise ValueError("Unknown state")
                description = f"YARN 应用状态：{state}"
            else:
                if payload["target_id"] != case.target_id or payload["target_type"] != case.target_type or payload["state"] != "DEMO_ONLY" or item.source != "demo":
                    raise ValueError("Unknown target response")
                description = "仅验证诊断流程；尚未采集此对象的真实状态。"
            observations.append({"text": description, "evidence_id": item.id, "source": item.source})
        except (ValueError, KeyError, TypeError):
            unresolved.append(f"证据 {item.id} 不符合该诊断对象的响应格式。")
    return {
        "location_status": "未定位", "root_cause": None,
        "observations": observations, "unresolved": list(dict.fromkeys(unresolved)),
        "evidence_ids": [item.id for item in evidence],
        "review_status": "pending", "mode": "deterministic-read-only",
    }
