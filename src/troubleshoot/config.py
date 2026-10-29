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
    def load(cls, environ=None):
        environ = os.environ if environ is None else environ
        result = cls(
            environ.get("DIAG_DATABASE_URL", "sqlite:///./data/diagnosis.db"),
            environ.get("DIAG_API_TOKEN", ""),
            environ.get("DIAG_REVIEWER_TOKEN", ""),
            environ.get("DIAG_EXECUTOR_URL", "http://127.0.0.1:18081"),
            environ.get("DIAG_EXECUTOR_TOKEN", ""),
        )
        if not all((result.api_token, result.reviewer_token, result.executor_token)):
            raise RuntimeError("Set DIAG_API_TOKEN, DIAG_REVIEWER_TOKEN and DIAG_EXECUTOR_TOKEN")
        if len({result.api_token, result.reviewer_token, result.executor_token}) != 3:
            raise RuntimeError("Service and reviewer credentials must be distinct")
        if any(token != token.strip() or not token.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in token) for token in (result.api_token, result.reviewer_token, result.executor_token)):
            raise RuntimeError("Credentials must use ASCII without surrounding whitespace or control characters")
        return result
