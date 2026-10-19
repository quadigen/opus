from __future__ import annotations

from typing import Any

from synapse.api.errors import SynapseError

INVALID_REQUEST = "KINEMIUM_INVALID_REQUEST"
UNAUTHORIZED = "KINEMIUM_UNAUTHORIZED"
FORBIDDEN = "KINEMIUM_FORBIDDEN"
NOT_FOUND = "KINEMIUM_NOT_FOUND"
CONFLICT = "KINEMIUM_CONFLICT"
RATE_LIMITED = "KINEMIUM_RATE_LIMITED"
INTERNAL_ERROR = "KINEMIUM_INTERNAL_ERROR"


class KinemiumError(SynapseError):
    def __init__(
        self,
        code: int,
        message: str,
        errcode: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        additional_fields = {"details": details} if details is not None else None
        super().__init__(code, message, errcode, additional_fields)


def invalid_request(message: str, details: dict[str, Any] | None = None) -> KinemiumError:
    return KinemiumError(400, message, INVALID_REQUEST, details)


def payload_too_large(message: str) -> KinemiumError:
    return KinemiumError(413, message, INVALID_REQUEST)


def unauthorized(message: str = "Valid authentication is required.") -> KinemiumError:
    return KinemiumError(401, message, UNAUTHORIZED)


def forbidden(message: str = "You do not have permission to perform this action.") -> KinemiumError:
    return KinemiumError(403, message, FORBIDDEN)


def not_found(message: str = "The requested resource was not found.") -> KinemiumError:
    return KinemiumError(404, message, NOT_FOUND)


def conflict(
    message: str, details: dict[str, Any] | None = None
) -> KinemiumError:
    return KinemiumError(409, message, CONFLICT, details)


def rate_limited(retry_after_ms: int) -> KinemiumError:
    error = KinemiumError(
        429,
        "Too many requests.",
        RATE_LIMITED,
        {"retryAfterMs": retry_after_ms},
    )
    error.headers = {"Retry-After": str(max(1, (retry_after_ms + 999) // 1000))}
    return error
