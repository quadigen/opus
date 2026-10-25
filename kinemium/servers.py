from __future__ import annotations

import hmac
import json
import re
import uuid
from typing import Any

from synapse.http.site import SynapseRequest

from .accounts import _ensure_profile, _get_game
from .auth import ACCOUNT_READ, ACCOUNT_WRITE, Caller
from .base import Service
from .errors import (
    conflict,
    invalid_request,
    not_found,
    payload_too_large,
)
from .http import (
    content_type_for_filename,
    parse_json_body,
    parse_multipart,
    query_value,
    send_file,
    set_cors_response_headers,
)
from .storage import sanitize_filename
from .util import decode_cursor, encode_cursor, now_ms
from .validation import (
    check_str,
    optional_int,
    optional_str,
    validate_id,
    validate_matrix_user_id,
)

REQUEST_STATUSES = ("pending", "claimed", "failed")
SERVER_STATUSES = ("starting", "online", "offline", "failed")
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_REQUEST_COLUMNS = (
    "request_id, game_id, owner_user_id, region, max_players, note,"
    " kine_filename, kine_storage_key, kine_digest, kine_size, status,"
    " claimed_by, claimed_at, failure_reason, created_at, updated_at"
)
_REQUEST_COLUMNS_PREFIXED = (
    "r.request_id, r.game_id, r.owner_user_id, r.region, r.max_players,"
    " r.note, r.kine_filename, r.kine_storage_key, r.kine_digest,"
    " r.kine_size, r.status, r.claimed_by, r.claimed_at, r.failure_reason,"
    " r.created_at, r.updated_at"
)
_SERVER_COLUMNS = (
    "server_id, game_id, owner_user_id, request_id, worker_id, region,"
    " address, port, max_players, status, created_at, updated_at,"
    " last_heartbeat_at"
)
_SERVER_COLUMNS_PREFIXED = (
    "s.server_id, s.game_id, s.owner_user_id, s.request_id, s.worker_id,"
    " s.region, s.address, s.port, s.max_players, s.status, s.created_at,"
    " s.updated_at, s.last_heartbeat_at"
)


def _request_row(txn: Any, request_id: str) -> tuple | None:
    txn.execute(
        "SELECT " + _REQUEST_COLUMNS + " FROM kinemium_game_server_requests"
        " WHERE request_id = %s",
        (request_id,),
    )
    return txn.fetchone()


def _insert_request(
    txn: Any,
    request_id: str,
    game_id: str,
    owner: str,
    region: str,
    max_players: int,
    note: str,
    filename: str,
    storage_key: str,
    digest: str,
    size: int,
    now: int,
    for_update: str,
) -> tuple:
    txn.execute(
        "SELECT game_id, owner_user_id FROM kinemium_games"
        " WHERE game_id = %s" + for_update,
        (game_id,),
    )
    game = txn.fetchone()
    if game is None or game[1] != owner:
        raise not_found("Game not found.")
    txn.execute(
        "SELECT request_id FROM kinemium_game_server_requests"
        " WHERE game_id = %s AND status IN ('pending', 'claimed')",
        (game_id,),
    )
    if txn.fetchone() is not None:
        raise conflict("This game already has an active server request.")
    txn.execute(
        "INSERT INTO kinemium_game_server_requests"
        " (request_id, game_id, owner_user_id, region, max_players, note,"
        " kine_filename, kine_storage_key, kine_digest, kine_size, status,"
        " created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)",
        (
            request_id,
            game_id,
            owner,
            region,
            max_players,
            note,
            filename,
            storage_key,
            digest,
            size,
            now,
            now,
        ),
    )
    row = _request_row(txn, request_id)
    if row is None:
        raise not_found("Server request not found.")
    return row


