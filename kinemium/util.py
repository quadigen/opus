from __future__ import annotations

import base64
import json
import math
import time
from typing import Any

from .errors import invalid_request, not_found


def now_ms() -> int:
    return int(time.time() * 1000)


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except Exception:
        raise invalid_request("Invalid base64url value.") from None


def encode_cursor(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return b64url_encode(raw)


def decode_cursor(value: str, max_length: int = 2048) -> dict[str, Any]:
    if not isinstance(value, str) or len(value) > max_length:
        raise invalid_request("Invalid pagination cursor.")
    try:
        decoded = json.loads(b64url_decode(value).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise invalid_request("Invalid pagination cursor.") from None
    if not isinstance(decoded, dict):
        raise invalid_request("Invalid pagination cursor.")
    return decoded


def json_dumps(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise invalid_request("Value is not valid JSON.") from None


def json_loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        raise ValueError("Unsupported JSON database value")
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def json_size(value: Any) -> int:
    return len(json_dumps(value).encode("utf-8"))


def validate_json_value(value: Any, max_bytes: int, depth: int = 0) -> Any:
    if depth > 32:
        raise invalid_request("JSON value is nested too deeply.")
    if value is None or isinstance(value, (str, bool, int)):
        pass
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise invalid_request("JSON numbers must be finite.")
    elif isinstance(value, list):
        for item in value:
            validate_json_value(item, max_bytes, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise invalid_request("JSON object keys must be strings.")
            validate_json_value(item, max_bytes, depth + 1)
    else:
        raise invalid_request("Unsupported JSON value type.")
    if depth == 0 and json_size(value) > max_bytes:
        raise invalid_request("JSON value exceeds the configured size limit.")
    return value


def require_found(value: Any, message: str = "The requested resource was not found.") -> Any:
    if value is None:
        raise not_found(message)
    return value


def coerce_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise invalid_request(f"{name} must be a boolean.")


def clamp_limit(requested: int | None, default: int, maximum: int) -> int:
    if requested is None:
        return default
    if requested < 1 or requested > maximum:
        raise invalid_request(f"limit must be between 1 and {maximum}.")
    return requested


def quoted_etag(version: int) -> str:
    return f'"v{version}"'


def parse_if_match(value: bytes | str | None) -> int | None:
    if value is None:
        return None
    text = value.decode("ascii", "ignore") if isinstance(value, bytes) else value
    text = text.strip()
    if text == "*":
        return None
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    if text.startswith("v"):
        text = text[1:]
    if not text.isdigit():
        raise invalid_request("If-Match must contain an entry version.")
    return int(text)
