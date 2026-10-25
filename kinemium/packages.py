from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import deque
from typing import Any

from synapse.http.site import SynapseRequest

from .assets import read_image, store_image
from .auth import (
    PACKAGES_READ,
    PACKAGES_WRITE,
    Caller,
)
from .base import Service, idempotent
from .errors import conflict, forbidden, invalid_request, not_found, payload_too_large
from .http import (
    content_type_for_filename,
    parse_json_body,
    parse_multipart,
    query_bool,
    query_int,
    query_value,
    send_file,
    set_cors_response_headers,
)
from .semver import (
    Constraint,
    ConstraintError,
    SemVer,
    SemVerError,
    parse_constraint,
    parse_semver,
)
from .storage import sanitize_filename, validate_archive
from .util import (
    decode_cursor,
    encode_cursor,
    json_dumps,
    json_loads,
    now_ms,
    validate_json_value,
)
from .validation import (
    check_str,
    optional_str,
    require_list,
    validate_matrix_user_id,
    validate_mxc_url,
    validate_relative_path,
    validate_room_id,
    validate_slug,
    validate_tags,
    validate_website,
)

ITEM_TYPES = ("model", "plugin", "template", "tool", "package")
VISIBILITIES = ("public", "unlisted", "private")
PACKAGE_COLUMNS = (
    "package_id, namespace, name, owner_user_id, description, license,"
    " repository, engine_constraint, deprecated, deprecated_at,"
    " created_at, updated_at, item_type, tags, tags_text, visibility,"
    " published_at, review_status, review_note, reviewed_by, reviewed_at,"
    " submitted_at, icon_url, banner_url, readme, archived_at, archived_by,"
    " archive_reason, space_id, official, official_at, official_by"
)
VERSION_COLUMNS = (
    "version_id, version, manifest, entrypoint, engine_constraint, digest,"
    " storage_key, size_bytes, yanked, published_at, package_id, changelog,"
    " review_status, review_note, reviewed_by, reviewed_at, submitted_at"
)
MAX_PACKAGE_VERSIONS_RETURNED = 100
MAX_MANIFEST_BYTES = 64 * 1024
MAX_RECENT_COMMENTS = 10
REACTION_LIKES = (
    "(SELECT COUNT(*) FROM kinemium_package_reactions r"
    " WHERE r.package_id = p.package_id AND r.value = 1)"
)
REACTION_DISLIKES = (
    "(SELECT COUNT(*) FROM kinemium_package_reactions r"
    " WHERE r.package_id = p.package_id AND r.value = -1)"
)
INSTALL_COUNT = (
    "(SELECT COUNT(*) FROM kinemium_marketplace_acquisitions a"
    " WHERE a.package_id = p.package_id)"
)
DOWNLOAD_COUNT = (
    "COALESCE((SELECT d.download_count FROM kinemium_package_downloads d"
    " WHERE d.package_id = p.package_id), 0)"
)

logger = logging.getLogger(__name__)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _tags_json(tags: list[str]) -> str:
    return json.dumps(tags, ensure_ascii=False, separators=(",", ":"))


def _tags_text(tags: list[str]) -> str:
    return "\x1f" + "\x1f".join(tag.lower() for tag in tags) + "\x1f"


def _optional_timestamp(body: dict[str, Any], name: str) -> int | None:
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise invalid_request(f"{name} must be a non-negative integer or null.")
    return value


def package_payload(row: tuple) -> dict[str, Any]:
    return {
        "packageId": row[0],
        "namespace": row[1],
        "name": row[2],
        "owner": row[3],
        "spaceId": row[28],
        "creator": {"userId": row[3], "spaceId": row[28]},
        "description": row[4],
        "license": row[5],
        "repository": row[6],
        "engineConstraint": row[7],
        "deprecated": bool(row[8]),
        "deprecatedAt": row[9],
        "createdAt": row[10],
        "updatedAt": row[11],
        "itemType": row[12],
        "tags": json_loads(row[13]),
        "visibility": row[15],
        "publishedAt": row[16],
        "reviewStatus": row[17],
        "reviewNote": row[18],
        "reviewedBy": row[19],
        "reviewedAt": row[20],
        "submittedAt": row[21],
        "iconUrl": row[22],
        "bannerUrl": row[23],
        "readme": row[24],
        "archived": row[25] is not None,
        "archivedAt": row[25],
        "archivedBy": row[26],
        "archiveReason": row[27],
        "official": bool(row[29]),
        "officialAt": row[30],
        "officialBy": row[31],
    }


def package_is_visible(
    package: tuple,
    caller: Caller | None,
    is_admin: bool = False,
    manages: bool = False,
) -> bool:
    if is_admin:
        return True
    if manages or (caller is not None and caller.user_id == package[3]):
        return True
    if package[25] is not None:
        return False
    return package[15] != "private" and package[17] == "approved"


def version_is_visible(
    version: tuple,
    package: tuple,
    caller: Caller | None,
    is_admin: bool = False,
    manages: bool = False,
) -> bool:
    if is_admin:
        return True
    if manages or (caller is not None and caller.user_id == package[3]):
        return True
    return version[12] == "approved"


def reaction_payload(
    likes: int, dislikes: int, mine: int | None = None
) -> dict[str, Any]:
    return {
        "likes": int(likes),
        "dislikes": int(dislikes),
        "score": int(likes) - int(dislikes),
        "mine": mine,
    }


def reaction_summary(
    txn: Any, package_id: str, user_id: str | None
) -> dict[str, Any]:
    txn.execute(
        "SELECT value, COUNT(*) FROM kinemium_package_reactions"
        " WHERE package_id = %s GROUP BY value",
        (package_id,),
    )
    counts = {int(row[0]): int(row[1]) for row in txn.fetchall()}
    mine = None
    if user_id is not None:
        txn.execute(
            "SELECT value FROM kinemium_package_reactions"
            " WHERE package_id = %s AND user_id = %s",
            (package_id, user_id),
        )
        row = txn.fetchone()
        mine = int(row[0]) if row is not None else None
    return reaction_payload(counts.get(1, 0), counts.get(-1, 0), mine)


def comment_payload(row: tuple) -> dict[str, Any]:
    return {
        "commentId": row[0],
        "packageId": row[1],
        "userId": row[2],
        "body": row[3],
        "createdAt": row[4],
        "updatedAt": row[5],
    }


def recent_comments(
    txn: Any, package_id: str, limit: int = MAX_RECENT_COMMENTS
) -> list[tuple]:
    txn.execute(
        "SELECT comment_id, package_id, user_id, body, created_at, updated_at"
        " FROM kinemium_package_comments"
        " WHERE package_id = %s AND deleted_at IS NULL"
        " ORDER BY created_at DESC, comment_id DESC LIMIT %s",
        (package_id, limit),
    )
    return txn.fetchall()


def comment_count(txn: Any, package_id: str) -> int:
    txn.execute(
        "SELECT COUNT(*) FROM kinemium_package_comments"
        " WHERE package_id = %s AND deleted_at IS NULL",
        (package_id,),
    )
    row = txn.fetchone()
    return int(row[0]) if row is not None else 0


def installation_count(txn: Any, package_id: str) -> int:
    txn.execute(
        "SELECT COUNT(*) FROM kinemium_marketplace_acquisitions"
        " WHERE package_id = %s",
        (package_id,),
    )
    row = txn.fetchone()
    return int(row[0]) if row is not None else 0


def download_count(txn: Any, package_id: str) -> int:
    txn.execute(
        "SELECT download_count FROM kinemium_package_downloads WHERE package_id = %s",
        (package_id,),
    )
    row = txn.fetchone()
    return int(row[0]) if row is not None else 0


def record_download(txn: Any, package_id: str) -> int:
    txn.execute(
        "INSERT INTO kinemium_package_downloads (package_id, download_count)"
        " VALUES (%s, 1)"
        " ON CONFLICT (package_id) DO UPDATE SET"
        " download_count = kinemium_package_downloads.download_count + 1",
        (package_id,),
    )
    return download_count(txn, package_id)


def version_payload(row: tuple, dependencies: list[tuple]) -> dict[str, Any]:
    return {
        "version": row[1],
        "manifest": json_loads(row[2]),
        "entrypoint": row[3],
        "engineConstraint": row[4],
        "digest": row[5],
        "size": int(row[7]),
        "yanked": bool(row[8]),
        "publishedAt": row[9],
        "changelog": row[11] if len(row) > 11 else "",
        "reviewStatus": row[12] if len(row) > 12 else "approved",
        "reviewNote": row[13] if len(row) > 13 else "",
        "reviewedBy": row[14] if len(row) > 14 else None,
        "reviewedAt": row[15] if len(row) > 15 else None,
        "submittedAt": row[16] if len(row) > 16 else 0,
        "dependencies": [
            {
                "namespace": dependency[0],
                "name": dependency[1],
                "constraint": dependency[2],
            }
            for dependency in dependencies
        ],
    }


def _namespace_for_user(user_id: str) -> str:
    localpart = user_id.split(":", 1)[0]
    if localpart.startswith("@"):
        localpart = localpart[1:]
    namespace = localpart.lower()
    try:
        return validate_slug(namespace, "namespace")
    except Exception:
        raise invalid_request(
            "Your user ID cannot be used as a package namespace."
        ) from None


def _package_row(txn: Any, namespace: str, name: str) -> tuple | None:
    txn.execute(
        f"SELECT {PACKAGE_COLUMNS} FROM kinemium_packages"
        " WHERE namespace = %s AND name = %s",
        (namespace, name),
    )
    return txn.fetchone()


def _lock_package(txn: Any, namespace: str, name: str, for_update: str) -> tuple | None:
    txn.execute(
        f"SELECT {PACKAGE_COLUMNS} FROM kinemium_packages"
        " WHERE namespace = %s AND name = %s" + for_update,
        (namespace, name),
    )
    return txn.fetchone()


def _version_row(txn: Any, package_id: str, version: str) -> tuple | None:
    txn.execute(
        f"SELECT {VERSION_COLUMNS} FROM kinemium_package_versions"
        " WHERE package_id = %s AND version = %s",
        (package_id, version),
    )
    return txn.fetchone()


def _version_rows(txn: Any, package_id: str) -> list[tuple]:
    txn.execute(
        f"SELECT {VERSION_COLUMNS} FROM kinemium_package_versions"
        " WHERE package_id = %s ORDER BY published_at DESC, version DESC"
        " LIMIT %s",
        (package_id, MAX_PACKAGE_VERSIONS_RETURNED),
    )
    return txn.fetchall()


def _all_yanked_versions(txn: Any, package_id: str) -> list[tuple]:
    txn.execute(
        "SELECT version FROM kinemium_package_versions"
        " WHERE package_id = %s AND yanked = FALSE",
        (package_id,),
    )
    return txn.fetchall()


def _dependencies(txn: Any, package_id: str) -> list[tuple]:
    txn.execute(
        "SELECT version, dependency_namespace, dependency_name, constraint_text"
        " FROM kinemium_package_dependencies WHERE package_id = %s",
        (package_id,),
    )
    return txn.fetchall()


def _version_dependencies(txn: Any, package_id: str, version: str) -> list[tuple]:
    txn.execute(
        "SELECT dependency_namespace, dependency_name, constraint_text"
        " FROM kinemium_package_dependencies"
        " WHERE package_id = %s AND version = %s"
        " ORDER BY dependency_namespace, dependency_name",
        (package_id, version),
    )
    return txn.fetchall()


def _create_package(
    txn: Any,
    owner: str,
    namespace: str,
    name: str,
    description: str,
    license_name: str | None,
    repository: str | None,
    engine_constraint: str | None,
    item_type: str | None,
    tags: list[str] | None,
    visibility: str | None,
    space_id: str | None,
    now: int,
    for_update: str,
) -> tuple:
    txn.execute(
        "INSERT INTO kinemium_profiles"
        " (user_id, bio, website, public_profile, public_marketplace,"
        " created_at, updated_at) VALUES (%s, '', NULL, TRUE, TRUE, %s, %s)"
        " ON CONFLICT (user_id) DO NOTHING",
        (owner, now, now),
    )
    txn.execute(
        "SELECT owner_user_id, space_id FROM kinemium_package_namespaces"
        " WHERE namespace = %s" + for_update,
        (namespace,),
    )
    namespace_row = txn.fetchone()
    if namespace_row is None:
        txn.execute(
            "INSERT INTO kinemium_package_namespaces"
            " (namespace, owner_user_id, space_id, created_at)"
            " VALUES (%s, %s, %s, %s)",
            (namespace, owner, space_id, now),
        )
    elif space_id is not None:
        if namespace_row[1] != space_id:
            raise forbidden("That package namespace belongs to another account.")
    elif namespace_row[0] != owner or namespace_row[1] is not None:
        raise forbidden("That package namespace belongs to another account.")
    existing = _lock_package(txn, namespace, name, for_update)
    if existing is None:
        package_id = "pkg_" + uuid.uuid4().hex
        txn.execute(
            f"INSERT INTO kinemium_packages ({PACKAGE_COLUMNS})"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                package_id,
                namespace,
                name,
                owner,
                description,
                license_name,
                repository,
                engine_constraint,
                False,
                None,
                now,
                now,
                item_type or "package",
                _tags_json(tags or []),
                _tags_text(tags or []),
                visibility or "public",
                None,
                "pending",
                "",
                None,
                None,
                now,
                None,
                None,
                "",
                None,
                None,
                "",
                space_id,
                False,
                None,
                None,
            ),
        )
        created = _package_row(txn, namespace, name)
        if created is None:
            raise not_found("Package not found.")
        return created
    if space_id is not None:
        if existing[28] != space_id:
            raise forbidden("That package belongs to another owner.")
    elif existing[3] != owner or existing[28] is not None:
        raise forbidden("You do not own that package.")
    txn.execute(
        "UPDATE kinemium_packages SET description = %s, license = %s,"
        " repository = %s, engine_constraint = %s, item_type = %s, tags = %s,"
        " tags_text = %s, visibility = %s, updated_at = %s"
        " WHERE package_id = %s",
        (
            description if description else existing[4],
            license_name if license_name is not None else existing[5],
            repository if repository is not None else existing[6],
            engine_constraint if engine_constraint is not None else existing[7],
            item_type if item_type is not None else existing[12],
            _tags_json(tags) if tags is not None else existing[13],
            _tags_text(tags) if tags is not None else existing[14],
            visibility if visibility is not None else existing[15],
            now,
            existing[0],
        ),
    )
    updated = _package_row(txn, namespace, name)
    if updated is None:
        raise not_found("Package not found.")
    return updated


