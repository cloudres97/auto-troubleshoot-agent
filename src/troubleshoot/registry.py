"""Administrator-owned resource registry, independent of the public case schema."""
import re
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from typing import Literal

from .contracts import StrictModel, TargetType


class RegisteredTarget(StrictModel):
    target_type: TargetType
    adapter: Literal["demo", "yarn"]
    label: str = Field(default="", max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    base_url: str | None = None
    application_id: str | None = Field(default=None, pattern=r"^application_[0-9]{1,20}_[0-9]{1,20}$")

    @model_validator(mode="after")
    def adapter_scope(self):
        if any(not name.strip() or len(name) > 100 for name in self.aliases):
            raise ValueError("Target aliases must contain 1-100 characters")
        if self.adapter == "yarn":
            if self.target_type != "yarn_application" or not self.application_id:
                raise ValueError("YARN adapter requires a YARN target and application_id")
            u = urlsplit(self.base_url or "")
            if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
                raise ValueError("YARN base_url must be a fixed origin without credentials or path")
        if self.target_type == "yarn_application" and not self.application_id:
            raise ValueError("YARN targets require application_id in the registry")
        if self.target_type != "yarn_application" and self.application_id:
            raise ValueError("Only YARN targets may have application_id")
        return self


def normalize_registry(registry):
    if not isinstance(registry, dict):
        raise ValueError("Environment registry must be an object")
    normalized = {}
    for environment_id, entry in registry.items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", environment_id) or not isinstance(entry, dict):
            raise ValueError("Invalid environment registration")
        if "targets" in entry:
            if set(entry) != {"targets"}:
                raise ValueError("Do not mix legacy registration and targets")
            targets = entry["targets"]
        else:
            # Retain existing environment files and queued YARN cases.
            if set(entry) - {"adapter", "applications", "base_url"} or entry.get("adapter") not in ("demo", "yarn"):
                raise ValueError("Unsupported legacy environment")
            applications = entry.get("applications", [])
            if entry["adapter"] == "yarn" and not applications:
                raise ValueError("Legacy YARN registration requires an applications allowlist")
            if not isinstance(applications, list) or any(not isinstance(a, str) for a in applications) or len(set(applications)) != len(applications):
                raise ValueError("Invalid applications allowlist")
            targets = {a: {"target_type": "yarn_application", "adapter": entry["adapter"], "base_url": entry.get("base_url"), "application_id": a} for a in applications}
        if not isinstance(targets, dict):
            raise ValueError("targets must be an object")
        normalized[environment_id] = {"targets": {}}
        for target_id, target in targets.items():
            if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", target_id):
                raise ValueError("Invalid target ID")
            normalized[environment_id]["targets"][target_id] = RegisteredTarget.model_validate(target).model_dump()
    return normalized
