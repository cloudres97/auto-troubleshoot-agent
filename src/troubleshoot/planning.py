"""Replaceable planner with a conservative local rule implementation.

Model planners may implement Planner, but must pass validate_plan before any tool
is invoked. User/log text never grants tools, targets or execution permissions.
"""
import json
import re
from importlib.resources import files
from typing import Literal, Protocol

from pydantic import Field, TypeAdapter, model_validator

from .contracts import StrictModel, TargetInfo, TargetType


class Skill(StrictModel):
    id: str
    version: str
    title: str
    target_types: list[TargetType]
    steps: list[str]
    tool: Literal["get_target_status"]


SKILLS = TypeAdapter(list[Skill]).validate_python(json.loads(files("troubleshoot").joinpath("skills.json").read_text(encoding="utf-8")))


class Plan(StrictModel):
    status: Literal["ready", "needs_information"]
    mode: str = "local-rules"
    environment_id: str | None = None
    target_id: str | None = None
    target_type: TargetType | None = None
    skill_id: str | None = None
    question: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def complete(self):
        if self.status == "ready" and not all((self.environment_id, self.target_id, self.target_type, self.skill_id)):
            raise ValueError("Ready plans need a complete scope and registered skill")
        if self.status == "needs_information" and not self.question:
            raise ValueError("Missing-information plans need a question")
        return self


class Planner(Protocol):
    def plan(self, problem: str, environment_info: str, environment_id: str, targets: list[TargetInfo]) -> Plan: ...


def contains(text, term):
    text, term = text.casefold(), term.casefold()
    if re.fullmatch(r"[a-z0-9_.-]+", term):
        return re.search(r"(?<![a-z0-9_.-])" + re.escape(term) + r"(?![a-z0-9_.-])", text) is not None
    return term in text


def skill_for(kind):
    skill = next((skill for skill in SKILLS if kind in skill.target_types), None)
    if skill is None:
        raise ValueError("No registered skill for target type")
    return skill


def registered_skill(skill_id):
    skill = next((skill for skill in SKILLS if skill.id == skill_id), None)
    if skill is None:
        raise ValueError("No registered skill with this ID")
    return skill


def validate_plan(plan, targets):
    # Revalidate even an object returned by a future model/custom planner.
    plan = Plan.model_validate(plan.model_dump() if isinstance(plan, Plan) else plan)
    if plan.status == "ready":
        if not any((t.environment_id, t.target_id, t.target_type) == (plan.environment_id, plan.target_id, plan.target_type) for t in targets):
            raise ValueError("Planner selected an unregistered scope")
        skill = next((s for s in SKILLS if s.id == plan.skill_id), None)
        if skill is None or plan.target_type not in skill.target_types:
            raise ValueError("Planner selected an incompatible/unregistered skill")
    return plan


class RulePlanner:
    keywords = {
        "yarn_application": ("yarn", "flink", "spark"),
        "java_service": ("java", "jvm", "spring", "tomcat"),
        "go_service": ("golang", "go 服务", "go服务", "goroutine", "pprof"),
        "docker_container": ("docker", "容器", "container"),
        "virtual_machine": ("虚机", "虚拟机", "主机", "服务器", "vm"),
        "network": ("网络", "dns", "tcp", "tls"),
        "storage": ("存储", "磁盘", "inode", "挂载", "空间不足"),
    }

    def plan(self, problem, environment_info, environment_id, targets):
        environments = {t.environment_id for t in targets}
        if environment_id:
            matching_environments = {environment_id} & environments
        else:
            matching_environments = {env for env in environments if contains(environment_info, env)}
            if not matching_environments:
                matching_environments = {t.environment_id for t in targets if any(contains(environment_info, name) for name in (t.target_id, t.label, *t.aliases))}
        if len(matching_environments) != 1:
            return Plan(status="needs_information", question="请补充环境名称或可识别的主机/服务信息，以便核对已登记的访问范围；无需选择技术场景。")
        env = next(iter(matching_environments))
        candidates = [t for t in targets if t.environment_id == env]
        context = problem + "\n" + environment_info
        named = [t for t in candidates if any(contains(context, name) for name in (t.target_id, t.label, *t.aliases))]
        if len(named) == 1:
            target = named[0]
        elif len(named) > 1:
            return Plan(status="needs_information", environment_id=env, question="描述涉及多个登记对象。请补充本次故障主要影响的服务名或主机名，以及发生时间。")
        else:
            kinds = {kind for kind, words in self.keywords.items() if any(contains(context, word) for word in words)}
            narrowed = [t for t in candidates if t.target_type in kinds] if kinds else candidates
            if len(narrowed) != 1:
                return Plan(status="needs_information", environment_id=env, question="请补充发生故障的服务名、主机名或具体报错。目前的信息不足以唯一核对受影响对象，无需判断它属于哪种技术场景。")
            target = narrowed[0]
        return Plan(status="ready", environment_id=env, target_id=target.target_id, target_type=target.target_type, skill_id=skill_for(target.target_type).id)
