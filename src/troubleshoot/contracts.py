from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TargetType = Literal["yarn_application", "docker_container", "virtual_machine", "java_service", "go_service", "network", "storage"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TargetScope(StrictModel):
    require_target: ClassVar[bool] = True
    environment_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    target_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    target_type: TargetType | None = None
    # Deprecated compatibility field; not required for any new target.
    application_id: str = Field(default="", pattern=r"^(application_[0-9]{1,20}_[0-9]{1,20})?$")

    @model_validator(mode="after")
    def scope(self):
        if self.application_id:
            if self.target_type not in (None, "yarn_application") or self.target_id not in (None, self.application_id):
                raise ValueError("Legacy application_id cannot be combined with another target")
            self.target_id = self.application_id
            self.target_type = "yarn_application"
        if not self.target_id and not self.target_type and not self.require_target:
            return self
        if not self.target_id or not self.target_type:
            raise ValueError("Choose target_id and target_type")
        return self


class CaseInput(TargetScope):
    require_target: ClassVar[bool] = False
    environment_id: str = Field(default="", pattern=r"^[a-zA-Z0-9_-]{0,64}$")
    environment_info: str = Field(default="", max_length=4000)
    symptom: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def intake(self):
        if not self.symptom.strip() or not (self.environment_id or self.environment_info.strip()):
            raise ValueError("Provide the problem and known environment information")
        if self.target_id and not self.environment_id:
            raise ValueError("Explicit target requests require environment_id")
        return self


class TargetInfo(StrictModel):
    environment_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    target_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    target_type: TargetType
    label: str = Field(max_length=100)
    source: Literal["demo", "yarn"]
    aliases: list[str] = Field(default_factory=list, max_length=20)


class ToolRequest(TargetScope):
    case_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    request_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    tool: Literal["get_target_status", "get_application_status", "get_application_diagnostics"]

    @model_validator(mode="after")
    def tool_scope(self):
        if self.tool != "get_target_status" and self.target_type != "yarn_application":
            raise ValueError("YARN tools require a YARN target")
        return self


class ToolResult(TargetScope):
    case_id: str
    request_id: str
    tool: Literal["get_target_status", "get_application_status", "get_application_diagnostics"]
    tool_version: Literal["1"] = "1"
    collected_at: str
    source: Literal["demo", "yarn"]
    status: Literal["ok", "error"]
    raw: str = Field(max_length=65536)
    truncated: bool = False


class Review(StrictModel):
    revision: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=2000)


class Followup(StrictModel):
    revision: int = Field(ge=1)
    message: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def meaningful(self):
        if not self.message.strip():
            raise ValueError("Message must not be blank")
        return self
