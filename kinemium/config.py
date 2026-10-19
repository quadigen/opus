from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any

from synapse.config._base import ConfigError

DEFAULT_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_JSON_BODY_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_MEDIA_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_EMOJI_BYTES = 1024 * 1024

_SIZE_SUFFIXES = {
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024 * 1024,
    "mb": 1024 * 1024,
    "mib": 1024 * 1024,
    "g": 1024 * 1024 * 1024,
    "gb": 1024 * 1024 * 1024,
    "gib": 1024 * 1024 * 1024,
}


def _parse_size(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be a byte size", path=(name,))
    if isinstance(value, int):
        if value <= 0:
            raise ConfigError(f"{name} must be positive", path=(name,))
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        suffix = ""
        for candidate in ("kib", "mib", "gib", "kb", "mb", "gb", "k", "m", "g"):
            if text.endswith(candidate):
                suffix = candidate
                text = text[: -len(candidate)].strip()
                break
        try:
            number = int(text)
        except ValueError:
            raise ConfigError(
                f"{name} must be an integer byte size", path=(name,)
            ) from None
        if number <= 0:
            raise ConfigError(f"{name} must be positive", path=(name,))
        return number * _SIZE_SUFFIXES.get(suffix, 1)
    raise ConfigError(f"{name} must be a byte size", path=(name,))


def _parse_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer", path=(name,))
    if value < minimum or value > maximum:
        raise ConfigError(
            f"{name} must be between {minimum} and {maximum}", path=(name,)
        )
    return value


def _parse_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number", path=(name,))
    result = float(value)
    if result <= 0:
        raise ConfigError(f"{name} must be positive", path=(name,))
    return result


def _parse_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be a boolean", path=(name,))
    return value


@dataclass(frozen=True)
class KinemiumConfig:
    storage_path: str
    storage_backend: str = "local"
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES
    max_json_body_bytes: int = DEFAULT_MAX_JSON_BODY_BYTES
    max_archive_uncompressed_bytes: int = DEFAULT_MAX_ARCHIVE_UNCOMPRESSED_BYTES
    max_archive_entries: int = 10000
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES
    max_emoji_bytes: int = DEFAULT_MAX_EMOJI_BYTES
    max_space_emojis: int = 50
    max_datastore_key_length: int = 128
    max_datastore_value_bytes: int = 64 * 1024
    max_datastore_batch: int = 50
    max_datastores_per_game: int = 32
    max_datastores_per_user: int = 128
    max_entries_per_store: int = 100000
    max_ordered_entries_per_store: int = 100000
    max_api_tokens_per_user: int = 25
    max_marketplace_versions_per_item: int = 100
    max_package_versions: int = 1000
    max_listing_tags: int = 20
    max_review_length: int = 4000
    max_dependency_count: int = 100
    max_resolve_packages: int = 100
    pagination_default_limit: int = 20
    pagination_max_limit: int = 100
    rate_limit_per_second: float = 20.0
    rate_limit_burst: int = 60
    datastore_rate_limit_per_second: float = 50.0
    datastore_rate_limit_burst: int = 200
    enable_cors: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "KinemiumConfig":
        if not isinstance(raw, dict):
            raise ConfigError("Kinemium module config must be an object")
        allowed = {field.name for field in fields(cls)}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ConfigError(
                "Unknown Kinemium configuration keys: " + ", ".join(unknown)
            )

        storage_path = raw.get("storage_path")
        if not isinstance(storage_path, str) or not storage_path.strip():
            raise ConfigError(
                "storage_path is required for the Kinemium module",
                path=("storage_path",),
            )
        storage_path = os.path.abspath(os.path.expanduser(storage_path))
        if not os.path.isabs(storage_path):
            raise ConfigError(
                "storage_path must be an absolute path", path=("storage_path",)
            )

        storage_backend = raw.get("storage_backend", "local")
        if storage_backend != "local":
            raise ConfigError(
                "storage_backend must be 'local'", path=("storage_backend",)
            )

        try:
            os.makedirs(storage_path, mode=0o750, exist_ok=True)
        except OSError as error:
            raise ConfigError(
                f"Unable to create storage_path: {error}", path=("storage_path",)
            ) from None
        if not os.path.isdir(storage_path):
            raise ConfigError(
                "storage_path must be a directory", path=("storage_path",)
            )
        if not os.access(storage_path, os.R_OK | os.W_OK | os.X_OK):
            raise ConfigError(
                "storage_path must be readable and writable",
                path=("storage_path",),
            )

        values: dict[str, Any] = {
            "storage_path": storage_path,
            "storage_backend": storage_backend,
        }
        size_fields = {
            "max_archive_bytes",
            "max_json_body_bytes",
            "max_archive_uncompressed_bytes",
            "max_media_bytes",
            "max_emoji_bytes",
        }
        int_bounds = {
            "max_archive_entries": (1, 1000000),
            "max_space_emojis": (0, 1000),
            "max_datastore_key_length": (1, 1024),
            "max_datastore_value_bytes": (1, 16 * 1024 * 1024),
            "max_datastore_batch": (1, 1000),
            "max_datastores_per_game": (1, 100000),
            "max_datastores_per_user": (1, 100000),
            "max_entries_per_store": (1, 100000000),
            "max_ordered_entries_per_store": (1, 100000000),
            "max_api_tokens_per_user": (1, 100000),
            "max_marketplace_versions_per_item": (1, 100000),
            "max_package_versions": (1, 1000000),
            "max_listing_tags": (0, 1000),
            "max_review_length": (1, 1000000),
            "max_dependency_count": (0, 10000),
            "max_resolve_packages": (1, 10000),
            "pagination_default_limit": (1, 1000),
            "pagination_max_limit": (1, 1000),
            "rate_limit_burst": (1, 1000000),
            "datastore_rate_limit_burst": (1, 1000000),
        }
        for name, (minimum, maximum) in int_bounds.items():
            if name in raw:
                values[name] = _parse_int(raw[name], name, minimum, maximum)
        for name in size_fields:
            if name in raw:
                values[name] = _parse_size(raw[name], name)
        for name in ("rate_limit_per_second", "datastore_rate_limit_per_second"):
            if name in raw:
                values[name] = _parse_float(raw[name], name)
        if "enable_cors" in raw:
            values["enable_cors"] = _parse_bool(raw["enable_cors"], "enable_cors")

        config = cls(**values)
        if config.pagination_default_limit > config.pagination_max_limit:
            raise ConfigError(
                "pagination_default_limit cannot exceed pagination_max_limit"
            )
        if config.max_archive_bytes > config.max_json_body_bytes:
            raise ConfigError(
                "max_json_body_bytes must be at least max_archive_bytes"
            )
        return config
