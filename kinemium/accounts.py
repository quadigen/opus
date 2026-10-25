from __future__ import annotations

import json
import re
import uuid
from typing import TYPE_CHECKING, Any

from synapse.api.errors import SynapseError
from synapse.http.site import SynapseRequest
from synapse.types import UserID, create_requester

from .assets import read_image, store_image
from .auth import (
    ACCOUNT_READ,
    ACCOUNT_WRITE,
    Caller,
    hash_token,
    new_token,
    validate_scopes,
)
from .base import Service
from .errors import conflict, forbidden, invalid_request, not_found
from .http import parse_json_body, query_bool
from .util import json_loads, now_ms
from .validation import (
    check_str,
    optional_bool,
    optional_str,
    require_str,
    validate_color,
    validate_id,
    validate_matrix_user_id,
    validate_mxc_url,
    validate_website,
)

if TYPE_CHECKING:
    from .marketplace import MarketplaceService


def _profile_row(txn: Any, user_id: str) -> tuple | None:
    txn.execute(
        "SELECT user_id, bio, website, public_profile, public_marketplace,"
        " banner_url, accent_color, status_text, status_emoji, pronouns,"
        " created_at, updated_at FROM kinemium_profiles WHERE user_id = %s",
        (user_id,),
    )
    return txn.fetchone()


def _ensure_profile(txn: Any, user_id: str, now: int) -> tuple:
    txn.execute(
        "INSERT INTO kinemium_profiles"
        " (user_id, bio, website, public_profile, public_marketplace,"
        " banner_url, accent_color, status_text, status_emoji, pronouns,"
        " created_at, updated_at)"
        " VALUES (%s, '', NULL, TRUE, TRUE, NULL, NULL, '', NULL, NULL, %s, %s)"
        " ON CONFLICT (user_id) DO NOTHING",
        (user_id, now, now),
    )
    row = _profile_row(txn, user_id)
    if row is None:
        raise not_found("Profile not found.")
    return row


def _update_profile(
    txn: Any,
    user_id: str,
    bio: str,
    website: str | None,
    public_profile: bool,
    public_marketplace: bool,
    banner_url: str | None,
    accent_color: str | None,
    status_text: str,
    status_emoji: str | None,
    pronouns: str | None,
    now: int,
) -> tuple:
    txn.execute(
        "UPDATE kinemium_profiles SET bio = %s, website = %s,"
        " public_profile = %s, public_marketplace = %s, banner_url = %s,"
        " accent_color = %s, status_text = %s, status_emoji = %s,"
        " pronouns = %s, updated_at = %s"
        " WHERE user_id = %s",
        (
            bio,
            website,
            public_profile,
            public_marketplace,
            banner_url,
            accent_color,
            status_text,
            status_emoji,
            pronouns,
            now,
            user_id,
        ),
    )
    row = _profile_row(txn, user_id)
    if row is None:
        raise not_found("Profile not found.")
    return row


_GAME_COLUMNS = (
    "game_id, owner_user_id, name, created_at, updated_at, description,"
    " icon_url, banner_url, website"
)


def _game_row(txn: Any, game_id: str, for_update: str = "") -> tuple | None:
    txn.execute(
        "SELECT " + _GAME_COLUMNS + " FROM kinemium_games"
        " WHERE game_id = %s" + for_update,
        (game_id,),
    )
    return txn.fetchone()


def _list_games(txn: Any, owner: str) -> list[tuple]:
    txn.execute(
        "SELECT " + _GAME_COLUMNS + " FROM kinemium_games"
        " WHERE owner_user_id = %s ORDER BY created_at DESC, game_id DESC",
        (owner,),
    )
    return txn.fetchall()


def _create_game(
    txn: Any, owner: str, name: str, now: int, for_update: str
) -> tuple:
    txn.execute(
        "SELECT user_id FROM kinemium_profiles WHERE user_id = %s" + for_update,
        (owner,),
    )
    txn.execute(
        "SELECT game_id FROM kinemium_games WHERE owner_user_id = %s AND name = %s",
        (owner, name),
    )
    if txn.fetchone() is not None:
        raise conflict("A game with that name already exists.")
    game_id = "game_" + uuid.uuid4().hex
    txn.execute(
        "INSERT INTO kinemium_games (game_id, owner_user_id, name, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        (game_id, owner, name, now, now),
    )
    row = _game_row(txn, game_id)
    if row is None:
        raise not_found("Game not found.")
    return row


