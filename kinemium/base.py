from __future__ import annotations

import json
from functools import wraps
from typing import Any, Callable

from synapse.api.errors import SynapseError
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi

from .auth import AuthService, Caller
from .config import KinemiumConfig
from .db import Database
from .errors import forbidden, invalid_request
from .http import query_int
from .ratelimit import RateLimiter
from .storage import LocalStorage
from .util import now_ms


def _idempotency_lookup(
    txn: Any, user_id: str, key: str, method: str, path: str
) -> tuple[int, str] | None:
    txn.execute(
        "SELECT status_code, response FROM kinemium_idempotency"
        " WHERE user_id = %s AND idempotency_key = %s AND method = %s"
        " AND path = %s",
        (user_id, key, method, path),
    )
    row = txn.fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1])


def _idempotency_store(
    txn: Any,
    user_id: str,
    key: str,
    method: str,
    path: str,
    status: int,
    response: str,
    now: int,
) -> None:
    txn.execute(
        "INSERT INTO kinemium_idempotency"
        " (user_id, idempotency_key, method, path, status_code, response,"
        " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (user_id, idempotency_key, method, path) DO NOTHING",
        (user_id, key, method, path, status, response, now),
    )
    txn.execute(
        "DELETE FROM kinemium_idempotency WHERE created_at < %s",
        (now - 7 * 86400000,),
    )


def idempotent(func: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(func)
    async def wrapper(self: Any, request: SynapseRequest, *args: Any, **kwargs: Any) -> Any:
        raw_key = request.getHeader(b"Idempotency-Key")
        if raw_key is None:
            return await func(self, request, *args, **kwargs)
        try:
            key = raw_key.decode("ascii").strip()
        except UnicodeDecodeError:
            raise invalid_request("Idempotency-Key must be ASCII.") from None
        if not key or len(key) > 255:
            raise invalid_request("Idempotency-Key must be 1-255 characters.")
        caller = await self.authenticate(request, None, required=True)
        assert caller is not None
        method = request.method.decode("ascii")
        path = request.path.decode("ascii")
        cached = await self.db.run(
            "kinemium_idempotency_lookup",
            _idempotency_lookup,
            caller.user_id,
            key,
            method,
            path,
        )
        if cached is not None:
            status, response = cached
            request.setResponseCode(status)
            request.setHeader(b"Idempotency-Replayed", b"true")
            return status, json.loads(response)
        result = await func(self, request, *args, **kwargs)
        if isinstance(result, tuple) and len(result) == 2:
            status, body = result
            await self.db.run(
                "kinemium_idempotency_store",
                _idempotency_store,
                caller.user_id,
                key,
                method,
                path,
                status,
                json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                now_ms(),
            )
            request.setHeader(b"Idempotency-Replayed", b"false")
        return result

    return wrapper


class Service:
    def __init__(
        self,
        api: ModuleApi,
        config: KinemiumConfig,
        db: Database,
        storage: LocalStorage,
        rate_limiter: RateLimiter,
        auth: AuthService,
    ) -> None:
        self.api = api
        self.config = config
        self.db = db
        self.storage = storage
        self.rate_limiter = rate_limiter
        self.auth = auth
        self.platform: Any = None

    async def authenticate(
        self,
        request: SynapseRequest,
        scope: str | None = None,
        required: bool = True,
    ) -> Caller | None:
        return await self.auth.authenticate(request, scope, required)

    async def _caller_is_admin(self, caller: Caller | None) -> bool:
        return (
            caller is not None
            and caller.is_matrix
            and await self.api.is_user_admin(caller.user_id)
        )

    async def can_manage_space(self, space_id: str, caller: Caller | None) -> bool:
        if caller is None or not space_id:
            return False
        from .spaces import can_manage_space, load_space_state

        try:
            state = await load_space_state(self.api, space_id, caller.user_id)
        except SynapseError:
            return False
        return can_manage_space(state, caller.user_id)

    async def can_manage_package(
        self, package: tuple, caller: Caller | None
    ) -> bool:
        if caller is None:
            return False
        if package[3] == caller.user_id:
            return True
        space_id = package[28] if len(package) > 28 else None
        if isinstance(space_id, str) and space_id:
            return await self.can_manage_space(space_id, caller)
        return False

    async def require_admin(self, request: SynapseRequest) -> Caller:
        caller = await self.authenticate(request, required=True)
        assert caller is not None
        if not caller.is_matrix or not await self.api.is_user_admin(caller.user_id):
            raise forbidden("This endpoint requires a Matrix server administrator.")
        return caller

    def enforce_rate_limit(
        self,
        request: SynapseRequest,
        caller: Caller | None,
        cost: float = 1.0,
    ) -> None:
        if caller is not None:
            key = f"user:{caller.game_id or caller.user_id}"
        else:
            key = f"ip:{request.getClientIP() or 'unknown'}"
        status = self.rate_limiter.check(
            key, self.config.rate_limit_per_second, self.config.rate_limit_burst, cost
        )
        request.setHeader(b"X-RateLimit-Limit", str(status.limit).encode())
        request.setHeader(b"X-RateLimit-Remaining", str(status.remaining).encode())
        request.setHeader(b"X-RateLimit-Reset", str(status.reset_seconds).encode())

    def page_limit(self, request: SynapseRequest) -> int:
        value = query_int(
            request,
            "limit",
            1,
            self.config.pagination_max_limit,
            None,
        )
        if value is None:
            return self.config.pagination_default_limit
        return value
