from __future__ import annotations

from typing import Any

from synapse.module_api import ModuleApi

from .accounts import AccountService
from .admin import AdminReviewResource
from .auth import AuthService
from .collab import CollabService
from .config import KinemiumConfig
from .db import Database
from .http import KinemiumResource
from .marketplace import MarketplaceService
from .packages import PackageService
from .platform import PlatformService
from .ratelimit import RateLimiter
from .servers import GameServerService
from .spaces import SpaceService
from .storage import LocalStorage
from .widget import WidgetService


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
        self.servers = GameServerService(
            api,
            self.config,
            self.db,
            self.storage,
            self.rate_limiter,
            self.auth,
        )
        self.collab = CollabService(
            api,
            self.config,
            self.db,
            self.storage,
            self.rate_limiter,
            self.auth,
        )
        self.platform = PlatformService(
            api,
            self.config,
            self.db,
            self.storage,
            self.rate_limiter,
            self.auth,
        )
        self.widget = WidgetService(
            api,
            self.config,
            self.db,
            self.storage,
            self.rate_limiter,
            self.auth,
        )
        self.accounts.marketplace = self.marketplace
        self.accounts.platform = self.platform
        self.marketplace.platform = self.platform
        self.packages.platform = self.platform
        self.spaces.platform = self.platform
        self.servers.platform = self.platform
        self.collab.platform = self.platform
        for service in (
            self.accounts,
            self.marketplace,
            self.packages,
            self.spaces,
            self.servers,
            self.collab,
            self.platform,
        ):
            service.register(self.resource)
        self.resource.register_options()
        self.widget_resource = KinemiumResource(api._hs, self.config)
        self.widget.register(self.widget_resource)
        self.api.register_web_resource("/_kinemium/v1", self.resource)
        self.api.register_web_resource(
            "/_kinemium/widget", self.widget_resource
        )
        self.api.register_web_resource(
            "/_kinemium/widget.svg", self.widget_resource
        )
        self.api.register_web_resource(
            "/_kinemium/admin", AdminReviewResource()
        )

    async def close(self) -> None:
        pass
