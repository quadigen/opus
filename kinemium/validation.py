from __future__ import annotations

import re
import urllib.parse
from typing import Any

from .errors import invalid_request

_USER_ID_RE = re.compile(r"^@[^:\x00-\x1f\x7f]{1,255}:[^:\x00-\x1f\x7f/]{1,255}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MXC_RE = re.compile(r"^mxc://[^/\s]+/[^/\s]+$")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def require_str(
    body: dict[str, Any],
    name: str,
    minimum: int = 1,
    maximum: int = 1024,
) -> str:
    value = body.get(name)
    if not isinstance(value, str):
        raise invalid_request(f"{name} must be a string.")
    return check_str(value, name, minimum, maximum)


def optional_str(
    body: dict[str, Any],
    name: str,
    maximum: int = 1024,
) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise invalid_request(f"{name} must be a string or null.")
    return check_str(value, name, 0, maximum)


def check_str(value: str, name: str, minimum: int = 0, maximum: int = 1024) -> str:
    if len(value) < minimum:
        raise invalid_request(f"{name} is too short.")
    if len(value) > maximum:
        raise invalid_request(f"{name} is too long.")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise invalid_request(f"{name} contains control characters.")
    return value


def require_bool(body: dict[str, Any], name: str) -> bool:
    value = body.get(name)
    if not isinstance(value, bool):
        raise invalid_request(f"{name} must be a boolean.")
    return value


def optional_bool(body: dict[str, Any], name: str) -> bool | None:
    if name not in body or body.get(name) is None:
        return None
    return require_bool(body, name)


def require_int(
    body: dict[str, Any],
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise invalid_request(f"{name} must be an integer.")
    if value < minimum or value > maximum:
        raise invalid_request(f"{name} must be between {minimum} and {maximum}.")
    return value


def optional_int(
    body: dict[str, Any],
    name: str,
    minimum: int,
    maximum: int,
) -> int | None:
    if name not in body or body.get(name) is None:
        return None
    return require_int(body, name, minimum, maximum)


def require_number(body: dict[str, Any], name: str) -> float:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise invalid_request(f"{name} must be a number.")
    return float(value)


def require_list(body: dict[str, Any], name: str, maximum: int) -> list[Any]:
    value = body.get(name)
    if not isinstance(value, list):
        raise invalid_request(f"{name} must be an array.")
    if len(value) > maximum:
        raise invalid_request(f"{name} contains too many entries.")
    return value


def validate_matrix_user_id(value: str) -> str:
    if not _USER_ID_RE.fullmatch(value):
        raise invalid_request("Invalid Matrix user ID.")
    return value


def validate_slug(value: str, name: str) -> str:
    if not _SLUG_RE.fullmatch(value):
        raise invalid_request(
            f"{name} must match [a-z0-9][a-z0-9_-] and be at most 64 characters."
        )
    return value


def validate_id(value: str, name: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise invalid_request(f"{name} contains invalid characters.")
    return value


def validate_entry_key(value: str, maximum: int = 128) -> str:
    if not value:
        raise invalid_request("Entry key must not be empty.")
    if len(value) > maximum:
        raise invalid_request("Entry key is too long.")
    if "/" in value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise invalid_request("Entry key contains forbidden characters.")
    return value


def validate_website(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if len(value) > 512:
        raise invalid_request("website is too long.")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise invalid_request("website must be an http or https URL.")
    return value


def validate_tags(value: Any, maximum: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise invalid_request("tags must be an array within the configured limit.")
    result: list[str] = []
    seen: set[str] = set()
    for tag in value:
        if not isinstance(tag, str):
            raise invalid_request("tags must contain strings only.")
        tag = tag.strip()
        if not tag or len(tag) > 32:
            raise invalid_request("Each tag must be between 1 and 32 characters.")
        if any(ord(character) < 32 or ord(character) == 127 for character in tag):
            raise invalid_request("Tags contain control characters.")
        lowered = tag.lower()
        if lowered not in seen:
            seen.add(lowered)
            result.append(tag)
    return result


def validate_relative_path(value: str, name: str) -> str:
    if not value or len(value) > 255:
        raise invalid_request(f"{name} must be a relative path of at most 255 characters.")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise invalid_request(f"{name} must be a relative path.")
    if any(part in ("", ".", "..") for part in normalized.split("/")):
        raise invalid_request(f"{name} contains an unsafe path.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise invalid_request(f"{name} contains control characters.")
    return value


def validate_mxc_url(value: str, name: str = "url") -> str:
    if len(value) > 512 or not _MXC_RE.fullmatch(value):
        raise invalid_request(f"{name} must be an mxc:// URL.")
    return value


def validate_color(value: str, name: str = "accentColor") -> str:
    if not _COLOR_RE.fullmatch(value):
        raise invalid_request(f"{name} must be a hex color such as #5865F2.")
    return value.upper()


def validate_shortcode(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_+-]{2,32}", value):
        raise invalid_request(
            "shortcode must be 2-32 characters using letters, digits, _, +, or -."
        )
    return value
