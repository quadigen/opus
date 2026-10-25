from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any

from synapse.api.errors import SynapseError
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi

from .db import Database
from .errors import forbidden, invalid_request, unauthorized
from .util import json_loads

TOKEN_PREFIX = "knm_"
TOKEN_BYTES = 32

ACCOUNT_READ = "account:read"
ACCOUNT_WRITE = "account:write"
MARKETPLACE_READ = "marketplace:read"
MARKETPLACE_WRITE = "marketplace:write"
MARKETPLACE_ACQUIRE = "marketplace:acquire"
PACKAGES_READ = "packages:read"
PACKAGES_WRITE = "packages:write"
SPACES_READ = "spaces:read"
SPACES_WRITE = "spaces:write"

ALL_SCOPES = frozenset(
    {
        ACCOUNT_READ,
        ACCOUNT_WRITE,
        MARKETPLACE_READ,
        MARKETPLACE_WRITE,
        MARKETPLACE_ACQUIRE,
        PACKAGES_READ,
        PACKAGES_WRITE,
        SPACES_READ,
        SPACES_WRITE,
    }
)


@dataclass(frozen=True)
class Caller:
    user_id: str
    scopes: frozenset[str]
    token_id: str | None = None
    game_id: str | None = None

    @property
    def is_matrix(self) -> bool:
        return self.token_id is None

    def has_scope(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes

    def require_scope(self, scope: str) -> None:
        if not self.has_scope(scope):
            raise forbidden(f"This credential lacks the {scope} scope.")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def validate_scopes(scopes: Any) -> list[str]:
    if not isinstance(scopes, list) or not scopes:
        raise invalid_request("scopes must be a non-empty array of strings.")
    if len(scopes) > 64:
        raise invalid_request("Too many scopes were requested.")
    result: list[str] = []
    for scope in scopes:
        if not isinstance(scope, str):
            raise invalid_request("scopes must contain strings only.")
        if scope != "*" and scope not in ALL_SCOPES:
            raise invalid_request(f"Unknown scope: {scope}")
        if scope not in result:
            result.append(scope)
    return result


def _extract_token(request: SynapseRequest) -> str | None:
    headers = request.requestHeaders.getRawHeaders(b"Authorization")
    args: dict[bytes, list[bytes]] = request.args or {}
    query = args.get(b"access_token")
    if headers:
        if query is not None:
            raise unauthorized("Do not mix Authorization headers and access_token.")
        if len(headers) != 1:
            raise unauthorized("Too many Authorization headers.")
        parts = headers[0].split(b" ")
        if len(parts) != 2 or parts[0].lower() != b"bearer":
            raise unauthorized("Invalid Authorization header.")
        try:
            return parts[1].decode("ascii")
        except UnicodeDecodeError:
            raise unauthorized("Invalid access token.") from None
    values = list(query) if query else []
    if not values:
        return None
    if len(values) > 1:
        raise unauthorized("Too many access_token parameters.")
    try:
        token = values[0].decode("ascii")
    except UnicodeDecodeError:
        raise unauthorized("Invalid access token.") from None
    if token.startswith(TOKEN_PREFIX):
        raise unauthorized(
            "Kinemium API tokens must be sent in the Authorization header."
        )
    return token


def _lookup_token(
    txn: Any, token_hash: str
) -> tuple[str, str, str | None, Any, int | None] | None:
    txn.execute(
        "SELECT token_id, user_id, game_id, scopes, revoked_at"
        " FROM kinemium_api_credentials WHERE token_hash = %s",
        (token_hash,),
    )
    return txn.fetchone()


class AuthService:
    def __init__(self, api: ModuleApi, db: Database) -> None:
        self.api = api
        self.db = db
        self._last_touch: dict[str, float] = {}

    async def authenticate(
        self,
        request: SynapseRequest,
        scope: str | None = None,
        required: bool = True,
    ) -> Caller | None:
        token = _extract_token(request)
        if token is None:
            if required:
                raise unauthorized("Missing access token.")
            return None

        if token.startswith(TOKEN_PREFIX):
            caller = await self._authenticate_api_token(token)
        else:
            caller = await self._authenticate_matrix(request)

        if scope is not None:
            caller.require_scope(scope)
        return caller

    async def _authenticate_matrix(self, request: SynapseRequest) -> Caller:
        try:
            requester = await self.api.get_user_by_req(request)
        except SynapseError:
            raise unauthorized("Invalid or expired access token.") from None
        return Caller(
            user_id=requester.user.to_string(),
            scopes=frozenset({"*"}),
        )

    async def _authenticate_api_token(self, token: str) -> Caller:
        token_hash = hash_token(token)
        row = await self.db.run(
            "kinemium_get_api_token", _lookup_token, token_hash
        )
        if row is None or row[4] is not None:
            raise unauthorized("Invalid or revoked API token.")
        token_id, user_id, game_id, raw_scopes, _revoked_at = row
        try:
            scopes_value = json_loads(raw_scopes)
        except (ValueError, TypeError):
            raise unauthorized("Invalid API token.") from None
        if not isinstance(scopes_value, list):
            raise unauthorized("Invalid API token.")
        scopes = frozenset(
            scope for scope in scopes_value if isinstance(scope, str)
        )
        caller = Caller(
            user_id=user_id,
            scopes=scopes,
            token_id=token_id,
            game_id=game_id,
        )
        await self._touch_token(token_id)
        return caller

    async def _touch_token(self, token_id: str) -> None:
        now = time.monotonic()
        previous = self._last_touch.get(token_id, 0.0)
        if now - previous < 300:
            return
        self._last_touch[token_id] = now
        try:
            await self.db.run(
                "kinemium_touch_api_token",
                _touch_token,
                token_id,
                int(time.time() * 1000),
            )
        except Exception:
            pass


def _touch_token(txn: Any, token_id: str, now_ms: int) -> None:
    txn.execute(
        "UPDATE kinemium_api_credentials SET last_used_at = %s WHERE token_id = %s",
        (now_ms, token_id),
    )
