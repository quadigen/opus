from __future__ import annotations

import math
import re
import uuid
from typing import Any

from synapse.http.site import SynapseRequest

from .auth import (
    DATASTORES_READ,
    DATASTORES_WRITE,
    Caller,
)
from .base import Service
from .errors import conflict, forbidden, invalid_request, not_found
from .http import (
    parse_json_body,
    query_bool,
    query_value,
)
from .util import (
    decode_cursor,
    encode_cursor,
    json_dumps,
    json_loads,
    now_ms,
    parse_if_match,
    quoted_etag,
    validate_json_value,
)
from .validation import (
    optional_str,
    require_list,
    require_number,
    require_str,
    validate_entry_key,
    validate_id,
)

STORE_COLUMNS = (
    "store_id, game_id, owner_user_id, name, description, config,"
    " created_at, updated_at"
)
ENTRY_COLUMNS = "entry_key, value, version, created_at, updated_at"


def _store_row(txn: Any, store_id: str) -> tuple | None:
    txn.execute(
        f"SELECT {STORE_COLUMNS} FROM kinemium_datastores WHERE store_id = %s",
        (store_id,),
    )
    return txn.fetchone()


def _lock_store(txn: Any, store_id: str, for_update: str) -> tuple | None:
    txn.execute(
        f"SELECT {STORE_COLUMNS} FROM kinemium_datastores WHERE store_id = %s"
        + for_update,
        (store_id,),
    )
    return txn.fetchone()


def _list_stores(
    txn: Any,
    owner: str | None,
    game_id: str | None,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    sql = f"SELECT {STORE_COLUMNS} FROM kinemium_datastores WHERE 1=1"
    args: list[Any] = []
    if owner is not None:
        sql += " AND owner_user_id = %s"
        args.append(owner)
    if game_id is not None:
        sql += " AND game_id = %s"
        args.append(game_id)
    if cursor_created is not None and cursor_id is not None:
        sql += " AND (created_at < %s OR (created_at = %s AND store_id < %s))"
        args.extend([cursor_created, cursor_created, cursor_id])
    sql += " ORDER BY created_at DESC, store_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _create_store(
    txn: Any,
    owner: str,
    game_id: str,
    store_id: str,
    name: str,
    description: str,
    config_json: str,
    now: int,
    max_per_game: int,
    max_per_user: int,
    for_update: str,
) -> tuple:
    txn.execute(
        "SELECT game_id FROM kinemium_games WHERE game_id = %s" + for_update,
        (game_id,),
    )
    if txn.fetchone() is None:
        raise not_found("Game not found.")
    txn.execute(
        "SELECT COUNT(*) FROM kinemium_datastores WHERE game_id = %s", (game_id,)
    )
    count = txn.fetchone()
    if count is not None and int(count[0]) >= max_per_game:
        raise conflict("Datastore limit reached for this game.", {"reason": "quota_exceeded"})
    txn.execute(
        "SELECT COUNT(*) FROM kinemium_datastores WHERE owner_user_id = %s", (owner,)
    )
    count = txn.fetchone()
    if count is not None and int(count[0]) >= max_per_user:
        raise conflict("Datastore limit reached for this account.", {"reason": "quota_exceeded"})
    txn.execute(
        "SELECT store_id FROM kinemium_datastores WHERE game_id = %s AND name = %s",
        (game_id, name),
    )
    if txn.fetchone() is not None:
        raise conflict("A datastore with that name already exists.")
    txn.execute(
        f"INSERT INTO kinemium_datastores ({STORE_COLUMNS})"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (store_id, game_id, owner, name, description, config_json, now, now),
    )
    return (store_id, game_id, owner, name, description, config_json, now, now)


def _update_store(
    txn: Any,
    store_id: str,
    name: str,
    description: str,
    config_json: str,
    now: int,
) -> tuple:
    txn.execute(
        "SELECT game_id FROM kinemium_datastores WHERE store_id = %s", (store_id,)
    )
    game_row = txn.fetchone()
    if game_row is None:
        raise not_found("Datastore not found.")
    txn.execute(
        "SELECT store_id FROM kinemium_datastores WHERE game_id = %s AND name = %s",
        (game_row[0], name),
    )
    existing = txn.fetchone()
    if existing is not None and existing[0] != store_id:
        raise conflict("A datastore with that name already exists.")
    txn.execute(
        "UPDATE kinemium_datastores SET name = %s, description = %s,"
        " config = %s, updated_at = %s WHERE store_id = %s",
        (name, description, config_json, now, store_id),
    )
    row = _store_row(txn, store_id)
    if row is None:
        raise not_found("Datastore not found.")
    return row


