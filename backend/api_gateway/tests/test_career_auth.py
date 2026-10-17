"""Only short-lived Hanggent career delegation may access the workspace."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from jose import jwt

from app.routes.career_auth import career_user


def test_career_bridge_rejects_missing_and_wrong_purpose(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "career-test-secret")
    with pytest.raises(HTTPException) as missing:
        career_user(None)
    assert missing.value.status_code == 401

    claims = {
        "sub": "42", "iss": "hanggent", "aud": "career-compass",
        "kind": "ordinary", "exp": datetime.now(timezone.utc) + timedelta(seconds=60),
    }
    token = jwt.encode(claims, "career-test-secret", algorithm="HS256")
    with pytest.raises(HTTPException) as wrong_purpose:
        career_user(f"Bearer {token}")
    assert wrong_purpose.value.status_code == 401

    claims["kind"] = "career_bridge"
    token = jwt.encode(claims, "career-test-secret", algorithm="HS256")
    assert career_user(f"Bearer {token}") == "42"
