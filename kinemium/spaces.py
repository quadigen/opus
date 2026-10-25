from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from synapse.api.errors import SynapseError
from synapse.http.site import SynapseRequest

from .assets import detect_image, read_image, store_image
from .auth import SPACES_READ, SPACES_WRITE, Caller
from .base import Service
from .errors import conflict, forbidden, invalid_request, not_found
from .http import parse_json_body, parse_multipart
from .util import now_ms
from .validation import (
    check_str,
    optional_str,
    validate_color,
    validate_mxc_url,
    validate_shortcode,
)

SPACE_PROFILE_EVENT = "com.quadigen.kinemium.space.profile"
SPACE_EMOJIS_EVENT = "com.quadigen.kinemium.space.emojis"

_CREATE = ("m.room.create", "")
_NAME = ("m.room.name", "")
_TOPIC = ("m.room.topic", "")
_AVATAR = ("m.room.avatar", "")
_POWER = ("m.room.power_levels", "")
_PROFILE = (SPACE_PROFILE_EVENT, "")
_EMOJIS = (SPACE_EMOJIS_EVENT, "")

_STATE_FILTER = [
    _CREATE,
    ("m.room.member", None),
    _NAME,
    _TOPIC,
    _AVATAR,
    _POWER,
    _PROFILE,
    _EMOJIS,
]


def _content(
    state: Mapping[tuple[str, str], Any], key: tuple[str, str]
) -> dict[str, Any]:
    event = state.get(key)
    if event is None:
        return {}
    return dict(event.content)


def _is_space(state: Mapping[tuple[str, str], Any]) -> bool:
    return _content(state, _CREATE).get("type") == "m.space"


def _member_membership(
    state: Mapping[tuple[str, str], Any], user_id: str
) -> str | None:
    event = state.get(("m.room.member", user_id))
    if event is None:
        return None
    value = event.content.get("membership")
    return value if isinstance(value, str) else None


