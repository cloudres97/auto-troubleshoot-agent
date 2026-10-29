import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_token: str
    reviewer_token: str
    executor_url: str
    executor_token: str

    @classmethod
    def load(cls):
        result = cls(
            os.getenv("DIAG_DATABASE_URL", "sqlite:///./data/diagnosis.db"),
            os.getenv("DIAG_API_TOKEN", ""),
            os.getenv("DIAG_REVIEWER_TOKEN", ""),
            os.getenv("DIAG_EXECUTOR_URL", "http://127.0.0.1:18081"),
            os.getenv("DIAG_EXECUTOR_TOKEN", ""),
        )
        if not all((result.api_token, result.reviewer_token, result.executor_token)):
            raise RuntimeError("Set DIAG_API_TOKEN, DIAG_REVIEWER_TOKEN and DIAG_EXECUTOR_TOKEN")
        if len({result.api_token, result.reviewer_token, result.executor_token}) != 3:
            raise RuntimeError("Service and reviewer credentials must be distinct")
        return result