def _publish_version(
    txn: Any,
    package: tuple,
    version: str,
    manifest_json: str,
    entrypoint: str | None,
    engine_constraint: str | None,
    digest: str,
    storage_key: str,
    size: int,
    dependencies: list[tuple[str, str, str]],
    maximum: int,
    now: int,
    for_update: str,
    review_status: str = "pending",
) -> tuple:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    if locked[3] != package[3]:
        raise forbidden("You do not own that package.")
    txn.execute(
        "SELECT COUNT(*) FROM kinemium_package_versions WHERE package_id = %s",
        (package[0],),
    )
    count = txn.fetchone()
    if count is not None and int(count[0]) >= maximum:
        raise conflict("Package version limit reached.", {"reason": "quota_exceeded"})
    if _version_row(txn, package[0], version) is not None:
        raise conflict("That package version already exists.")
    version_id = "pkgv_" + uuid.uuid4().hex
    txn.execute(
        f"INSERT INTO kinemium_package_versions ({VERSION_COLUMNS})"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
        " %s, %s, %s, %s, %s)",
        (
            version_id,
            version,
            manifest_json,
            entrypoint,
            engine_constraint,
            digest,
            storage_key,
            size,
            False,
            now,
            package[0],
            "",
            review_status,
            "",
            None,
            None,
            now,
        ),
    )
    for dependency_namespace, dependency_name, constraint_text in dependencies:
        txn.execute(
            "INSERT INTO kinemium_package_dependencies"
            " (package_id, version, dependency_namespace, dependency_name,"
            " constraint_text) VALUES (%s, %s, %s, %s, %s)",
            (
                package[0],
                version,
                dependency_namespace,
                dependency_name,
                constraint_text,
            ),
        )
    if review_status == "pending" and locked[17] != "approved":
        txn.execute(
            "UPDATE kinemium_packages SET updated_at = %s,"
            " published_at = COALESCE(published_at, %s),"
            " review_status = 'pending', review_note = '', reviewed_by = NULL,"
            " reviewed_at = NULL, submitted_at = %s WHERE package_id = %s",
            (now, now, now, package[0]),
        )
    else:
        txn.execute(
            "UPDATE kinemium_packages SET updated_at = %s,"
            " published_at = COALESCE(published_at, %s) WHERE package_id = %s",
            (now, now, package[0]),
        )
    row = _version_row(txn, package[0], version)
    if row is None:
        raise not_found("Package version not found.")
    return row


def _set_yanked(
    txn: Any,
    package: tuple,
    version: str,
    yanked: bool,
    for_update: str,
) -> tuple:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    if locked[3] != package[3]:
        raise forbidden("You do not own that package.")
    row = _version_row(txn, package[0], version)
    if row is None:
        raise not_found("Package version not found.")
    txn.execute(
        "UPDATE kinemium_package_versions SET yanked = %s"
        " WHERE package_id = %s AND version = %s",
        (yanked, package[0], version),
    )
    updated = _version_row(txn, package[0], version)
    if updated is None:
        raise not_found("Package version not found.")
    return updated


def _update_package_metadata(
    txn: Any,
    package: tuple,
    description: str | None,
    license_name: str | None,
    license_set: bool,
    repository: str | None,
    repository_set: bool,
    deprecated: bool | None,
    item_type: str | None,
    tags: list[str] | None,
    visibility: str | None,
    now: int,
) -> tuple:
    txn.execute(
        "UPDATE kinemium_packages SET description = %s, license = %s,"
        " repository = %s, deprecated = %s, deprecated_at = %s, item_type = %s,"
        " tags = %s, tags_text = %s, visibility = %s, updated_at = %s"
        " WHERE package_id = %s",
        (
            description if description is not None else package[4],
            license_name if license_set else package[5],
            repository if repository_set else package[6],
            deprecated if deprecated is not None else package[8],
            now if deprecated else (None if deprecated is False else package[9]),
            item_type if item_type is not None else package[12],
            _tags_json(tags) if tags is not None else package[13],
            _tags_text(tags) if tags is not None else package[14],
            visibility if visibility is not None else package[15],
            now,
            package[0],
        ),
    )
    updated = _package_row(txn, package[1], package[2])
    if updated is None:
        raise not_found("Package not found.")
    return updated


def _set_review(
    txn: Any,
    package: tuple,
    status: str,
    note: str,
    reviewer: str,
    now: int,
    for_update: str,
) -> tuple:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    txn.execute(
        "UPDATE kinemium_packages SET review_status = %s, review_note = %s,"
        " reviewed_by = %s, reviewed_at = %s, updated_at = %s"
        " WHERE package_id = %s",
        (status, note, reviewer, now, now, locked[0]),
    )
    if status in ("approved", "rejected"):
        txn.execute(
            "UPDATE kinemium_package_versions SET review_status = %s,"
            " review_note = %s, reviewed_by = %s, reviewed_at = %s"
            " WHERE package_id = %s AND review_status = 'pending'",
            (status, note, reviewer, now, locked[0]),
        )
    updated = _package_row(txn, locked[1], locked[2])
    if updated is None:
        raise not_found("Package not found.")
    return updated


def _set_package_media(
    txn: Any,
    package: tuple,
    column: str,
    url: str | None,
    now: int,
    for_update: str,
) -> tuple:
    if column not in ("icon_url", "banner_url"):
        raise invalid_request("Unknown package image type.")
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    txn.execute(
        f"UPDATE kinemium_packages SET {column} = %s, updated_at = %s"
        " WHERE package_id = %s",
        (url, now, locked[0]),
    )
    updated = _package_row(txn, locked[1], locked[2])
    if updated is None:
        raise not_found("Package not found.")
    return updated


def _admin_update_package(
    txn: Any,
    package: tuple,
    changes: dict[str, Any],
    now: int,
    for_update: str,
) -> tuple:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    values = list(locked)
    indexes = {
        "owner": 3,
        "description": 4,
        "license": 5,
        "repository": 6,
        "engine_constraint": 7,
        "deprecated": 8,
        "deprecated_at": 9,
        "created_at": 10,
        "updated_at": 11,
        "item_type": 12,
        "visibility": 15,
        "published_at": 16,
        "review_status": 17,
        "review_note": 18,
        "reviewed_by": 19,
        "reviewed_at": 20,
        "submitted_at": 21,
        "icon_url": 22,
        "banner_url": 23,
        "readme": 24,
        "archived_at": 25,
        "archived_by": 26,
        "archive_reason": 27,
        "official": 29,
        "official_at": 30,
        "official_by": 31,
    }
    for key, value in changes.items():
        if key == "tags":
            values[13] = _tags_json(value)
            values[14] = _tags_text(value)
            continue
        index = indexes.get(key)
        if index is not None:
            values[index] = value
    if "updated_at" not in changes:
        values[11] = now
    if values[3] != locked[3]:
        txn.execute(
            "INSERT INTO kinemium_profiles"
            " (user_id, bio, website, public_profile, public_marketplace,"
            " created_at, updated_at) VALUES (%s, '', NULL, TRUE, TRUE, %s, %s)"
            " ON CONFLICT (user_id) DO NOTHING",
            (values[3], now, now),
        )
    txn.execute(
        "UPDATE kinemium_packages SET owner_user_id = %s, description = %s,"
        " license = %s, repository = %s, engine_constraint = %s,"
        " deprecated = %s, deprecated_at = %s, created_at = %s, updated_at = %s,"
        " item_type = %s, tags = %s, tags_text = %s, visibility = %s,"
        " published_at = %s, review_status = %s, review_note = %s,"
        " reviewed_by = %s, reviewed_at = %s, submitted_at = %s,"
        " icon_url = %s, banner_url = %s, readme = %s, archived_at = %s,"
        " archived_by = %s, archive_reason = %s, official = %s,"
        " official_at = %s, official_by = %s WHERE package_id = %s",
        (
            values[3],
            values[4],
            values[5],
            values[6],
            values[7],
            values[8],
            values[9],
            values[10],
            values[11],
            values[12],
            values[13],
            values[14],
            values[15],
            values[16],
            values[17],
            values[18],
            values[19],
            values[20],
            values[21],
            values[22],
            values[23],
            values[24],
            values[25],
            values[26],
            values[27],
            values[29],
            values[30],
            values[31],
            locked[0],
        ),
    )
    updated = _package_row(txn, locked[1], locked[2])
    if updated is None:
        raise not_found("Package not found.")
    return updated


def _admin_update_version(
    txn: Any,
    package: tuple,
    version: str,
    changes: dict[str, Any],
    dependencies: list[tuple[str, str, str]] | None,
    now: int,
    for_update: str,
) -> tuple:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    row = _version_row(txn, locked[0], version)
    if row is None:
        raise not_found("Package version not found.")
    values = list(row)
    indexes = {
        "manifest": 2,
        "entrypoint": 3,
        "engine_constraint": 4,
        "yanked": 8,
        "published_at": 9,
        "changelog": 11,
    }
    for key, value in changes.items():
        index = indexes.get(key)
        if index is not None:
            values[index] = value
    manifest_value = values[2]
    if not isinstance(manifest_value, str):
        manifest_value = json_dumps(manifest_value)
    txn.execute(
        "UPDATE kinemium_package_versions SET manifest = %s, entrypoint = %s,"
        " engine_constraint = %s, yanked = %s, published_at = %s,"
        " changelog = %s WHERE package_id = %s AND version = %s",
        (
            manifest_value,
            values[3],
            values[4],
            values[8],
            values[9],
            values[11],
            locked[0],
            version,
        ),
    )
    if dependencies is not None:
        txn.execute(
            "DELETE FROM kinemium_package_dependencies"
            " WHERE package_id = %s AND version = %s",
            (locked[0], version),
        )
        for dependency_namespace, dependency_name, constraint_text in dependencies:
            txn.execute(
                "INSERT INTO kinemium_package_dependencies"
                " (package_id, version, dependency_namespace, dependency_name,"
                " constraint_text) VALUES (%s, %s, %s, %s, %s)",
                (
                    locked[0],
                    version,
                    dependency_namespace,
                    dependency_name,
                    constraint_text,
                ),
            )
    txn.execute(
        "UPDATE kinemium_packages SET updated_at = %s WHERE package_id = %s",
        (now, locked[0]),
    )
    updated = _version_row(txn, locked[0], version)
    if updated is None:
        raise not_found("Package version not found.")
    return updated


def _admin_delete_version(
    txn: Any,
    package: tuple,
    version: str,
    now: int,
    for_update: str,
) -> str:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    row = _version_row(txn, locked[0], version)
    if row is None:
        raise not_found("Package version not found.")
    txn.execute(
        "DELETE FROM kinemium_package_versions"
        " WHERE package_id = %s AND version = %s",
        (locked[0], version),
    )
    txn.execute(
        "UPDATE kinemium_packages SET updated_at = %s WHERE package_id = %s",
        (now, locked[0]),
    )
    return row[6]


def _package_storage_keys(txn: Any, package_id: str) -> list[str]:
    txn.execute(
        "SELECT storage_key FROM kinemium_package_versions WHERE package_id = %s",
        (package_id,),
    )
    return [row[0] for row in txn.fetchall()]


def _delete_package(txn: Any, package: tuple, for_update: str) -> list[str]:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    keys = _package_storage_keys(txn, locked[0])
    txn.execute(
        "DELETE FROM kinemium_packages WHERE package_id = %s", (locked[0],)
    )
    return keys


def _set_version_review(
    txn: Any,
    package: tuple,
    version: str,
    status: str,
    note: str,
    reviewer: str,
    now: int,
    for_update: str,
) -> tuple:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    row = _version_row(txn, locked[0], version)
    if row is None:
        raise not_found("Package version not found.")
    if row[12] != "pending":
        raise conflict("That package version is not awaiting review.")
    txn.execute(
        "UPDATE kinemium_package_versions SET review_status = %s,"
        " review_note = %s, reviewed_by = %s, reviewed_at = %s"
        " WHERE package_id = %s AND version = %s",
        (status, note, reviewer, now, locked[0], version),
    )
    updated = _version_row(txn, locked[0], version)
    if updated is None:
        raise not_found("Package version not found.")
    return updated