def _int_value(value: Any, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _power_content(state: Mapping[tuple[str, str], Any]) -> dict[str, Any]:
    return _content(state, _POWER)


def _user_power(state: Mapping[tuple[str, str], Any], user_id: str) -> int:
    content = _power_content(state)
    users = content.get("users")
    if isinstance(users, dict):
        value = users.get(user_id)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    create = state.get(_CREATE)
    if create is not None and create.sender == user_id:
        return 100
    return _int_value(content.get("users_default"), 0)


def _required_power(
    state: Mapping[tuple[str, str], Any], event_type: str
) -> int:
    content = _power_content(state)
    events = content.get("events")
    if isinstance(events, dict):
        value = events.get(event_type)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return _int_value(content.get("state_default"), 50)


def _require_power(
    state: Mapping[tuple[str, str], Any], user_id: str, event_types: set[str]
) -> None:
    power = _user_power(state, user_id)
    for event_type in event_types:
        required = _required_power(state, event_type)
        if power < required:
            raise forbidden(
                "You do not have permission to customize this space."
            )


def _can_customize(state: Mapping[tuple[str, str], Any], user_id: str) -> bool:
    power = _user_power(state, user_id)
    return all(
        power >= _required_power(state, event_type)
        for event_type in (
            "m.room.name",
            "m.room.topic",
            "m.room.avatar",
            SPACE_PROFILE_EVENT,
            SPACE_EMOJIS_EVENT,
        )
    )


async def load_space_state(
    api: Any, room_id: str, user_id: str
) -> Mapping[tuple[str, str], Any]:
    try:
        state = await api.get_room_state(room_id, _STATE_FILTER)
    except SynapseError:
        raise not_found("Space not found.") from None
    if not _is_space(state):
        raise not_found("Space not found.")
    if _member_membership(state, user_id) != "join":
        raise forbidden("You must be a member of this space.")
    return state


def can_manage_space(state: Mapping[tuple[str, str], Any], user_id: str) -> bool:
    return _can_customize(state, user_id)


def space_name(state: Mapping[tuple[str, str], Any]) -> str | None:
    value = _content(state, _NAME).get("name")
    return value if isinstance(value, str) else None


def _emojis(state: Mapping[tuple[str, str], Any]) -> list[dict[str, Any]]:
    content = _content(state, _EMOJIS)
    raw = content.get("emojis")
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        shortcode = item.get("shortcode")
        url = item.get("url")
        if not isinstance(shortcode, str) or not isinstance(url, str):
            continue
        result.append(
            {
                "shortcode": shortcode,
                "url": url,
                "animated": bool(item.get("animated", False)),
            }
        )
    return result


class SpaceService(Service):
    def register(self, resource: Any) -> None:
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/spaces$")],
            self.get_spaces,
            "KinemiumSpacesServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/spaces/(?P<room_id>[^/]+)/profile$")],
            self.get_space_profile,
            "KinemiumSpaceProfileServlet",
        )
        resource.register_paths(
            "PATCH",
            [re.compile(r"^/_kinemium/v1/spaces/(?P<room_id>[^/]+)/profile$")],
            self.patch_space_profile,
            "KinemiumSpaceProfileServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/spaces/(?P<room_id>[^/]+)/avatar$")],
            self.post_space_avatar,
            "KinemiumSpaceAvatarServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/spaces/(?P<room_id>[^/]+)/banner$")],
            self.post_space_banner,
            "KinemiumSpaceBannerServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/spaces/(?P<room_id>[^/]+)/avatar$")],
            self.delete_space_avatar,
            "KinemiumSpaceAvatarServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/spaces/(?P<room_id>[^/]+)/banner$")],
            self.delete_space_banner,
            "KinemiumSpaceBannerServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/spaces/(?P<room_id>[^/]+)/emojis$")],
            self.get_space_emojis,
            "KinemiumSpaceEmojisServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/spaces/(?P<room_id>[^/]+)/emojis$")],
            self.post_space_emoji,
            "KinemiumSpaceEmojisServlet",
        )
        resource.register_paths(
            "DELETE",
            [
                re.compile(
                    r"^/_kinemium/v1/spaces/(?P<room_id>[^/]+)/emojis/"
                    r"(?P<shortcode>[^/]+)$"
                )
            ],
            self.delete_space_emoji,
            "KinemiumSpaceEmojiServlet",
        )

    async def _require(self, request: SynapseRequest, scope: str) -> Caller:
        caller = await self.authenticate(request, scope, required=True)
        assert caller is not None
        return caller

    async def _load_space(
        self, room_id: str, user_id: str
    ) -> Mapping[tuple[str, str], Any]:
        return await load_space_state(self.api, room_id, user_id)

    async def _send_state(
        self,
        room_id: str,
        sender: str,
        event_type: str,
        content: dict[str, Any],
    ) -> None:
        await self.api.create_and_send_event_into_room(
            {
                "type": event_type,
                "state_key": "",
                "room_id": room_id,
                "sender": sender,
                "content": content,
            }
        )

    def _payload(
        self,
        room_id: str,
        state: Mapping[tuple[str, str], Any],
        user_id: str,
    ) -> dict[str, Any]:
        profile = _content(state, _PROFILE)
        member_count = sum(
            1
            for (event_type, _), event in state.items()
            if event_type == "m.room.member"
            and event.content.get("membership") == "join"
        )
        return {
            "roomId": room_id,
            "name": _content(state, _NAME).get("name"),
            "topic": _content(state, _TOPIC).get("topic"),
            "avatarUrl": _content(state, _AVATAR).get("url"),
            "bannerUrl": profile.get("banner_url"),
            "accentColor": profile.get("accent_color"),
            "description": profile.get("description", ""),
            "emojis": _emojis(state),
            "memberCount": member_count,
            "joined": True,
            "canCustomize": _can_customize(state, user_id),
        }

    async def get_spaces(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, SPACES_READ)
        self.enforce_rate_limit(request, caller)
        room_ids = await self.api._store.get_rooms_for_user(caller.user_id)
        spaces: list[dict[str, Any]] = []
        for room_id in sorted(room_ids):
            try:
                state = await self.api.get_room_state(room_id, _STATE_FILTER)
            except SynapseError:
                continue
            if not _is_space(state):
                continue
            if _member_membership(state, caller.user_id) != "join":
                continue
            spaces.append(self._payload(room_id, state, caller.user_id))
        return 200, {"spaces": spaces}

    async def get_space_profile(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, SPACES_READ)
        self.enforce_rate_limit(request, caller)
        state = await self._load_space(room_id, caller.user_id)
        return 200, self._payload(room_id, state, caller.user_id)

    async def patch_space_profile(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, SPACES_WRITE)
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {
            "name",
            "topic",
            "avatarUrl",
            "bannerUrl",
            "accentColor",
            "description",
        }
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown space profile fields: " + ", ".join(unknown))
        state = await self._load_space(room_id, caller.user_id)

        event_types: set[str] = set()
        name: str | None = None
        topic: str | None = None
        avatar_url: str | None = None
        avatar_given = False
        profile = _content(state, _PROFILE)
        profile_changed = False
        now = now_ms()

        if "name" in body:
            value = body.get("name")
            if not isinstance(value, str):
                raise invalid_request("name must be a string.")
            name = check_str(value.strip(), "name", 0, 255)
            event_types.add("m.room.name")
        if "topic" in body:
            value = optional_str(body, "topic", 1000)
            topic = value or ""
            event_types.add("m.room.topic")
        if "avatarUrl" in body:
            value = optional_str(body, "avatarUrl", 512)
            avatar_url = validate_mxc_url(value, "avatarUrl") if value else None
            avatar_given = True
            event_types.add("m.room.avatar")
        if "bannerUrl" in body:
            value = optional_str(body, "bannerUrl", 512)
            if value:
                profile["banner_url"] = validate_mxc_url(value, "bannerUrl")
            else:
                profile.pop("banner_url", None)
            profile_changed = True
        if "accentColor" in body:
            value = optional_str(body, "accentColor", 16)
            if value:
                profile["accent_color"] = validate_color(value)
            else:
                profile.pop("accent_color", None)
            profile_changed = True
        if "description" in body:
            value = optional_str(body, "description", 500)
            if value:
                profile["description"] = value
            else:
                profile.pop("description", None)
            profile_changed = True
        if profile_changed:
            profile["updated_at"] = now
            event_types.add(SPACE_PROFILE_EVENT)

        if not event_types:
            return 200, self._payload(room_id, state, caller.user_id)

        _require_power(state, caller.user_id, event_types)

        if "m.room.name" in event_types:
            assert name is not None
            await self._send_state(room_id, caller.user_id, "m.room.name", {"name": name})
        if "m.room.topic" in event_types:
            assert topic is not None
            await self._send_state(
                room_id, caller.user_id, "m.room.topic", {"topic": topic}
            )
        if avatar_given:
            content = {"url": avatar_url} if avatar_url else {}
            await self._send_state(
                room_id, caller.user_id, "m.room.avatar", content
            )
        if profile_changed:
            await self._send_state(
                room_id, caller.user_id, SPACE_PROFILE_EVENT, profile
            )

        state = await self._load_space(room_id, caller.user_id)
        return 200, self._payload(room_id, state, caller.user_id)

    async def post_space_avatar(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, SPACES_WRITE)
        self.enforce_rate_limit(request, caller)
        state = await self._load_space(room_id, caller.user_id)
        _require_power(state, caller.user_id, {"m.room.avatar"})
        image = read_image(request, self.config.max_media_bytes)
        avatar_url = await store_image(self.api, caller.user_id, image, "space-avatar")
        await self._send_state(
            room_id, caller.user_id, "m.room.avatar", {"url": avatar_url}
        )
        state = await self._load_space(room_id, caller.user_id)
        return 200, self._payload(room_id, state, caller.user_id)

    async def post_space_banner(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, SPACES_WRITE)
        self.enforce_rate_limit(request, caller)
        state = await self._load_space(room_id, caller.user_id)
        _require_power(state, caller.user_id, {SPACE_PROFILE_EVENT})
        image = read_image(request, self.config.max_media_bytes)
        banner_url = await store_image(self.api, caller.user_id, image, "space-banner")
        profile = _content(state, _PROFILE)
        profile["banner_url"] = banner_url
        profile["updated_at"] = now_ms()
        await self._send_state(room_id, caller.user_id, SPACE_PROFILE_EVENT, profile)
        state = await self._load_space(room_id, caller.user_id)
        return 200, self._payload(room_id, state, caller.user_id)

    async def delete_space_avatar(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, SPACES_WRITE)
        self.enforce_rate_limit(request, caller)
        state = await self._load_space(room_id, caller.user_id)
        _require_power(state, caller.user_id, {"m.room.avatar"})
        await self._send_state(room_id, caller.user_id, "m.room.avatar", {})
        state = await self._load_space(room_id, caller.user_id)
        return 200, self._payload(room_id, state, caller.user_id)

    async def delete_space_banner(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, SPACES_WRITE)
        self.enforce_rate_limit(request, caller)
        state = await self._load_space(room_id, caller.user_id)
        _require_power(state, caller.user_id, {SPACE_PROFILE_EVENT})
        profile = _content(state, _PROFILE)
        profile.pop("banner_url", None)
        profile["updated_at"] = now_ms()
        await self._send_state(
            room_id, caller.user_id, SPACE_PROFILE_EVENT, profile
        )
        state = await self._load_space(room_id, caller.user_id)
        return 200, self._payload(room_id, state, caller.user_id)

    async def get_space_emojis(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, SPACES_READ)
        self.enforce_rate_limit(request, caller)
        state = await self._load_space(room_id, caller.user_id)
        return 200, {"emojis": _emojis(state)}

    async def post_space_emoji(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, SPACES_WRITE)
        self.enforce_rate_limit(request, caller)
        state = await self._load_space(room_id, caller.user_id)
        _require_power(state, caller.user_id, {SPACE_EMOJIS_EVENT})
        fields, file_data, _ = parse_multipart(
            request, self.config.max_emoji_bytes + 64 * 1024
        )
        shortcode = validate_shortcode(fields.get("shortcode", ""))
        if file_data is None:
            raise invalid_request("An emoji image is required.")
        if len(file_data) > self.config.max_emoji_bytes:
            raise invalid_request("Emoji image exceeds the configured limit.")
        image = detect_image(file_data)
        if image is None:
            raise invalid_request("Only PNG, JPEG, GIF, and WebP images are supported.")
        emojis = _emojis(state)
        if len(emojis) >= self.config.max_space_emojis:
            raise conflict("Space emoji limit reached.")
        if any(item["shortcode"] == shortcode for item in emojis):
            raise conflict("That emoji shortcode already exists.")
        url = await store_image(
            self.api, caller.user_id, image, f"space-emoji-{shortcode}"
        )
        item = {"shortcode": shortcode, "url": url, "animated": image.animated}
        emojis.append(item)
        await self._send_state(
            room_id,
            caller.user_id,
            SPACE_EMOJIS_EVENT,
            {"emojis": emojis, "updated_at": now_ms()},
        )
        return 201, {"emoji": item, "emojis": emojis}

    async def delete_space_emoji(
        self, request: SynapseRequest, room_id: str, shortcode: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self._require(request, SPACES_WRITE)
        self.enforce_rate_limit(request, caller)
        shortcode = validate_shortcode(shortcode)
        state = await self._load_space(room_id, caller.user_id)
        _require_power(state, caller.user_id, {SPACE_EMOJIS_EVENT})
        emojis = _emojis(state)
        remaining = [item for item in emojis if item["shortcode"] != shortcode]
        if len(remaining) == len(emojis):
            raise not_found("Emoji not found.")
        await self._send_state(
            room_id,
            caller.user_id,
            SPACE_EMOJIS_EVENT,
            {"emojis": remaining, "updated_at": now_ms()},
        )
        return 200, {"emojis": remaining}