def _update_game(
    txn: Any,
    game_id: str,
    owner: str,
    name: str | None,
    description: str | None,
    website: str | None,
    website_set: bool,
    icon_url: str | None,
    icon_set: bool,
    banner_url: str | None,
    banner_set: bool,
    now: int,
    for_update: str,
) -> tuple:
    row = _game_row(txn, game_id, for_update)
    if row is None or row[1] != owner:
        raise not_found("Game not found.")
    new_name = name if name is not None else row[2]
    if new_name != row[2]:
        txn.execute(
            "SELECT game_id FROM kinemium_games WHERE owner_user_id = %s"
            " AND name = %s AND game_id != %s",
            (owner, new_name, game_id),
        )
        if txn.fetchone() is not None:
            raise conflict("A game with that name already exists.")
    txn.execute(
        "UPDATE kinemium_games SET name = %s, description = %s, website = %s,"
        " icon_url = %s, banner_url = %s, updated_at = %s WHERE game_id = %s",
        (
            new_name,
            description if description is not None else row[5],
            website if website_set else row[8],
            icon_url if icon_set else row[6],
            banner_url if banner_set else row[7],
            now,
            game_id,
        ),
    )
    updated = _game_row(txn, game_id)
    if updated is None:
        raise not_found("Game not found.")
    return updated


def _delete_game(txn: Any, game_id: str, owner: str) -> list[str]:
    row = _game_row(txn, game_id)
    if row is None or row[1] != owner:
        raise not_found("Game not found.")
    txn.execute(
        "SELECT kine_storage_key FROM kinemium_game_server_requests"
        " WHERE game_id = %s",
        (game_id,),
    )
    storage_keys = [str(item[0]) for item in txn.fetchall()]
    txn.execute(
        "DELETE FROM kinemium_games WHERE game_id = %s AND owner_user_id = %s",
        (game_id, owner),
    )
    if int(txn.rowcount or 0) == 0:
        raise not_found("Game not found.")
    return storage_keys


def _get_game(txn: Any, game_id: str) -> tuple | None:
    return _game_row(txn, game_id)


def _list_tokens(
    txn: Any,
    user_id: str,
    include_revoked: bool,
    game_id: str | None,
) -> list[tuple]:
    sql = (
        "SELECT token_id, name, scopes, game_id, created_at, last_used_at, revoked_at"
        " FROM kinemium_api_credentials WHERE user_id = %s"
    )
    params: list[Any] = [user_id]
    if game_id is not None:
        sql += " AND game_id = %s"
        params.append(game_id)
    if not include_revoked:
        sql += " AND revoked_at IS NULL"
    sql += " ORDER BY created_at DESC, token_id DESC"
    txn.execute(sql, tuple(params))
    return txn.fetchall()


def _create_token(
    txn: Any,
    user_id: str,
    token_id: str,
    name: str,
    scopes: list[str],
    game_id: str | None,
    token_hash: str,
    now: int,
    maximum: int,
    for_update: str,
) -> tuple:
    txn.execute(
        "SELECT user_id FROM kinemium_profiles WHERE user_id = %s" + for_update,
        (user_id,),
    )
    txn.execute(
        "SELECT COUNT(*) FROM kinemium_api_credentials"
        " WHERE user_id = %s AND revoked_at IS NULL",
        (user_id,),
    )
    count_row = txn.fetchone()
    if count_row is not None and int(count_row[0]) >= maximum:
        raise conflict("API token limit reached.")
    txn.execute(
        "INSERT INTO kinemium_api_credentials"
        " (token_id, user_id, game_id, name, token_hash, scopes,"
        " created_at, last_used_at, revoked_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, NULL)",
        (
            token_id,
            user_id,
            game_id,
            name,
            token_hash,
            json.dumps(scopes),
            now,
        ),
    )
    return (token_id, name, json.dumps(scopes), game_id, now, None, None)


