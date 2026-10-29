from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaseInput(StrictModel):
    environment_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    application_id: str = Field(pattern=r"^application_[0-9]{1,20}_[0-9]{1,20}$")
    symptom: str = Field(min_length=1, max_length=4000)


class ToolRequest(StrictModel):
    case_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    request_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    environment_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    application_id: str = Field(pattern=r"^application_[0-9]{1,20}_[0-9]{1,20}$")
    tool: Literal["get_application_status", "get_application_diagnostics"]


class ToolResult(StrictModel):
    case_id: str
    request_id: str
    environment_id: str
    application_id: str
    tool: Literal["get_application_status", "get_application_diagnostics"]
    tool_version: Literal["1"] = "1"
    collected_at: str
    source: Literal["demo", "yarn"]
    status: Literal["ok", "error"]
    raw: str = Field(max_length=65536)
    truncated: bool = False


class Review(StrictModel):
    revision: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=2000)