def _delete_store(txn: Any, store_id: str) -> None:
    txn.execute("DELETE FROM kinemium_datastores WHERE store_id = %s", (store_id,))


def _get_entry(txn: Any, store_id: str, key: str) -> tuple | None:
    txn.execute(
        f"SELECT {ENTRY_COLUMNS} FROM kinemium_datastore_entries"
        " WHERE store_id = %s AND entry_key = %s",
        (store_id, key),
    )
    return txn.fetchone()


def _count_entries(txn: Any, store_id: str) -> int:
    txn.execute(
        "SELECT COUNT(*) FROM kinemium_datastore_entries WHERE store_id = %s",
        (store_id,),
    )
    row = txn.fetchone()
    return int(row[0]) if row is not None else 0


def _put_entry(
    txn: Any,
    store_id: str,
    key: str,
    value_json: str,
    expected: int | None,
    now: int,
    maximum: int,
    for_update: str,
) -> tuple:
    if _lock_store(txn, store_id, for_update) is None:
        raise not_found("Datastore not found.")
    current = _get_entry(txn, store_id, key)
    if current is None:
        if expected is not None and expected != 0:
            raise conflict("Entry version conflict.", {"currentVersion": 0})
        if _count_entries(txn, store_id) >= maximum:
            raise conflict("Datastore entry limit reached.", {"reason": "quota_exceeded"})
        txn.execute(
            "INSERT INTO kinemium_datastore_entries"
            " (store_id, entry_key, value, version, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (store_id, key, value_json, 1, now, now),
        )
        return (key, value_json, 1, now, now)
    current_version = int(current[2])
    if expected is not None and expected != current_version:
        raise conflict(
            "Entry version conflict.",
            {"currentVersion": current_version},
        )
    new_version = current_version + 1
    txn.execute(
        "UPDATE kinemium_datastore_entries SET value = %s, version = %s,"
        " updated_at = %s WHERE store_id = %s AND entry_key = %s",
        (value_json, new_version, now, store_id, key),
    )
    return (key, value_json, new_version, current[3], now)


def _patch_entry(
    txn: Any,
    store_id: str,
    key: str,
    operation: str,
    operand: Any,
    expected: int | None,
    now: int,
    max_entries: int,
    maximum: int,
    for_update: str,
) -> tuple:
    if _lock_store(txn, store_id, for_update) is None:
        raise not_found("Datastore not found.")
    current = _get_entry(txn, store_id, key)
    if current is None:
        if expected is not None and expected != 0:
            raise conflict("Entry version conflict.", {"currentVersion": 0})
        if _count_entries(txn, store_id) >= max_entries:
            raise conflict("Datastore entry limit reached.", {"reason": "quota_exceeded"})
        current_value = None
        current_version = 0
        created_at = now
    else:
        current_value = json_loads(current[1])
        current_version = int(current[2])
        created_at = current[3]
        if expected is not None and expected != current_version:
            raise conflict(
                "Entry version conflict.",
                {"currentVersion": current_version},
            )
    if operation == "set":
        new_value = operand
    elif operation == "increment":
        base = current_value if current_value is not None else 0
        if isinstance(base, bool) or not isinstance(base, (int, float)):
            raise conflict("Entry value is not numeric.")
        if isinstance(base, int) and isinstance(operand, int):
            new_value = base + operand
        else:
            new_value = float(base) + float(operand)
    elif operation == "merge":
        if current_value is None:
            base = {}
        elif isinstance(current_value, dict):
            base = current_value
        else:
            raise conflict("Entry value is not an object.")
        if not isinstance(operand, dict):
            raise invalid_request("merge requires an object value.")
        new_value = dict(base)
        new_value.update(operand)
    elif operation == "append":
        if current_value is None:
            base = []
        elif isinstance(current_value, list):
            base = current_value
        else:
            raise conflict("Entry value is not an array.")
        if not isinstance(operand, list):
            raise invalid_request("append requires an array value.")
        new_value = list(base) + list(operand)
    else:
        raise invalid_request("Unsupported update operation.")
    new_value = validate_json_value(new_value, maximum)
    value_json = json_dumps(new_value)
    new_version = current_version + 1
    if current is None:
        txn.execute(
            "INSERT INTO kinemium_datastore_entries"
            " (store_id, entry_key, value, version, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (store_id, key, value_json, new_version, now, now),
        )
    else:
        txn.execute(
            "UPDATE kinemium_datastore_entries SET value = %s, version = %s,"
            " updated_at = %s WHERE store_id = %s AND entry_key = %s",
            (value_json, new_version, now, store_id, key),
        )
    return (key, value_json, new_version, created_at, now)


