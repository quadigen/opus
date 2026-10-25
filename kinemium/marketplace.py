from __future__ import annotations

import re
import uuid
from typing import Any

from synapse.http.site import SynapseRequest

from .auth import (
    MARKETPLACE_ACQUIRE,
    MARKETPLACE_READ,
    MARKETPLACE_WRITE,
    Caller,
)
from .base import Service, idempotent
from .errors import forbidden, invalid_request, not_found
from .http import parse_json_body, query_value
from .packages import (
    DOWNLOAD_COUNT,
    INSTALL_COUNT,
    ITEM_TYPES,
    PACKAGE_COLUMNS,
    REACTION_DISLIKES,
    REACTION_LIKES,
    _dependencies,
    _lock_package,
    _package_row,
    _version_rows,
    comment_count,
    comment_payload,
    download_count,
    package_payload,
    reaction_payload,
    reaction_summary,
    recent_comments,
    version_payload,
)
from .semver import SemVerError, parse_semver
from .util import (
    decode_cursor,
    encode_cursor,
    now_ms,
)
from .validation import optional_str, validate_slug

PACKAGE_SELECT = ", ".join(
    "p." + column.strip() for column in PACKAGE_COLUMNS.split(",")
)
LATEST_VERSION = (
    "(SELECT v.version FROM kinemium_package_versions v"
    " WHERE v.package_id = p.package_id AND v.yanked = FALSE"
    " AND v.review_status = 'approved'"
    " ORDER BY v.published_at DESC, v.version DESC LIMIT 1)"
)
RATING_AVERAGE = (
    "(SELECT AVG(r.rating) FROM kinemium_marketplace_reviews r"
    " WHERE r.package_id = p.package_id AND r.deleted_at IS NULL)"
)
RATING_COUNT = (
    "(SELECT COUNT(*) FROM kinemium_marketplace_reviews r"
    " WHERE r.package_id = p.package_id AND r.deleted_at IS NULL)"
)
MAX_VERSIONS_RETURNED = 100


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _has_version(txn: Any, package_id: str) -> bool:
    txn.execute(
        "SELECT 1 FROM kinemium_package_versions"
        " WHERE package_id = %s AND yanked = FALSE"
        " AND review_status = 'approved' LIMIT 1",
        (package_id,),
    )
    return txn.fetchone() is not None


def _rating(txn: Any, package_id: str) -> tuple[float | None, int]:
    txn.execute(
        "SELECT AVG(rating), COUNT(*) FROM kinemium_marketplace_reviews"
        " WHERE package_id = %s AND deleted_at IS NULL",
        (package_id,),
    )
    row = txn.fetchone()
    if row is None or row[1] is None:
        return None, 0
    return (float(row[0]) if row[0] is not None else None), int(row[1])


def _acquisition_count(txn: Any, package_id: str) -> int:
    txn.execute(
        "SELECT COUNT(*) FROM kinemium_marketplace_acquisitions"
        " WHERE package_id = %s",
        (package_id,),
    )
    row = txn.fetchone()
    return int(row[0]) if row is not None else 0


def _has_acquisition(txn: Any, package_id: str, user_id: str) -> bool:
    txn.execute(
        "SELECT acquisition_id FROM kinemium_marketplace_acquisitions"
        " WHERE package_id = %s AND user_id = %s",
        (package_id, user_id),
    )
    return txn.fetchone() is not None


