from __future__ import annotations

from typing import Any

from synapse.module_api import ModuleApi

from .accounts import AccountService
from .auth import AuthService
from .config import KinemiumConfig
from .datastores import DataStoreService
from .db import Database
from .http import KinemiumResource
from .marketplace import MarketplaceService
from .packages import PackageService
from .ratelimit import RateLimiter
from .spaces import SpaceService
from .storage import LocalStorage


class KinemiumModule:
    def __init__(self, config: dict[str, Any], api: ModuleApi) -> None:
        self.config = KinemiumConfig.from_dict(config)
        self.api = api
        self.db = Database(api)
        self.storage = LocalStorage(
            self.config.storage_path, self.config.max_archive_bytes
        )
        self.rate_limiter = RateLimiter()
        self.auth = AuthService(api, self.db)
        self.resource = KinemiumResource(api._hs, self.config)
        self.accounts = AccountService(
            api,
            self.config,
            self.db,
            self.storage,
            self.rate_limiter,
            self.auth,
        )
        self.marketplace = MarketplaceService(
            api,
            self.config,
            self.db,
            self.storage,
            self.rate_limiter,
            self.auth,
        )
        self.datastores = DataStoreService(
            api,
            self.config,
            self.db,
            self.storage,
            self.rate_limiter,
            self.auth,
        )
        self.packages = PackageService(
            api,
            self.config,
            self.db,
            self.storage,
            self.rate_limiter,
            self.auth,
        )
        self.spaces = SpaceService(
            api,
            self.config,
            self.db,
            self.storage,
            self.rate_limiter,
            self.auth,
        )
        self.accounts.marketplace = self.marketplace
        for service in (
            self.accounts,
            self.marketplace,
            self.datastores,
            self.packages,
            self.spaces,
        ):
            service.register(self.resource)
        self.resource.register_options()
        self.api.register_web_resource("/_kinemium/v1", self.resource)

    async def close(self) -> None:
        pass