def _delete_entry(
    txn: Any,
    store_id: str,
    key: str,
    expected: int | None,
    for_update: str,
) -> None:
    if _lock_store(txn, store_id, for_update) is None:
        raise not_found("Datastore not found.")
    current = _get_entry(txn, store_id, key)
    if current is None:
        raise not_found("Entry not found.")
    current_version = int(current[2])
    if expected is not None and expected != current_version:
        raise conflict(
            "Entry version conflict.",
            {"currentVersion": current_version},
        )
    txn.execute(
        "DELETE FROM kinemium_datastore_entries WHERE store_id = %s AND entry_key = %s",
        (store_id, key),
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _list_keys(
    txn: Any,
    store_id: str,
    prefix: str,
    cursor: str | None,
    limit: int,
) -> tuple[list[str], bool]:
    sql = (
        "SELECT entry_key FROM kinemium_datastore_entries"
        " WHERE store_id = %s AND entry_key LIKE %s ESCAPE '\\'"
    )
    args: list[Any] = [store_id, _escape_like(prefix) + "%"]
    if cursor is not None:
        sql += " AND entry_key > %s"
        args.append(cursor)
    sql += " ORDER BY entry_key ASC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = [row[0] for row in txn.fetchall()]
    return rows[:limit], len(rows) > limit


def _get_ordered(txn: Any, store_id: str, key: str) -> tuple | None:
    txn.execute(
        "SELECT entry_key, score, value, version, created_at, updated_at"
        " FROM kinemium_datastore_ordered_entries"
        " WHERE store_id = %s AND entry_key = %s",
        (store_id, key),
    )
    return txn.fetchone()


def _count_ordered(txn: Any, store_id: str) -> int:
    txn.execute(
        "SELECT COUNT(*) FROM kinemium_datastore_ordered_entries WHERE store_id = %s",
        (store_id,),
    )
    row = txn.fetchone()
    return int(row[0]) if row is not None else 0


def _put_ordered(
    txn: Any,
    store_id: str,
    key: str,
    score: float,
    value_json: str | None,
    has_value: bool,
    expected: int | None,
    now: int,
    maximum: int,
    for_update: str,
) -> tuple:
    if _lock_store(txn, store_id, for_update) is None:
        raise not_found("Datastore not found.")
    current = _get_ordered(txn, store_id, key)
    if current is None:
        if expected is not None and expected != 0:
            raise conflict("Entry version conflict.", {"currentVersion": 0})
        if _count_ordered(txn, store_id) >= maximum:
            raise conflict(
                "Ordered entry limit reached.", {"reason": "quota_exceeded"}
            )
        txn.execute(
            "INSERT INTO kinemium_datastore_ordered_entries"
            " (store_id, entry_key, score, value, version, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (store_id, key, score, value_json, 1, now, now),
        )
        return (key, score, value_json, 1, now, now)
    current_version = int(current[3])
    if expected is not None and expected != current_version:
        raise conflict(
            "Entry version conflict.", {"currentVersion": current_version}
        )
    new_version = current_version + 1
    new_value = value_json if has_value else current[2]
    txn.execute(
        "UPDATE kinemium_datastore_ordered_entries SET score = %s, value = %s,"
        " version = %s, updated_at = %s WHERE store_id = %s AND entry_key = %s",
        (score, new_value, new_version, now, store_id, key),
    )
    return (key, score, new_value, new_version, current[4], now)


def _list_ordered(
    txn: Any,
    store_id: str,
    order: str,
    minimum: float | None,
    maximum: float | None,
    cursor_score: float | None,
    cursor_key: str | None,
    limit: int,
) -> tuple[list[tuple], bool]:
    sql = (
        "SELECT entry_key, score, value, version, created_at, updated_at"
        " FROM kinemium_datastore_ordered_entries WHERE store_id = %s"
    )
    args: list[Any] = [store_id]
    if minimum is not None:
        sql += " AND score >= %s"
        args.append(minimum)
    if maximum is not None:
        sql += " AND score <= %s"
        args.append(maximum)
    if cursor_score is not None and cursor_key is not None:
        if order == "asc":
            sql += " AND (score > %s OR (score = %s AND entry_key > %s))"
        else:
            sql += " AND (score < %s OR (score = %s AND entry_key < %s))"
        args.extend([cursor_score, cursor_score, cursor_key])
    direction = "ASC" if order == "asc" else "DESC"
    sql += f" ORDER BY score {direction}, entry_key {direction} LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _game_owner(txn: Any, game_id: str) -> tuple | None:
    txn.execute(
        "SELECT owner_user_id FROM kinemium_games WHERE game_id = %s",
        (game_id,),
    )
    return txn.fetchone()


def _batch_read(txn: Any, store_id: str, keys: list[str]) -> dict[str, tuple]:
    if not keys:
        return {}
    placeholders = ",".join(["%s"] * len(keys))
    txn.execute(
        f"SELECT {ENTRY_COLUMNS} FROM kinemium_datastore_entries"
        f" WHERE store_id = %s AND entry_key IN ({placeholders})",
        tuple([store_id] + keys),
    )
    return {row[0]: row for row in txn.fetchall()}


def _batch_write(
    txn: Any,
    store_id: str,
    entries: list[tuple[str, str, int | None]],
    now: int,
    maximum: int,
    for_update: str,
) -> list[tuple]:
    if _lock_store(txn, store_id, for_update) is None:
        raise not_found("Datastore not found.")
    existing_count = _count_entries(txn, store_id)
    created = 0
    results: list[tuple] = []
    for key, value_json, expected in entries:
        current = _get_entry(txn, store_id, key)
        if current is None:
            if expected is not None and expected != 0:
                raise conflict("Entry version conflict.", {"currentVersion": 0, "key": key})
            created += 1
            if existing_count + created > maximum:
                raise conflict(
                    "Datastore entry limit reached.",
                    {"reason": "quota_exceeded"},
                )
            txn.execute(
                "INSERT INTO kinemium_datastore_entries"
                " (store_id, entry_key, value, version, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (store_id, key, value_json, 1, now, now),
            )
            results.append((key, value_json, 1, now, now))
        else:
            current_version = int(current[2])
            if expected is not None and expected != current_version:
                raise conflict(
                    "Entry version conflict.",
                    {"currentVersion": current_version, "key": key},
                )
            new_version = current_version + 1
            txn.execute(
                "UPDATE kinemium_datastore_entries SET value = %s, version = %s,"
                " updated_at = %s WHERE store_id = %s AND entry_key = %s",
                (value_json, new_version, now, store_id, key),
            )
            results.append((key, value_json, new_version, current[3], now))
    return results


class DataStoreService(Service):
    def register(self, resource: Any) -> None:
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/datastores$")],
            self.list_datastores,
            "KinemiumDataStoresServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/datastores$")],
            self.create_datastore,
            "KinemiumDataStoresServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/datastores/(?P<store_id>[^/]+)/entries/batch-read$")],
            self.batch_read,
            "KinemiumDataStoreBatchReadServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/datastores/(?P<store_id>[^/]+)/entries/batch-write$")],
            self.batch_write,
            "KinemiumDataStoreBatchWriteServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/datastores/(?P<store_id>[^/]+)/entries$")],
            self.list_entries,
            "KinemiumDataStoreEntriesServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/datastores/(?P<store_id>[^/]+)/entries/(?P<key>[^/]+)$")],
            self.get_entry,
            "KinemiumDataStoreEntryServlet",
        )
        resource.register_paths(
            "PUT",
            [re.compile(r"^/_kinemium/v1/datastores/(?P<store_id>[^/]+)/entries/(?P<key>[^/]+)$")],
            self.put_entry,
            "KinemiumDataStoreEntryServlet",
        )
        resource.register_paths(
            "PATCH",
            [re.compile(r"^/_kinemium/v1/datastores/(?P<store_id>[^/]+)/entries/(?P<key>[^/]+)$")],
            self.patch_entry,
            "KinemiumDataStoreEntryServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/datastores/(?P<store_id>[^/]+)/entries/(?P<key>[^/]+)$")],
            self.delete_entry,
            "KinemiumDataStoreEntryServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/datastores/(?P<store_id>[^/]+)/ordered-entries$")],
            self.list_ordered,
            "KinemiumDataStoreOrderedServlet",
        )
        resource.register_paths(
            "PUT",
            [re.compile(r"^/_kinemium/v1/datastores/(?P<store_id>[^/]+)/ordered-entries/(?P<key>[^/]+)$")],
            self.put_ordered,
            "KinemiumDataStoreOrderedEntryServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/datastores/(?P<store_id>[^/]+)$")],
            self.get_datastore,
            "KinemiumDataStoreServlet",
        )
        resource.register_paths(
            "PATCH",
            [re.compile(r"^/_kinemium/v1/datastores/(?P<store_id>[^/]+)$")],
            self.patch_datastore,
            "KinemiumDataStoreServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/datastores/(?P<store_id>[^/]+)$")],
            self.delete_datastore,
            "KinemiumDataStoreServlet",
        )

    def _store_payload(self, row: tuple) -> dict[str, Any]:
        return {
            "storeId": row[0],
            "gameId": row[1],
            "owner": row[2],
            "name": row[3],
            "description": row[4],
            "config": json_loads(row[5]),
            "createdAt": row[6],
            "updatedAt": row[7],
        }

    def _entry_payload(self, row: tuple) -> dict[str, Any]:
        return {
            "key": row[0],
            "value": json_loads(row[1]),
            "version": int(row[2]),
            "createdAt": row[3],
            "updatedAt": row[4],
        }

    def _ordered_payload(self, row: tuple) -> dict[str, Any]:
        return {
            "key": row[0],
            "score": float(row[1]),
            "value": json_loads(row[2]) if row[2] is not None else None,
            "version": int(row[3]),
            "createdAt": row[4],
            "updatedAt": row[5],
        }

    async def _require_store(
        self,
        caller: Caller,
        store_id: str,
        write: bool,
    ) -> tuple:
        validate_id(store_id, "storeId")
        row = await self.db.run("kinemium_get_store", _store_row, store_id)
        if row is None:
            raise not_found("Datastore not found.")
        if caller.game_id is not None:
            if row[1] != caller.game_id:
                raise forbidden("This credential cannot access that datastore.")
        elif row[2] != caller.user_id:
            raise forbidden("You do not own that datastore.")
        return row

    async def _resolve_create_game(
        self, caller: Caller, requested: str | None
    ) -> str:
        if caller.game_id is not None:
            if requested is not None and requested != caller.game_id:
                raise forbidden("This credential is bound to another game.")
            return caller.game_id
        if requested is None:
            raise invalid_request("gameId is required.")
        validate_id(requested, "gameId")
        row = await self.db.run(
            "kinemium_get_game_owner", _game_owner, requested
        )
        if row is None:
            raise not_found("Game not found.")
        if row[0] != caller.user_id:
            raise forbidden("You do not own that game.")
        return requested

    async def list_datastores(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, DATASTORES_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller, datastore=True)
        limit = self.page_limit(request)
        game_id: str | None = None
        owner: str | None = caller.user_id
        if caller.game_id is not None:
            game_id = caller.game_id
            owner = None
        else:
            requested_game = query_value(request, "gameId", None, 128)
            if requested_game is not None:
                validate_id(requested_game, "gameId")
                game_id = await self._resolve_create_game(caller, requested_game)
                owner = None
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_created: int | None = None
        cursor_id: str | None = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_created = cursor.get("createdAt")
            cursor_id = cursor.get("storeId")
            if (
                not isinstance(cursor_created, int)
                or not isinstance(cursor_id, str)
            ):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_list_datastores",
            _list_stores,
            owner,
            game_id,
            limit,
            cursor_created,
            cursor_id,
        )
        result: dict[str, Any] = {
            "datastores": [self._store_payload(row) for row in rows]
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][6], "storeId": rows[-1][0]}
            )
        return 200, result

    async def create_datastore(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, DATASTORES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller, datastore=True)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        requested_game = body.get("gameId")
        if requested_game is not None and not isinstance(requested_game, str):
            raise invalid_request("gameId must be a string.")
        game_id = await self._resolve_create_game(caller, requested_game)
        name = require_str(body, "name", 1, 64).strip()
        if not name:
            raise invalid_request("name must not be blank.")
        description = optional_str(body, "description", 2000) or ""
        config = body.get("config", {})
        if not isinstance(config, dict):
            raise invalid_request("config must be an object.")
        validate_json_value(config, min(self.config.max_datastore_value_bytes, 16384))
        now = now_ms()
        row = await self.db.run(
            "kinemium_create_datastore",
            _create_store,
            caller.user_id,
            game_id,
            "store_" + uuid.uuid4().hex,
            name,
            description,
            self.db.json(config),
            now,
            self.config.max_datastores_per_game,
            self.config.max_datastores_per_user,
            self.db.for_update(),
        )
        return 201, self._store_payload(row)

    async def get_datastore(
        self, request: SynapseRequest, store_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, DATASTORES_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller, datastore=True)
        row = await self._require_store(caller, store_id, False)
        return 200, self._store_payload(row)

    async def patch_datastore(
        self, request: SynapseRequest, store_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, DATASTORES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller, datastore=True)
        row = await self._require_store(caller, store_id, True)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {"name", "description", "config"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown datastore fields: " + ", ".join(unknown))
        name = row[3]
        description = row[4]
        config = json_loads(row[5])
        if "name" in body:
            name = require_str(body, "name", 1, 64).strip()
            if not name:
                raise invalid_request("name must not be blank.")
        if "description" in body:
            description = optional_str(body, "description", 2000) or ""
        if "config" in body:
            config = body.get("config")
            if not isinstance(config, dict):
                raise invalid_request("config must be an object.")
            validate_json_value(
                config, min(self.config.max_datastore_value_bytes, 16384)
            )
        updated = await self.db.run(
            "kinemium_update_datastore",
            _update_store,
            store_id,
            name,
            description,
            self.db.json(config),
            now_ms(),
        )
        return 200, self._store_payload(updated)

    async def delete_datastore(
        self, request: SynapseRequest, store_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, DATASTORES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller, datastore=True)
        await self._require_store(caller, store_id, True)
        if not bool(query_bool(request, "confirm", False)):
            raise invalid_request("Datastore deletion requires confirm=true.")
        await self.db.run("kinemium_delete_datastore", _delete_store, store_id)
        return 200, {"deleted": True, "storeId": store_id}

    def _expected_version(
        self, request: SynapseRequest, body: dict[str, Any]
    ) -> int | None:
        header_value = parse_if_match(request.getHeader(b"If-Match"))
        body_value = body.get("expectedVersion")
        if body_value is not None and (
            isinstance(body_value, bool) or not isinstance(body_value, int)
        ):
            raise invalid_request("expectedVersion must be an integer.")
        if body_value is not None and body_value < 0:
            raise invalid_request("expectedVersion must not be negative.")
        if header_value is not None and body_value is not None and header_value != body_value:
            raise invalid_request("If-Match and expectedVersion disagree.")
        return body_value if body_value is not None else header_value

    async def get_entry(
        self, request: SynapseRequest, store_id: str, key: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, DATASTORES_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller, datastore=True)
        await self._require_store(caller, store_id, False)
        validate_entry_key(key, self.config.max_datastore_key_length)
        row = await self.db.run("kinemium_get_entry", _get_entry, store_id, key)
        if row is None:
            raise not_found("Entry not found.")
        request.setHeader(b"ETag", quoted_etag(int(row[2])).encode("ascii"))
        return 200, self._entry_payload(row)

    async def put_entry(
        self, request: SynapseRequest, store_id: str, key: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, DATASTORES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller, datastore=True)
        await self._require_store(caller, store_id, True)
        validate_entry_key(key, self.config.max_datastore_key_length)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        if "value" not in body:
            raise invalid_request("value is required.")
        value = validate_json_value(body.get("value"), self.config.max_datastore_value_bytes)
        expected = self._expected_version(request, body)
        row = await self.db.run(
            "kinemium_put_entry",
            _put_entry,
            store_id,
            key,
            self.db.json(value),
            expected,
            now_ms(),
            self.config.max_entries_per_store,
            self.db.for_update(),
        )
        request.setHeader(b"ETag", quoted_etag(int(row[2])).encode("ascii"))
        return 200, self._entry_payload(row)

    async def patch_entry(
        self, request: SynapseRequest, store_id: str, key: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, DATASTORES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller, datastore=True)
        await self._require_store(caller, store_id, True)
        validate_entry_key(key, self.config.max_datastore_key_length)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        operation = body.get("operation")
        if operation not in ("set", "increment", "merge", "append"):
            raise invalid_request(
                "operation must be set, increment, merge, or append."
            )
        if operation == "set":
            if "value" not in body:
                raise invalid_request("value is required for set.")
            operand = body.get("value")
        elif operation == "increment":
            operand = body.get("amount")
            if isinstance(operand, bool) or not isinstance(operand, (int, float)):
                raise invalid_request("amount must be a number.")
            if not math.isfinite(float(operand)):
                raise invalid_request("amount must be finite.")
        else:
            operand = body.get("value")
        validate_json_value(operand, self.config.max_datastore_value_bytes)
        expected = self._expected_version(request, body)
        row = await self.db.run(
            "kinemium_patch_entry",
            _patch_entry,
            store_id,
            key,
            operation,
            operand,
            expected,
            now_ms(),
            self.config.max_entries_per_store,
            self.config.max_datastore_value_bytes,
            self.db.for_update(),
        )
        request.setHeader(b"ETag", quoted_etag(int(row[2])).encode("ascii"))
        return 200, self._entry_payload(row)

    async def delete_entry(
        self, request: SynapseRequest, store_id: str, key: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, DATASTORES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller, datastore=True)
        await self._require_store(caller, store_id, True)
        validate_entry_key(key, self.config.max_datastore_key_length)
        expected = parse_if_match(request.getHeader(b"If-Match"))
        await self.db.run(
            "kinemium_delete_entry",
            _delete_entry,
            store_id,
            key,
            expected,
            self.db.for_update(),
        )
        return 200, {"deleted": True, "key": key}

    async def list_entries(
        self, request: SynapseRequest, store_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, DATASTORES_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller, datastore=True)
        await self._require_store(caller, store_id, False)
        limit = self.page_limit(request)
        prefix = query_value(request, "prefix", "", self.config.max_datastore_key_length)
        assert prefix is not None
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor = None
        if cursor_raw is not None:
            value = decode_cursor(cursor_raw).get("key")
            if not isinstance(value, str):
                raise invalid_request("Invalid pagination cursor.")
            cursor = value
        rows, has_more = await self.db.run(
            "kinemium_list_entry_keys",
            _list_keys,
            store_id,
            prefix,
            cursor,
            limit,
        )
        result: dict[str, Any] = {"keys": rows}
        if has_more and rows:
            result["nextCursor"] = encode_cursor({"key": rows[-1]})
        return 200, result

    async def batch_read(
        self, request: SynapseRequest, store_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, DATASTORES_READ, required=True)
        assert caller is not None
        body = parse_json_body(request, self.config.max_json_body_bytes)
        keys = require_list(body, "keys", self.config.max_datastore_batch)
        parsed: list[str] = []
        seen: set[str] = set()
        for key in keys:
            if not isinstance(key, str):
                raise invalid_request("keys must contain strings.")
            validate_entry_key(key, self.config.max_datastore_key_length)
            if key in seen:
                raise invalid_request("keys must not contain duplicates.")
            seen.add(key)
            parsed.append(key)
        self.enforce_rate_limit(
            request, caller, datastore=True, cost=max(1.0, len(parsed) / 10.0)
        )
        await self._require_store(caller, store_id, False)
        rows = await self.db.run("kinemium_batch_read", _batch_read, store_id, parsed)
        entries = []
        for key in parsed:
            row = rows.get(key)
            entries.append(
                {"key": key, "entry": self._entry_payload(row) if row else None}
            )
        return 200, {"entries": entries}

    async def batch_write(
        self, request: SynapseRequest, store_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, DATASTORES_WRITE, required=True)
        assert caller is not None
        body = parse_json_body(request, self.config.max_json_body_bytes)
        entries = require_list(body, "entries", self.config.max_datastore_batch)
        parsed: list[tuple[str, str, int | None]] = []
        seen: set[str] = set()
        for item in entries:
            if not isinstance(item, dict):
                raise invalid_request("entries must contain objects.")
            key = item.get("key")
            if not isinstance(key, str):
                raise invalid_request("Each entry requires a string key.")
            validate_entry_key(key, self.config.max_datastore_key_length)
            if key in seen:
                raise invalid_request("entries must not contain duplicate keys.")
            seen.add(key)
            if "value" not in item:
                raise invalid_request("Each entry requires a value.")
            value = validate_json_value(
                item.get("value"), self.config.max_datastore_value_bytes
            )
            expected = item.get("expectedVersion")
            if expected is not None and (
                isinstance(expected, bool) or not isinstance(expected, int) or expected < 0
            ):
                raise invalid_request("expectedVersion must be a non-negative integer.")
            parsed.append((key, self.db.json(value), expected))
        self.enforce_rate_limit(
            request, caller, datastore=True, cost=max(1.0, len(parsed) / 10.0)
        )
        await self._require_store(caller, store_id, True)
        rows = await self.db.run(
            "kinemium_batch_write",
            _batch_write,
            store_id,
            parsed,
            now_ms(),
            self.config.max_entries_per_store,
            self.db.for_update(),
        )
        return 200, {"entries": [self._entry_payload(row) for row in rows]}

    async def put_ordered(
        self, request: SynapseRequest, store_id: str, key: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, DATASTORES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller, datastore=True)
        await self._require_store(caller, store_id, True)
        validate_entry_key(key, self.config.max_datastore_key_length)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        score = require_number(body, "score")
        if not math.isfinite(score):
            raise invalid_request("score must be finite.")
        has_value = "value" in body
        value_json = None
        if has_value:
            value = validate_json_value(
                body.get("value"), self.config.max_datastore_value_bytes
            )
            value_json = self.db.json(value)
        expected = self._expected_version(request, body)
        row = await self.db.run(
            "kinemium_put_ordered",
            _put_ordered,
            store_id,
            key,
            score,
            value_json,
            has_value,
            expected,
            now_ms(),
            self.config.max_ordered_entries_per_store,
            self.db.for_update(),
        )
        request.setHeader(b"ETag", quoted_etag(int(row[3])).encode("ascii"))
        return 200, self._ordered_payload(row)

    async def list_ordered(
        self, request: SynapseRequest, store_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, DATASTORES_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller, datastore=True)
        await self._require_store(caller, store_id, False)
        limit = self.page_limit(request)
        order = query_value(request, "order", "asc", 8)
        if order not in ("asc", "desc"):
            raise invalid_request("order must be asc or desc.")
        minimum_raw = query_value(request, "minScore", None, 64)
        maximum_raw = query_value(request, "maxScore", None, 64)
        minimum = float(minimum_raw) if minimum_raw is not None else None
        maximum = float(maximum_raw) if maximum_raw is not None else None
        if minimum is not None and not math.isfinite(minimum):
            raise invalid_request("minScore must be finite.")
        if maximum is not None and not math.isfinite(maximum):
            raise invalid_request("maxScore must be finite.")
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_score = None
        cursor_key = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_score = cursor.get("score")
            cursor_key = cursor.get("key")
            if (
                isinstance(cursor_score, bool)
                or not isinstance(cursor_score, (int, float))
                or not isinstance(cursor_key, str)
            ):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_list_ordered",
            _list_ordered,
            store_id,
            order,
            minimum,
            maximum,
            float(cursor_score) if cursor_score is not None else None,
            cursor_key,
            limit,
        )
        result: dict[str, Any] = {
            "entries": [self._ordered_payload(row) for row in rows],
            "order": order,
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"score": float(rows[-1][1]), "key": rows[-1][0]}
            )
        return 200, result