def _browse(
    txn: Any,
    query: str | None,
    item_type: str | None,
    tag: str | None,
    namespace: str | None,
    official: bool | None,
    sort: str,
    limit: int,
    cursor: dict[str, Any] | None,
) -> tuple[list[tuple], bool]:
    sql = (
        f"SELECT {PACKAGE_SELECT}, {LATEST_VERSION} AS latest_version,"
        f" {RATING_AVERAGE} AS rating_average, {RATING_COUNT} AS rating_count,"
        f" {REACTION_LIKES} AS like_count, {REACTION_DISLIKES} AS dislike_count,"
        f" {INSTALL_COUNT} AS install_count, {DOWNLOAD_COUNT} AS download_count"
        " FROM kinemium_packages p"
        " WHERE p.visibility = 'public' AND p.review_status = 'approved'"
        " AND p.archived_at IS NULL"
        " AND EXISTS (SELECT 1 FROM kinemium_package_versions v"
        "  WHERE v.package_id = p.package_id AND v.yanked = FALSE"
        "  AND v.review_status = 'approved')"
    )
    args: list[Any] = []
    if query is not None:
        pattern = "%" + _escape_like(query.lower()) + "%"
        sql += (
            " AND (LOWER(p.namespace) LIKE %s ESCAPE '\\'"
            " OR LOWER(p.name) LIKE %s ESCAPE '\\'"
            " OR LOWER(p.description) LIKE %s ESCAPE '\\'"
            " OR p.tags_text LIKE %s ESCAPE '\\')"
        )
        args.extend([pattern, pattern, pattern, pattern])
    if item_type is not None:
        sql += " AND p.item_type = %s"
        args.append(item_type)
    if tag is not None:
        sql += " AND p.tags_text LIKE %s ESCAPE '\\'"
        args.append("%\x1f" + _escape_like(tag.lower()) + "\x1f%")
    if namespace is not None:
        sql += " AND p.namespace = %s"
        args.append(namespace)
    if official is not None:
        sql += " AND p.official = %s"
        args.append(official)
    if cursor is not None:
        if sort == "name":
            sql += " AND (p.namespace > %s OR (p.namespace = %s AND p.name > %s))"
            args.extend(
                [cursor.get("namespace"), cursor.get("namespace"), cursor.get("name")]
            )
        else:
            sql += " AND (p.published_at < %s OR (p.published_at = %s AND p.package_id < %s))"
            args.extend(
                [
                    cursor.get("publishedAt"),
                    cursor.get("publishedAt"),
                    cursor.get("packageId"),
                ]
            )
    if sort == "name":
        sql += " ORDER BY p.namespace ASC, p.name ASC LIMIT %s"
    else:
        sql += " ORDER BY p.published_at DESC, p.package_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _owner_packages(
    txn: Any,
    owner: str,
    is_owner: bool,
    limit: int,
    cursor_updated: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    sql = (
        f"SELECT {PACKAGE_SELECT}, {LATEST_VERSION} AS latest_version,"
        f" {RATING_AVERAGE} AS rating_average, {RATING_COUNT} AS rating_count,"
        f" {REACTION_LIKES} AS like_count, {REACTION_DISLIKES} AS dislike_count,"
        f" {INSTALL_COUNT} AS install_count, {DOWNLOAD_COUNT} AS download_count"
        " FROM kinemium_packages p"
        " WHERE p.owner_user_id = %s"
    )
    args: list[Any] = [owner]
    if not is_owner:
        sql += (
            " AND p.visibility = 'public' AND p.review_status = 'approved'"
            " AND p.archived_at IS NULL"
            " AND EXISTS (SELECT 1 FROM kinemium_package_versions v"
            "  WHERE v.package_id = p.package_id AND v.yanked = FALSE"
            "  AND v.review_status = 'approved')"
        )
    if cursor_updated is not None and cursor_id is not None:
        sql += " AND (p.updated_at < %s OR (p.updated_at = %s AND p.package_id < %s))"
        args.extend([cursor_updated, cursor_updated, cursor_id])
    sql += " ORDER BY p.updated_at DESC, p.package_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _acquire(
    txn: Any,
    package: tuple,
    user_id: str,
    now: int,
    for_update: str,
) -> tuple[tuple, bool]:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    if locked[15] not in ("public", "unlisted") and locked[3] != user_id:
        raise not_found("Package not found.")
    if locked[17] != "approved" and locked[3] != user_id:
        raise not_found("Package not found.")
    if locked[25] is not None:
        raise not_found("Package not found.")
    if not _has_version(txn, locked[0]):
        raise not_found("Package not found.")
    txn.execute(
        "INSERT INTO kinemium_profiles"
        " (user_id, bio, website, public_profile, public_marketplace,"
        " created_at, updated_at) VALUES (%s, '', NULL, TRUE, TRUE, %s, %s)"
        " ON CONFLICT (user_id) DO NOTHING",
        (user_id, now, now),
    )
    txn.execute(
        "SELECT acquisition_id, package_id, user_id, created_at,"
        " pinned_version, updated_at FROM kinemium_marketplace_acquisitions"
        " WHERE package_id = %s AND user_id = %s",
        (locked[0], user_id),
    )
    existing = txn.fetchone()
    if existing is not None:
        return existing, False
    acquisition = (
        ("acq_" + uuid.uuid4().hex), locked[0], user_id, now, None, now
    )
    txn.execute(
        "INSERT INTO kinemium_marketplace_acquisitions"
        " (acquisition_id, package_id, user_id, created_at, pinned_version,"
        " updated_at) VALUES (%s, %s, %s, %s, %s, %s)",
        acquisition,
    )
    txn.execute(
        "INSERT INTO kinemium_package_events"
        " (event_id, package_id, version, user_id, event_type, created_at)"
        " VALUES (%s, %s, %s, %s, 'install', %s)",
        ("evt_" + uuid.uuid4().hex, locked[0], None, user_id, now),
    )
    return acquisition, True


