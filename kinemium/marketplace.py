from __future__ import annotations

import json
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
from .base import Service
from .errors import conflict, forbidden, invalid_request, not_found
from .http import (
    content_type_for_filename,
    parse_json_body,
    parse_multipart,
    query_value,
    send_file,
)
from .semver import SemVerError, parse_semver
from .storage import sanitize_filename, validate_archive
from .util import (
    decode_cursor,
    encode_cursor,
    json_loads,
    now_ms,
)
from .validation import (
    check_str,
    optional_str,
    require_str,
    validate_id,
    validate_tags,
)

ITEM_TYPES = ("model", "plugin", "template", "tool", "package")
VISIBILITIES = ("public", "unlisted", "private")
ITEM_COLUMNS = (
    "item_id, owner_user_id, name, description, item_type, tags, tags_text,"
    " visibility, status, price_amount, price_currency, package_id,"
    " package_version, created_at, updated_at, published_at, deleted_at"
)
VERSION_COLUMNS = (
    "version_id, item_id, version, changelog, filename, storage_key, digest,"
    " size_bytes, created_at"
)
MAX_VERSIONS_RETURNED = 50


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _item_row(txn: Any, item_id: str) -> tuple | None:
    txn.execute(
        f"SELECT {ITEM_COLUMNS} FROM kinemium_marketplace_items"
        " WHERE item_id = %s",
        (item_id,),
    )
    return txn.fetchone()


def _lock_item(txn: Any, item_id: str, for_update: str) -> tuple | None:
    txn.execute(
        f"SELECT {ITEM_COLUMNS} FROM kinemium_marketplace_items"
        " WHERE item_id = %s" + for_update,
        (item_id,),
    )
    return txn.fetchone()


def _version_row(txn: Any, item_id: str, version: str) -> tuple | None:
    txn.execute(
        f"SELECT {VERSION_COLUMNS} FROM kinemium_marketplace_versions"
        " WHERE item_id = %s AND version = %s",
        (item_id, version),
    )
    return txn.fetchone()


def _versions(txn: Any, item_id: str) -> list[tuple]:
    txn.execute(
        f"SELECT {VERSION_COLUMNS} FROM kinemium_marketplace_versions"
        " WHERE item_id = %s ORDER BY created_at DESC, version DESC LIMIT %s",
        (item_id, MAX_VERSIONS_RETURNED),
    )
    return txn.fetchall()


def _latest_version(txn: Any, item_id: str) -> tuple | None:
    txn.execute(
        f"SELECT {VERSION_COLUMNS} FROM kinemium_marketplace_versions"
        " WHERE item_id = %s ORDER BY created_at DESC, version DESC LIMIT 1",
        (item_id,),
    )
    return txn.fetchone()


def _version_count(txn: Any, item_id: str) -> int:
    txn.execute(
        "SELECT COUNT(*) FROM kinemium_marketplace_versions WHERE item_id = %s",
        (item_id,),
    )
    row = txn.fetchone()
    return int(row[0]) if row is not None else 0


def _rating(txn: Any, item_id: str) -> tuple[float | None, int]:
    txn.execute(
        "SELECT AVG(rating), COUNT(*) FROM kinemium_marketplace_reviews"
        " WHERE item_id = %s",
        (item_id,),
    )
    row = txn.fetchone()
    if row is None or row[1] is None:
        return None, 0
    return (float(row[0]) if row[0] is not None else None), int(row[1])


def _acquisition_count(txn: Any, item_id: str) -> int:
    txn.execute(
        "SELECT COUNT(*) FROM kinemium_marketplace_acquisitions WHERE item_id = %s",
        (item_id,),
    )
    row = txn.fetchone()
    return int(row[0]) if row is not None else 0


def _has_acquisition(txn: Any, item_id: str, user_id: str) -> bool:
    txn.execute(
        "SELECT acquisition_id FROM kinemium_marketplace_acquisitions"
        " WHERE item_id = %s AND user_id = %s",
        (item_id, user_id),
    )
    return txn.fetchone() is not None


def _package_belongs_to(txn: Any, package_id: str, owner: str) -> tuple | None:
    txn.execute(
        "SELECT package_id, namespace, name FROM kinemium_packages"
        " WHERE package_id = %s AND owner_user_id = %s",
        (package_id, owner),
    )
    return txn.fetchone()