def _submit_package(
    txn: Any, package: tuple, now: int, for_update: str
) -> tuple:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    txn.execute(
        "UPDATE kinemium_packages SET review_status = 'pending',"
        " reviewed_by = NULL, reviewed_at = NULL, submitted_at = %s,"
        " updated_at = %s WHERE package_id = %s",
        (now, now, locked[0]),
    )
    txn.execute(
        "UPDATE kinemium_package_versions SET review_status = 'pending',"
        " review_note = '', reviewed_by = NULL, reviewed_at = NULL,"
        " submitted_at = %s WHERE package_id = %s AND review_status = 'rejected'",
        (now, locked[0]),
    )
    updated = _package_row(txn, locked[1], locked[2])
    if updated is None:
        raise not_found("Package not found.")
    return updated


def _pending_reviews(
    txn: Any,
    limit: int,
    cursor_submitted: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    latest = (
        "(SELECT v.version FROM kinemium_package_versions v"
        " WHERE v.package_id = p.package_id AND v.yanked = FALSE"
        " ORDER BY v.published_at DESC, v.version DESC LIMIT 1)"
    )
    sql = (
        f"SELECT {', '.join('p.' + c.strip() for c in PACKAGE_COLUMNS.split(','))},"
        f" {latest} AS latest_version,"
        f" {REACTION_LIKES} AS like_count, {REACTION_DISLIKES} AS dislike_count,"
        f" {INSTALL_COUNT} AS install_count, {DOWNLOAD_COUNT} AS download_count"
        " FROM kinemium_packages p WHERE p.review_status = 'pending'"
    )
    args: list[Any] = []
    if cursor_submitted is not None and cursor_id is not None:
        sql += (
            " AND (p.submitted_at > %s OR"
            " (p.submitted_at = %s AND p.package_id > %s))"
        )
        args.extend([cursor_submitted, cursor_submitted, cursor_id])
    sql += " ORDER BY p.submitted_at ASC, p.package_id ASC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _pending_version_reviews(
    txn: Any,
    limit: int,
    cursor_submitted: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    columns = ", ".join("v." + column.strip() for column in VERSION_COLUMNS.split(","))
    sql = (
        f"SELECT {columns}, p.package_id, p.namespace, p.name,"
        " p.owner_user_id, p.review_status"
        " FROM kinemium_package_versions v"
        " JOIN kinemium_packages p ON p.package_id = v.package_id"
        " WHERE v.review_status = 'pending' AND p.archived_at IS NULL"
    )
    args: list[Any] = []
    if cursor_submitted is not None and cursor_id is not None:
        sql += (
            " AND (v.submitted_at > %s OR"
            " (v.submitted_at = %s AND v.version_id > %s))"
        )
        args.extend([cursor_submitted, cursor_submitted, cursor_id])
    sql += " ORDER BY v.submitted_at ASC, v.version_id ASC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _admin_packages(
    txn: Any,
    query: str | None,
    status: str | None,
    limit: int,
    cursor_updated: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    latest = (
        "(SELECT v.version FROM kinemium_package_versions v"
        " WHERE v.package_id = p.package_id AND v.yanked = FALSE"
        " ORDER BY v.published_at DESC, v.version DESC LIMIT 1)"
    )
    sql = (
        f"SELECT {', '.join('p.' + c.strip() for c in PACKAGE_COLUMNS.split(','))},"
        f" {latest} AS latest_version,"
        f" {REACTION_LIKES} AS like_count, {REACTION_DISLIKES} AS dislike_count,"
        f" {INSTALL_COUNT} AS install_count, {DOWNLOAD_COUNT} AS download_count"
        " FROM kinemium_packages p WHERE 1=1"
    )
    args: list[Any] = []
    if query is not None:
        pattern = "%" + _escape_like(query.lower()) + "%"
        sql += (
            " AND (LOWER(p.namespace) LIKE %s ESCAPE '\\'"
            " OR LOWER(p.name) LIKE %s ESCAPE '\\'"
            " OR LOWER(p.description) LIKE %s ESCAPE '\\'"
            " OR LOWER(p.owner_user_id) LIKE %s ESCAPE '\\')"
        )
        args.extend([pattern, pattern, pattern, pattern])
    if status is not None:
        sql += " AND p.review_status = %s"
        args.append(status)
    if cursor_updated is not None and cursor_id is not None:
        sql += (
            " AND (p.updated_at < %s OR"
            " (p.updated_at = %s AND p.package_id < %s))"
        )
        args.extend([cursor_updated, cursor_updated, cursor_id])
    sql += " ORDER BY p.updated_at DESC, p.package_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _available_versions(
    txn: Any, namespace: str, name: str, viewer: str | None
) -> tuple[str | None, list[tuple[str, SemVer]]]:
    txn.execute(
        "SELECT p.package_id, v.version FROM kinemium_packages p"
        " JOIN kinemium_package_versions v ON v.package_id = p.package_id"
        " WHERE p.namespace = %s AND p.name = %s AND v.yanked = FALSE"
        " AND v.review_status = 'approved'"
        " AND p.archived_at IS NULL"
        " AND (p.visibility != 'private' OR p.owner_user_id = %s)"
        " AND (p.review_status = 'approved' OR p.owner_user_id = %s)"
        " ORDER BY v.published_at DESC, v.version DESC",
        (namespace, name, viewer, viewer),
    )
    rows = txn.fetchall()
    if not rows:
        return None, []
    package_id = rows[0][0]
    versions: list[tuple[str, SemVer]] = []
    for _package_id, raw_version in rows:
        try:
            versions.append((raw_version, parse_semver(raw_version)))
        except SemVerError:
            continue
    return package_id, versions


def _resolve(
    txn: Any,
    requirements: list[tuple[str, str, Constraint]],
    maximum: int,
    viewer: str | None,
) -> list[tuple[str, str, str, list[tuple[str, str, str]]]]:
    constraints: dict[tuple[str, str], list[Constraint]] = {}
    queue: deque[tuple[str, str]] = deque()
    for namespace, name, constraint in requirements:
        key = (namespace, name)
        constraints.setdefault(key, []).append(constraint)
        queue.append(key)
    selected: dict[tuple[str, str], tuple[str, str, str, list[tuple[str, str, str]]]] = {}
    iterations = 0
    while queue:
        iterations += 1
        if iterations > maximum * 20:
            raise conflict("Dependency graph is too complex.")
        namespace, name = queue.popleft()
        key = (namespace, name)
        package_id, versions = _available_versions(txn, namespace, name, viewer)
        if package_id is None:
            raise conflict(
                "Package is not available.",
                {"reason": "unresolved", "namespace": namespace, "name": name},
            )
        matching = [
            (raw, parsed)
            for raw, parsed in versions
            if all(constraint.matches(parsed) for constraint in constraints[key])
        ]
        if not matching:
            raise conflict(
                "No package version satisfies the constraints.",
                {"reason": "unresolved", "namespace": namespace, "name": name},
            )
        best_raw, _best = max(matching, key=lambda item: item[1].version)
        previous = selected.get(key)
        if previous is not None and previous[2] == best_raw:
            continue
        dependencies: list[tuple[str, str, str]] = []
        for dependency_namespace, dependency_name, constraint_text in _version_dependencies(
            txn, package_id, best_raw
        ):
            try:
                dependency_constraint = parse_constraint(constraint_text)
            except ConstraintError:
                raise conflict(
                    "Package metadata contains an invalid dependency constraint."
                ) from None
            dependencies.append(
                (
                    dependency_namespace,
                    dependency_name,
                    constraint_text,
                )
            )
            dependency_key = (dependency_namespace, dependency_name)
            constraints.setdefault(dependency_key, []).append(dependency_constraint)
            queue.append(dependency_key)
        selected[key] = (namespace, name, best_raw, dependencies)
        if len(selected) > maximum:
            raise conflict(
                "Dependency resolution exceeded the configured package limit.",
                {"reason": "quota_exceeded"},
            )
    result = sorted(selected.values(), key=lambda item: (item[0], item[1]))
    return result



def _version_page(
    txn: Any,
    package_id: str,
    limit: int,
    cursor_published: int | None,
    cursor_id: str | None,
    include_yanked: bool,
    include_unapproved: bool,
) -> tuple[list[tuple], bool]:
    sql = (
        f"SELECT {VERSION_COLUMNS} FROM kinemium_package_versions"
        " WHERE package_id = %s"
    )
    args: list[Any] = [package_id]
    if not include_yanked:
        sql += " AND yanked = FALSE"
    if not include_unapproved:
        sql += " AND review_status = 'approved'"
    if cursor_published is not None and cursor_id is not None:
        sql += (
            " AND (published_at < %s OR"
            " (published_at = %s AND version_id < %s))"
        )
        args.extend([cursor_published, cursor_published, cursor_id])
    sql += " ORDER BY published_at DESC, version_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _set_readme(
    txn: Any, package: tuple, readme: str, now: int
) -> tuple:
    txn.execute(
        "UPDATE kinemium_packages SET readme = %s, updated_at = %s"
        " WHERE package_id = %s",
        (readme, now, package[0]),
    )
    updated = _package_row(txn, package[1], package[2])
    if updated is None:
        raise not_found("Package not found.")
    return updated


def _set_archive(
    txn: Any,
    package: tuple,
    archived: bool,
    reason: str,
    actor: str,
    now: int,
) -> tuple:
    txn.execute(
        "UPDATE kinemium_packages SET archived_at = %s, archived_by = %s,"
        " archive_reason = %s, updated_at = %s WHERE package_id = %s",
        (now if archived else None, actor if archived else None, reason if archived else "", now, package[0]),
    )
    updated = _package_row(txn, package[1], package[2])
    if updated is None:
        raise not_found("Package not found.")
    return updated


def _set_changelog(
    txn: Any,
    package: tuple,
    version: str,
    changelog: str,
    now: int,
    for_update: str,
) -> tuple:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    row = _version_row(txn, locked[0], version)
    if row is None:
        raise not_found("Package version not found.")
    txn.execute(
        "UPDATE kinemium_package_versions SET changelog = %s"
        " WHERE package_id = %s AND version = %s",
        (changelog, locked[0], version),
    )
    txn.execute(
        "UPDATE kinemium_packages SET updated_at = %s WHERE package_id = %s",
        (now, locked[0]),
    )
    updated = _version_row(txn, locked[0], version)
    if updated is None:
        raise not_found("Package version not found.")
    return updated


def _dependents(
    txn: Any,
    namespace: str,
    name: str,
    limit: int,
    cursor_namespace: str | None,
    cursor_name: str | None,
    cursor_version: str | None,
) -> tuple[list[tuple], bool]:
    sql = (
        "SELECT p.package_id, p.namespace, p.name, d.version, d.constraint_text"
        " FROM kinemium_package_dependencies d"
        " JOIN kinemium_packages p ON p.package_id = d.package_id"
        " WHERE d.dependency_namespace = %s AND d.dependency_name = %s"
        " AND p.visibility != 'private' AND p.review_status = 'approved'"
        " AND p.archived_at IS NULL"
    )
    args: list[Any] = [namespace, name]
    if cursor_namespace is not None and cursor_name is not None and cursor_version is not None:
        sql += (
            " AND (p.namespace > %s OR (p.namespace = %s AND p.name > %s)"
            " OR (p.namespace = %s AND p.name = %s AND d.version > %s))"
        )
        args.extend([
            cursor_namespace,
            cursor_namespace,
            cursor_name,
            cursor_namespace,
            cursor_name,
            cursor_version,
        ])
    sql += (
        " ORDER BY p.namespace ASC, p.name ASC, d.version ASC LIMIT %s"
    )
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _screenshots(txn: Any, package_id: str) -> list[tuple]:
    txn.execute(
        "SELECT screenshot_id, package_id, url, caption, sort_order, created_at"
        " FROM kinemium_package_screenshots WHERE package_id = %s"
        " ORDER BY sort_order ASC, created_at ASC, screenshot_id ASC",
        (package_id,),
    )
    return txn.fetchall()


def _add_screenshot(
    txn: Any,
    package: tuple,
    url: str,
    caption: str,
    maximum: int,
    now: int,
) -> tuple:
    txn.execute(
        "SELECT COUNT(*), COALESCE(MAX(sort_order), -1)"
        " FROM kinemium_package_screenshots WHERE package_id = %s",
        (package[0],),
    )
    count_row = txn.fetchone()
    count = int(count_row[0]) if count_row is not None else 0
    order = int(count_row[1]) + 1 if count_row is not None else 0
    if count >= maximum:
        raise conflict("Package screenshot limit reached.")
    screenshot_id = "shot_" + uuid.uuid4().hex
    txn.execute(
        "INSERT INTO kinemium_package_screenshots"
        " (screenshot_id, package_id, url, caption, sort_order, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (screenshot_id, package[0], url, caption, order, now),
    )
    return (screenshot_id, package[0], url, caption, order, now)


def _delete_screenshot(
    txn: Any, package: tuple, screenshot_id: str
) -> str | None:
    txn.execute(
        "SELECT url FROM kinemium_package_screenshots"
        " WHERE package_id = %s AND screenshot_id = %s",
        (package[0], screenshot_id),
    )
    row = txn.fetchone()
    if row is None:
        raise not_found("Screenshot not found.")
    txn.execute(
        "DELETE FROM kinemium_package_screenshots"
        " WHERE package_id = %s AND screenshot_id = %s",
        (package[0], screenshot_id),
    )
    return row[0]