def _acquisitions(
    txn: Any,
    user_id: str,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    sql = (
        "SELECT a.acquisition_id, a.package_id, a.user_id, a.created_at,"
        " a.pinned_version, a.updated_at,"
        f" {PACKAGE_SELECT}"
        " FROM kinemium_marketplace_acquisitions a"
        " JOIN kinemium_packages p ON p.package_id = a.package_id"
        " WHERE a.user_id = %s"
    )
    args: list[Any] = [user_id]
    if cursor_created is not None and cursor_id is not None:
        sql += " AND (a.created_at < %s OR (a.created_at = %s AND a.acquisition_id < %s))"
        args.extend([cursor_created, cursor_created, cursor_id])
    sql += " ORDER BY a.created_at DESC, a.acquisition_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _reviews(
    txn: Any,
    package_id: str,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    sql = (
        "SELECT review_id, package_id, user_id, rating, body, created_at, updated_at"
        " FROM kinemium_marketplace_reviews"
        " WHERE package_id = %s AND deleted_at IS NULL"
    )
    args: list[Any] = [package_id]
    if cursor_created is not None and cursor_id is not None:
        sql += " AND (created_at < %s OR (created_at = %s AND review_id < %s))"
        args.extend([cursor_created, cursor_created, cursor_id])
    sql += " ORDER BY created_at DESC, review_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _upsert_review(
    txn: Any,
    package: tuple,
    user_id: str,
    rating: int,
    body: str,
    now: int,
    for_update: str,
) -> tuple[tuple, bool]:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    if locked[15] not in ("public", "unlisted") and locked[3] != user_id:
        raise not_found("Package not found.")
    if locked[17] != "approved" and locked[3] != user_id:
        raise not_found("Package not found.")
    if locked[25] is not None:
        raise not_found("Package not found.")
    if not _has_version(txn, locked[0]):
        raise not_found("Package not found.")
    if not _has_acquisition(txn, locked[0], user_id):
        raise forbidden("Acquire this package before reviewing it.")
    txn.execute(
        "SELECT review_id, package_id, user_id, rating, body, created_at, updated_at"
        " FROM kinemium_marketplace_reviews WHERE package_id = %s AND user_id = %s",
        (locked[0], user_id),
    )
    existing = txn.fetchone()
    if existing is not None:
        txn.execute(
            "UPDATE kinemium_marketplace_reviews SET rating = %s, body = %s,"
            " updated_at = %s, deleted_at = NULL, deleted_by = NULL"
            " WHERE review_id = %s",
            (rating, body, now, existing[0]),
        )
        return (
            (existing[0], locked[0], user_id, rating, body, existing[5], now),
            False,
        )
    review_id = "rev_" + uuid.uuid4().hex
    txn.execute(
        "INSERT INTO kinemium_marketplace_reviews"
        " (review_id, package_id, user_id, rating, body, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (review_id, locked[0], user_id, rating, body, now, now),
    )
    return ((review_id, locked[0], user_id, rating, body, now, now), True)


def _comments(
    txn: Any,
    package_id: str,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    sql = (
        "SELECT comment_id, package_id, user_id, body, created_at, updated_at"
        " FROM kinemium_package_comments"
        " WHERE package_id = %s AND deleted_at IS NULL"
    )
    args: list[Any] = [package_id]
    if cursor_created is not None and cursor_id is not None:
        sql += " AND (created_at < %s OR (created_at = %s AND comment_id < %s))"
        args.extend([cursor_created, cursor_created, cursor_id])
    sql += " ORDER BY created_at DESC, comment_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _insert_comment(
    txn: Any,
    package: tuple,
    user_id: str,
    body: str,
    now: int,
    for_update: str,
) -> tuple:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    if locked[15] not in ("public", "unlisted") and locked[3] != user_id:
        raise not_found("Package not found.")
    if locked[17] != "approved" and locked[3] != user_id:
        raise not_found("Package not found.")
    if locked[25] is not None:
        raise not_found("Package not found.")
    if not _has_version(txn, locked[0]):
        raise not_found("Package not found.")
    txn.execute(
        "INSERT INTO kinemium_profiles"
        " (user_id, bio, website, public_profile, public_marketplace,"
        " created_at, updated_at) VALUES (%s, '', NULL, TRUE, TRUE, %s, %s)"
        " ON CONFLICT (user_id) DO NOTHING",
        (user_id, now, now),
    )
    comment = ("cmt_" + uuid.uuid4().hex, locked[0], user_id, body, now, now)
    txn.execute(
        "INSERT INTO kinemium_package_comments"
        " (comment_id, package_id, user_id, body, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        comment,
    )
    return comment


def _set_reaction(
    txn: Any,
    package: tuple,
    user_id: str,
    value: int,
    now: int,
    for_update: str,
) -> dict[str, Any]:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    if locked[15] not in ("public", "unlisted") and locked[3] != user_id:
        raise not_found("Package not found.")
    if locked[17] != "approved" and locked[3] != user_id:
        raise not_found("Package not found.")
    if locked[25] is not None:
        raise not_found("Package not found.")
    if not _has_version(txn, locked[0]):
        raise not_found("Package not found.")
    txn.execute(
        "INSERT INTO kinemium_profiles"
        " (user_id, bio, website, public_profile, public_marketplace,"
        " created_at, updated_at) VALUES (%s, '', NULL, TRUE, TRUE, %s, %s)"
        " ON CONFLICT (user_id) DO NOTHING",
        (user_id, now, now),
    )
    if value == 0:
        txn.execute(
            "DELETE FROM kinemium_package_reactions"
            " WHERE package_id = %s AND user_id = %s",
            (locked[0], user_id),
        )
    else:
        txn.execute(
            "SELECT value FROM kinemium_package_reactions"
            " WHERE package_id = %s AND user_id = %s",
            (locked[0], user_id),
        )
        existing = txn.fetchone()
        if existing is None:
            txn.execute(
                "INSERT INTO kinemium_package_reactions"
                " (package_id, user_id, value, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (locked[0], user_id, value, now, now),
            )
        elif int(existing[0]) != value:
            txn.execute(
                "UPDATE kinemium_package_reactions SET value = %s, updated_at = %s"
                " WHERE package_id = %s AND user_id = %s",
                (value, now, locked[0], user_id),
            )
    return reaction_summary(txn, locked[0], user_id)


