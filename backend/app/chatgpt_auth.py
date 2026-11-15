from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
import jwt

AUTHORIZATION_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
REVOCATION_URL = "https://auth.openai.com/api/accounts/oauth/revoke"
JWKS_URL = "https://auth.openai.com/.well-known/jwks.json"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"


class ChatGPTRefreshError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_store() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config"))
    return base / "AIJobApplicationWorkbench" / "chatgpt"


class ChatGPTAuth:
    """Backend-only OIDC Authorization Code + PKCE store."""

    def __init__(self, store: Path | None = None, http: httpx.Client | None = None) -> None:
        self.store = store or default_store()
        self._owns_http = http is None
        self.http = http if http is not None else httpx.Client(timeout=30)
        self.pending: dict[str, dict[str, str]] = {}
        self.lock = threading.Lock()

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def _read(self, name: str) -> dict:
        path = self.store / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def _write(self, name: str, value: dict) -> None:
        self.store.mkdir(parents=True, exist_ok=True)
        path = self.store / name
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(path)

    def _host_id(self) -> str:
        host = self._read("registration.json")
        if not host.get("ext_agent_host_id"):
            host["ext_agent_host_id"] = f"urn:uuid:{uuid.uuid4()}"
            self._write("registration.json", host)
        return host["ext_agent_host_id"]

    def start(self, redirect_uri: str) -> str:
        cutoff = _utcnow() - timedelta(minutes=10)
        self.pending = {key: value for key, value in self.pending.items()
                        if datetime.fromisoformat(value["created_at"]) > cutoff}
        registration = self._read("registration.json")
        client_id = registration.get("client_id", DYNAMIC_CLIENT_ID)
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = hashlib.sha256(verifier.encode()).digest()
        challenge = __import__("base64").urlsafe_b64encode(challenge).rstrip(b"=").decode()
        self.pending[state] = {"nonce": nonce, "verifier": verifier, "redirect_uri": redirect_uri,
                               "client_id": client_id, "created_at": _utcnow().isoformat()}
        params = {
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
            "scope": SCOPES, "resource": RESOURCE, "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "ext_agent_host_id": self._host_id(), "agent_name_hint": "job_tracker",
        }
        return f"{AUTHORIZATION_URL}?{urlencode(params)}"

    def callback(self, code: str, state: str, issued_client_id: str | None = None) -> None:
        transaction = self.pending.pop(state, None)
        if not transaction:
            raise ValueError("Invalid or expired OAuth state")
        if datetime.fromisoformat(transaction["created_at"]) < _utcnow() - timedelta(minutes=10):
            raise ValueError("OAuth state expired")
        client_id = issued_client_id or transaction["client_id"]
        if client_id == DYNAMIC_CLIENT_ID:
            raise ValueError("OpenAI did not return the registered client_id")
        response = self.http.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code, "client_id": client_id,
            "redirect_uri": transaction["redirect_uri"], "code_verifier": transaction["verifier"],
            "resource": RESOURCE,
        })
        response.raise_for_status()
        tokens = response.json()
        claims = jwt.decode(
            tokens["id_token"], jwt.PyJWKClient(JWKS_URL).get_signing_key_from_jwt(tokens["id_token"]).key,
            algorithms=["RS256"], audience=client_id, issuer=ISSUER,
        )
        if claims.get("nonce") != transaction["nonce"]:
            raise ValueError("Invalid OpenID Connect nonce")
        scopes = set(tokens.get("scope", "").split())
        if "chatgpt.tokens.use.direct" not in scopes:
            raise PermissionError("ChatGPT quota consent refused")
        expires_at = (_utcnow() + timedelta(seconds=int(tokens.get("expires_in", 0)))).isoformat()
        with self.lock:
            self._write("credentials.json", {**tokens, "client_id": client_id, "expires_at": expires_at,
                                             "email": claims.get("email"), "subject": claims.get("sub")})
            registration = self._read("registration.json")
            registration.update({"client_id": client_id, "email": claims.get("email"), "subject": claims.get("sub")})
            self._write("registration.json", registration)

    def status(self) -> dict:
        credentials = self._read("credentials.json")
        return {"connected": bool(credentials.get("refresh_token") or credentials.get("access_token")),
                "account": credentials.get("email")}

    def access_token(self, *, rejected_token: str | None = None) -> str:
        with self.lock:
            credentials = self._read("credentials.json")
            if not credentials:
                raise PermissionError("ChatGPT is not connected")
            expiry = datetime.fromisoformat(credentials["expires_at"])
            if (expiry > _utcnow() + timedelta(seconds=60)
                    and credentials.get("access_token")
                    and credentials["access_token"] != rejected_token):
                return credentials["access_token"]
            if not credentials.get("refresh_token"):
                raise PermissionError("ChatGPT session cannot be refreshed. Sign in again.")
            response = self.http.post(TOKEN_URL, data={
                "grant_type": "refresh_token", "refresh_token": credentials["refresh_token"],
                "client_id": credentials["client_id"], "resource": RESOURCE,
            })
            if response.status_code >= 400:
                try:
                    payload = response.json().get("error", "")
                    code = payload.get("code", "") if isinstance(payload, dict) else payload
                except (ValueError, AttributeError):
                    code = ""
                if code in {"invalid_grant", "invalid_refresh_token", "refresh_token_expired",
                            "refresh_token_reused", "refresh_token_invalidated"}:
                    raise PermissionError("ChatGPT session expired or revoked. Sign in again.")
                raise ChatGPTRefreshError(
                    f"ChatGPT token refresh refused (HTTP {response.status_code}).",
                    response.status_code,
                )
            refreshed = response.json()
            if not refreshed.get("access_token") or int(refreshed.get("expires_in", 0)) <= 0:
                raise ChatGPTRefreshError("Invalid ChatGPT token refresh response.", 502)
            credentials.update(refreshed)
            credentials["expires_at"] = (_utcnow() + timedelta(seconds=int(refreshed.get("expires_in", 0)))).isoformat()
            self._write("credentials.json", credentials)
            return credentials["access_token"]

    def logout(self) -> None:
        with self.lock:
            credentials = self._read("credentials.json")
            token = credentials.get("refresh_token")
            if token:
                try:
                    self.http.post(REVOCATION_URL, data={
                        "token": token,
                        "token_type_hint": "refresh_token",
                        "client_id": credentials.get("client_id"),
                    })
                except httpx.HTTPError:
                    pass
            path = self.store / "credentials.json"
            if path.exists():
                path.unlink()
