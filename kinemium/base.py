from __future__ import annotations

from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi

from .auth import AuthService, Caller
from .config import KinemiumConfig
from .db import Database
from .http import query_int
from .ratelimit import RateLimiter
from .storage import LocalStorage


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

    async def authenticate(
        self,
        request: SynapseRequest,
        scope: str | None = None,
        required: bool = True,
    ) -> Caller | None:
        return await self.auth.authenticate(request, scope, required)

    def enforce_rate_limit(
        self,
        request: SynapseRequest,
        caller: Caller | None,
        datastore: bool = False,
        cost: float = 1.0,
    ) -> None:
        if caller is not None:
            key = caller.game_id or caller.user_id
            if datastore and caller.game_id is not None:
                key = f"datastore:{caller.game_id}"
            else:
                key = f"user:{key}"
        else:
            key = f"ip:{request.getClientIP() or 'unknown'}"
        if datastore:
            rate = self.config.datastore_rate_limit_per_second
            burst = self.config.datastore_rate_limit_burst
        else:
            rate = self.config.rate_limit_per_second
            burst = self.config.rate_limit_burst
        self.rate_limiter.check(key, rate, burst, cost)

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