def _pin_acquisition(
    txn: Any,
    package: tuple,
    user_id: str,
    version: str | None,
    now: int,
    for_update: str,
) -> tuple:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    txn.execute(
        "SELECT acquisition_id, package_id, user_id, created_at,"
        " pinned_version, updated_at FROM kinemium_marketplace_acquisitions"
        " WHERE package_id = %s AND user_id = %s",
        (locked[0], user_id),
    )
    row = txn.fetchone()
    if row is None:
        raise not_found("Package is not acquired.")
    if version is not None:
        txn.execute(
            "SELECT 1 FROM kinemium_package_versions"
            " WHERE package_id = %s AND version = %s AND yanked = FALSE"
            " AND review_status = 'approved'",
            (locked[0], version),
        )
        if txn.fetchone() is None:
            raise not_found("Package version not found.")
    previous = row[4]
    txn.execute(
        "UPDATE kinemium_marketplace_acquisitions"
        " SET pinned_version = %s, updated_at = %s"
        " WHERE acquisition_id = %s",
        (version, now, row[0]),
    )
    if version is not None and version != previous:
        txn.execute(
            "INSERT INTO kinemium_package_events"
            " (event_id, package_id, version, user_id, event_type, created_at)"
            " VALUES (%s, %s, %s, %s, 'update', %s)",
            ("evt_" + uuid.uuid4().hex, locked[0], version, user_id, now),
        )
    return (row[0], row[1], row[2], row[3], version, now)


def _uninstall(
    txn: Any,
    package: tuple,
    user_id: str,
    now: int,
    for_update: str,
) -> tuple:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    txn.execute(
        "SELECT acquisition_id, package_id, user_id, created_at,"
        " pinned_version, updated_at FROM kinemium_marketplace_acquisitions"
        " WHERE package_id = %s AND user_id = %s",
        (locked[0], user_id),
    )
    row = txn.fetchone()
    if row is None:
        raise not_found("Package is not acquired.")
    txn.execute(
        "DELETE FROM kinemium_marketplace_acquisitions"
        " WHERE acquisition_id = %s",
        (row[0],),
    )
    txn.execute(
        "INSERT INTO kinemium_package_events"
        " (event_id, package_id, version, user_id, event_type, created_at)"
        " VALUES (%s, %s, %s, %s, 'uninstall', %s)",
        ("evt_" + uuid.uuid4().hex, locked[0], row[4], user_id, now),
    )
    return row


def _updates(
    txn: Any,
    user_id: str,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    latest = (
        "(SELECT v.version FROM kinemium_package_versions v"
        " WHERE v.package_id = a.package_id AND v.yanked = FALSE"
        " AND v.review_status = 'approved'"
        " ORDER BY v.published_at DESC, v.version DESC LIMIT 1)"
    )
    sql = (
        "SELECT a.acquisition_id, a.package_id, a.created_at, a.pinned_version,"
        " a.updated_at, p.namespace, p.name, p.visibility, p.review_status,"
        f" {latest} AS latest_version"
        " FROM kinemium_marketplace_acquisitions a"
        " JOIN kinemium_packages p ON p.package_id = a.package_id"
        " WHERE a.user_id = %s AND p.archived_at IS NULL"
    )
    args: list[Any] = [user_id]
    if cursor_created is not None and cursor_id is not None:
        sql += " AND (a.created_at < %s OR (a.created_at = %s AND a.acquisition_id < %s))"
        args.extend([cursor_created, cursor_created, cursor_id])
    sql += " ORDER BY a.created_at DESC, a.acquisition_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _comment_row(
    txn: Any, package_id: str, comment_id: str
) -> tuple | None:
    txn.execute(
        "SELECT comment_id, package_id, user_id, body, created_at, updated_at"
        " FROM kinemium_package_comments"
        " WHERE comment_id = %s AND package_id = %s AND deleted_at IS NULL",
        (comment_id, package_id),
    )
    return txn.fetchone()


def _edit_comment(
    txn: Any,
    package: tuple,
    comment_id: str,
    user_id: str,
    body: str,
    is_admin: bool,
    now: int,
) -> tuple:
    row = _comment_row(txn, package[0], comment_id)
    if row is None:
        raise not_found("Comment not found.")
    if row[2] != user_id and not is_admin:
        raise forbidden("You cannot edit that comment.")
    txn.execute(
        "UPDATE kinemium_package_comments SET body = %s, updated_at = %s"
        " WHERE comment_id = %s",
        (body, now, comment_id),
    )
    return (row[0], row[1], row[2], body, row[4], now)


