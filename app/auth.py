"""Auth.

Clients: your existing dashboard backend mints a short-lived HS256 JWT signed with JWT_SECRET:
    {"sub": "<your client/tenant id>", "name": "<display name>", "exp": <unix ts>}
and the UI sends it as `Authorization: Bearer <token>`. This service never stores passwords.

Admins / internal services (your AI dialer): header `X-Admin-Key: <ADMIN_API_KEY>`.
"""
import hashlib
import hmac
import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

_bearer = HTTPBearer(auto_error=False)


@dataclass
class CurrentClient:
    client_id: str
    name: str


def mint_client_token(client_id: str, name: str = "", ttl_seconds: int = 3600, settings: Settings | None = None) -> str:
    s = settings or get_settings()
    now = int(time.time())
    return jwt.encode({"sub": client_id, "name": name or client_id, "iat": now, "exp": now + ttl_seconds},
                      s.jwt_secret, algorithm="HS256")


def get_current_client(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> CurrentClient:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    try:
        claims = jwt.decode(creds.credentials, settings.jwt_secret, algorithms=["HS256"],
                            options={"require": ["sub", "exp"]})
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {exc}") from exc
    client_id = str(claims["sub"]).strip()
    if not client_id or len(client_id) > 64:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid client id in token")
    return CurrentClient(client_id=client_id, name=str(claims.get("name") or client_id))


def require_admin(x_admin_key: str | None = Header(default=None), settings: Settings = Depends(get_settings)) -> None:
    if not x_admin_key or not hmac.compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid admin key")


def webhook_token(application_id: int, settings: Settings | None = None) -> str:
    """Unguessable token embedded in the compliance callback URL so random hits can't trigger syncs."""
    s = settings or get_settings()
    return hmac.new(s.jwt_secret.encode(), f"compliance:{application_id}".encode(), hashlib.sha256).hexdigest()[:32]