def _package_events(
    txn: Any, package_id: str, since: int
) -> list[tuple]:
    txn.execute(
        "SELECT event_type, version, user_id, created_at"
        " FROM kinemium_package_events"
        " WHERE package_id = %s AND created_at >= %s"
        " ORDER BY created_at ASC",
        (package_id, since),
    )
    return txn.fetchall()


def _record_event(
    txn: Any,
    package_id: str,
    version: str | None,
    user_id: str | None,
    event_type: str,
    now: int,
) -> bool:
    txn.execute(
        "INSERT INTO kinemium_package_events"
        " (event_id, package_id, version, user_id, event_type, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        ("evt_" + uuid.uuid4().hex, package_id, version, user_id, event_type, now),
    )
    return True


def _my_packages(
    txn: Any,
    owner: str,
    query: str | None,
    review_status: str | None,
    visibility: str | None,
    archived: bool | None,
    limit: int,
    cursor_updated: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    latest = (
        "(SELECT v.version FROM kinemium_package_versions v"
        " WHERE v.package_id = p.package_id AND v.yanked = FALSE"
        " AND v.review_status = 'approved'"
        " ORDER BY v.published_at DESC, v.version DESC LIMIT 1)"
    )
    sql = (
        f"SELECT {', '.join('p.' + c.strip() for c in PACKAGE_COLUMNS.split(','))},"
        f" {latest} AS latest_version"
        " FROM kinemium_packages p WHERE p.owner_user_id = %s"
    )
    args: list[Any] = [owner]
    if query is not None:
        pattern = "%" + _escape_like(query.lower()) + "%"
        sql += (
            " AND (LOWER(p.namespace) LIKE %s ESCAPE '\'"
            " OR LOWER(p.name) LIKE %s ESCAPE '\')"
        )
        args.extend([pattern, pattern])
    if review_status is not None:
        sql += " AND p.review_status = %s"
        args.append(review_status)
    if visibility is not None:
        sql += " AND p.visibility = %s"
        args.append(visibility)
    if archived is True:
        sql += " AND p.archived_at IS NOT NULL"
    elif archived is False:
        sql += " AND p.archived_at IS NULL"
    if cursor_updated is not None and cursor_id is not None:
        sql += " AND (p.updated_at < %s OR (p.updated_at = %s AND p.package_id < %s))"
        args.extend([cursor_updated, cursor_updated, cursor_id])
    sql += " ORDER BY p.updated_at DESC, p.package_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _list_namespaces(txn: Any, owner: str) -> list[tuple]:
    txn.execute(
        "SELECT n.namespace, n.owner_user_id, n.created_at,"
        " (SELECT COUNT(*) FROM kinemium_packages p"
        "  WHERE p.namespace = n.namespace) AS package_count, n.space_id"
        " FROM kinemium_package_namespaces n WHERE n.owner_user_id = %s"
        " ORDER BY n.namespace ASC",
        (owner,),
    )
    return txn.fetchall()


def _namespace_owner(txn: Any, namespace: str) -> str | None:
    txn.execute(
        "SELECT owner_user_id FROM kinemium_package_namespaces"
        " WHERE namespace = %s",
        (namespace,),
    )
    row = txn.fetchone()
    return row[0] if row is not None else None


def _namespace_row(txn: Any, namespace: str) -> tuple | None:
    txn.execute(
        "SELECT n.namespace, n.owner_user_id, n.created_at,"
        " (SELECT COUNT(*) FROM kinemium_packages p"
        "  WHERE p.namespace = n.namespace) AS package_count, n.space_id"
        " FROM kinemium_package_namespaces n WHERE n.namespace = %s",
        (namespace,),
    )
    return txn.fetchone()


def _namespace_for_space(txn: Any, space_id: str) -> tuple | None:
    txn.execute(
        "SELECT n.namespace, n.owner_user_id, n.created_at,"
        " (SELECT COUNT(*) FROM kinemium_packages p"
        "  WHERE p.namespace = n.namespace) AS package_count, n.space_id"
        " FROM kinemium_package_namespaces n WHERE n.space_id = %s",
        (space_id,),
    )
    return txn.fetchone()


def _claim_namespace(
    txn: Any,
    namespace: str,
    owner: str,
    space_id: str | None,
    now: int,
    for_update: str,
) -> tuple:
    txn.execute(
        "INSERT INTO kinemium_profiles"
        " (user_id, bio, website, public_profile, public_marketplace,"
        " created_at, updated_at) VALUES (%s, '', NULL, TRUE, TRUE, %s, %s)"
        " ON CONFLICT (user_id) DO NOTHING",
        (owner, now, now),
    )
    txn.execute(
        "SELECT owner_user_id, space_id FROM kinemium_package_namespaces"
        " WHERE namespace = %s" + for_update,
        (namespace,),
    )
    row = txn.fetchone()
    if row is not None:
        if row[0] != owner:
            raise conflict("That namespace is already claimed.")
        if row[1] is not None and space_id is not None and row[1] != space_id:
            raise conflict("That namespace already belongs to a space.")
        if space_id is not None and row[1] is None:
            txn.execute(
                "SELECT namespace FROM kinemium_package_namespaces"
                " WHERE space_id = %s AND namespace != %s",
                (space_id, namespace),
            )
            if txn.fetchone() is not None:
                raise conflict("That space already owns a package namespace.")
            txn.execute(
                "UPDATE kinemium_package_namespaces SET space_id = %s"
                " WHERE namespace = %s",
                (space_id, namespace),
            )
            txn.execute(
                "UPDATE kinemium_packages SET space_id = %s, updated_at = %s"
                " WHERE namespace = %s",
                (space_id, now, namespace),
            )
        existing = _namespace_row(txn, namespace)
        if existing is None:
            raise not_found("Namespace not found.")
        return existing
    if space_id is not None:
        txn.execute(
            "SELECT namespace FROM kinemium_package_namespaces"
            " WHERE space_id = %s",
            (space_id,),
        )
        if txn.fetchone() is not None:
            raise conflict("That space already owns a package namespace.")
    txn.execute(
        "INSERT INTO kinemium_package_namespaces"
        " (namespace, owner_user_id, space_id, created_at)"
        " VALUES (%s, %s, %s, %s)",
        (namespace, owner, space_id, now),
    )
    created = _namespace_row(txn, namespace)
    if created is None:
        raise not_found("Namespace not found.")
    return created


def _transfer_namespace(
    txn: Any, namespace: str, owner: str, now: int, for_update: str
) -> tuple:
    txn.execute(
        "SELECT owner_user_id FROM kinemium_package_namespaces"
        " WHERE namespace = %s" + for_update,
        (namespace,),
    )
    row = txn.fetchone()
    if row is None:
        raise not_found("Namespace not found.")
    txn.execute(
        "UPDATE kinemium_package_namespaces SET owner_user_id = %s"
        " WHERE namespace = %s",
        (owner, namespace),
    )
    txn.execute(
        "UPDATE kinemium_packages SET owner_user_id = %s, updated_at = %s"
        " WHERE namespace = %s",
        (owner, now, namespace),
    )
    updated = _namespace_row(txn, namespace)
    if updated is None:
        raise not_found("Namespace not found.")
    return updated



class PackageService(Service):
    def register(self, resource: Any) -> None:
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/packages$")],
            self.publish_package,
            "KinemiumPackagesServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/packages/resolve$")],
            self.resolve_packages,
            "KinemiumPackageResolveServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages/toreview$")],
            self.list_review_queue,
            "KinemiumPackageReviewQueueServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/admin/package-versions$")],
            self.list_pending_versions,
            "KinemiumAdminPackageVersionsReviewServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/admin/packages$")],
            self.list_admin_packages,
            "KinemiumAdminPackagesServlet",
        )
        resource.register_paths(
            "PATCH",
            [re.compile(r"^/_kinemium/v1/admin/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)$")],
            self.admin_patch_package,
            "KinemiumAdminPackageServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/admin/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)$")],
            self.admin_delete_package,
            "KinemiumAdminPackageServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/admin/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/versions$")],
            self.list_admin_versions,
            "KinemiumAdminPackageVersionsServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/admin/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/versions$")],
            self.admin_add_version,
            "KinemiumAdminPackageVersionsServlet",
        )
        resource.register_paths(
            "PATCH",
            [re.compile(r"^/_kinemium/v1/admin/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/versions/(?P<version>[^/]+)$")],
            self.admin_patch_version,
            "KinemiumAdminPackageVersionServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/admin/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/versions/(?P<version>[^/]+)$")],
            self.admin_delete_version,
            "KinemiumAdminPackageVersionServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/review$")],
            self.review_package,
            "KinemiumPackageReviewServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/admin/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/versions/(?P<version>[^/]+)/review$")],
            self.review_version,
            "KinemiumAdminPackageVersionReviewServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/submit$")],
            self.submit_package,
            "KinemiumPackageSubmitServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/(?P<asset>icon|banner)$")],
            self.post_package_image,
            "KinemiumPackageImageServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/(?P<asset>icon|banner)$")],
            self.delete_package_image,
            "KinemiumPackageImageServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/versions/(?P<version>[^/]+)/download$")],
            self.download_version,
            "KinemiumPackageDownloadServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/versions/(?P<version>[^/]+)/yank$")],
            self.yank_version,
            "KinemiumPackageVersionServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/versions/(?P<version>[^/]+)$")],
            self.get_version,
            "KinemiumPackageVersionServlet",
        )
        resource.register_paths(
            "PATCH",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)$")],
            self.patch_package,
            "KinemiumPackageServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)$")],
            self.get_package,
            "KinemiumPackageServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/versions$")],
            self.list_versions,
            "KinemiumPackageVersionsServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/manifest$")],
            self.get_manifest,
            "KinemiumPackageManifestServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/readme$")],
            self.get_readme,
            "KinemiumPackageReadmeServlet",
        )
        resource.register_paths(
            "PUT",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/readme$")],
            self.put_readme,
            "KinemiumPackageReadmeServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/versions/(?P<version>[^/]+)/changelog$")],
            self.get_changelog,
            "KinemiumPackageChangelogServlet",
        )
        resource.register_paths(
            "PUT",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/versions/(?P<version>[^/]+)/changelog$")],
            self.put_changelog,
            "KinemiumPackageChangelogServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/archive$")],
            self.archive_package,
            "KinemiumPackageArchiveServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/restore$")],
            self.restore_package,
            "KinemiumPackageRestoreServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)$")],
            self.delete_owned_package,
            "KinemiumPackageServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/dependents$")],
            self.list_dependents,
            "KinemiumPackageDependentsServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/stats$")],
            self.get_stats,
            "KinemiumPackageStatsServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/screenshots$")],
            self.list_screenshots,
            "KinemiumPackageScreenshotsServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/screenshots$")],
            self.add_screenshot,
            "KinemiumPackageScreenshotsServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/screenshots/(?P<screenshot_id>[^/]+)$")],
            self.delete_screenshot,
            "KinemiumPackageScreenshotServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/me/packages$")],
            self.list_my_packages,
            "KinemiumMyPackagesServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/namespaces$")],
            self.list_namespaces,
            "KinemiumNamespacesServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/namespaces$")],
            self.claim_namespace,
            "KinemiumNamespacesServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/namespaces/(?P<namespace>[^/]+)$")],
            self.get_namespace,
            "KinemiumNamespaceServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/admin/namespaces/(?P<namespace>[^/]+)/transfer$")],
            self.admin_transfer_namespace,
            "KinemiumAdminNamespaceTransferServlet",
        )

    def _package_payload(self, row: tuple) -> dict[str, Any]:
        return package_payload(row)

    def _version_payload(
        self, row: tuple, dependencies: list[tuple]
    ) -> dict[str, Any]:
        return version_payload(row, dependencies)

    async def _package_and_versions(
        self, namespace: str, name: str
    ) -> tuple[tuple, list[tuple], dict[str, list[tuple]]]:
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        versions = await self.db.run(
            "kinemium_get_package_versions", _version_rows, package[0]
        )
        dependency_rows = await self.db.run(
            "kinemium_get_package_dependencies", _dependencies, package[0]
        )
        grouped: dict[str, list[tuple]] = {}
        for dependency in dependency_rows:
            grouped.setdefault(dependency[0], []).append(dependency[1:])
        return package, versions, grouped

    def _parse_manifest(
        self, fields: dict[str, str]
    ) -> tuple[
        dict[str, Any],
        str,
        str,
        str,
        str | None,
        str | None,
        str | None,
        str | None,
        list[tuple[str, str, str]],
        str | None,
        str | None,
        list[str] | None,
        str | None,
        str | None,
    ]:
        raw_manifest = fields.get("manifest")
        if raw_manifest is not None:
            try:
                manifest = json.loads(raw_manifest)
            except json.JSONDecodeError:
                raise invalid_request("manifest must be valid JSON.") from None
            if not isinstance(manifest, dict):
                raise invalid_request("manifest must be a JSON object.")
        else:
            manifest = {}
            for key in (
                "namespace",
                "name",
                "version",
                "description",
                "license",
                "repository",
                "engineConstraint",
                "entrypoint",
                "type",
                "visibility",
                "spaceId",
            ):
                if key in fields:
                    manifest[key] = fields[key]
            if "dependencies" in fields:
                try:
                    manifest["dependencies"] = json.loads(fields["dependencies"])
                except json.JSONDecodeError:
                    raise invalid_request("dependencies must be valid JSON.") from None
            if "tags" in fields:
                try:
                    manifest["tags"] = json.loads(fields["tags"])
                except json.JSONDecodeError:
                    raise invalid_request("tags must be valid JSON.") from None
        validate_json_value(manifest, MAX_MANIFEST_BYTES)
        name = require_manifest_str(manifest, "name", 1, 64)
        validate_slug(name, "name")
        version = require_manifest_str(manifest, "version", 1, 128)
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        description = optional_manifest_str(manifest, "description", 4000) or ""
        license_name = optional_manifest_str(manifest, "license", 128)
        repository = optional_manifest_str(manifest, "repository", 512)
        if repository is not None:
            repository = validate_website(repository)
        engine_constraint = optional_manifest_str(
            manifest, "engineConstraint", 512
        )
        if engine_constraint is not None:
            try:
                parse_constraint(engine_constraint)
            except ConstraintError as error:
                raise invalid_request(str(error)) from None
        entrypoint = optional_manifest_str(manifest, "entrypoint", 255)
        if entrypoint is not None:
            entrypoint = validate_relative_path(entrypoint, "entrypoint")
        dependencies = self._parse_dependencies(manifest.get("dependencies"))
        namespace = optional_manifest_str(manifest, "namespace", 64)
        item_type = optional_manifest_str(manifest, "type", 16)
        if item_type is not None and item_type not in ITEM_TYPES:
            raise invalid_request("Unknown package type.")
        tags = (
            validate_tags(manifest.get("tags"), self.config.max_listing_tags)
            if "tags" in manifest
            else None
        )
        visibility = optional_manifest_str(manifest, "visibility", 16)
        if visibility is not None and visibility not in VISIBILITIES:
            raise invalid_request("visibility must be public, unlisted, or private.")
        space_id = optional_manifest_str(manifest, "spaceId", 255)
        if space_id is not None:
            validate_room_id(space_id)
        return (
            manifest,
            name,
            version,
            description,
            license_name,
            repository,
            engine_constraint,
            entrypoint,
            dependencies,
            namespace,
            item_type,
            tags,
            visibility,
            space_id,
        )

    def _parse_dependencies(self, value: Any) -> list[tuple[str, str, str]]:
        items: list[tuple[str, str, str]] = []
        if value is None:
            return items
        if isinstance(value, dict):
            for key, constraint in value.items():
                if not isinstance(key, str) or not isinstance(constraint, str):
                    raise invalid_request(
                        "dependencies must map package names to constraints."
                    )
                namespace, separator, name = key.partition("/")
                if not separator:
                    raise invalid_request(
                        "Dependency keys must use namespace/name format."
                    )
                items.append((namespace, name, constraint))
        elif isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    raise invalid_request("dependencies must contain objects.")
                dependency_namespace = item.get("namespace")
                dependency_name = item.get("name")
                dependency_constraint = item.get("constraint")
                if (
                    not isinstance(dependency_namespace, str)
                    or not isinstance(dependency_name, str)
                    or not isinstance(dependency_constraint, str)
                ):
                    raise invalid_request(
                        "Dependencies require namespace, name, and constraint strings."
                    )
                items.append(
                    (dependency_namespace, dependency_name, dependency_constraint)
                )
        else:
            raise invalid_request("dependencies must be an object or array.")
        if len(items) > self.config.max_dependency_count:
            raise invalid_request("Too many dependencies.")
        seen: set[tuple[str, str]] = set()
        result: list[tuple[str, str, str]] = []
        for namespace, name, constraint in items:
            validate_slug(namespace, "dependency namespace")
            validate_slug(name, "dependency name")
            try:
                parse_constraint(constraint)
            except ConstraintError as error:
                raise invalid_request(str(error)) from None
            key = (namespace, name)
            if key in seen:
                raise invalid_request("Dependencies must not contain duplicates.")
            seen.add(key)
            result.append((namespace, name, constraint))
        return result

    @idempotent
    async def publish_package(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        fields, archive, filename = parse_multipart(
            request, self.config.max_json_body_bytes
        )
        if archive is None:
            raise invalid_request("An archive file is required.")
        if len(archive) > self.config.max_archive_bytes:
            raise payload_too_large(
                "The uploaded file exceeds the configured limit."
            )
        archive_kind = validate_archive(
            archive,
            filename,
            self.config.max_archive_entries,
            self.config.max_archive_uncompressed_bytes,
        )
        if archive_kind is None:
            raise invalid_request("Packages must be ZIP or tar archives.")
        (
            manifest,
            name,
            version,
            description,
            license_name,
            repository,
            engine_constraint,
            entrypoint,
            dependencies,
            manifest_namespace,
            item_type,
            tags,
            visibility,
            manifest_space_id,
        ) = self._parse_manifest(fields)
        default_namespace = _namespace_for_user(caller.user_id)
        namespace = manifest_namespace or default_namespace
        if manifest_space_id is not None:
            bound = await self.db.run(
                "kinemium_namespace_for_space",
                _namespace_for_space,
                manifest_space_id,
            )
            if bound is None:
                raise conflict("That space has not claimed a package namespace.")
            if manifest_namespace is not None and manifest_namespace != bound[0]:
                raise forbidden("That namespace does not belong to that space.")
            if not await self.can_manage_space(manifest_space_id, caller):
                raise forbidden(
                    "You do not have permission to publish for that space."
                )
            namespace = bound[0]
            space_id = manifest_space_id
        else:
            namespace_row = await self.db.run(
                "kinemium_get_namespace", _namespace_row, namespace
            )
            if namespace_row is not None and namespace_row[4] is not None:
                space_id = namespace_row[4]
                if not await self.can_manage_space(space_id, caller):
                    raise forbidden(
                        "You do not have permission to publish for that space."
                    )
            else:
                space_id = None
                if (
                    manifest_namespace is not None
                    and namespace != default_namespace
                    and (
                        namespace_row is None
                        or namespace_row[1] != caller.user_id
                    )
                ):
                    raise forbidden("You do not own that package namespace.")
        storage_key = self.storage.new_key(
            "packages", f"{namespace}/{name}/{version}"
        )
        digest, size = await self.storage.write(storage_key, archive)
        try:
            package = await self.db.run(
                "kinemium_create_package",
                _create_package,
                caller.user_id,
                namespace,
                name,
                description,
                license_name,
                repository,
                engine_constraint,
                item_type,
                tags,
                visibility,
                space_id,
                now_ms(),
                self.db.for_update(),
            )
            row = await self.db.run(
                "kinemium_publish_version",
                _publish_version,
                package,
                version,
                self.db.json(manifest),
                entrypoint,
                engine_constraint,
                digest,
                storage_key,
                size,
                dependencies,
                self.config.max_package_versions,
                now_ms(),
                self.db.for_update(),
            )
        except Exception:
            await self.storage.delete(storage_key)
            raise
        published: tuple | None = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if published is None:
            raise not_found("Package not found.")
        dependencies = await self.db.run(
            "kinemium_get_version_dependencies",
            _version_dependencies,
            published[0],
            version,
        )
        return 201, {
            "package": self._package_payload(package),
            "version": self._version_payload(row, dependencies),
            "archive": {
                "format": archive_kind,
                "filename": sanitize_filename(filename, f"{name}-{version}.zip"),
                "size": size,
                "digest": digest,
            },
        }

    async def get_package(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        package, versions, grouped = await self._package_and_versions(
            namespace, name
        )
        is_admin = await self._caller_is_admin(caller)
        manages = (
            caller is not None
            and not is_admin
            and await self.can_manage_package(package, caller)
        )
        if not package_is_visible(package, caller, is_admin, manages):
            raise not_found("Package not found.")
        is_owner = manages or (caller is not None and caller.user_id == package[3])
        visible_versions = [
            row
            for row in versions
            if version_is_visible(row, package, caller, is_admin, manages)
        ]
        if (
            not is_owner
            and not is_admin
            and not any(
                row[12] == "approved" and not row[8] for row in visible_versions
            )
        ):
            raise not_found("Package not found.")
        latest = next(
            (
                row
                for row in visible_versions
                if row[12] == "approved" and not row[8]
            ),
            None,
        )
        payload = self._package_payload(package)
        payload["latestVersion"] = latest[1] if latest is not None else None
        payload["versions"] = [
            self._version_payload(row, grouped.get(row[1], []))
            for row in visible_versions
        ]
        viewer = caller.user_id if caller is not None else None
        (
            payload["reactions"],
            comments,
            payload["commentCount"],
            payload["installCount"],
            payload["downloadCount"],
        ) = await self.db.run(
            "kinemium_package_social",
            lambda txn: (
                reaction_summary(txn, package[0], viewer),
                recent_comments(txn, package[0]),
                comment_count(txn, package[0]),
                installation_count(txn, package[0]),
                download_count(txn, package[0]),
            ),
        )
        payload["comments"] = [comment_payload(row) for row in comments]
        return 200, payload

    async def get_version(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        version: str,
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        is_admin = await self._caller_is_admin(caller)
        manages = (
            package is not None
            and caller is not None
            and not is_admin
            and await self.can_manage_package(package, caller)
        )
        if package is None or not package_is_visible(
            package, caller, is_admin, manages
        ):
            raise not_found("Package not found.")
        row = await self.db.run(
            "kinemium_get_version", _version_row, package[0], version
        )
        if row is None or not version_is_visible(
            row, package, caller, is_admin, manages
        ):
            raise not_found("Package version not found.")
        dependencies = await self.db.run(
            "kinemium_get_version_dependencies", _version_dependencies, package[0], version
        )
        viewer = caller.user_id if caller is not None else None
        (
            reactions,
            comments,
            comments_total,
            installs,
            downloads,
        ) = await self.db.run(
            "kinemium_package_social",
            lambda txn: (
                reaction_summary(txn, package[0], viewer),
                recent_comments(txn, package[0]),
                comment_count(txn, package[0]),
                installation_count(txn, package[0]),
                download_count(txn, package[0]),
            ),
        )
        return 200, {
            "package": self._package_payload(package),
            "version": self._version_payload(row, dependencies),
            "reactions": reactions,
            "comments": [comment_payload(comment) for comment in comments],
            "commentCount": comments_total,
            "installCount": installs,
            "downloadCount": downloads,
        }

    async def download_version(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        version: str,
    ) -> None:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        is_admin = await self._caller_is_admin(caller)
        manages = (
            package is not None
            and caller is not None
            and not is_admin
            and await self.can_manage_package(package, caller)
        )
        if package is None or not package_is_visible(
            package, caller, is_admin, manages
        ):
            raise not_found("Package not found.")
        row = await self.db.run(
            "kinemium_get_version", _version_row, package[0], version
        )
        if row is None or not version_is_visible(
            row, package, caller, is_admin, manages
        ):
            raise not_found("Package version not found.")
        data = await self.storage.read_verified(row[6], row[5])
        if data.startswith(b"PK\x03\x04"):
            filename = f"{name}-{version}.zip"
        elif data.startswith(b"\x1f\x8b"):
            filename = f"{name}-{version}.tar.gz"
        else:
            filename = f"{name}-{version}.tar"
        counts = await self.db.run(
            "kinemium_record_package_download",
            lambda txn: (
                record_download(txn, package[0]),
                _record_event(
                    txn,
                    package[0],
                    version,
                    caller.user_id if caller is not None else None,
                    "download",
                    now_ms(),
                ),
            ),
        )
        downloads = counts[0]
        extra_headers = {
            "X-Kinemium-Yanked": "true" if row[8] else "false",
            "X-Kinemium-Download-Count": str(downloads),
        }
        set_cors_response_headers(self.config, request)
        send_file(
            request,
            data,
            filename,
            content_type_for_filename(filename),
            row[5],
            extra_headers,
        )

    async def yank_version(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        version: str,
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        body = parse_json_body(request, self.config.max_json_body_bytes)
        yanked = body.get("yanked")
        if not isinstance(yanked, bool):
            raise invalid_request("yanked must be a boolean.")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        if not await self.can_manage_package(package, caller):
            raise forbidden("You do not own that package.")
        row = await self.db.run(
            "kinemium_set_yanked",
            _set_yanked,
            package,
            version,
            yanked,
            self.db.for_update(),
        )
        dependencies = await self.db.run(
            "kinemium_get_version_dependencies", _version_dependencies, package[0], version
        )
        return 200, {
            "package": self._package_payload(package),
            "version": self._version_payload(row, dependencies),
        }

    async def patch_package(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        if not await self.can_manage_package(package, caller):
            raise forbidden("You do not own that package.")
        return await self._apply_package_patch(request, package)

    async def _apply_package_patch(
        self, request: SynapseRequest, package: tuple
    ) -> tuple[int, dict[str, Any]]:
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {
            "description",
            "license",
            "repository",
            "deprecated",
            "type",
            "tags",
            "visibility",
        }
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown package fields: " + ", ".join(unknown))
        description = None
        license_name = None
        license_set = "license" in body
        repository = None
        repository_set = "repository" in body
        deprecated = None
        item_type = None
        tags = None
        visibility = None
        if "description" in body:
            value = body.get("description")
            if not isinstance(value, str):
                raise invalid_request("description must be a string.")
            description = check_str(value, "description", 0, 4000)
        if "license" in body:
            value = body.get("license")
            license_name = None if value is None else check_str(value, "license", 0, 128)
        if "repository" in body:
            value = body.get("repository")
            if value is not None and not isinstance(value, str):
                raise invalid_request("repository must be a string or null.")
            repository = validate_website(value)
        if "deprecated" in body:
            if not isinstance(body.get("deprecated"), bool):
                raise invalid_request("deprecated must be a boolean.")
            deprecated = body["deprecated"]
        if "type" in body:
            value = body.get("type")
            if not isinstance(value, str):
                raise invalid_request("type must be a string.")
            item_type = check_str(value, "type", 1, 16)
            if item_type not in ITEM_TYPES:
                raise invalid_request("Unknown package type.")
        if "tags" in body:
            tags = validate_tags(body.get("tags"), self.config.max_listing_tags)
        if "visibility" in body:
            value = body.get("visibility")
            if not isinstance(value, str):
                raise invalid_request("visibility must be a string.")
            visibility = check_str(value, "visibility", 1, 16)
            if visibility not in VISIBILITIES:
                raise invalid_request("visibility must be public, unlisted, or private.")
        updated = await self.db.run(
            "kinemium_update_package",
            _update_package_metadata,
            package,
            description,
            license_name,
            license_set,
            repository,
            repository_set,
            deprecated,
            item_type,
            tags,
            visibility,
            now_ms(),
        )
        return 200, self._package_payload(updated)

    async def list_admin_packages(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        limit = self.page_limit(request)
        query = query_value(request, "q", None, 256)
        if query is not None:
            query = query.strip() or None
        status = query_value(request, "status", None, 32)
        if status in (None, "", "all"):
            status = None
        elif status not in ("pending", "approved", "rejected"):
            raise invalid_request("status must be pending, approved, or rejected.")
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_updated = None
        cursor_id = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_updated = cursor.get("updatedAt")
            cursor_id = cursor.get("packageId")
            if not isinstance(cursor_updated, int) or not isinstance(cursor_id, str):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_admin_packages",
            _admin_packages,
            query,
            status,
            limit,
            cursor_updated,
            cursor_id,
        )
        packages = []
        for row in rows:
            payload = package_payload(row[:32])
            payload["latestVersion"] = row[32]
            payload["reactions"] = reaction_payload(int(row[33]), int(row[34]))
            payload["installCount"] = int(row[35])
            payload["downloadCount"] = int(row[36])
            packages.append(payload)
        result: dict[str, Any] = {"packages": packages}
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"updatedAt": rows[-1][11], "packageId": rows[-1][0]}
            )
        return 200, result

    async def admin_patch_package(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        result = await self._apply_admin_package_patch(request, package, caller)
        if self.platform is not None:
            await self.platform.record_audit(
                caller.user_id,
                "package.admin_update",
                "package",
                package[0],
                {"namespace": namespace, "name": name},
            )
        return result

    async def _apply_admin_package_patch(
        self, request: SynapseRequest, package: tuple, caller: Caller
    ) -> tuple[int, dict[str, Any]]:
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {
            "description",
            "license",
            "repository",
            "engineConstraint",
            "deprecated",
            "deprecatedAt",
            "type",
            "tags",
            "visibility",
            "publishedAt",
            "reviewStatus",
            "reviewNote",
            "reviewedBy",
            "reviewedAt",
            "owner",
            "submittedAt",
            "createdAt",
            "updatedAt",
            "iconUrl",
            "bannerUrl",
            "readme",
            "archived",
            "archiveReason",
            "official",
        }
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown package fields: " + ", ".join(unknown))
        now = now_ms()
        changes: dict[str, Any] = {}
        if "description" in body:
            value = body.get("description")
            if not isinstance(value, str):
                raise invalid_request("description must be a string.")
            changes["description"] = check_str(value, "description", 0, 4000)
        if "license" in body:
            value = body.get("license")
            changes["license"] = (
                None if value is None else check_str(value, "license", 0, 128)
            )
        if "repository" in body:
            value = body.get("repository")
            if value is not None and not isinstance(value, str):
                raise invalid_request("repository must be a string or null.")
            changes["repository"] = validate_website(value)
        if "engineConstraint" in body:
            value = body.get("engineConstraint")
            if value is None:
                changes["engine_constraint"] = None
            elif isinstance(value, str):
                try:
                    parse_constraint(value)
                except ConstraintError as error:
                    raise invalid_request(str(error)) from None
                changes["engine_constraint"] = value
            else:
                raise invalid_request(
                    "engineConstraint must be a string or null."
                )
        if "type" in body:
            value = body.get("type")
            if not isinstance(value, str):
                raise invalid_request("type must be a string.")
            item_type = check_str(value, "type", 1, 16)
            if item_type not in ITEM_TYPES:
                raise invalid_request("Unknown package type.")
            changes["item_type"] = item_type
        if "tags" in body:
            changes["tags"] = validate_tags(
                body.get("tags"), self.config.max_listing_tags
            )
        if "visibility" in body:
            value = body.get("visibility")
            if not isinstance(value, str):
                raise invalid_request("visibility must be a string.")
            visibility = check_str(value, "visibility", 1, 16)
            if visibility not in VISIBILITIES:
                raise invalid_request("visibility must be public, unlisted, or private.")
            changes["visibility"] = visibility
        if "owner" in body:
            value = body.get("owner")
            if not isinstance(value, str):
                raise invalid_request("owner must be a Matrix user ID.")
            changes["owner"] = validate_matrix_user_id(value)
        if "publishedAt" in body:
            changes["published_at"] = _optional_timestamp(body, "publishedAt")
        if "submittedAt" in body:
            submitted_at = _optional_timestamp(body, "submittedAt")
            changes["submitted_at"] = submitted_at if submitted_at is not None else 0
        if "createdAt" in body:
            created_at = _optional_timestamp(body, "createdAt")
            if created_at is None:
                raise invalid_request("createdAt must be a non-negative integer.")
            changes["created_at"] = created_at
        if "updatedAt" in body:
            updated_at = _optional_timestamp(body, "updatedAt")
            if updated_at is None:
                raise invalid_request("updatedAt must be a non-negative integer.")
            changes["updated_at"] = updated_at
        if "reviewedBy" in body:
            value = body.get("reviewedBy")
            changes["reviewed_by"] = (
                None if value is None else validate_matrix_user_id(value)
            )
        if "reviewedAt" in body:
            changes["reviewed_at"] = _optional_timestamp(body, "reviewedAt")
        if "reviewNote" in body:
            value = body.get("reviewNote")
            if value is None:
                changes["review_note"] = ""
            elif not isinstance(value, str):
                raise invalid_request("reviewNote must be a string or null.")
            else:
                changes["review_note"] = check_str(
                    value, "reviewNote", 0, self.config.max_review_length
                )
        if "reviewStatus" in body:
            value = body.get("reviewStatus")
            if not isinstance(value, str):
                raise invalid_request("reviewStatus must be a string.")
            status = check_str(value, "reviewStatus", 1, 32)
            if status not in ("pending", "approved", "rejected"):
                raise invalid_request(
                    "reviewStatus must be pending, approved, or rejected."
                )
            changes["review_status"] = status
            if status == "pending":
                changes["reviewed_by"] = None
                changes["reviewed_at"] = None
            elif status != package[17]:
                if "reviewed_by" not in changes:
                    changes["reviewed_by"] = caller.user_id
                if "reviewed_at" not in changes:
                    changes["reviewed_at"] = now
        if "deprecated" in body:
            if not isinstance(body.get("deprecated"), bool):
                raise invalid_request("deprecated must be a boolean.")
            deprecated = body["deprecated"]
            changes["deprecated"] = deprecated
            if deprecated != bool(package[8]):
                changes["deprecated_at"] = now if deprecated else None
        if "deprecatedAt" in body:
            changes["deprecated_at"] = _optional_timestamp(body, "deprecatedAt")
        if "iconUrl" in body:
            value = body.get("iconUrl")
            changes["icon_url"] = (
                None if value is None else validate_mxc_url(value, "iconUrl")
            )
        if "bannerUrl" in body:
            value = body.get("bannerUrl")
            changes["banner_url"] = (
                None if value is None else validate_mxc_url(value, "bannerUrl")
            )
        if "readme" in body:
            value = body.get("readme")
            if not isinstance(value, str):
                raise invalid_request("readme must be a string.")
            changes["readme"] = check_str(value, "readme", 0, 200000)
        if "archived" in body:
            if not isinstance(body.get("archived"), bool):
                raise invalid_request("archived must be a boolean.")
            changes["archived_at"] = now if body["archived"] else None
            changes["archived_by"] = caller.user_id if body["archived"] else None
        if "archiveReason" in body:
            value = body.get("archiveReason")
            if not isinstance(value, str):
                raise invalid_request("archiveReason must be a string.")
            changes["archive_reason"] = check_str(value, "archiveReason", 0, 1000)
        if "official" in body:
            if not isinstance(body.get("official"), bool):
                raise invalid_request("official must be a boolean.")
            official = body["official"]
            changes["official"] = official
            if official != bool(package[29]):
                changes["official_at"] = now if official else None
                changes["official_by"] = caller.user_id if official else None
        updated = await self.db.run(
            "kinemium_admin_update_package",
            _admin_update_package,
            package,
            changes,
            now,
            self.db.for_update(),
        )
        return 200, self._package_payload(updated)

    async def list_admin_versions(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package, versions, grouped = await self._package_and_versions(
            namespace, name
        )
        return 200, {
            "package": self._package_payload(package),
            "versions": [
                self._version_payload(row, grouped.get(row[1], []))
                for row in versions
            ],
        }

    async def admin_add_version(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        fields, archive, filename = parse_multipart(
            request, self.config.max_json_body_bytes
        )
        if archive is None:
            raise invalid_request("An archive file is required.")
        if len(archive) > self.config.max_archive_bytes:
            raise payload_too_large(
                "The uploaded file exceeds the configured limit."
            )
        archive_kind = validate_archive(
            archive,
            filename,
            self.config.max_archive_entries,
            self.config.max_archive_uncompressed_bytes,
        )
        if archive_kind is None:
            raise invalid_request("Packages must be ZIP or tar archives.")
        manifest: dict[str, Any] = {}
        raw_manifest = fields.get("manifest")
        if raw_manifest is not None:
            try:
                parsed = json.loads(raw_manifest)
            except json.JSONDecodeError:
                raise invalid_request("manifest must be valid JSON.") from None
            if not isinstance(parsed, dict):
                raise invalid_request("manifest must be a JSON object.")
            manifest = parsed
        typed_version = fields.get("version")
        if typed_version is not None:
            if (
                "version" in manifest
                and manifest.get("version") != typed_version
            ):
                raise invalid_request("manifest version does not match version.")
            manifest["version"] = typed_version
        manifest.setdefault("name", name)
        manifest.setdefault("namespace", namespace)
        fields = dict(fields)
        fields["manifest"] = json.dumps(manifest, ensure_ascii=False)
        parsed_manifest = self._parse_manifest(fields)
        manifest_name = parsed_manifest[1]
        version = parsed_manifest[2]
        engine_constraint = parsed_manifest[6]
        entrypoint = parsed_manifest[7]
        dependencies = parsed_manifest[8]
        manifest_namespace = parsed_manifest[9]
        if manifest_name != name:
            raise invalid_request("manifest name does not match the package.")
        if manifest_namespace is not None and manifest_namespace != namespace:
            raise invalid_request(
                "manifest namespace does not match the package."
            )
        storage_key = self.storage.new_key(
            "packages", f"{namespace}/{name}/{version}"
        )
        digest, size = await self.storage.write(storage_key, archive)
        try:
            row = await self.db.run(
                "kinemium_admin_publish_version",
                _publish_version,
                package,
                version,
                self.db.json(manifest),
                entrypoint,
                engine_constraint,
                digest,
                storage_key,
                size,
                dependencies,
                self.config.max_package_versions,
                now_ms(),
                self.db.for_update(),
                "approved",
            )
        except Exception:
            await self.storage.delete(storage_key)
            raise
        published = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if published is None:
            raise not_found("Package not found.")
        dependency_rows = await self.db.run(
            "kinemium_get_version_dependencies",
            _version_dependencies,
            published[0],
            version,
        )
        if self.platform is not None:
            await self.platform.record_audit(
                caller.user_id,
                "package.version_admin_add",
                "package",
                published[0],
                {"namespace": namespace, "name": name, "version": version},
            )
        return 201, {
            "package": self._package_payload(published),
            "version": self._version_payload(row, dependency_rows),
            "archive": {
                "format": archive_kind,
                "filename": sanitize_filename(filename, f"{name}-{version}.zip"),
                "size": size,
                "digest": digest,
            },
        }

    async def admin_patch_version(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        version: str,
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {
            "yanked",
            "entrypoint",
            "engineConstraint",
            "manifest",
            "publishedAt",
            "dependencies",
            "changelog",
        }
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown version fields: " + ", ".join(unknown))
        changes: dict[str, Any] = {}
        if "yanked" in body:
            if not isinstance(body.get("yanked"), bool):
                raise invalid_request("yanked must be a boolean.")
            changes["yanked"] = body["yanked"]
        if "entrypoint" in body:
            value = body.get("entrypoint")
            changes["entrypoint"] = (
                None
                if value is None
                else validate_relative_path(value, "entrypoint")
            )
        if "engineConstraint" in body:
            value = body.get("engineConstraint")
            if value is None:
                changes["engine_constraint"] = None
            elif isinstance(value, str):
                try:
                    parse_constraint(value)
                except ConstraintError as error:
                    raise invalid_request(str(error)) from None
                changes["engine_constraint"] = value
            else:
                raise invalid_request(
                    "engineConstraint must be a string or null."
                )
        if "publishedAt" in body:
            changes["published_at"] = _optional_timestamp(body, "publishedAt")
        if "changelog" in body:
            value = body.get("changelog")
            if not isinstance(value, str):
                raise invalid_request("changelog must be a string.")
            changes["changelog"] = check_str(value, "changelog", 0, 20000)
        dependencies: list[tuple[str, str, str]] | None = None
        if "manifest" in body:
            value = body.get("manifest")
            if not isinstance(value, dict):
                raise invalid_request("manifest must be a JSON object.")
            validate_json_value(value, MAX_MANIFEST_BYTES)
            if (
                "version" in value
                and isinstance(value.get("version"), str)
                and value["version"] != version
            ):
                raise invalid_request("manifest version does not match.")
            if (
                "name" in value
                and isinstance(value.get("name"), str)
                and value["name"] != name
            ):
                raise invalid_request("manifest name does not match.")
            changes["manifest"] = self.db.json(value)
            if "dependencies" in value:
                dependencies = self._parse_dependencies(value.get("dependencies"))
        if "dependencies" in body:
            dependencies = self._parse_dependencies(body.get("dependencies"))
        row = await self.db.run(
            "kinemium_admin_update_version",
            _admin_update_version,
            package,
            version,
            changes,
            dependencies,
            now_ms(),
            self.db.for_update(),
        )
        dependency_rows = await self.db.run(
            "kinemium_get_version_dependencies",
            _version_dependencies,
            package[0],
            version,
        )
        updated_package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if updated_package is None:
            raise not_found("Package not found.")
        if self.platform is not None:
            await self.platform.record_audit(
                caller.user_id,
                "package.version_admin_update",
                "package",
                package[0],
                {"namespace": namespace, "name": name, "version": version},
            )
        return 200, {
            "package": self._package_payload(updated_package),
            "version": self._version_payload(row, dependency_rows),
        }

    async def admin_delete_version(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        version: str,
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        storage_key = await self.db.run(
            "kinemium_admin_delete_version",
            _admin_delete_version,
            package,
            version,
            now_ms(),
            self.db.for_update(),
        )
        try:
            await self.storage.delete(storage_key)
        except Exception:
            logger.warning(
                "Unable to delete package archive %s", storage_key, exc_info=True
            )
        updated_package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if updated_package is None:
            raise not_found("Package not found.")
        versions = await self.db.run(
            "kinemium_get_package_versions", _version_rows, package[0]
        )
        latest = next(
            (
                item
                for item in versions
                if not item[8] and item[12] == "approved"
            ),
            None,
        )
        payload = self._package_payload(updated_package)
        payload["latestVersion"] = latest[1] if latest is not None else None
        if self.platform is not None:
            await self.platform.record_audit(
                caller.user_id,
                "package.version_admin_delete",
                "package",
                package[0],
                {"namespace": namespace, "name": name, "version": version},
            )
        return 200, {
            "deleted": True,
            "namespace": namespace,
            "name": name,
            "version": version,
            "package": payload,
        }

    async def admin_delete_package(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        keys = await self.db.run(
            "kinemium_delete_package",
            _delete_package,
            package,
            self.db.for_update(),
        )
        for key in keys:
            try:
                await self.storage.delete(key)
            except Exception:
                logger.warning("Unable to delete package archive %s", key, exc_info=True)
        if self.platform is not None:
            await self.platform.record_audit(
                caller.user_id,
                "package.admin_delete",
                "package",
                package[0],
                {"namespace": namespace, "name": name},
            )
        return 200, {
            "deleted": True,
            "namespace": namespace,
            "name": name,
        }

    async def list_review_queue(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        limit = self.page_limit(request)
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_submitted = None
        cursor_id = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_submitted = cursor.get("submittedAt")
            cursor_id = cursor.get("packageId")
            if not isinstance(cursor_submitted, int) or not isinstance(cursor_id, str):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_list_pending_packages",
            _pending_reviews,
            limit,
            cursor_submitted,
            cursor_id,
        )
        packages = []
        for row in rows:
            payload = package_payload(row[:32])
            payload["latestVersion"] = row[32]
            payload["reactions"] = reaction_payload(int(row[33]), int(row[34]))
            payload["installCount"] = int(row[35])
            payload["downloadCount"] = int(row[36])
            packages.append(payload)
        result: dict[str, Any] = {"packages": packages}
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"submittedAt": rows[-1][21], "packageId": rows[-1][0]}
            )
        return 200, result

    async def list_pending_versions(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        limit = self.page_limit(request)
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_submitted = None
        cursor_id = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_submitted = cursor.get("submittedAt")
            cursor_id = cursor.get("versionId")
            if not isinstance(cursor_submitted, int) or not isinstance(cursor_id, str):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_list_pending_versions",
            _pending_version_reviews,
            limit,
            cursor_submitted,
            cursor_id,
        )
        versions = []
        for row in rows:
            payload = version_payload(row[:17], [])
            payload["package"] = {
                "packageId": row[17],
                "namespace": row[18],
                "name": row[19],
                "owner": row[20],
                "reviewStatus": row[21],
            }
            versions.append(payload)
        result: dict[str, Any] = {"versions": versions}
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"submittedAt": rows[-1][16], "versionId": rows[-1][0]}
            )
        return 200, result

    async def review_version(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        version: str,
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {"decision", "note"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown review fields: " + ", ".join(unknown))
        decision = body.get("decision")
        if decision not in ("approve", "reject"):
            raise invalid_request("decision must be approve or reject.")
        note = optional_str(body, "note", self.config.max_review_length) or ""
        if decision == "reject" and not note.strip():
            raise invalid_request("A rejection note is required.")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        status = "approved" if decision == "approve" else "rejected"
        row = await self.db.run(
            "kinemium_review_version",
            _set_version_review,
            package,
            version,
            status,
            note,
            caller.user_id,
            now_ms(),
            self.db.for_update(),
        )
        dependencies = await self.db.run(
            "kinemium_get_version_dependencies",
            _version_dependencies,
            package[0],
            version,
        )
        if self.platform is not None:
            await self.platform.record_audit(
                caller.user_id,
                "package.version_review",
                "package",
                package[0],
                {
                    "namespace": namespace,
                    "name": name,
                    "version": version,
                    "decision": decision,
                    "note": note,
                },
            )
            await self.platform.notify(
                package[3],
                "package.version_reviewed",
                caller.user_id,
                package[0],
                {
                    "namespace": namespace,
                    "name": name,
                    "version": version,
                    "decision": decision,
                    "note": note,
                },
            )
        return 200, {
            "package": self._package_payload(package),
            "version": self._version_payload(row, dependencies),
        }

    async def review_package(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {"decision", "note"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown review fields: " + ", ".join(unknown))
        decision = body.get("decision")
        if decision not in ("approve", "reject"):
            raise invalid_request("decision must be approve or reject.")
        note = optional_str(body, "note", self.config.max_review_length) or ""
        if decision == "reject" and not note.strip():
            raise invalid_request("A rejection note is required.")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        updated = await self.db.run(
            "kinemium_review_package",
            _set_review,
            package,
            "approved" if decision == "approve" else "rejected",
            note,
            caller.user_id,
            now_ms(),
            self.db.for_update(),
        )
        if self.platform is not None:
            await self.platform.record_audit(
                caller.user_id,
                "package.review",
                "package",
                package[0],
                {"decision": decision, "note": note},
            )
            await self.platform.notify(
                package[3],
                "package.reviewed",
                caller.user_id,
                package[0],
                {"namespace": namespace, "name": name, "decision": decision, "note": note},
            )
        return 200, {"package": self._package_payload(updated)}

    async def submit_package(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        if not await self.can_manage_package(package, caller):
            raise forbidden("You do not own that package.")
        updated = await self.db.run(
            "kinemium_submit_package",
            _submit_package,
            package,
            now_ms(),
            self.db.for_update(),
        )
        return 200, {"package": self._package_payload(updated)}

    async def post_package_image(
        self, request: SynapseRequest, namespace: str, name: str, asset: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        if not await self.can_manage_package(package, caller) and not (
            await self._caller_is_admin(caller)
        ):
            raise forbidden("You do not own that package.")
        image = read_image(request, self.config.max_media_bytes)
        image_url = await store_image(
            self.api, caller.user_id, image, "package-" + asset
        )
        column = "icon_url" if asset == "icon" else "banner_url"
        updated = await self.db.run(
            "kinemium_set_package_media",
            _set_package_media,
            package,
            column,
            image_url,
            now_ms(),
            self.db.for_update(),
        )
        return 200, {
            "package": self._package_payload(updated),
            "asset": {"type": asset, "url": image_url},
        }

    async def delete_package_image(
        self, request: SynapseRequest, namespace: str, name: str, asset: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        if not await self.can_manage_package(package, caller) and not (
            await self._caller_is_admin(caller)
        ):
            raise forbidden("You do not own that package.")
        column = "icon_url" if asset == "icon" else "banner_url"
        updated = await self.db.run(
            "kinemium_set_package_media",
            _set_package_media,
            package,
            column,
            None,
            now_ms(),
            self.db.for_update(),
        )
        return 200, {
            "package": self._package_payload(updated),
            "asset": {"type": asset, "url": None},
        }

    async def resolve_packages(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        requirements = require_list(
            body, "requirements", self.config.max_resolve_packages
        )
        parsed: list[tuple[str, str, Constraint]] = []
        for requirement in requirements:
            if not isinstance(requirement, dict):
                raise invalid_request("requirements must contain objects.")
            namespace = requirement.get("namespace")
            name = requirement.get("name")
            constraint_text = requirement.get("constraint", "*")
            if (
                not isinstance(namespace, str)
                or not isinstance(name, str)
                or not isinstance(constraint_text, str)
            ):
                raise invalid_request(
                    "Requirements need namespace, name, and constraint strings."
                )
            validate_slug(namespace, "namespace")
            validate_slug(name, "name")
            try:
                constraint = parse_constraint(constraint_text)
            except ConstraintError as error:
                raise invalid_request(str(error)) from None
            parsed.append((namespace, name, constraint))
        rows = await self.db.run(
            "kinemium_resolve_packages",
            _resolve,
            parsed,
            self.config.max_resolve_packages,
            caller.user_id if caller is not None else None,
        )
        return 200, {
            "packages": [
                {
                    "namespace": row[0],
                    "name": row[1],
                    "version": row[2],
                    "dependencies": [
                        {
                            "namespace": dependency[0],
                            "name": dependency[1],
                            "constraint": dependency[2],
                        }
                        for dependency in row[3]
                    ],
                }
                for row in rows
            ]
        }


    async def list_versions(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        is_admin = await self._caller_is_admin(caller)
        manages = (
            package is not None
            and caller is not None
            and not is_admin
            and await self.can_manage_package(package, caller)
        )
        if package is None or not package_is_visible(
            package, caller, is_admin, manages
        ):
            raise not_found("Package not found.")
        is_owner = manages or (caller is not None and caller.user_id == package[3])
        include_yanked = query_bool(request, "includeYanked", False)
        if include_yanked and not (is_admin or is_owner):
            raise forbidden("Only the owner can include yanked versions.")
        limit = self.page_limit(request)
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_published = None
        cursor_id = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_published = cursor.get("publishedAt")
            cursor_id = cursor.get("versionId")
            if not isinstance(cursor_published, int) or not isinstance(cursor_id, str):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_list_package_versions",
            _version_page,
            package[0],
            limit,
            cursor_published,
            cursor_id,
            include_yanked,
            is_admin or is_owner,
        )
        dependency_rows = await self.db.run(
            "kinemium_get_package_dependencies", _dependencies, package[0]
        )
        grouped: dict[str, list[tuple]] = {}
        for dependency in dependency_rows:
            grouped.setdefault(dependency[0], []).append(dependency[1:])
        result: dict[str, Any] = {
            "versions": [
                self._version_payload(row, grouped.get(row[1], [])) for row in rows
            ]
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"publishedAt": rows[-1][9], "versionId": rows[-1][0]}
            )
        return 200, result

    async def get_manifest(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        is_admin = await self._caller_is_admin(caller)
        manages = (
            package is not None
            and caller is not None
            and not is_admin
            and await self.can_manage_package(package, caller)
        )
        if package is None or not package_is_visible(
            package, caller, is_admin, manages
        ):
            raise not_found("Package not found.")
        version = query_value(request, "version", None, 128)
        if version is None:
            versions = await self.db.run(
                "kinemium_get_package_versions", _version_rows, package[0]
            )
            row = next(
                (
                    item
                    for item in versions
                    if not item[8] and item[12] == "approved"
                ),
                None,
            )
        else:
            try:
                parse_semver(version)
            except SemVerError as error:
                raise invalid_request(str(error)) from None
            row = await self.db.run(
                "kinemium_get_version", _version_row, package[0], version
            )
            if row is not None and not version_is_visible(
                row, package, caller, is_admin, manages
            ):
                row = None
        if row is None:
            raise not_found("Package version not found.")
        dependencies = await self.db.run(
            "kinemium_get_version_dependencies",
            _version_dependencies,
            package[0],
            row[1],
        )
        return 200, {
            "package": self._package_payload(package),
            "version": self._version_payload(row, dependencies),
            "manifest": json_loads(row[2]),
        }

    async def get_readme(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        package = await self.db.run(
            "kinemium_get_package_row", _package_row, namespace, name
        )
        is_admin = await self._caller_is_admin(caller)
        manages = (
            package is not None
            and caller is not None
            and not is_admin
            and await self.can_manage_package(package, caller)
        )
        if package is None or not package_is_visible(
            package, caller, is_admin, manages
        ):
            raise not_found("Package not found.")
        return 200, {
            "packageId": package[0],
            "namespace": package[1],
            "name": package[2],
            "readme": package[24],
            "updatedAt": package[11],
        }

    async def put_readme(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        package = await self._owned_package(namespace, name, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        unknown = sorted(set(body) - {"readme"})
        if unknown:
            raise invalid_request("Unknown readme fields: " + ", ".join(unknown))
        value = body.get("readme")
        if not isinstance(value, str):
            raise invalid_request("readme must be a string.")
        readme = check_str(value, "readme", 0, 200000)
        updated = await self.db.run(
            "kinemium_set_package_readme", _set_readme, package, readme, now_ms()
        )
        return 200, {"package": self._package_payload(updated), "readme": updated[24]}

    async def get_changelog(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        version: str,
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        package = await self.db.run(
            "kinemium_get_package_row", _package_row, namespace, name
        )
        is_admin = await self._caller_is_admin(caller)
        manages = (
            package is not None
            and caller is not None
            and not is_admin
            and await self.can_manage_package(package, caller)
        )
        if package is None or not package_is_visible(
            package, caller, is_admin, manages
        ):
            raise not_found("Package not found.")
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        row = await self.db.run(
            "kinemium_get_version", _version_row, package[0], version
        )
        if row is None or not version_is_visible(
            row, package, caller, is_admin, manages
        ):
            raise not_found("Package version not found.")
        return 200, {
            "packageId": package[0],
            "version": version,
            "changelog": row[11],
            "reviewStatus": row[12],
        }

    async def put_changelog(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        version: str,
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        package = await self._owned_package(namespace, name, caller)
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        body = parse_json_body(request, self.config.max_json_body_bytes)
        unknown = sorted(set(body) - {"changelog"})
        if unknown:
            raise invalid_request("Unknown changelog fields: " + ", ".join(unknown))
        value = body.get("changelog")
        if not isinstance(value, str):
            raise invalid_request("changelog must be a string.")
        changelog = check_str(value, "changelog", 0, 20000)
        row = await self.db.run(
            "kinemium_set_version_changelog",
            _set_changelog,
            package,
            version,
            changelog,
            now_ms(),
            self.db.for_update(),
        )
        return 200, {
            "version": self._version_payload(row, []),
            "changelog": row[11],
        }

    async def archive_package(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        package = await self._owned_package(namespace, name, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        reason = optional_str(body, "reason", 1000) or ""
        updated = await self.db.run(
            "kinemium_archive_package",
            _set_archive,
            package,
            True,
            reason,
            caller.user_id,
            now_ms(),
        )
        return 200, {"package": self._package_payload(updated), "archived": True}

    async def restore_package(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        package = await self._owned_package(namespace, name, caller)
        updated = await self.db.run(
            "kinemium_restore_package",
            _set_archive,
            package,
            False,
            "",
            caller.user_id,
            now_ms(),
        )
        return 200, {"package": self._package_payload(updated), "archived": False}

    async def delete_owned_package(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        package = await self._owned_package(namespace, name, caller)
        keys = await self.db.run(
            "kinemium_delete_package",
            _delete_package,
            package,
            self.db.for_update(),
        )
        for key in keys:
            try:
                await self.storage.delete(key)
            except Exception:
                logger.warning("Unable to delete package archive %s", key, exc_info=True)
        if self.platform is not None:
            await self.platform.record_audit(
                caller.user_id,
                "package.delete",
                "package",
                package[0],
                {"namespace": package[1], "name": package[2]},
            )
        return 200, {"deleted": True, "namespace": namespace, "name": name}

    async def list_dependents(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        package = await self.db.run(
            "kinemium_get_package_row", _package_row, namespace, name
        )
        is_admin = await self._caller_is_admin(caller)
        manages = (
            package is not None
            and caller is not None
            and not is_admin
            and await self.can_manage_package(package, caller)
        )
        if package is None or not package_is_visible(
            package, caller, is_admin, manages
        ):
            raise not_found("Package not found.")
        limit = self.page_limit(request)
        cursor_raw = query_value(request, "cursor", None, 2048)
        c_ns = c_name = c_version = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            c_ns, c_name, c_version = (
                cursor.get("namespace"),
                cursor.get("name"),
                cursor.get("version"),
            )
            if not all(isinstance(item, str) for item in (c_ns, c_name, c_version)):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_list_package_dependents",
            _dependents,
            namespace,
            name,
            limit,
            c_ns,
            c_name,
            c_version,
        )
        result: dict[str, Any] = {
            "dependents": [
                {
                    "packageId": row[0],
                    "namespace": row[1],
                    "name": row[2],
                    "version": row[3],
                    "constraint": row[4],
                }
                for row in rows
            ]
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"namespace": rows[-1][1], "name": rows[-1][2], "version": rows[-1][3]}
            )
        return 200, result

    async def list_screenshots(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        package = await self.db.run(
            "kinemium_get_package_row", _package_row, namespace, name
        )
        is_admin = await self._caller_is_admin(caller)
        manages = (
            package is not None
            and caller is not None
            and not is_admin
            and await self.can_manage_package(package, caller)
        )
        if package is None or not package_is_visible(
            package, caller, is_admin, manages
        ):
            raise not_found("Package not found.")
        rows = await self.db.run(
            "kinemium_list_package_screenshots", _screenshots, package[0]
        )
        return 200, {"screenshots": [_screenshot_payload(row) for row in rows]}

    async def add_screenshot(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        package = await self._owned_package(namespace, name, caller)
        caption = query_value(request, "caption", "", 200) or ""
        image = read_image(request, self.config.max_media_bytes)
        url = await store_image(
            self.api, caller.user_id, image, "package-screenshot"
        )
        row = await self.db.run(
            "kinemium_add_package_screenshot",
            _add_screenshot,
            package,
            url,
            caption,
            12,
            now_ms(),
        )
        return 201, {"screenshot": _screenshot_payload(row)}

    async def delete_screenshot(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        screenshot_id: str,
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        package = await self._owned_package(namespace, name, caller)
        await self.db.run(
            "kinemium_delete_package_screenshot",
            _delete_screenshot,
            package,
            screenshot_id,
        )
        return 200, {"deleted": True, "screenshotId": screenshot_id}

    async def get_stats(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        package = await self.db.run(
            "kinemium_get_package_row", _package_row, namespace, name
        )
        is_admin = await self._caller_is_admin(caller)
        manages = (
            package is not None
            and caller is not None
            and not is_admin
            and await self.can_manage_package(package, caller)
        )
        if package is None or not package_is_visible(
            package, caller, is_admin, manages
        ):
            raise not_found("Package not found.")
        days_value = query_int(request, "days", 1, 365, 30)
        assert days_value is not None
        since = now_ms() - days_value * 86400000
        events, installs, downloads = await self.db.run(
            "kinemium_package_stats",
            lambda txn: (
                _package_events(txn, package[0], since),
                installation_count(txn, package[0]),
                download_count(txn, package[0]),
            ),
        )
        buckets: dict[str, dict[str, int]] = {}
        totals = {"download": 0, "install": 0, "uninstall": 0, "update": 0}
        for event_type, _version, _user_id, created_at in events:
            kind = str(event_type)
            if kind not in totals:
                continue
            totals[kind] += 1
            day = time.strftime("%Y-%m-%d", time.gmtime(int(created_at) / 1000))
            bucket = buckets.setdefault(
                day, {"download": 0, "install": 0, "uninstall": 0, "update": 0}
            )
            bucket[kind] += 1
        return 200, {
            "packageId": package[0],
            "namespace": package[1],
            "name": package[2],
            "days": days_value,
            "totals": {
                "installs": installs,
                "downloads": downloads,
                "events": totals,
            },
            "series": [
                {
                    "day": day,
                    "download": values["download"],
                    "install": values["install"],
                    "uninstall": values["uninstall"],
                    "update": values["update"],
                }
                for day, values in sorted(buckets.items())
            ],
        }

    async def list_my_packages(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        limit = self.page_limit(request)
        query = query_value(request, "q", None, 100)
        if query is not None:
            query = query.strip() or None
        review_status = query_value(request, "status", None, 16)
        if review_status is not None and review_status not in (
            "pending",
            "approved",
            "rejected",
        ):
            raise invalid_request("status must be pending, approved, or rejected.")
        visibility = query_value(request, "visibility", None, 16)
        if visibility is not None and visibility not in VISIBILITIES:
            raise invalid_request("visibility must be public, unlisted, or private.")
        archived_raw = query_value(request, "archived", None, 8)
        archived = None if archived_raw is None else archived_raw.lower() == "true"
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_updated = None
        cursor_id = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_updated = cursor.get("updatedAt")
            cursor_id = cursor.get("packageId")
            if not isinstance(cursor_updated, int) or not isinstance(cursor_id, str):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_list_my_packages",
            _my_packages,
            caller.user_id,
            query,
            review_status,
            visibility,
            archived,
            limit,
            cursor_updated,
            cursor_id,
        )
        packages = []
        for row in rows:
            payload = self._package_payload(row[:32])
            payload["latestVersion"] = row[32]
            packages.append(payload)
        result: dict[str, Any] = {"packages": packages}
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"updatedAt": rows[-1][11], "packageId": rows[-1][0]}
            )
        return 200, result

    async def list_namespaces(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        rows = await self.db.run(
            "kinemium_list_namespaces", _list_namespaces, caller.user_id
        )
        return 200, {"namespaces": [_namespace_payload(row) for row in rows]}

    async def get_namespace(
        self, request: SynapseRequest, namespace: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        row = await self.db.run(
            "kinemium_get_namespace", _namespace_row, namespace
        )
        if row is None:
            raise not_found("Namespace not found.")
        return 200, _namespace_payload(row)

    @idempotent
    async def claim_namespace(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        unknown = sorted(set(body) - {"namespace", "spaceId"})
        if unknown:
            raise invalid_request("Unknown namespace fields: " + ", ".join(unknown))
        value = body.get("namespace")
        if not isinstance(value, str):
            raise invalid_request("namespace must be a string.")
        namespace = validate_slug(value.strip().lower(), "namespace")
        space_id = body.get("spaceId")
        if space_id is not None:
            if not isinstance(space_id, str):
                raise invalid_request("spaceId must be a string or null.")
            space_id = validate_room_id(space_id)
            if not await self.can_manage_space(space_id, caller):
                raise forbidden(
                    "You do not have permission to claim a namespace for that space."
                )
        row = await self.db.run(
            "kinemium_claim_namespace",
            _claim_namespace,
            namespace,
            caller.user_id,
            space_id,
            now_ms(),
            self.db.for_update(),
        )
        return 201, _namespace_payload(row)

    async def admin_transfer_namespace(
        self, request: SynapseRequest, namespace: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        body = parse_json_body(request, self.config.max_json_body_bytes)
        unknown = sorted(set(body) - {"owner"})
        if unknown:
            raise invalid_request("Unknown transfer fields: " + ", ".join(unknown))
        owner = body.get("owner")
        if not isinstance(owner, str):
            raise invalid_request("owner must be a Matrix user ID.")
        validate_matrix_user_id(owner)
        row = await self.db.run(
            "kinemium_transfer_namespace",
            _transfer_namespace,
            namespace,
            owner,
            now_ms(),
            self.db.for_update(),
        )
        if self.platform is not None:
            await self.platform.record_audit(
                caller.user_id,
                "namespace.transfer",
                "namespace",
                namespace,
                {"owner": owner},
            )
            await self.platform.notify(
                owner,
                "namespace.transfer",
                caller.user_id,
                None,
                {"namespace": namespace},
            )
        return 200, _namespace_payload(row)

    async def _owned_package(
        self, namespace: str, name: str, caller: Caller
    ) -> tuple:
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_package_row", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        if not await self.can_manage_package(package, caller) and not (
            await self._caller_is_admin(caller)
        ):
            raise forbidden("You do not own that package.")
        return package


def _screenshot_payload(row: tuple) -> dict[str, Any]:
    return {
        "screenshotId": row[0],
        "packageId": row[1],
        "url": row[2],
        "caption": row[3],
        "order": int(row[4]),
        "createdAt": row[5],
    }


def _namespace_payload(row: tuple) -> dict[str, Any]:
    return {
        "namespace": row[0],
        "owner": row[1],
        "createdAt": row[2],
        "packageCount": int(row[3]),
        "spaceId": row[4] if len(row) > 4 else None,
    }


def require_manifest_str(
    manifest: dict[str, Any], name: str, minimum: int, maximum: int
) -> str:
    value = manifest.get(name)
    if not isinstance(value, str):
        raise invalid_request(f"manifest.{name} must be a string.")
    try:
        return check_str(value, f"manifest.{name}", minimum, maximum)
    except TypeError:
        raise invalid_request(f"manifest.{name} must be a string.") from None


def optional_manifest_str(
    manifest: dict[str, Any], name: str, maximum: int
) -> str | None:
    value = manifest.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise invalid_request(f"manifest.{name} must be a string or null.")
    return check_str(value, f"manifest.{name}", 0, maximum)