def _delete_comment(
    txn: Any,
    package: tuple,
    comment_id: str,
    actor: str,
    is_admin: bool,
    now: int,
) -> tuple:
    row = _comment_row(txn, package[0], comment_id)
    if row is None:
        raise not_found("Comment not found.")
    if row[2] != actor and package[3] != actor and not is_admin:
        raise forbidden("You cannot delete that comment.")
    txn.execute(
        "UPDATE kinemium_package_comments SET deleted_at = %s, deleted_by = %s"
        " WHERE comment_id = %s",
        (now, actor, comment_id),
    )
    return row


def _delete_review(
    txn: Any,
    package: tuple,
    review_id: str,
    actor: str,
    is_admin: bool,
    now: int,
) -> tuple:
    txn.execute(
        "SELECT review_id, package_id, user_id FROM kinemium_marketplace_reviews"
        " WHERE review_id = %s AND package_id = %s AND deleted_at IS NULL",
        (review_id, package[0]),
    )
    row = txn.fetchone()
    if row is None:
        raise not_found("Review not found.")
    if row[2] != actor and package[3] != actor and not is_admin:
        raise forbidden("You cannot delete that review.")
    txn.execute(
        "UPDATE kinemium_marketplace_reviews SET deleted_at = %s, deleted_by = %s"
        " WHERE review_id = %s",
        (now, actor, review_id),
    )
    return row