def _list_requests(
    txn: Any,
    owner: str | None,
    game_id: str | None,
    status: str | None,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    sql = (
        "SELECT " + _REQUEST_COLUMNS_PREFIXED + ", g.name"
        " FROM kinemium_game_server_requests r"
        " JOIN kinemium_games g ON g.game_id = r.game_id"
    )
    clauses: list[str] = []
    args: list[Any] = []
    if owner is not None:
        clauses.append("r.owner_user_id = %s")
        args.append(owner)
    if game_id is not None:
        clauses.append("r.game_id = %s")
        args.append(game_id)
    if status is not None:
        clauses.append("r.status = %s")
        args.append(status)
    if cursor_created is not None and cursor_id is not None:
        clauses.append(
            "(r.created_at < %s OR (r.created_at = %s AND r.request_id < %s))"
        )
        args.extend([cursor_created, cursor_created, cursor_id])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY r.created_at DESC, r.request_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _claim_request(
    txn: Any, request_id: str, worker: str, now: int, for_update: str
) -> tuple:
    txn.execute(
        "SELECT " + _REQUEST_COLUMNS + " FROM kinemium_game_server_requests"
        " WHERE request_id = %s" + for_update,
        (request_id,),
    )
    row = txn.fetchone()
    if row is None:
        raise not_found("Server request not found.")
    if row[10] != "pending":
        raise conflict("Only pending server requests can be claimed.")
    txn.execute(
        "UPDATE kinemium_game_server_requests SET status = 'claimed',"
        " claimed_by = %s, claimed_at = %s, updated_at = %s"
        " WHERE request_id = %s",
        (worker, now, now, request_id),
    )
    updated = _request_row(txn, request_id)
    if updated is None:
        raise not_found("Server request not found.")
    return updated


def _fail_request(
    txn: Any,
    request_id: str,
    reason: str,
    now: int,
    for_update: str,
) -> tuple:
    txn.execute(
        "SELECT " + _REQUEST_COLUMNS + " FROM kinemium_game_server_requests"
        " WHERE request_id = %s" + for_update,
        (request_id,),
    )
    row = txn.fetchone()
    if row is None:
        raise not_found("Server request not found.")
    if row[10] not in ("pending", "claimed"):
        raise conflict("That server request is already finished.")
    txn.execute(
        "UPDATE kinemium_game_server_requests SET status = 'failed',"
        " failure_reason = %s, updated_at = %s WHERE request_id = %s",
        (reason, now, request_id),
    )
    updated = _request_row(txn, request_id)
    if updated is None:
        raise not_found("Server request not found.")
    return updated


def _delete_request(
    txn: Any, request_id: str, game_id: str, owner: str, for_update: str
) -> tuple:
    txn.execute(
        "SELECT " + _REQUEST_COLUMNS + " FROM kinemium_game_server_requests"
        " WHERE request_id = %s" + for_update,
        (request_id,),
    )
    row = txn.fetchone()
    if row is None or row[1] != game_id or row[2] != owner:
        raise not_found("Server request not found.")
    if row[10] == "claimed":
        raise conflict("A claimed server request cannot be cancelled.")
    txn.execute(
        "DELETE FROM kinemium_game_server_requests WHERE request_id = %s",
        (request_id,),
    )
    return row


def _server_row(txn: Any, server_id: str) -> tuple | None:
    txn.execute(
        "SELECT " + _SERVER_COLUMNS + " FROM kinemium_game_servers"
        " WHERE server_id = %s",
        (server_id,),
    )
    return txn.fetchone()


def _complete_request(
    txn: Any,
    request_id: str,
    worker: str,
    address: str | None,
    port: int | None,
    region: str | None,
    max_players: int | None,
    now: int,
    for_update: str,
) -> tuple[tuple, tuple]:
    txn.execute(
        "SELECT " + _REQUEST_COLUMNS + " FROM kinemium_game_server_requests"
        " WHERE request_id = %s" + for_update,
        (request_id,),
    )
    row = txn.fetchone()
    if row is None:
        raise not_found("Server request not found.")
    if row[10] != "claimed":
        raise conflict("Only claimed server requests can be completed.")
    server_id = "srv_" + uuid.uuid4().hex
    txn.execute(
        "INSERT INTO kinemium_game_servers"
        " (server_id, game_id, owner_user_id, request_id, worker_id, region,"
        " address, port, max_players, status, created_at, updated_at,"
        " last_heartbeat_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'starting', %s, %s, %s)",
        (
            server_id,
            row[1],
            row[2],
            row[0],
            worker,
            region or row[3],
            address,
            port,
            max_players if max_players is not None else row[4],
            now,
            now,
            now,
        ),
    )
    txn.execute(
        "DELETE FROM kinemium_game_server_requests WHERE request_id = %s",
        (request_id,),
    )
    server = _server_row(txn, server_id)
    if server is None:
        raise not_found("Server not found.")
    return row, server


def _list_servers(
    txn: Any,
    owner: str | None,
    game_id: str | None,
    status: str | None,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    sql = (
        "SELECT " + _SERVER_COLUMNS_PREFIXED + ", g.name"
        " FROM kinemium_game_servers s"
        " JOIN kinemium_games g ON g.game_id = s.game_id"
    )
    clauses: list[str] = []
    args: list[Any] = []
    if owner is not None:
        clauses.append("s.owner_user_id = %s")
        args.append(owner)
    if game_id is not None:
        clauses.append("s.game_id = %s")
        args.append(game_id)
    if status is not None:
        clauses.append("s.status = %s")
        args.append(status)
    if cursor_created is not None and cursor_id is not None:
        clauses.append(
            "(s.created_at < %s OR (s.created_at = %s AND s.server_id < %s))"
        )
        args.extend([cursor_created, cursor_created, cursor_id])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY s.created_at DESC, s.server_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _update_server(
    txn: Any,
    server_id: str,
    status: str | None,
    address: str | None,
    address_set: bool,
    port: int | None,
    port_set: bool,
    now: int,
    for_update: str,
) -> tuple:
    txn.execute(
        "SELECT " + _SERVER_COLUMNS + " FROM kinemium_game_servers"
        " WHERE server_id = %s" + for_update,
        (server_id,),
    )
    row = txn.fetchone()
    if row is None:
        raise not_found("Server not found.")
    txn.execute(
        "UPDATE kinemium_game_servers SET status = %s, address = %s,"
        " port = %s, updated_at = %s, last_heartbeat_at = %s"
        " WHERE server_id = %s",
        (
            status if status is not None else row[9],
            address if address_set else row[6],
            port if port_set else row[7],
            now,
            now,
            server_id,
        ),
    )
    updated = _server_row(txn, server_id)
    if updated is None:
        raise not_found("Server not found.")
    return updated


def _delete_server(txn: Any, server_id: str, for_update: str) -> tuple:
    txn.execute(
        "SELECT " + _SERVER_COLUMNS + " FROM kinemium_game_servers"
        " WHERE server_id = %s" + for_update,
        (server_id,),
    )
    row = txn.fetchone()
    if row is None:
        raise not_found("Server not found.")
    txn.execute("DELETE FROM kinemium_game_servers WHERE server_id = %s", (server_id,))
    return row


def _parse_region(value: Any) -> str:
    if not isinstance(value, str):
        raise invalid_request("region must be a string.")
    region = value.strip().lower()
    if not _REGION_RE.fullmatch(region):
        raise invalid_request(
            "region must match [a-z0-9][a-z0-9_-] and be at most 32 characters."
        )
    return region


class GameServerService(Service):
    def register(self, resource: Any) -> None:
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/me/games/(?P<game_id>[^/]+)/server-requests$"
                )
            ],
            self.post_request,
            "KinemiumGameServerRequestsServlet",
        )
        resource.register_paths(
            "GET",
            [
                re.compile(
                    r"^/_kinemium/v1/me/games/(?P<game_id>[^/]+)/server-requests$"
                )
            ],
            self.list_game_requests,
            "KinemiumGameServerRequestsServlet",
        )
        resource.register_paths(
            "GET",
            [
                re.compile(
                    r"^/_kinemium/v1/me/games/(?P<game_id>[^/]+)/server-requests/"
                    r"(?P<request_id>[^/]+)$"
                )
            ],
            self.get_game_request,
            "KinemiumGameServerRequestServlet",
        )
        resource.register_paths(
            "DELETE",
            [
                re.compile(
                    r"^/_kinemium/v1/me/games/(?P<game_id>[^/]+)/server-requests/"
                    r"(?P<request_id>[^/]+)$"
                )
            ],
            self.delete_game_request,
            "KinemiumGameServerRequestServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/me/games/(?P<game_id>[^/]+)/servers$")],
            self.list_game_servers,
            "KinemiumGameServersServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/me/servers$")],
            self.list_my_servers,
            "KinemiumMyServersServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/admin/game-server-requests$")],
            self.admin_list_requests,
            "KinemiumAdminGameServerRequestsServlet",
        )
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/admin/game-server-requests/"
                    r"(?P<request_id>[^/]+)/claim$"
                )
            ],
            self.admin_claim_request,
            "KinemiumAdminGameServerRequestServlet",
        )
        resource.register_paths(
            "GET",
            [
                re.compile(
                    r"^/_kinemium/v1/admin/game-server-requests/"
                    r"(?P<request_id>[^/]+)/download$"
                )
            ],
            self.admin_download_request,
            "KinemiumAdminGameServerRequestServlet",
        )
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/admin/game-server-requests/"
                    r"(?P<request_id>[^/]+)/complete$"
                )
            ],
            self.admin_complete_request,
            "KinemiumAdminGameServerRequestServlet",
        )
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/admin/game-server-requests/"
                    r"(?P<request_id>[^/]+)/fail$"
                )
            ],
            self.admin_fail_request,
            "KinemiumAdminGameServerRequestServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/admin/game-servers$")],
            self.admin_list_servers,
            "KinemiumAdminGameServersServlet",
        )
        resource.register_paths(
            "PATCH",
            [re.compile(r"^/_kinemium/v1/admin/game-servers/(?P<server_id>[^/]+)$")],
            self.admin_update_server,
            "KinemiumAdminGameServerServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/admin/game-servers/(?P<server_id>[^/]+)$")],
            self.admin_delete_server,
            "KinemiumAdminGameServerServlet",
        )

    def _request_payload(self, row: tuple) -> dict[str, Any]:
        payload = {
            "requestId": row[0],
            "gameId": row[1],
            "ownerUserId": row[2],
            "region": row[3],
            "maxPlayers": row[4],
            "note": row[5],
            "fileName": row[6],
            "fileSize": row[9],
            "fileDigest": row[8],
            "status": row[10],
            "claimedBy": row[11],
            "claimedAt": row[12],
            "failureReason": row[13],
            "createdAt": row[14],
            "updatedAt": row[15],
        }
        if len(row) > 16:
            payload["gameName"] = row[16]
        return payload

    def _server_payload(self, row: tuple) -> dict[str, Any]:
        payload = {
            "serverId": row[0],
            "gameId": row[1],
            "ownerUserId": row[2],
            "requestId": row[3],
            "workerId": row[4],
            "region": row[5],
            "address": row[6],
            "port": row[7],
            "maxPlayers": row[8],
            "status": row[9],
            "createdAt": row[10],
            "updatedAt": row[11],
            "lastHeartbeatAt": row[12],
        }
        if len(row) > 13:
            payload["gameName"] = row[13]
        return payload

    async def _game_for_owner(
        self, request: SynapseRequest, game_id: str, scope: str
    ) -> tuple[Caller, tuple]:
        caller = await self.authenticate(request, scope, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(game_id, "gameId")
        if caller.game_id is not None and caller.game_id != game_id:
            raise not_found("Game not found.")
        row = await self.db.run("kinemium_get_game", _get_game, game_id)
        if row is None or row[1] != caller.user_id:
            raise not_found("Game not found.")
        return caller, row

    async def _require_worker(
        self, request: SynapseRequest
    ) -> tuple[str, Caller | None]:
        token = self.config.server_worker_token
        if token is not None:
            raw = request.getHeader(b"X-Kinemium-Worker-Token")
            if raw is not None:
                if hmac.compare_digest(raw, token.encode("utf-8")):
                    raw_id = request.getHeader(b"X-Kinemium-Worker-Id")
                    worker_id = "main"
                    if raw_id is not None:
                        try:
                            worker_id = raw_id.decode("utf-8").strip()
                        except UnicodeDecodeError:
                            raise invalid_request("workerId must be UTF-8.") from None
                        if not worker_id:
                            raise invalid_request("workerId must not be blank.")
                        check_str(worker_id, "workerId", 1, 128)
                    return f"worker:{worker_id}", None
        caller = await self.require_admin(request)
        return caller.user_id, caller

    def _worker_name(self, actor: str, body: dict[str, Any]) -> str:
        worker = optional_str(body, "workerId", 128)
        if worker is not None:
            worker = worker.strip()
            if not worker:
                raise invalid_request("workerId must not be blank.")
            return worker
        if actor.startswith("worker:"):
            return actor[len("worker:") :]
        return actor

    def _parse_metadata(self, fields: dict[str, str]) -> tuple[str, int, str]:
        raw = fields.get("metadata")
        data: dict[str, Any]
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                raise invalid_request("metadata must be valid JSON.") from None
            if not isinstance(parsed, dict):
                raise invalid_request("metadata must be a JSON object.")
            data = parsed
        else:
            data = {}
            if "region" in fields:
                data["region"] = fields["region"]
            if "maxPlayers" in fields:
                try:
                    data["maxPlayers"] = int(fields["maxPlayers"])
                except ValueError:
                    raise invalid_request("maxPlayers must be an integer.") from None
            if "note" in fields:
                data["note"] = fields["note"]
        unknown = sorted(set(data) - {"region", "maxPlayers", "note"})
        if unknown:
            raise invalid_request("Unknown metadata fields: " + ", ".join(unknown))
        region = _parse_region(data.get("region", "auto"))
        max_players = data.get("maxPlayers", 16)
        if (
            isinstance(max_players, bool)
            or not isinstance(max_players, int)
            or not 1 <= max_players <= 1000
        ):
            raise invalid_request("maxPlayers must be between 1 and 1000.")
        note = data.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise invalid_request("note must be a string.")
        check_str(note, "note", 0, 500)
        return region, max_players, note.strip()

    def _cursor(self, request: SynapseRequest) -> tuple[int | None, str | None]:
        cursor_raw = query_value(request, "cursor", None, 2048)
        if cursor_raw is None:
            return None, None
        cursor = decode_cursor(cursor_raw)
        cursor_created = cursor.get("createdAt")
        cursor_id = cursor.get("id")
        if not isinstance(cursor_created, int) or not isinstance(cursor_id, str):
            raise invalid_request("Invalid pagination cursor.")
        return cursor_created, cursor_id

    async def post_request(
        self, request: SynapseRequest, game_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _game = await self._game_for_owner(request, game_id, ACCOUNT_WRITE)
        fields, kine, filename = parse_multipart(
            request, self.config.max_json_body_bytes
        )
        if kine is None:
            raise invalid_request("A .kine file is required.")
        if not kine:
            raise invalid_request("The .kine file is empty.")
        if len(kine) > self.config.max_archive_bytes:
            raise payload_too_large(
                "The .kine file exceeds the configured archive limit."
            )
        safe_name = sanitize_filename(filename, "server.kine")
        if not safe_name.lower().endswith(".kine"):
            raise invalid_request("Server requests require a .kine file.")
        region, max_players, note = self._parse_metadata(fields)
        now = now_ms()
        await self.db.run(
            "kinemium_ensure_profile", _ensure_profile, caller.user_id, now
        )
        request_id = "sreq_" + uuid.uuid4().hex
        storage_key = self.storage.new_key("game-server-requests", game_id)
        digest, size = await self.storage.write(storage_key, kine)
        try:
            row = await self.db.run(
                "kinemium_create_game_server_request",
                _insert_request,
                request_id,
                game_id,
                caller.user_id,
                region,
                max_players,
                note,
                safe_name,
                storage_key,
                digest,
                size,
                now,
                self.db.for_update(),
            )
        except Exception:
            await self.storage.delete(storage_key)
            raise
        return 201, self._request_payload(row)

    async def list_game_requests(
        self, request: SynapseRequest, game_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _game = await self._game_for_owner(request, game_id, ACCOUNT_READ)
        status = query_value(request, "status", None, 32)
        if status is not None and status not in REQUEST_STATUSES:
            raise invalid_request("Unknown server request status.")
        limit = self.page_limit(request)
        cursor_created, cursor_id = self._cursor(request)
        rows, has_more = await self.db.run(
            "kinemium_list_game_server_requests",
            _list_requests,
            caller.user_id,
            game_id,
            status,
            limit,
            cursor_created,
            cursor_id,
        )
        result: dict[str, Any] = {
            "requests": [self._request_payload(row) for row in rows]
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][14], "id": rows[-1][0]}
            )
        return 200, result

    async def get_game_request(
        self, request: SynapseRequest, game_id: str, request_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _game = await self._game_for_owner(request, game_id, ACCOUNT_READ)
        validate_id(request_id, "requestId")
        row = await self.db.run(
            "kinemium_get_game_server_request", _request_row, request_id
        )
        if row is None or row[1] != game_id or row[2] != caller.user_id:
            raise not_found("Server request not found.")
        return 200, self._request_payload(row)

    async def delete_game_request(
        self, request: SynapseRequest, game_id: str, request_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _game = await self._game_for_owner(request, game_id, ACCOUNT_WRITE)
        validate_id(request_id, "requestId")
        row = await self.db.run(
            "kinemium_delete_game_server_request",
            _delete_request,
            request_id,
            game_id,
            caller.user_id,
            self.db.for_update(),
        )
        await self.storage.delete(row[7])
        return 200, {"deleted": True, "requestId": request_id}

    async def list_game_servers(
        self, request: SynapseRequest, game_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _game = await self._game_for_owner(request, game_id, ACCOUNT_READ)
        return await self._list_servers(request, caller.user_id, game_id)

    async def list_my_servers(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        game_id = query_value(request, "gameId", None, 128)
        if game_id is not None:
            validate_id(game_id, "gameId")
        if caller.game_id is not None:
            if game_id is not None and caller.game_id != game_id:
                raise not_found("Game not found.")
            game_id = caller.game_id
        return await self._list_servers(request, caller.user_id, game_id)

    async def _list_servers(
        self,
        request: SynapseRequest,
        owner: str,
        game_id: str | None,
    ) -> tuple[int, dict[str, Any]]:
        status = query_value(request, "status", None, 32)
        if status is not None and status not in SERVER_STATUSES:
            raise invalid_request("Unknown server status.")
        limit = self.page_limit(request)
        cursor_created, cursor_id = self._cursor(request)
        rows, has_more = await self.db.run(
            "kinemium_list_game_servers",
            _list_servers,
            owner,
            game_id,
            status,
            limit,
            cursor_created,
            cursor_id,
        )
        result: dict[str, Any] = {
            "servers": [self._server_payload(row) for row in rows]
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][10], "id": rows[-1][0]}
            )
        return 200, result

    async def admin_list_requests(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        _actor, caller = await self._require_worker(request)
        self.enforce_rate_limit(request, caller)
        game_id = query_value(request, "gameId", None, 128)
        if game_id is not None:
            validate_id(game_id, "gameId")
        owner = query_value(request, "owner", None, 255)
        if owner is not None:
            validate_matrix_user_id(owner)
        status = query_value(request, "status", None, 32)
        if status is not None and status not in REQUEST_STATUSES:
            raise invalid_request("Unknown server request status.")
        limit = self.page_limit(request)
        cursor_created, cursor_id = self._cursor(request)
        rows, has_more = await self.db.run(
            "kinemium_list_game_server_requests",
            _list_requests,
            owner,
            game_id,
            status,
            limit,
            cursor_created,
            cursor_id,
        )
        result: dict[str, Any] = {
            "requests": [self._request_payload(row) for row in rows]
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][14], "id": rows[-1][0]}
            )
        return 200, result

    async def admin_claim_request(
        self, request: SynapseRequest, request_id: str
    ) -> tuple[int, dict[str, Any]]:
        actor, caller = await self._require_worker(request)
        self.enforce_rate_limit(request, caller)
        validate_id(request_id, "requestId")
        body = parse_json_body(
            request, self.config.max_json_body_bytes, allow_empty=True
        )
        unknown = sorted(set(body) - {"workerId"})
        if unknown:
            raise invalid_request("Unknown claim fields: " + ", ".join(unknown))
        worker = self._worker_name(actor, body)
        row = await self.db.run(
            "kinemium_claim_game_server_request",
            _claim_request,
            request_id,
            worker,
            now_ms(),
            self.db.for_update(),
        )
        payload = self._request_payload(row)
        payload["downloadUrl"] = (
            f"/_kinemium/v1/admin/game-server-requests/{request_id}/download"
        )
        if self.platform is not None:
            await self.platform.record_audit(
                actor,
                "game_server.request_claim",
                "game_server_request",
                request_id,
                {"gameId": row[1], "worker": worker},
            )
        return 200, payload

    async def admin_download_request(
        self, request: SynapseRequest, request_id: str
    ) -> None:
        _actor, caller = await self._require_worker(request)
        self.enforce_rate_limit(request, caller)
        validate_id(request_id, "requestId")
        row = await self.db.run(
            "kinemium_get_game_server_request", _request_row, request_id
        )
        if row is None:
            raise not_found("Server request not found.")
        data = await self.storage.read_verified(row[7], row[8])
        set_cors_response_headers(self.config, request)
        send_file(
            request,
            data,
            row[6],
            content_type_for_filename(row[6]),
            row[8],
            {
                "X-Kinemium-Server-Request": row[0],
                "X-Kinemium-Server-Request-Status": row[10],
            },
        )

    async def admin_complete_request(
        self, request: SynapseRequest, request_id: str
    ) -> tuple[int, dict[str, Any]]:
        actor, caller = await self._require_worker(request)
        self.enforce_rate_limit(request, caller)
        validate_id(request_id, "requestId")
        body = parse_json_body(
            request, self.config.max_json_body_bytes, allow_empty=True
        )
        unknown = sorted(
            set(body) - {"workerId", "address", "port", "region", "maxPlayers"}
        )
        if unknown:
            raise invalid_request("Unknown completion fields: " + ", ".join(unknown))
        worker = self._worker_name(actor, body)
        address = optional_str(body, "address", 255)
        if address is not None:
            address = address.strip()
            if not address:
                address = None
            else:
                check_str(address, "address", 1, 255)
        port = optional_int(body, "port", 1, 65535)
        region = optional_str(body, "region", 32)
        if region is not None:
            region = _parse_region(region)
        max_players = optional_int(body, "maxPlayers", 1, 1000)
        request_row, server_row = await self.db.run(
            "kinemium_complete_game_server_request",
            _complete_request,
            request_id,
            worker,
            address,
            port,
            region,
            max_players,
            now_ms(),
            self.db.for_update(),
        )
        await self.storage.delete(request_row[7])
        if self.platform is not None:
            await self.platform.record_audit(
                actor,
                "game_server.request_complete",
                "game_server",
                server_row[0],
                {
                    "requestId": request_id,
                    "gameId": server_row[1],
                    "worker": worker,
                    "address": address,
                    "port": port,
                },
            )
        return 200, {
            "removed": True,
            "requestId": request_id,
            "server": self._server_payload(server_row),
        }

    async def admin_fail_request(
        self, request: SynapseRequest, request_id: str
    ) -> tuple[int, dict[str, Any]]:
        actor, caller = await self._require_worker(request)
        self.enforce_rate_limit(request, caller)
        validate_id(request_id, "requestId")
        body = parse_json_body(
            request, self.config.max_json_body_bytes, allow_empty=True
        )
        unknown = sorted(set(body) - {"reason"})
        if unknown:
            raise invalid_request("Unknown failure fields: " + ", ".join(unknown))
        reason = (optional_str(body, "reason", 500) or "").strip()
        row = await self.db.run(
            "kinemium_fail_game_server_request",
            _fail_request,
            request_id,
            reason,
            now_ms(),
            self.db.for_update(),
        )
        if self.platform is not None:
            await self.platform.record_audit(
                actor,
                "game_server.request_fail",
                "game_server_request",
                request_id,
                {"gameId": row[1], "reason": reason},
            )
        return 200, self._request_payload(row)

    async def admin_list_servers(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        _actor, caller = await self._require_worker(request)
        self.enforce_rate_limit(request, caller)
        game_id = query_value(request, "gameId", None, 128)
        if game_id is not None:
            validate_id(game_id, "gameId")
        status = query_value(request, "status", None, 32)
        if status is not None and status not in SERVER_STATUSES:
            raise invalid_request("Unknown server status.")
        limit = self.page_limit(request)
        cursor_created, cursor_id = self._cursor(request)
        rows, has_more = await self.db.run(
            "kinemium_list_game_servers",
            _list_servers,
            None,
            game_id,
            status,
            limit,
            cursor_created,
            cursor_id,
        )
        result: dict[str, Any] = {
            "servers": [self._server_payload(row) for row in rows]
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][10], "id": rows[-1][0]}
            )
        return 200, result

    async def admin_update_server(
        self, request: SynapseRequest, server_id: str
    ) -> tuple[int, dict[str, Any]]:
        actor, caller = await self._require_worker(request)
        self.enforce_rate_limit(request, caller)
        validate_id(server_id, "serverId")
        body = parse_json_body(request, self.config.max_json_body_bytes)
        unknown = sorted(set(body) - {"status", "address", "port"})
        if unknown:
            raise invalid_request("Unknown server fields: " + ", ".join(unknown))
        status = optional_str(body, "status", 32)
        if status is not None and status not in SERVER_STATUSES:
            raise invalid_request("Unknown server status.")
        address_set = "address" in body
        address = optional_str(body, "address", 255)
        if address is not None:
            address = address.strip()
            if not address:
                address = None
            else:
                check_str(address, "address", 1, 255)
        port_set = "port" in body
        port = optional_int(body, "port", 1, 65535)
        row = await self.db.run(
            "kinemium_update_game_server",
            _update_server,
            server_id,
            status,
            address,
            address_set,
            port,
            port_set,
            now_ms(),
            self.db.for_update(),
        )
        if self.platform is not None:
            await self.platform.record_audit(
                actor,
                "game_server.update",
                "game_server",
                server_id,
                {
                    "gameId": row[1],
                    "status": status,
                    "address": address if address_set else None,
                    "port": port if port_set else None,
                },
            )
        return 200, self._server_payload(row)

    async def admin_delete_server(
        self, request: SynapseRequest, server_id: str
    ) -> tuple[int, dict[str, Any]]:
        actor, caller = await self._require_worker(request)
        self.enforce_rate_limit(request, caller)
        validate_id(server_id, "serverId")
        row = await self.db.run(
            "kinemium_delete_game_server",
            _delete_server,
            server_id,
            self.db.for_update(),
        )
        if self.platform is not None:
            await self.platform.record_audit(
                actor,
                "game_server.delete",
                "game_server",
                server_id,
                {"gameId": row[1]},
            )
        return 200, {"deleted": True, "serverId": server_id}
