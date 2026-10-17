"""Short-lived Hanggent identity delegation for Career Recommendations."""

import os

from fastapi import Header, HTTPException
from jose import JWTError, jwt


def career_user(authorization: str | None = Header(default=None)) -> str:
    secret = os.getenv("SECRET_KEY")
    if not secret or not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Career identity required")
    try:
        claims = jwt.decode(
            authorization.removeprefix("Bearer "),
            secret,
            algorithms=["HS256"],
            audience="career-compass",
            issuer="hanggent",
        )
        if claims.get("kind") != "career_bridge" or not str(claims.get("sub", "")).isdigit():
            raise ValueError("Invalid career identity")
        return str(claims["sub"])
    except (JWTError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid career identity") from exc