def _package_version_exists(txn: Any, package_id: str, version: str) -> bool:
    txn.execute(
        "SELECT version FROM kinemium_package_versions"
        " WHERE package_id = %s AND version = %s",
        (package_id, version),
    )
    return txn.fetchone() is not None


def _tags_text(tags: list[str]) -> str:
    return " " + " ".join(tag.lower() for tag in tags) + " "


def _create_item(
    txn: Any,
    owner: str,
    name: str,
    description: str,
    item_type: str,
    tags: list[str],
    visibility: str,
    package_id: str | None,
    package_version: str | None,
    now: int,
) -> tuple:
    txn.execute(
        "INSERT INTO kinemium_profiles"
        " (user_id, bio, website, public_profile, public_marketplace,"
        " created_at, updated_at) VALUES (%s, '', NULL, TRUE, TRUE, %s, %s)"
        " ON CONFLICT (user_id) DO NOTHING",
        (owner, now, now),
    )
    if package_id is not None:
        if _package_belongs_to(txn, package_id, owner) is None:
            raise forbidden("You may only link packages you own.")
        if package_version is not None and not _package_version_exists(
            txn, package_id, package_version
        ):
            raise not_found("Package version not found.")
    item_id = "item_" + uuid.uuid4().hex
    txn.execute(
        f"INSERT INTO kinemium_marketplace_items ({ITEM_COLUMNS})"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'draft', 0, 'USD',"
        " %s, %s, %s, %s, NULL, NULL)",
        (
            item_id,
            owner,
            name,
            description,
            item_type,
            txn_json(tags),
            _tags_text(tags),
            visibility,
            package_id,
            package_version,
            now,
            now,
        ),
    )
    row = _item_row(txn, item_id)
    if row is None:
        raise not_found("Marketplace item not found.")
    return row


def txn_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _update_item(
    txn: Any,
    item_id: str,
    owner: str,
    name: str | None,
    description: str | None,
    tags: list[str] | None,
    visibility: str | None,
    package_id: str | None,
    package_id_set: bool,
    package_version: str | None,
    package_version_set: bool,
    now: int,
    for_update: str,
) -> tuple:
    current = _lock_item(txn, item_id, for_update)
    if current is None or current[16] is not None:
        raise not_found("Marketplace item not found.")
    if current[1] != owner:
        raise forbidden("You do not own that marketplace item.")
    new_package_id = package_id if package_id_set else current[11]
    new_package_version = (
        package_version if package_version_set else current[12]
    )
    if new_package_id is None:
        new_package_version = None
    elif package_id_set and not package_version_set:
        new_package_version = None
    if new_package_id is not None:
        if _package_belongs_to(txn, new_package_id, owner) is None:
            raise forbidden("You may only link packages you own.")
        if new_package_version is not None and not _package_version_exists(
            txn, new_package_id, new_package_version
        ):
            raise not_found("Package version not found.")
    txn.execute(
        "UPDATE kinemium_marketplace_items SET name = %s, description = %s,"
        " tags = %s, tags_text = %s, visibility = %s, package_id = %s,"
        " package_version = %s, updated_at = %s WHERE item_id = %s",
        (
            name if name is not None else current[2],
            description if description is not None else current[3],
            txn_json(tags) if tags is not None else txn_json(json_loads(current[5])),
            _tags_text(tags) if tags is not None else current[6],
            visibility if visibility is not None else current[7],
            new_package_id,
            new_package_version,
            now,
            item_id,
        ),
    )
    row = _item_row(txn, item_id)
    if row is None:
        raise not_found("Marketplace item not found.")
    return row


