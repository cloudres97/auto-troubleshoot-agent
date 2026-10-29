import secrets

from fastapi import Header, HTTPException


def bearer(expected):
    def check(authorization: str = Header(default="")):
        if not secrets.compare_digest(authorization, "Bearer " + expected):
            raise HTTPException(401, "Invalid service credential")
    return check