def _revoke_token(
    txn: Any,
    token_id: str,
    user_id: str,
    now: int,
    game_id: str | None,
) -> tuple:
    sql = (
        "SELECT token_id, user_id, name, scopes, game_id, created_at,"
        " last_used_at, revoked_at FROM kinemium_api_credentials"
        " WHERE token_id = %s AND user_id = %s"
    )
    params: list[Any] = [token_id, user_id]
    if game_id is not None:
        sql += " AND game_id = %s"
        params.append(game_id)
    txn.execute(sql, tuple(params))
    row = txn.fetchone()
    if row is None:
        raise not_found("API token not found.")
    if row[7] is None:
        txn.execute(
            "UPDATE kinemium_api_credentials SET revoked_at = %s WHERE token_id = %s",
            (now, token_id),
        )
        txn.execute(
            "SELECT token_id, user_id, name, scopes, game_id, created_at,"
            " last_used_at, revoked_at FROM kinemium_api_credentials"
            " WHERE token_id = %s",
            (token_id,),
        )
        row = txn.fetchone()
        if row is None:
            raise not_found("API token not found.")
    return (row[0], row[2], row[3], row[4], row[5], row[6], row[7])


class AccountService(Service):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.marketplace: MarketplaceService | None = None

    def register(self, resource: Any) -> None:
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/me$")],
            self.get_me,
            "KinemiumMeServlet",
        )
        resource.register_paths(
            "PATCH",
            [re.compile(r"^/_kinemium/v1/me$")],
            self.patch_me,
            "KinemiumMeServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/me/banner$")],
            self.post_my_banner,
            "KinemiumMyBannerServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/me/banner$")],
            self.delete_my_banner,
            "KinemiumMyBannerServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/me/avatar$")],
            self.post_my_avatar,
            "KinemiumMyAvatarServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/me/games$")],
            self.get_my_games,
            "KinemiumMyGamesServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/me/games$")],
            self.post_my_games,
            "KinemiumMyGamesServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/me/games/(?P<game_id>[^/]+)$")],
            self.get_my_game,
            "KinemiumMyGameServlet",
        )
        resource.register_paths(
            "PATCH",
            [re.compile(r"^/_kinemium/v1/me/games/(?P<game_id>[^/]+)$")],
            self.patch_my_game,
            "KinemiumMyGameServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/me/games/(?P<game_id>[^/]+)$")],
            self.delete_my_game,
            "KinemiumMyGameServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/me/games/(?P<game_id>[^/]+)/icon$")],
            self.post_my_game_icon,
            "KinemiumMyGameIconServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/me/games/(?P<game_id>[^/]+)/icon$")],
            self.delete_my_game_icon,
            "KinemiumMyGameIconServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/me/games/(?P<game_id>[^/]+)/banner$")],
            self.post_my_game_banner,
            "KinemiumMyGameBannerServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/me/games/(?P<game_id>[^/]+)/banner$")],
            self.delete_my_game_banner,
            "KinemiumMyGameBannerServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/games/(?P<game_id>[^/]+)$")],
            self.get_public_game,
            "KinemiumPublicGameServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/me/avatar$")],
            self.delete_my_avatar,
            "KinemiumMyAvatarServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/me/tokens$")],
            self.get_tokens,
            "KinemiumTokensServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/me/tokens$")],
            self.post_tokens,
            "KinemiumTokensServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/me/tokens/(?P<token_id>[^/]+)$")],
            self.delete_token,
            "KinemiumTokenServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/accounts/(?P<user_id>[^/]+)$")],
            self.get_account,
            "KinemiumAccountServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/accounts/(?P<user_id>[^/]+)/marketplace$")],
            self.get_account_marketplace,
            "KinemiumAccountMarketplaceServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/accounts/(?P<user_id>[^/]+)/games$")],
            self.get_account_games,
            "KinemiumAccountGamesServlet",
        )

    async def _matrix_profile(self, user_id: str) -> tuple[str | None, str | None]:
        try:
            parsed = UserID.from_string(user_id)
        except Exception:
            return None, None
        if parsed.domain != self.api.server_name:
            return None, None
        try:
            profile = await self.api.get_profile_for_user(parsed.localpart)
        except SynapseError:
            return None, None
        return profile.display_name, profile.avatar_url

    async def _profile_payload(self, row: tuple) -> dict[str, Any]:
        display_name, avatar_url = await self._matrix_profile(row[0])
        return {
            "userId": row[0],
            "displayName": display_name,
            "avatarUrl": avatar_url,
            "bio": row[1],
            "website": row[2],
            "publicProfile": bool(row[3]),
            "publicMarketplace": bool(row[4]),
            "bannerUrl": row[5],
            "accentColor": row[6],
            "statusText": row[7],
            "statusEmoji": row[8],
            "pronouns": row[9],
            "createdAt": row[10],
            "updatedAt": row[11],
        }

    def _game_payload(self, row: tuple, can_manage: bool) -> dict[str, Any]:
        return {
            "gameId": row[0],
            "ownerUserId": row[1],
            "name": row[2],
            "description": row[5],
            "iconUrl": row[6],
            "bannerUrl": row[7],
            "website": row[8],
            "createdAt": row[3],
            "updatedAt": row[4],
            "canManage": can_manage,
        }

    def _token_payload(self, row: tuple) -> dict[str, Any]:
        scopes = json_loads(row[2])
        if not isinstance(scopes, list):
            scopes = []
        return {
            "tokenId": row[0],
            "name": row[1],
            "scopes": scopes,
            "gameId": row[3],
            "createdAt": row[4],
            "lastUsedAt": row[5],
            "revokedAt": row[6],
        }

    async def get_me(self, request: SynapseRequest) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, ACCOUNT_READ)
        self.enforce_rate_limit(request, caller)
        now = now_ms()
        row = await self.db.run("kinemium_get_profile", _ensure_profile, caller.user_id, now)
        return 200, await self._profile_payload(row)

    async def patch_me(self, request: SynapseRequest) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, ACCOUNT_WRITE)
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {
            "displayName",
            "bio",
            "website",
            "publicProfile",
            "publicMarketplace",
            "bannerUrl",
            "accentColor",
            "statusText",
            "statusEmoji",
            "pronouns",
        }
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown profile fields: " + ", ".join(unknown))
        display_name_requested = "displayName" in body
        if display_name_requested:
            value = body.get("displayName")
            if value is not None:
                if not isinstance(value, str):
                    raise invalid_request("displayName must be a string or null.")
                check_str(value, "displayName", 0, 255)
                try:
                    await self.api.set_displayname(
                        UserID.from_string(caller.user_id), value
                    )
                except SynapseError:
                    raise invalid_request("Unable to update the Matrix display name.") from None
        now = now_ms()
        current = await self.db.run(
            "kinemium_get_profile", _ensure_profile, caller.user_id, now
        )
        bio = current[1]
        website = current[2]
        public_profile = bool(current[3])
        public_marketplace = bool(current[4])
        banner_url = current[5]
        accent_color = current[6]
        status_text = current[7]
        status_emoji = current[8]
        pronouns = current[9]
        if "bio" in body:
            value = optional_str(body, "bio", 4000)
            bio = value or ""
        if "website" in body:
            website = validate_website(optional_str(body, "website", 512))
        if "publicProfile" in body:
            value = optional_bool(body, "publicProfile")
            if value is not None:
                public_profile = value
        if "publicMarketplace" in body:
            value = optional_bool(body, "publicMarketplace")
            if value is not None:
                public_marketplace = value
        if "bannerUrl" in body:
            value = optional_str(body, "bannerUrl", 512)
            banner_url = validate_mxc_url(value, "bannerUrl") if value else None
        if "accentColor" in body:
            value = optional_str(body, "accentColor", 16)
            accent_color = validate_color(value) if value else None
        if "statusText" in body:
            value = optional_str(body, "statusText", 128)
            status_text = value or ""
        if "statusEmoji" in body:
            value = optional_str(body, "statusEmoji", 32)
            status_emoji = value or None
        if "pronouns" in body:
            value = optional_str(body, "pronouns", 40)
            pronouns = value or None
        row = await self.db.run(
            "kinemium_update_profile",
            _update_profile,
            caller.user_id,
            bio,
            website,
            public_profile,
            public_marketplace,
            banner_url,
            accent_color,
            status_text,
            status_emoji,
            pronouns,
            now,
        )
        return 200, await self._profile_payload(row)

    async def _update_banner(
        self, caller: Caller, banner_url: str | None
    ) -> tuple[int, dict[str, Any]]:
        now = now_ms()
        current = await self.db.run(
            "kinemium_get_profile", _ensure_profile, caller.user_id, now
        )
        row = await self.db.run(
            "kinemium_update_profile",
            _update_profile,
            caller.user_id,
            current[1],
            current[2],
            bool(current[3]),
            bool(current[4]),
            banner_url,
            current[6],
            current[7],
            current[8],
            current[9],
            now,
        )
        return 200, await self._profile_payload(row)

    async def post_my_banner(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, ACCOUNT_WRITE)
        self.enforce_rate_limit(request, caller)
        image = read_image(request, self.config.max_media_bytes)
        banner_url = await store_image(
            self.api, caller.user_id, image, "account-banner"
        )
        return await self._update_banner(caller, banner_url)

    async def delete_my_banner(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, ACCOUNT_WRITE)
        self.enforce_rate_limit(request, caller)
        return await self._update_banner(caller, None)

    async def post_my_avatar(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, ACCOUNT_WRITE)
        self.enforce_rate_limit(request, caller)
        image = read_image(request, self.config.max_media_bytes)
        avatar_url = await store_image(
            self.api, caller.user_id, image, "account-avatar"
        )
        profile_handler = self.api._hs.get_profile_handler()
        await profile_handler.set_avatar_url(
            target_user=UserID.from_string(caller.user_id),
            requester=create_requester(caller.user_id),
            new_avatar_url=avatar_url,
        )
        now = now_ms()
        row = await self.db.run(
            "kinemium_get_profile", _ensure_profile, caller.user_id, now
        )
        return 200, await self._profile_payload(row)

    async def delete_my_avatar(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, ACCOUNT_WRITE)
        self.enforce_rate_limit(request, caller)
        profile_handler = self.api._hs.get_profile_handler()
        await profile_handler.set_avatar_url(
            target_user=UserID.from_string(caller.user_id),
            requester=create_requester(caller.user_id),
            new_avatar_url="",
        )
        now = now_ms()
        row = await self.db.run(
            "kinemium_get_profile", _ensure_profile, caller.user_id, now
        )
        return 200, await self._profile_payload(row)

    async def _game_for_write(
        self, request: SynapseRequest, game_id: str
    ) -> tuple[Caller, tuple]:
        caller = await self._require(request, ACCOUNT_WRITE)
        self.enforce_rate_limit(request, caller)
        validate_id(game_id, "gameId")
        if caller.game_id is not None and caller.game_id != game_id:
            raise not_found("Game not found.")
        row = await self.db.run("kinemium_get_game", _get_game, game_id)
        if row is None or row[1] != caller.user_id:
            raise not_found("Game not found.")
        return caller, row

    async def get_my_game(
        self, request: SynapseRequest, game_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, ACCOUNT_READ)
        self.enforce_rate_limit(request, caller)
        validate_id(game_id, "gameId")
        if caller.game_id is not None and caller.game_id != game_id:
            raise not_found("Game not found.")
        row = await self.db.run("kinemium_get_game", _get_game, game_id)
        if row is None or row[1] != caller.user_id:
            raise not_found("Game not found.")
        return 200, self._game_payload(row, caller.game_id is None)

    async def get_public_game(
        self, request: SynapseRequest, game_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, None, required=False)
        self.enforce_rate_limit(request, caller)
        validate_id(game_id, "gameId")
        row = await self.db.run("kinemium_get_game", _get_game, game_id)
        if row is None:
            raise not_found("Game not found.")
        can_manage = (
            caller is not None
            and caller.user_id == row[1]
            and (caller.game_id is None or caller.game_id == game_id)
        )
        return 200, self._game_payload(row, can_manage)

    async def patch_my_game(
        self, request: SynapseRequest, game_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _row = await self._game_for_write(request, game_id)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {"name", "description", "website", "iconUrl", "bannerUrl"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown game fields: " + ", ".join(unknown))
        if caller.game_id is not None and "name" in body:
            raise forbidden("Game-bound tokens cannot rename games.")
        name = None
        if "name" in body:
            name = require_str(body, "name", 1, 64).strip()
            if not name:
                raise invalid_request("name must not be blank.")
        description = None
        if "description" in body:
            description = optional_str(body, "description", 4000) or ""
        website_set = "website" in body
        website = None
        if website_set:
            website = validate_website(optional_str(body, "website", 512))
        icon_set = "iconUrl" in body
        icon_url = None
        if icon_set:
            value = optional_str(body, "iconUrl", 512)
            icon_url = validate_mxc_url(value, "iconUrl") if value else None
        banner_set = "bannerUrl" in body
        banner_url = None
        if banner_set:
            value = optional_str(body, "bannerUrl", 512)
            banner_url = validate_mxc_url(value, "bannerUrl") if value else None
        row = await self.db.run(
            "kinemium_update_game",
            _update_game,
            game_id,
            caller.user_id,
            name,
            description,
            website,
            website_set,
            icon_url,
            icon_set,
            banner_url,
            banner_set,
            now_ms(),
            self.db.for_update(),
        )
        return 200, self._game_payload(row, True)

    async def _update_game_media(
        self,
        caller: Caller,
        game_id: str,
        icon_set: bool = False,
        icon_url: str | None = None,
        banner_set: bool = False,
        banner_url: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        row = await self.db.run(
            "kinemium_update_game",
            _update_game,
            game_id,
            caller.user_id,
            None,
            None,
            None,
            False,
            icon_url,
            icon_set,
            banner_url,
            banner_set,
            now_ms(),
            self.db.for_update(),
        )
        return 200, self._game_payload(row, True)

    async def post_my_game_icon(
        self, request: SynapseRequest, game_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _row = await self._game_for_write(request, game_id)
        image = read_image(request, self.config.max_media_bytes)
        icon_url = await store_image(
            self.api, caller.user_id, image, f"game-{game_id}-icon"
        )
        return await self._update_game_media(
            caller, game_id, icon_set=True, icon_url=icon_url
        )

    async def delete_my_game_icon(
        self, request: SynapseRequest, game_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _row = await self._game_for_write(request, game_id)
        return await self._update_game_media(caller, game_id, icon_set=True)

    async def post_my_game_banner(
        self, request: SynapseRequest, game_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _row = await self._game_for_write(request, game_id)
        image = read_image(request, self.config.max_media_bytes)
        banner_url = await store_image(
            self.api, caller.user_id, image, f"game-{game_id}-banner"
        )
        return await self._update_game_media(
            caller, game_id, banner_set=True, banner_url=banner_url
        )

    async def delete_my_game_banner(
        self, request: SynapseRequest, game_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _row = await self._game_for_write(request, game_id)
        return await self._update_game_media(caller, game_id, banner_set=True)

    async def delete_my_game(
        self, request: SynapseRequest, game_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, ACCOUNT_WRITE)
        self.enforce_rate_limit(request, caller)
        if caller.game_id is not None:
            raise forbidden("Game-bound tokens cannot manage games.")
        validate_id(game_id, "gameId")
        storage_keys = await self.db.run(
            "kinemium_delete_game", _delete_game, game_id, caller.user_id
        )
        for key in storage_keys:
            await self.storage.delete(key)
        return 200, {"deleted": True, "gameId": game_id}

    async def get_account(
        self, request: SynapseRequest, user_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, None, required=False)
        self.enforce_rate_limit(request, caller)
        validate_matrix_user_id(user_id)
        row = await self.db.run("kinemium_get_profile_row", _profile_row, user_id)
        if row is None:
            raise not_found("Profile not found.")
        is_owner = caller is not None and caller.user_id == user_id
        if not row[3] and not is_owner:
            raise not_found("Profile not found.")
        return 200, await self._profile_payload(row)

    async def get_account_marketplace(
        self, request: SynapseRequest, user_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, None, required=False)
        self.enforce_rate_limit(request, caller)
        validate_matrix_user_id(user_id)
        row = await self.db.run("kinemium_get_profile_row", _profile_row, user_id)
        if row is None:
            raise not_found("Profile not found.")
        is_owner = caller is not None and caller.user_id == user_id
        if not row[4] and not is_owner:
            raise forbidden("This developer's marketplace is private.")
        if self.marketplace is None:
            raise not_found("Marketplace service is unavailable.")
        return 200, await self.marketplace.list_owner_items(request, user_id)

    async def get_account_games(
        self, request: SynapseRequest, user_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, None, required=False)
        self.enforce_rate_limit(request, caller)
        validate_matrix_user_id(user_id)
        row = await self.db.run("kinemium_get_profile_row", _profile_row, user_id)
        if row is None:
            raise not_found("Profile not found.")
        can_manage = caller is not None and caller.user_id == user_id
        rows = await self.db.run("kinemium_list_games", _list_games, user_id)
        games = [self._game_payload(game, can_manage) for game in rows]
        return 200, {"games": games}

    async def get_my_games(self, request: SynapseRequest) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, ACCOUNT_READ)
        self.enforce_rate_limit(request, caller)
        now = now_ms()
        await self.db.run("kinemium_ensure_profile", _ensure_profile, caller.user_id, now)
        rows = await self.db.run("kinemium_list_games", _list_games, caller.user_id)
        games = [
            self._game_payload(game, caller.game_id is None)
            for game in rows
            if caller.game_id is None or game[0] == caller.game_id
        ]
        return 200, {"games": games}

    async def post_my_games(self, request: SynapseRequest) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, ACCOUNT_WRITE)
        self.enforce_rate_limit(request, caller)
        if caller.game_id is not None:
            raise forbidden("Game-bound tokens cannot manage games.")
        body = parse_json_body(request, self.config.max_json_body_bytes)
        name = require_str(body, "name", 1, 64).strip()
        if not name:
            raise invalid_request("name must not be blank.")
        now = now_ms()
        await self.db.run("kinemium_ensure_profile", _ensure_profile, caller.user_id, now)
        row = await self.db.run(
            "kinemium_create_game",
            _create_game,
            caller.user_id,
            name,
            now,
            self.db.for_update(),
        )
        return 201, self._game_payload(row, True)

    async def get_tokens(self, request: SynapseRequest) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, ACCOUNT_READ)
        self.enforce_rate_limit(request, caller)
        include_revoked = bool(query_bool(request, "includeRevoked", False))
        rows = await self.db.run(
            "kinemium_list_tokens",
            _list_tokens,
            caller.user_id,
            include_revoked,
            caller.game_id,
        )
        return 200, {"tokens": [self._token_payload(row) for row in rows]}

    async def post_tokens(self, request: SynapseRequest) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, ACCOUNT_WRITE)
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        name = require_str(body, "name", 1, 64).strip()
        if not name:
            raise invalid_request("name must not be blank.")
        scopes = validate_scopes(body.get("scopes"))
        requested_game = body.get("gameId")
        if requested_game is not None:
            if not isinstance(requested_game, str):
                raise invalid_request("gameId must be a string.")
            validate_id(requested_game, "gameId")
        if not caller.is_matrix:
            if "*" not in caller.scopes:
                forbidden_scopes = [
                    scope for scope in scopes if scope not in caller.scopes
                ]
                if forbidden_scopes:
                    raise forbidden("The requested scopes exceed this credential.")
            if caller.game_id is not None:
                if requested_game is not None and requested_game != caller.game_id:
                    raise forbidden("A game token cannot create tokens for another game.")
                requested_game = caller.game_id
        if requested_game is not None:
            game = await self.db.run(
                "kinemium_get_game", _get_game, requested_game
            )
            if game is None or game[1] != caller.user_id:
                raise forbidden("You do not own that game.")
        now = now_ms()
        await self.db.run(
            "kinemium_ensure_profile", _ensure_profile, caller.user_id, now
        )
        token = new_token()
        token_id = "tok_" + uuid.uuid4().hex
        row = await self.db.run(
            "kinemium_create_token",
            _create_token,
            caller.user_id,
            token_id,
            name,
            scopes,
            requested_game,
            hash_token(token),
            now,
            self.config.max_api_tokens_per_user,
            self.db.for_update(),
        )
        payload = self._token_payload(row)
        payload["token"] = token
        return 201, payload

    async def delete_token(
        self, request: SynapseRequest, token_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, ACCOUNT_WRITE)
        self.enforce_rate_limit(request, caller)
        validate_id(token_id, "tokenId")
        row = await self.db.run(
            "kinemium_revoke_token",
            _revoke_token,
            token_id,
            caller.user_id,
            now_ms(),
            caller.game_id,
        )
        payload = self._token_payload(row)
        return 200, payload

    async def _require(self, request: SynapseRequest, scope: str) -> Caller:
        caller = await self.authenticate(request, scope, required=True)
        assert caller is not None
        return caller