def _set_status(
    txn: Any,
    item_id: str,
    owner: str,
    status: str,
    now: int,
    for_update: str,
) -> tuple:
    current = _lock_item(txn, item_id, for_update)
    if current is None or current[16] is not None:
        raise not_found("Marketplace item not found.")
    if current[1] != owner:
        raise forbidden("You do not own that marketplace item.")
    if current[8] == "removed":
        raise not_found("Marketplace item not found.")
    if status == "published":
        if _version_count(txn, item_id) == 0:
            raise conflict("Add at least one version before publishing.")
        published_at = current[15] or now
        txn.execute(
            "UPDATE kinemium_marketplace_items SET status = 'published',"
            " published_at = %s, updated_at = %s WHERE item_id = %s",
            (published_at, now, item_id),
        )
    elif status == "unpublished":
        txn.execute(
            "UPDATE kinemium_marketplace_items SET status = 'unpublished',"
            " updated_at = %s WHERE item_id = %s",
            (now, item_id),
        )
    elif status == "removed":
        txn.execute(
            "UPDATE kinemium_marketplace_items SET status = 'removed',"
            " visibility = 'private', deleted_at = %s, updated_at = %s"
            " WHERE item_id = %s",
            (now, now, item_id),
        )
    else:
        raise invalid_request("Unsupported marketplace status.")
    row = _item_row(txn, item_id)
    if row is None:
        raise not_found("Marketplace item not found.")
    return row


def _publish_version(
    txn: Any,
    item_id: str,
    owner: str,
    version: str,
    changelog: str,
    filename: str,
    storage_key: str,
    digest: str,
    size: int,
    maximum: int,
    now: int,
    for_update: str,
) -> tuple:
    item = _lock_item(txn, item_id, for_update)
    if item is None or item[16] is not None:
        raise not_found("Marketplace item not found.")
    if item[1] != owner:
        raise forbidden("You do not own that marketplace item.")
    if _version_row(txn, item_id, version) is not None:
        raise conflict("That item version already exists.")
    if _version_count(txn, item_id) >= maximum:
        raise conflict("Item version limit reached.", {"reason": "quota_exceeded"})
    version_id = "mver_" + uuid.uuid4().hex
    txn.execute(
        f"INSERT INTO kinemium_marketplace_versions ({VERSION_COLUMNS})"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            version_id,
            item_id,
            version,
            changelog,
            filename,
            storage_key,
            digest,
            size,
            now,
        ),
    )
    txn.execute(
        "UPDATE kinemium_marketplace_items SET updated_at = %s WHERE item_id = %s",
        (now, item_id),
    )
    row = _version_row(txn, item_id, version)
    if row is None:
        raise not_found("Item version not found.")
    return row


def _acquire(
    txn: Any,
    item_id: str,
    user_id: str,
    now: int,
    for_update: str,
) -> tuple[tuple, bool]:
    item = _lock_item(txn, item_id, for_update)
    if item is None or item[16] is not None or item[8] != "published":
        raise not_found("Marketplace item not found.")
    if item[7] not in ("public", "unlisted") and item[1] != user_id:
        raise not_found("Marketplace item not found.")
    if int(item[9]) != 0:
        raise conflict("Paid acquisitions are not supported.")
    txn.execute(
        "INSERT INTO kinemium_profiles"
        " (user_id, bio, website, public_profile, public_marketplace,"
        " created_at, updated_at) VALUES (%s, '', NULL, TRUE, TRUE, %s, %s)"
        " ON CONFLICT (user_id) DO NOTHING",
        (user_id, now, now),
    )
    txn.execute(
        "SELECT acquisition_id, item_id, user_id, created_at"
        " FROM kinemium_marketplace_acquisitions"
        " WHERE item_id = %s AND user_id = %s",
        (item_id, user_id),
    )
    existing = txn.fetchone()
    if existing is not None:
        return existing, False
    acquisition = (("acq_" + uuid.uuid4().hex), item_id, user_id, now)
    txn.execute(
        "INSERT INTO kinemium_marketplace_acquisitions"
        " (acquisition_id, item_id, user_id, created_at)"
        " VALUES (%s, %s, %s, %s)",
        acquisition,
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
        "SELECT a.acquisition_id, a.item_id, a.user_id, a.created_at,"
        f" {', '.join('i.' + column.strip() for column in ITEM_COLUMNS.split(','))}"
        " FROM kinemium_marketplace_acquisitions a"
        " JOIN kinemium_marketplace_items i ON i.item_id = a.item_id"
        " WHERE a.user_id = %s AND i.deleted_at IS NULL"
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
    item_id: str,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    sql = (
        "SELECT review_id, item_id, user_id, rating, body, created_at, updated_at"
        " FROM kinemium_marketplace_reviews WHERE item_id = %s"
    )
    args: list[Any] = [item_id]
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
    item_id: str,
    user_id: str,
    rating: int,
    body: str,
    now: int,
    for_update: str,
) -> tuple[tuple, bool]:
    item = _lock_item(txn, item_id, for_update)
    if item is None or item[16] is not None or item[8] != "published":
        raise not_found("Marketplace item not found.")
    if item[7] not in ("public", "unlisted") and item[1] != user_id:
        raise not_found("Marketplace item not found.")
    if not _has_acquisition(txn, item_id, user_id):
        raise forbidden("Acquire this item before reviewing it.")
    txn.execute(
        "SELECT review_id, item_id, user_id, rating, body, created_at, updated_at"
        " FROM kinemium_marketplace_reviews WHERE item_id = %s AND user_id = %s",
        (item_id, user_id),
    )
    existing = txn.fetchone()
    if existing is not None:
        txn.execute(
            "UPDATE kinemium_marketplace_reviews SET rating = %s, body = %s,"
            " updated_at = %s WHERE review_id = %s",
            (rating, body, now, existing[0]),
        )
        return (
            (existing[0], item_id, user_id, rating, body, existing[5], now),
            False,
        )
    review_id = "rev_" + uuid.uuid4().hex
    txn.execute(
        "INSERT INTO kinemium_marketplace_reviews"
        " (review_id, item_id, user_id, rating, body, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (review_id, item_id, user_id, rating, body, now, now),
    )
    return ((review_id, item_id, user_id, rating, body, now, now), True)