class MarketplaceService(Service):
    def register(self, resource: Any) -> None:
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/marketplace$")],
            self.browse,
            "KinemiumMarketplaceServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/marketplace/acquisitions$")],
            self.list_acquisitions,
            "KinemiumMarketplaceAcquisitionsServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/marketplace/(?P<namespace>[^/]+)/(?P<name>[^/]+)/acquire$")],
            self.acquire_package,
            "KinemiumMarketplaceAcquireServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/marketplace/(?P<namespace>[^/]+)/(?P<name>[^/]+)/acquire$")],
            self.uninstall_package,
            "KinemiumMarketplaceAcquireServlet",
        )
        resource.register_paths(
            "PATCH",
            [re.compile(r"^/_kinemium/v1/marketplace/(?P<namespace>[^/]+)/(?P<name>[^/]+)/acquire$")],
            self.pin_acquisition,
            "KinemiumMarketplaceAcquireServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/marketplace/updates$")],
            self.list_updates,
            "KinemiumMarketplaceUpdatesServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/marketplace/(?P<namespace>[^/]+)/(?P<name>[^/]+)/reviews$")],
            self.get_reviews,
            "KinemiumMarketplaceReviewsServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/marketplace/(?P<namespace>[^/]+)/(?P<name>[^/]+)/reviews$")],
            self.post_review,
            "KinemiumMarketplaceReviewsServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/marketplace/(?P<namespace>[^/]+)/(?P<name>[^/]+)/comments$")],
            self.get_comments,
            "KinemiumMarketplaceCommentsServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/marketplace/(?P<namespace>[^/]+)/(?P<name>[^/]+)/comments$")],
            self.post_comment,
            "KinemiumMarketplaceCommentsServlet",
        )
        resource.register_paths(
            "PATCH",
            [re.compile(r"^/_kinemium/v1/marketplace/(?P<namespace>[^/]+)/(?P<name>[^/]+)/comments/(?P<comment_id>[^/]+)$")],
            self.edit_comment,
            "KinemiumMarketplaceCommentServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/marketplace/(?P<namespace>[^/]+)/(?P<name>[^/]+)/comments/(?P<comment_id>[^/]+)$")],
            self.delete_comment,
            "KinemiumMarketplaceCommentServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/marketplace/(?P<namespace>[^/]+)/(?P<name>[^/]+)/reviews/(?P<review_id>[^/]+)$")],
            self.delete_review,
            "KinemiumMarketplaceReviewServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/marketplace/(?P<namespace>[^/]+)/(?P<name>[^/]+)/reaction$")],
            self.post_reaction,
            "KinemiumMarketplaceReactionServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/marketplace/(?P<namespace>[^/]+)/(?P<name>[^/]+)$")],
            self.get_package,
            "KinemiumMarketplacePackageServlet",
        )

    def _summary_payload(self, row: tuple) -> dict[str, Any]:
        payload = package_payload(row[:32])
        payload["latestVersion"] = row[32]
        payload["rating"] = {
            "average": float(row[33]) if row[33] is not None else None,
            "count": int(row[34]),
        }
        payload["reactions"] = reaction_payload(int(row[35]), int(row[36]))
        payload["installCount"] = int(row[37])
        payload["downloadCount"] = int(row[38])
        return payload

    async def _visible_package(
        self, namespace: str, name: str, caller: Caller | None
    ) -> tuple[tuple, bool]:
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        row = await self.db.run(
            "kinemium_get_marketplace_package", _package_row, namespace, name
        )
        if row is None:
            raise not_found("Package not found.")
        is_owner = await self.can_manage_package(row, caller)
        if not is_owner and row[15] == "private":
            raise not_found("Package not found.")
        if not is_owner and row[15] not in ("public", "unlisted"):
            raise not_found("Package not found.")
        if not is_owner and row[17] != "approved":
            raise not_found("Package not found.")
        if not is_owner:
            has_version = await self.db.run(
                "kinemium_marketplace_has_version", _has_version, row[0]
            )
            if not has_version:
                raise not_found("Package not found.")
        return row, is_owner

    async def _detail(
        self, row: tuple, caller: Caller | None, is_owner: bool
    ) -> dict[str, Any]:
        (
            versions,
            dependency_rows,
            rating,
            acquisition_count,
            acquired,
            reactions,
            comments,
            comments_total,
            downloads,
        ) = await self.db.run(
            "kinemium_marketplace_package_detail",
            lambda txn: (
                _version_rows(txn, row[0]),
                _dependencies(txn, row[0]),
                _rating(txn, row[0]),
                _acquisition_count(txn, row[0]),
                (
                    _has_acquisition(txn, row[0], caller.user_id)
                    if caller is not None
                    else False
                ),
                reaction_summary(
                    txn, row[0], caller.user_id if caller is not None else None
                ),
                recent_comments(txn, row[0]),
                comment_count(txn, row[0]),
                download_count(txn, row[0]),
            ),
        )
        grouped: dict[str, list[tuple]] = {}
        for dependency in dependency_rows:
            grouped.setdefault(dependency[0], []).append(dependency[1:])
        visible_versions = (
            versions
            if is_owner
            else [version for version in versions if version[12] == "approved"]
        )
        latest = next(
            (
                version
                for version in visible_versions
                if version[12] == "approved" and not version[8]
            ),
            None,
        )
        rating_average, rating_count = rating
        payload = package_payload(row)
        payload["latestVersion"] = latest[1] if latest is not None else None
        payload["versions"] = [
            version_payload(version, grouped.get(version[1], []))
            for version in visible_versions[:MAX_VERSIONS_RETURNED]
        ]
        payload["rating"] = {"average": rating_average, "count": rating_count}
        payload["reactions"] = reactions
        payload["comments"] = [comment_payload(comment) for comment in comments]
        payload["commentCount"] = comments_total
        payload["acquisitionCount"] = acquisition_count
        payload["installCount"] = acquisition_count
        payload["downloadCount"] = downloads
        payload["acquired"] = acquired
        payload["canManage"] = is_owner
        return payload

    async def browse(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_READ, required=False)
        self.enforce_rate_limit(request, caller)
        limit = self.page_limit(request)
        query = query_value(request, "q", None, 100)
        if query is not None:
            query = query.strip() or None
        item_type = query_value(request, "type", None, 16)
        if item_type is not None and item_type not in ITEM_TYPES:
            raise invalid_request("Unknown package type.")
        tag = query_value(request, "tag", None, 32)
        if tag is not None:
            tag = tag.strip().lower() or None
        namespace = query_value(request, "namespace", None, 64)
        if namespace is not None:
            namespace = namespace.strip().lower() or None
            if namespace is not None:
                validate_slug(namespace, "namespace")
        official_raw = query_value(request, "official", None, 8)
        official: bool | None = None
        if official_raw in ("true", "1"):
            official = True
        elif official_raw in ("false", "0"):
            official = False
        elif official_raw is not None:
            raise invalid_request("official must be true or false.")
        sort = query_value(request, "sort", "recent", 16)
        if sort not in ("recent", "name"):
            raise invalid_request("sort must be recent or name.")
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            if sort == "name":
                if not isinstance(cursor.get("namespace"), str) or not isinstance(
                    cursor.get("name"), str
                ):
                    raise invalid_request("Invalid pagination cursor.")
            elif not isinstance(cursor.get("publishedAt"), int) or not isinstance(
                cursor.get("packageId"), str
            ):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_browse_marketplace",
            _browse,
            query,
            item_type,
            tag,
            namespace,
            official,
            sort,
            limit,
            cursor,
        )
        packages = [self._summary_payload(row) for row in rows]
        result: dict[str, Any] = {"packages": packages, "sort": sort}
        if has_more and rows:
            if sort == "name":
                result["nextCursor"] = encode_cursor(
                    {"namespace": rows[-1][1], "name": rows[-1][2]}
                )
            else:
                result["nextCursor"] = encode_cursor(
                    {"publishedAt": rows[-1][16], "packageId": rows[-1][0]}
                )
        return 200, result

    async def get_package(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_READ, required=False)
        self.enforce_rate_limit(request, caller)
        row, is_owner = await self._visible_package(namespace, name, caller)
        return 200, await self._detail(row, caller, is_owner)

    @idempotent
    async def acquire_package(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_ACQUIRE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_marketplace_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        acquisition, created = await self.db.run(
            "kinemium_acquire_marketplace_package",
            _acquire,
            package,
            caller.user_id,
            now_ms(),
            self.db.for_update(),
        )
        if created and self.platform is not None:
            await self.platform.notify(
                package[3],
                "package.acquired",
                caller.user_id,
                package[0],
                {"namespace": namespace, "name": name},
            )
        return (201 if created else 200), {
            "acquired": True,
            "acquisition": {
                "acquisitionId": acquisition[0],
                "packageId": acquisition[1],
                "userId": acquisition[2],
                "createdAt": acquisition[3],
                "pinnedVersion": acquisition[4],
                "updatedAt": acquisition[5],
            },
        }

    async def uninstall_package(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_ACQUIRE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_marketplace_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        row = await self.db.run(
            "kinemium_uninstall_marketplace_package",
            _uninstall,
            package,
            caller.user_id,
            now_ms(),
            self.db.for_update(),
        )
        if self.platform is not None:
            await self.platform.notify(
                package[3],
                "package.uninstalled",
                caller.user_id,
                package[0],
                {"namespace": namespace, "name": name, "version": row[4]},
            )
        return 200, {
            "uninstalled": True,
            "packageId": package[0],
            "namespace": namespace,
            "name": name,
        }

    async def pin_acquisition(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_ACQUIRE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        package, _is_owner = await self._visible_package(namespace, name, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {"version"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown acquisition fields: " + ", ".join(unknown))
        version = body.get("version")
        if version is not None and not isinstance(version, str):
            raise invalid_request("version must be a string or null.")
        if version is not None:
            try:
                parse_semver(version)
            except SemVerError as error:
                raise invalid_request(str(error)) from None
        row = await self.db.run(
            "kinemium_pin_marketplace_acquisition",
            _pin_acquisition,
            package,
            caller.user_id,
            version,
            now_ms(),
            self.db.for_update(),
        )
        return 200, {
            "acquisition": {
                "acquisitionId": row[0],
                "packageId": row[1],
                "userId": row[2],
                "createdAt": row[3],
                "pinnedVersion": row[4],
                "updatedAt": row[5],
            }
        }

    async def list_updates(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        limit = self.page_limit(request)
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_created = None
        cursor_id = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_created = cursor.get("createdAt")
            cursor_id = cursor.get("acquisitionId")
            if not isinstance(cursor_created, int) or not isinstance(cursor_id, str):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_list_marketplace_updates",
            _updates,
            caller.user_id,
            limit,
            cursor_created,
            cursor_id,
        )
        updates = []
        for row in rows:
            pinned = row[3]
            latest = row[9]
            updates.append(
                {
                    "acquisitionId": row[0],
                    "packageId": row[1],
                    "createdAt": row[2],
                    "pinnedVersion": pinned,
                    "updatedAt": row[4],
                    "namespace": row[5],
                    "name": row[6],
                    "latestVersion": latest,
                    "updateAvailable": pinned is not None and latest is not None and pinned != latest,
                }
            )
        result: dict[str, Any] = {"updates": updates}
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][2], "acquisitionId": rows[-1][0]}
            )
        return 200, result

    async def list_acquisitions(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        limit = self.page_limit(request)
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_created = None
        cursor_id = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_created = cursor.get("createdAt")
            cursor_id = cursor.get("acquisitionId")
            if not isinstance(cursor_created, int) or not isinstance(cursor_id, str):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_list_marketplace_acquisitions",
            _acquisitions,
            caller.user_id,
            limit,
            cursor_created,
            cursor_id,
        )
        acquisitions = [
            {
                "acquisitionId": row[0],
                "packageId": row[1],
                "userId": row[2],
                "createdAt": row[3],
                "pinnedVersion": row[4],
                "updatedAt": row[5],
                "package": package_payload(row[6:38]),
            }
            for row in rows
        ]
        result: dict[str, Any] = {"acquisitions": acquisitions}
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][3], "acquisitionId": rows[-1][0]}
            )
        return 200, result

    async def get_reviews(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_READ, required=False)
        self.enforce_rate_limit(request, caller)
        row, _is_owner = await self._visible_package(namespace, name, caller)
        limit = self.page_limit(request)
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_created = None
        cursor_id = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_created = cursor.get("createdAt")
            cursor_id = cursor.get("reviewId")
            if not isinstance(cursor_created, int) or not isinstance(cursor_id, str):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_list_marketplace_reviews",
            _reviews,
            row[0],
            limit,
            cursor_created,
            cursor_id,
        )
        average, count = await self.db.run(
            "kinemium_marketplace_package_rating", _rating, row[0]
        )
        result: dict[str, Any] = {
            "reviews": [
                {
                    "reviewId": review[0],
                    "packageId": review[1],
                    "userId": review[2],
                    "rating": int(review[3]),
                    "body": review[4],
                    "createdAt": review[5],
                    "updatedAt": review[6],
                }
                for review in rows
            ],
            "rating": {"average": average, "count": count},
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][5], "reviewId": rows[-1][0]}
            )
        return 200, result

    @idempotent
    async def post_review(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        rating = body.get("rating")
        if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
            raise invalid_request("rating must be an integer between 1 and 5.")
        body_text = optional_str(body, "body", self.config.max_review_length) or ""
        package = await self.db.run(
            "kinemium_get_marketplace_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        row, created = await self.db.run(
            "kinemium_upsert_marketplace_review",
            _upsert_review,
            package,
            caller.user_id,
            rating,
            body_text,
            now_ms(),
            self.db.for_update(),
        )
        if created and self.platform is not None:
            await self.platform.notify(
                package[3],
                "package.review",
                caller.user_id,
                package[0],
                {
                    "namespace": namespace,
                    "name": name,
                    "reviewId": row[0],
                    "rating": int(row[3]),
                },
            )
        return (201 if created else 200), {
            "review": {
                "reviewId": row[0],
                "packageId": row[1],
                "userId": row[2],
                "rating": int(row[3]),
                "body": row[4],
                "createdAt": row[5],
                "updatedAt": row[6],
            }
        }

    async def get_comments(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_READ, required=False)
        self.enforce_rate_limit(request, caller)
        row, _is_owner = await self._visible_package(namespace, name, caller)
        limit = self.page_limit(request)
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_created = None
        cursor_id = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_created = cursor.get("createdAt")
            cursor_id = cursor.get("commentId")
            if not isinstance(cursor_created, int) or not isinstance(cursor_id, str):
                raise invalid_request("Invalid pagination cursor.")
        rows, total = await self.db.run(
            "kinemium_list_package_comments",
            lambda txn: (
                _comments(txn, row[0], limit, cursor_created, cursor_id),
                comment_count(txn, row[0]),
            ),
        )
        comments, more = rows
        result: dict[str, Any] = {
            "comments": [comment_payload(comment) for comment in comments],
            "count": total,
        }
        if more and comments:
            result["nextCursor"] = encode_cursor(
                {"createdAt": comments[-1][4], "commentId": comments[-1][0]}
            )
        return 200, result

    @idempotent
    async def post_comment(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        text = optional_str(body, "body", self.config.max_review_length)
        if text is None or not text.strip():
            raise invalid_request("body is required.")
        package, _is_owner = await self._visible_package(namespace, name, caller)
        comment = await self.db.run(
            "kinemium_insert_package_comment",
            _insert_comment,
            package,
            caller.user_id,
            text.strip(),
            now_ms(),
            self.db.for_update(),
        )
        if self.platform is not None:
            await self.platform.notify(
                package[3],
                "package.comment",
                caller.user_id,
                package[0],
                {
                    "namespace": namespace,
                    "name": name,
                    "commentId": comment[0],
                    "body": text[:500],
                },
            )
        return 201, {"comment": comment_payload(comment)}

    async def edit_comment(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        comment_id: str,
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        package, _is_owner = await self._visible_package(namespace, name, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        unknown = sorted(set(body) - {"body"})
        if unknown:
            raise invalid_request("Unknown comment fields: " + ", ".join(unknown))
        text = optional_str(body, "body", self.config.max_review_length)
        if text is None or not text.strip():
            raise invalid_request("body is required.")
        is_admin = await self._caller_is_admin(caller)
        row = await self.db.run(
            "kinemium_edit_package_comment",
            _edit_comment,
            package,
            comment_id,
            caller.user_id,
            text.strip(),
            is_admin,
            now_ms(),
        )
        return 200, {"comment": comment_payload(row)}

    async def delete_comment(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        comment_id: str,
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        package, _is_owner = await self._visible_package(namespace, name, caller)
        is_admin = await self._caller_is_admin(caller)
        await self.db.run(
            "kinemium_delete_package_comment",
            _delete_comment,
            package,
            comment_id,
            caller.user_id,
            is_admin,
            now_ms(),
        )
        return 200, {"deleted": True, "commentId": comment_id}

    async def delete_review(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        review_id: str,
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        package, _is_owner = await self._visible_package(namespace, name, caller)
        is_admin = await self._caller_is_admin(caller)
        await self.db.run(
            "kinemium_delete_marketplace_review",
            _delete_review,
            package,
            review_id,
            caller.user_id,
            is_admin,
            now_ms(),
        )
        return 200, {"deleted": True, "reviewId": review_id}

    async def post_reaction(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        value = body.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value not in (-1, 0, 1)
        ):
            raise invalid_request("value must be -1, 0, or 1.")
        package, _is_owner = await self._visible_package(namespace, name, caller)
        reactions = await self.db.run(
            "kinemium_set_package_reaction",
            _set_reaction,
            package,
            caller.user_id,
            value,
            now_ms(),
            self.db.for_update(),
        )
        return 200, {"reactions": reactions}

    async def list_owner_items(
        self, request: SynapseRequest, user_id: str
    ) -> dict[str, Any]:
        caller = await self.authenticate(request, MARKETPLACE_READ, required=False)
        self.enforce_rate_limit(request, caller)
        is_owner = caller is not None and caller.user_id == user_id
        limit = self.page_limit(request)
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
            "kinemium_list_owner_marketplace",
            _owner_packages,
            user_id,
            is_owner,
            limit,
            cursor_updated,
            cursor_id,
        )
        packages = [self._summary_payload(row) for row in rows]
        result: dict[str, Any] = {"packages": packages}
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"updatedAt": rows[-1][11], "packageId": rows[-1][0]}
            )
        return result