def _browse(
    txn: Any,
    query: str | None,
    item_type: str | None,
    tag: str | None,
    sort: str,
    limit: int,
    cursor: dict[str, Any] | None,
) -> tuple[list[tuple], bool]:
    sql = (
        f"SELECT {ITEM_COLUMNS},"
        " (SELECT AVG(r.rating) FROM kinemium_marketplace_reviews r"
        "  WHERE r.item_id = i.item_id) AS rating_average,"
        " (SELECT COUNT(*) FROM kinemium_marketplace_reviews r"
        "  WHERE r.item_id = i.item_id) AS rating_count"
        " FROM kinemium_marketplace_items i"
        " WHERE status = 'published' AND visibility = 'public'"
        " AND deleted_at IS NULL"
    )
    args: list[Any] = []
    if query is not None:
        pattern = "%" + _escape_like(query.lower()) + "%"
        sql += (
            " AND (LOWER(name) LIKE %s ESCAPE '\\'"
            " OR LOWER(description) LIKE %s ESCAPE '\\'"
            " OR tags_text LIKE %s ESCAPE '\\')"
        )
        args.extend([pattern, pattern, pattern])
    if item_type is not None:
        sql += " AND item_type = %s"
        args.append(item_type)
    if tag is not None:
        sql += " AND tags_text LIKE %s ESCAPE '\\'"
        args.append("% " + _escape_like(tag.lower()) + " %")
    if cursor is not None:
        if sort == "name":
            sql += " AND (LOWER(name) > %s OR (LOWER(name) = %s AND item_id > %s))"
            args.extend([cursor.get("name"), cursor.get("name"), cursor.get("itemId")])
        else:
            sql += " AND (published_at < %s OR (published_at = %s AND item_id < %s))"
            args.extend(
                [cursor.get("publishedAt"), cursor.get("publishedAt"), cursor.get("itemId")]
            )
    if sort == "name":
        sql += " ORDER BY LOWER(name) ASC, item_id ASC LIMIT %s"
    else:
        sql += " ORDER BY published_at DESC, item_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _owner_items(
    txn: Any,
    owner: str,
    is_owner: bool,
    status: str | None,
    limit: int,
    cursor_updated: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    sql = (
        f"SELECT {ITEM_COLUMNS},"
        " (SELECT AVG(r.rating) FROM kinemium_marketplace_reviews r"
        "  WHERE r.item_id = i.item_id) AS rating_average,"
        " (SELECT COUNT(*) FROM kinemium_marketplace_reviews r"
        "  WHERE r.item_id = i.item_id) AS rating_count"
        " FROM kinemium_marketplace_items i"
        " WHERE owner_user_id = %s AND deleted_at IS NULL"
    )
    args: list[Any] = [owner]
    if not is_owner:
        sql += " AND status = 'published' AND visibility = 'public'"
    if status is not None:
        sql += " AND status = %s"
        args.append(status)
    if cursor_updated is not None and cursor_id is not None:
        sql += " AND (updated_at < %s OR (updated_at = %s AND item_id < %s))"
        args.extend([cursor_updated, cursor_updated, cursor_id])
    sql += " ORDER BY updated_at DESC, item_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


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
            [re.compile(r"^/_kinemium/v1/marketplace/items$")],
            self.create_item,
            "KinemiumMarketplaceItemsServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/marketplace/items/(?P<item_id>[^/]+)/versions$")],
            self.publish_version,
            "KinemiumMarketplaceVersionsServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/marketplace/items/(?P<item_id>[^/]+)/versions$")],
            self.get_versions,
            "KinemiumMarketplaceVersionsServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/marketplace/items/(?P<item_id>[^/]+)/versions/(?P<version>[^/]+)/download$")],
            self.download_version,
            "KinemiumMarketplaceDownloadServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/marketplace/items/(?P<item_id>[^/]+)/publish$")],
            self.publish_item,
            "KinemiumMarketplacePublishServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/marketplace/items/(?P<item_id>[^/]+)/unpublish$")],
            self.unpublish_item,
            "KinemiumMarketplaceUnpublishServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/marketplace/items/(?P<item_id>[^/]+)/acquire$")],
            self.acquire_item,
            "KinemiumMarketplaceAcquireServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/marketplace/items/(?P<item_id>[^/]+)/reviews$")],
            self.get_reviews,
            "KinemiumMarketplaceReviewsServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/marketplace/items/(?P<item_id>[^/]+)/reviews$")],
            self.post_review,
            "KinemiumMarketplaceReviewsServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/marketplace/items/(?P<item_id>[^/]+)$")],
            self.delete_item,
            "KinemiumMarketplaceItemServlet",
        )
        resource.register_paths(
            "PATCH",
            [re.compile(r"^/_kinemium/v1/marketplace/items/(?P<item_id>[^/]+)$")],
            self.patch_item,
            "KinemiumMarketplaceItemServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/marketplace/items/(?P<item_id>[^/]+)$")],
            self.get_item,
            "KinemiumMarketplaceItemServlet",
        )

    def _item_payload(
        self,
        row: tuple,
        rating_average: float | None = None,
        rating_count: int = 0,
        can_manage: bool = False,
    ) -> dict[str, Any]:
        return {
            "itemId": row[0],
            "owner": row[1],
            "name": row[2],
            "description": row[3],
            "itemType": row[4],
            "tags": json_loads(row[5]),
            "visibility": row[7],
            "status": row[8],
            "priceAmount": int(row[9]),
            "priceCurrency": row[10],
            "packageId": row[11],
            "packageVersion": row[12],
            "createdAt": row[13],
            "updatedAt": row[14],
            "publishedAt": row[15],
            "rating": {"average": rating_average, "count": rating_count},
            "canManage": can_manage,
        }

    def _version_payload(self, row: tuple) -> dict[str, Any]:
        return {
            "version": row[2],
            "changelog": row[3],
            "filename": row[4],
            "digest": row[6],
            "size": int(row[7]),
            "createdAt": row[8],
        }

    async def _visible_item(
        self, item_id: str, caller: Caller | None
    ) -> tuple[tuple, bool]:
        validate_id(item_id, "itemId")
        row = await self.db.run(
            "kinemium_get_marketplace_item", _item_row, item_id
        )
        if row is None or row[16] is not None or row[8] == "removed":
            raise not_found("Marketplace item not found.")
        is_owner = caller is not None and caller.user_id == row[1]
        if not is_owner and not (
            row[8] == "published" and row[7] in ("public", "unlisted")
        ):
            raise not_found("Marketplace item not found.")
        return row, is_owner

    async def _detail(
        self, row: tuple, caller: Caller | None, is_owner: bool
    ) -> dict[str, Any]:
        versions, rating, acquisition_count, acquired = await self.db.run(
            "kinemium_marketplace_detail",
            lambda txn: (
                _versions(txn, row[0]),
                _rating(txn, row[0]),
                _acquisition_count(txn, row[0]),
                (
                    _has_acquisition(txn, row[0], caller.user_id)
                    if caller is not None
                    else False
                ),
            ),
        )
        rating_average, rating_count = rating
        payload = self._item_payload(
            row, rating_average, rating_count, can_manage=is_owner
        )
        payload["latestVersion"] = versions[0][2] if versions else None
        payload["versions"] = [self._version_payload(version) for version in versions]
        payload["acquisitionCount"] = acquisition_count
        payload["acquired"] = acquired
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
            raise invalid_request("Unknown marketplace item type.")
        tag = query_value(request, "tag", None, 32)
        if tag is not None:
            tag = tag.strip().lower()
            if not tag:
                tag = None
        sort = query_value(request, "sort", "recent", 16)
        if sort not in ("recent", "name"):
            raise invalid_request("sort must be recent or name.")
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            if sort == "name":
                if not isinstance(cursor.get("name"), str) or not isinstance(
                    cursor.get("itemId"), str
                ):
                    raise invalid_request("Invalid pagination cursor.")
            elif not isinstance(cursor.get("publishedAt"), int) or not isinstance(
                cursor.get("itemId"), str
            ):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_browse_marketplace",
            _browse,
            query,
            item_type,
            tag,
            sort,
            limit,
            cursor,
        )
        items = [self._item_payload(row, row[17], int(row[18])) for row in rows]
        result: dict[str, Any] = {"items": items, "sort": sort}
        if has_more and rows:
            if sort == "name":
                result["nextCursor"] = encode_cursor(
                    {"name": rows[-1][2].lower(), "itemId": rows[-1][0]}
                )
            else:
                result["nextCursor"] = encode_cursor(
                    {"publishedAt": rows[-1][15], "itemId": rows[-1][0]}
                )
        return 200, result

    async def create_item(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {
            "name",
            "description",
            "itemType",
            "tags",
            "visibility",
            "packageId",
            "packageVersion",
            "priceAmount",
            "priceCurrency",
        }
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown marketplace fields: " + ", ".join(unknown))
        name = require_str(body, "name", 1, 100).strip()
        if not name:
            raise invalid_request("name must not be blank.")
        description = optional_str(body, "description", 4000) or ""
        item_type = require_str(body, "itemType", 1, 16)
        if item_type not in ITEM_TYPES:
            raise invalid_request("Unknown marketplace item type.")
        tags = validate_tags(body.get("tags"), self.config.max_listing_tags)
        visibility = optional_str(body, "visibility", 16) or "public"
        if visibility not in VISIBILITIES:
            raise invalid_request("visibility must be public, unlisted, or private.")
        package_id = body.get("packageId")
        if package_id is not None and not isinstance(package_id, str):
            raise invalid_request("packageId must be a string or null.")
        package_version = body.get("packageVersion")
        if package_version is not None:
            if not isinstance(package_version, str):
                raise invalid_request("packageVersion must be a string or null.")
            try:
                parse_semver(package_version)
            except SemVerError as error:
                raise invalid_request(str(error)) from None
        price_amount = body.get("priceAmount", 0)
        if isinstance(price_amount, bool) or not isinstance(price_amount, int):
            raise invalid_request("priceAmount must be an integer.")
        if price_amount != 0:
            raise invalid_request("Paid marketplace listings are not supported.")
        price_currency = optional_str(body, "priceCurrency", 8) or "USD"
        if price_currency != "USD":
            raise invalid_request("Only USD is supported.")
        if item_type == "package" and package_id is None:
            raise invalid_request("Package listings require packageId.")
        row = await self.db.run(
            "kinemium_create_marketplace_item",
            _create_item,
            caller.user_id,
            name,
            description,
            item_type,
            tags,
            visibility,
            package_id,
            package_version,
            now_ms(),
        )
        return 201, await self._detail(row, caller, True)

    async def get_item(
        self, request: SynapseRequest, item_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_READ, required=False)
        self.enforce_rate_limit(request, caller)
        row, is_owner = await self._visible_item(item_id, caller)
        return 200, await self._detail(row, caller, is_owner)

    async def patch_item(
        self, request: SynapseRequest, item_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {
            "name",
            "description",
            "tags",
            "visibility",
            "packageId",
            "packageVersion",
        }
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown marketplace fields: " + ", ".join(unknown))
        name = None
        if "name" in body:
            name = require_str(body, "name", 1, 100).strip()
            if not name:
                raise invalid_request("name must not be blank.")
        description = None
        if "description" in body:
            description = optional_str(body, "description", 4000) or ""
        tags = None
        if "tags" in body:
            tags = validate_tags(body.get("tags"), self.config.max_listing_tags)
        visibility = None
        if "visibility" in body:
            visibility = optional_str(body, "visibility", 16)
            if visibility not in VISIBILITIES:
                raise invalid_request(
                    "visibility must be public, unlisted, or private."
                )
        package_id = None
        package_id_set = "packageId" in body
        if package_id_set:
            value = body.get("packageId")
            if value is not None and not isinstance(value, str):
                raise invalid_request("packageId must be a string or null.")
            package_id = value
        package_version = None
        package_version_set = "packageVersion" in body
        if package_version_set:
            value = body.get("packageVersion")
            if value is not None and not isinstance(value, str):
                raise invalid_request("packageVersion must be a string or null.")
            package_version = value
            if value is not None:
                try:
                    parse_semver(value)
                except SemVerError as error:
                    raise invalid_request(str(error)) from None
        row = await self.db.run(
            "kinemium_update_marketplace_item",
            _update_item,
            item_id,
            caller.user_id,
            name,
            description,
            tags,
            visibility,
            package_id,
            package_id_set,
            package_version,
            package_version_set,
            now_ms(),
            self.db.for_update(),
        )
        return 200, await self._detail(row, caller, True)

    async def delete_item(
        self, request: SynapseRequest, item_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(item_id, "itemId")
        row = await self.db.run(
            "kinemium_remove_marketplace_item",
            _set_status,
            item_id,
            caller.user_id,
            "removed",
            now_ms(),
            self.db.for_update(),
        )
        return 200, {"deleted": True, "itemId": row[0]}

    async def publish_item(
        self, request: SynapseRequest, item_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(item_id, "itemId")
        row = await self.db.run(
            "kinemium_publish_marketplace_item",
            _set_status,
            item_id,
            caller.user_id,
            "published",
            now_ms(),
            self.db.for_update(),
        )
        return 200, await self._detail(row, caller, True)

    async def unpublish_item(
        self, request: SynapseRequest, item_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(item_id, "itemId")
        row = await self.db.run(
            "kinemium_unpublish_marketplace_item",
            _set_status,
            item_id,
            caller.user_id,
            "unpublished",
            now_ms(),
            self.db.for_update(),
        )
        return 200, await self._detail(row, caller, True)

    async def get_versions(
        self, request: SynapseRequest, item_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_READ, required=False)
        self.enforce_rate_limit(request, caller)
        row, _is_owner = await self._visible_item(item_id, caller)
        versions = await self.db.run(
            "kinemium_get_marketplace_versions", _versions, row[0]
        )
        return 200, {
            "itemId": row[0],
            "versions": [self._version_payload(version) for version in versions],
        }

    async def publish_version(
        self, request: SynapseRequest, item_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(item_id, "itemId")
        fields, data, filename = parse_multipart(
            request, self.config.max_json_body_bytes
        )
        if data is None:
            raise invalid_request("A file is required.")
        version = fields.get("version")
        if not isinstance(version, str):
            raise invalid_request("version is required.")
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        changelog = check_str(
            fields.get("changelog", ""), "changelog", 0, 8000
        )
        safe_name = sanitize_filename(filename, f"{item_id}-{version}.bin")
        if validate_archive(
            data,
            safe_name,
            self.config.max_archive_entries,
            self.config.max_archive_uncompressed_bytes,
        ) is None and (
            safe_name.lower().endswith((".zip", ".tar", ".tar.gz", ".tgz"))
        ):
            raise invalid_request("The uploaded file is not a valid archive.")
        storage_key = self.storage.new_key(
            "marketplace", f"{item_id}/{version}"
        )
        digest, size = await self.storage.write(storage_key, data)
        try:
            row = await self.db.run(
                "kinemium_publish_marketplace_version",
                _publish_version,
                item_id,
                caller.user_id,
                version,
                changelog,
                safe_name,
                storage_key,
                digest,
                size,
                self.config.max_marketplace_versions_per_item,
                now_ms(),
                self.db.for_update(),
            )
        except Exception:
            await self.storage.delete(storage_key)
            raise
        return 201, {
            "itemId": item_id,
            "version": self._version_payload(row),
        }

    async def download_version(
        self, request: SynapseRequest, item_id: str, version: str
    ) -> None:
        caller = await self.authenticate(request, MARKETPLACE_READ, required=False)
        self.enforce_rate_limit(request, caller)
        row, _is_owner = await self._visible_item(item_id, caller)
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        version_row = await self.db.run(
            "kinemium_get_marketplace_version", _version_row, row[0], version
        )
        if version_row is None:
            raise not_found("Item version not found.")
        data = await self.storage.read(version_row[5])
        send_file(
            request,
            data,
            version_row[4],
            content_type_for_filename(version_row[4]),
            version_row[6],
            {"X-Kinemium-Version": version},
        )

    async def acquire_item(
        self, request: SynapseRequest, item_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_ACQUIRE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(item_id, "itemId")
        acquisition, created = await self.db.run(
            "kinemium_acquire_marketplace_item",
            _acquire,
            item_id,
            caller.user_id,
            now_ms(),
            self.db.for_update(),
        )
        return (201 if created else 200), {
            "acquired": True,
            "acquisition": {
                "acquisitionId": acquisition[0],
                "itemId": acquisition[1],
                "userId": acquisition[2],
                "createdAt": acquisition[3],
            },
        }

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
            "kinemium_list_acquisitions",
            _acquisitions,
            caller.user_id,
            limit,
            cursor_created,
            cursor_id,
        )
        acquisitions = [
            {
                "acquisitionId": row[0],
                "itemId": row[1],
                "userId": row[2],
                "createdAt": row[3],
                "item": self._item_payload(row[4:21]),
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
        self, request: SynapseRequest, item_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_READ, required=False)
        self.enforce_rate_limit(request, caller)
        row, _is_owner = await self._visible_item(item_id, caller)
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
            "kinemium_list_reviews",
            _reviews,
            row[0],
            limit,
            cursor_created,
            cursor_id,
        )
        average, count = await self.db.run(
            "kinemium_marketplace_rating", _rating, row[0]
        )
        result: dict[str, Any] = {
            "reviews": [
                {
                    "reviewId": review[0],
                    "itemId": review[1],
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

    async def post_review(
        self, request: SynapseRequest, item_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        rating = body.get("rating")
        if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
            raise invalid_request("rating must be an integer between 1 and 5.")
        body_text = optional_str(body, "body", self.config.max_review_length) or ""
        row, created = await self.db.run(
            "kinemium_upsert_review",
            _upsert_review,
            item_id,
            caller.user_id,
            rating,
            body_text,
            now_ms(),
            self.db.for_update(),
        )
        return (201 if created else 200), {
            "review": {
                "reviewId": row[0],
                "itemId": row[1],
                "userId": row[2],
                "rating": int(row[3]),
                "body": row[4],
                "createdAt": row[5],
                "updatedAt": row[6],
            }
        }

    async def list_owner_items(
        self, request: SynapseRequest, user_id: str
    ) -> dict[str, Any]:
        caller = await self.authenticate(request, MARKETPLACE_READ, required=False)
        self.enforce_rate_limit(request, caller)
        is_owner = caller is not None and caller.user_id == user_id
        limit = self.page_limit(request)
        status = query_value(request, "status", None, 16)
        if status is not None and status not in ("draft", "published", "unpublished"):
            raise invalid_request("Unknown marketplace status.")
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_updated = None
        cursor_id = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_updated = cursor.get("updatedAt")
            cursor_id = cursor.get("itemId")
            if not isinstance(cursor_updated, int) or not isinstance(cursor_id, str):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_list_owner_marketplace",
            _owner_items,
            user_id,
            is_owner,
            status,
            limit,
            cursor_updated,
            cursor_id,
        )
        items = [
            self._item_payload(row, row[17], int(row[18]), can_manage=is_owner)
            for row in rows
        ]
        result: dict[str, Any] = {"items": items}
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"updatedAt": rows[-1][14], "itemId": rows[-1][0]}
            )
        return result
