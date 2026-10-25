from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from typing import Any

from synapse.http.site import SynapseRequest

from .accounts import _ensure_profile, _get_game
from .auth import ACCOUNT_READ, ACCOUNT_WRITE, Caller
from .base import Service
from .errors import conflict, forbidden, invalid_request, not_found
from .http import parse_json_body, query_int, query_value
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
    optional_int,
    optional_str,
    require_str,
    validate_id,
    validate_matrix_user_id,
)

SESSION_STATUSES = ("active", "ended", "expired")
INVITE_STATUSES = ("pending", "accepted", "declined", "revoked", "expired")
JOIN_REQUEST_STATUSES = ("pending", "approved", "denied", "cancelled")
SESSION_ROLES = ("hosted", "joined")
_SIGNAL_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

_SESSION_COLUMNS = (
    "s.session_id, s.game_id, s.host_user_id, s.name, s.status,"
    " s.join_code_hash, s.next_signal_sequence, s.max_participants,"
    " s.created_at, s.updated_at, s.last_heartbeat_at, s.expires_at,"
    " s.ended_at, s.ended_by, g.name,"
    " (SELECT COUNT(*) FROM kinemium_collab_participants pc"
    "  WHERE pc.session_id = s.session_id AND pc.status = 'joined')"
)
_SESSION_FROM = (
    " FROM kinemium_collab_sessions s JOIN kinemium_games g ON g.game_id = s.game_id"
)

_PARTICIPANT_COLUMNS = (
    "user_id, role, status, invited_by, joined_at, last_seen_at, left_at"
)

_INVITE_COLUMNS = (
    "invite_id, session_id, inviter_user_id, invitee_user_id, status,"
    " created_at, updated_at, expires_at, responded_at"
)
_INVITE_COLUMNS_PREFIXED = (
    "i.invite_id, i.session_id, i.inviter_user_id, i.invitee_user_id,"
    " i.status, i.created_at, i.updated_at, i.expires_at, i.responded_at"
)
_INVITE_FROM = (
    " FROM kinemium_collab_invites i"
    " JOIN kinemium_collab_sessions s ON s.session_id = i.session_id"
    " JOIN kinemium_games g ON g.game_id = s.game_id"
)
_INVITE_ROW_COLUMNS = (
    _INVITE_COLUMNS_PREFIXED
    + ", s.game_id, s.host_user_id, s.name, s.status, s.expires_at, g.name"
)

_JOIN_REQUEST_COLUMNS = (
    "request_id, session_id, user_id, status, created_at, updated_at,"
    " decided_at, decided_by"
)
_JOIN_REQUEST_COLUMNS_PREFIXED = (
    "r.request_id, r.session_id, r.user_id, r.status, r.created_at,"
    " r.updated_at, r.decided_at, r.decided_by"
)
_JOIN_REQUEST_FROM = (
    " FROM kinemium_collab_join_requests r"
    " JOIN kinemium_collab_sessions s ON s.session_id = r.session_id"
    " JOIN kinemium_games g ON g.game_id = s.game_id"
)
_JOIN_REQUEST_ROW_COLUMNS = (
    _JOIN_REQUEST_COLUMNS_PREFIXED
    + ", s.game_id, s.host_user_id, s.name, s.status, s.expires_at, g.name"
)

_SIGNAL_COLUMNS = (
    "signal_id, session_id, sequence, from_user_id, to_user_id, kind,"
    " payload, created_at, expires_at"
)


def _participant_row(txn: Any, session_id: str, user_id: str) -> tuple | None:
    txn.execute(
        "SELECT " + _PARTICIPANT_COLUMNS + " FROM kinemium_collab_participants"
        " WHERE session_id = %s AND user_id = %s",
        (session_id, user_id),
    )
    return txn.fetchone()


def _participant_count(txn: Any, session_id: str) -> int:
    txn.execute(
        "SELECT COUNT(*) FROM kinemium_collab_participants"
        " WHERE session_id = %s AND status = 'joined'",
        (session_id,),
    )
    row = txn.fetchone()
    return int(row[0]) if row is not None else 0


def _end_session_txn(
    txn: Any,
    session_id: str,
    ended_by: str,
    now: int,
    status: str,
    only_if_expired: bool = False,
) -> None:
    sql = (
        "UPDATE kinemium_collab_sessions SET status = %s, ended_at = %s,"
        " ended_by = %s, updated_at = %s"
        " WHERE session_id = %s AND status = 'active'"
    )
    args: list[Any] = [status, now, ended_by, now, session_id]
    if only_if_expired:
        sql += " AND expires_at <= %s"
        args.append(now)
    txn.execute(sql, tuple(args))
    if int(txn.rowcount or 0) == 0:
        return
    txn.execute(
        "UPDATE kinemium_collab_participants SET status = 'left', left_at = %s"
        " WHERE session_id = %s AND status = 'joined'",
        (now, session_id),
    )
    txn.execute(
        "UPDATE kinemium_collab_invites SET status = 'expired', updated_at = %s"
        " WHERE session_id = %s AND status = 'pending'",
        (now, session_id),
    )
    txn.execute(
        "UPDATE kinemium_collab_join_requests SET status = 'cancelled',"
        " updated_at = %s WHERE session_id = %s AND status = 'pending'",
        (now, session_id),
    )
    txn.execute(
        "DELETE FROM kinemium_collab_signals WHERE session_id = %s",
        (session_id,),
    )


def _expire_active_sessions(txn: Any, now: int) -> None:
    txn.execute(
        "SELECT session_id FROM kinemium_collab_sessions"
        " WHERE status = 'active' AND expires_at <= %s",
        (now,),
    )
    for (session_id,) in txn.fetchall():
        _end_session_txn(txn, str(session_id), "system", now, "expired", True)
    txn.execute(
        "UPDATE kinemium_collab_invites SET status = 'expired', updated_at = %s"
        " WHERE status = 'pending' AND expires_at <= %s",
        (now, now),
    )


def _session_row(
    txn: Any,
    session_id: str,
    now: int,
    for_update: str = "",
) -> tuple | None:
    sql = (
        "SELECT "
        + _SESSION_COLUMNS
        + _SESSION_FROM
        + " WHERE s.session_id = %s"
        + for_update
    )
    txn.execute(sql, (session_id,))
    row = txn.fetchone()
    if row is not None and row[4] == "active" and int(row[11]) <= now:
        _end_session_txn(txn, session_id, "system", now, "expired", True)
        txn.execute(sql, (session_id,))
        row = txn.fetchone()
    return row


def _session_row_by_code(
    txn: Any,
    code_hash: str,
    now: int,
    for_update: str,
) -> tuple | None:
    txn.execute(
        "SELECT session_id FROM kinemium_collab_sessions WHERE join_code_hash = %s",
        (code_hash,),
    )
    row = txn.fetchone()
    if row is None:
        return None
    return _session_row(txn, str(row[0]), now, for_update)


def _insert_participant(
    txn: Any,
    session_id: str,
    user_id: str,
    role: str,
    invited_by: str | None,
    now: int,
    max_participants: int,
) -> tuple:
    existing = _participant_row(txn, session_id, user_id)
    if existing is not None and existing[2] == "joined":
        return existing
    if _participant_count(txn, session_id) >= max_participants:
        raise conflict("Collaboration session is full.")
    if existing is None:
        txn.execute(
            "INSERT INTO kinemium_collab_participants"
            " (session_id, user_id, role, status, invited_by, joined_at,"
            " last_seen_at, left_at)"
            " VALUES (%s, %s, %s, 'joined', %s, %s, %s, NULL)",
            (session_id, user_id, role, invited_by, now, now),
        )
    else:
        txn.execute(
            "UPDATE kinemium_collab_participants SET role = %s, status = 'joined',"
            " invited_by = %s, joined_at = %s, last_seen_at = %s, left_at = NULL"
            " WHERE session_id = %s AND user_id = %s",
            (role, invited_by, now, now, session_id, user_id),
        )
    row = _participant_row(txn, session_id, user_id)
    if row is None:
        raise not_found("Collaboration participant not found.")
    return row


def _create_session(
    txn: Any,
    session_id: str,
    game_id: str,
    host: str,
    name: str,
    max_participants: int,
    code_hash: str,
    now: int,
    expires_at: int,
    for_update: str,
) -> tuple:
    txn.execute(
        "SELECT game_id, owner_user_id, name FROM kinemium_games"
        " WHERE game_id = %s" + for_update,
        (game_id,),
    )
    game = txn.fetchone()
    if game is None or game[1] != host:
        raise not_found("Game not found.")
    _ensure_profile(txn, host, now)
    txn.execute(
        "SELECT session_id FROM kinemium_collab_sessions"
        " WHERE game_id = %s AND status = 'active'",
        (game_id,),
    )
    existing = txn.fetchone()
    if existing is not None:
        active = _session_row(txn, str(existing[0]), now, for_update)
        if active is not None and active[4] == "active":
            raise conflict("This game already has an active collaboration session.")
    session_name = name or str(game[2])
    txn.execute(
        "INSERT INTO kinemium_collab_sessions"
        " (session_id, game_id, host_user_id, name, status, join_code_hash,"
        " next_signal_sequence, max_participants, created_at, updated_at,"
        " last_heartbeat_at, expires_at)"
        " VALUES (%s, %s, %s, %s, 'active', %s, 1, %s, %s, %s, %s, %s)",
        (
            session_id,
            game_id,
            host,
            session_name,
            code_hash,
            max_participants,
            now,
            now,
            now,
            expires_at,
        ),
    )
    _insert_participant(txn, session_id, host, "host", None, now, max_participants)
    row = _session_row(txn, session_id, now)
    if row is None:
        raise not_found("Collaboration session not found.")
    return row


def _list_sessions_host(
    txn: Any,
    host: str,
    game_id: str | None,
    status: str | None,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
    now: int,
) -> tuple[list[tuple], bool]:
    _expire_active_sessions(txn, now)
    sql = "SELECT " + _SESSION_COLUMNS + _SESSION_FROM + " WHERE s.host_user_id = %s"
    args: list[Any] = [host]
    if game_id is not None:
        sql += " AND s.game_id = %s"
        args.append(game_id)
    if status is not None:
        sql += " AND s.status = %s"
        args.append(status)
    if cursor_created is not None and cursor_id is not None:
        sql += " AND (s.created_at < %s OR (s.created_at = %s AND s.session_id < %s))"
        args.extend([cursor_created, cursor_created, cursor_id])
    sql += " ORDER BY s.created_at DESC, s.session_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _list_sessions_joined(
    txn: Any,
    user_id: str,
    game_id: str | None,
    status: str | None,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
    now: int,
) -> tuple[list[tuple], bool]:
    _expire_active_sessions(txn, now)
    sql = (
        "SELECT "
        + _SESSION_COLUMNS
        + ", p.role"
        + _SESSION_FROM
        + " JOIN kinemium_collab_participants p"
        + " ON p.session_id = s.session_id AND p.user_id = %s"
        + " AND p.status = 'joined' WHERE 1 = 1"
    )
    args: list[Any] = [user_id]
    if game_id is not None:
        sql += " AND s.game_id = %s"
        args.append(game_id)
    if status is not None:
        sql += " AND s.status = %s"
        args.append(status)
    if cursor_created is not None and cursor_id is not None:
        sql += " AND (s.created_at < %s OR (s.created_at = %s AND s.session_id < %s))"
        args.extend([cursor_created, cursor_created, cursor_id])
    sql += " ORDER BY s.created_at DESC, s.session_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _rotate_join_code(
    txn: Any,
    session_id: str,
    host: str,
    code_hash: str,
    now: int,
    for_update: str,
) -> tuple:
    row = _session_row(txn, session_id, now, for_update)
    if row is None or row[2] != host:
        raise not_found("Collaboration session not found.")
    if row[4] != "active":
        raise conflict("Collaboration session has ended.")
    txn.execute(
        "UPDATE kinemium_collab_sessions SET join_code_hash = %s,"
        " updated_at = %s WHERE session_id = %s",
        (code_hash, now, session_id),
    )
    updated = _session_row(txn, session_id, now)
    if updated is None:
        raise not_found("Collaboration session not found.")
    return updated


def _stop_session(
    txn: Any,
    session_id: str,
    host: str,
    now: int,
    for_update: str,
) -> tuple[tuple, list[str]]:
    row = _session_row(txn, session_id, now, for_update)
    if row is None or row[2] != host:
        raise not_found("Collaboration session not found.")
    if row[4] != "active":
        return row, []
    txn.execute(
        "SELECT user_id FROM kinemium_collab_participants"
        " WHERE session_id = %s AND status = 'joined' AND role = 'guest'",
        (session_id,),
    )
    guests = [str(item[0]) for item in txn.fetchall()]
    _end_session_txn(txn, session_id, host, now, "ended")
    updated = _session_row(txn, session_id, now)
    if updated is None:
        raise not_found("Collaboration session not found.")
    return updated, guests


def _invite_row(txn: Any, invite_id: str) -> tuple | None:
    txn.execute(
        "SELECT " + _INVITE_ROW_COLUMNS + _INVITE_FROM + " WHERE i.invite_id = %s",
        (invite_id,),
    )
    return txn.fetchone()


def _insert_invite(
    txn: Any,
    invite_id: str,
    session_id: str,
    inviter: str,
    invitee: str,
    invite_ttl_ms: int,
    now: int,
    for_update: str,
) -> tuple:
    session = _session_row(txn, session_id, now, for_update)
    if session is None or session[2] != inviter:
        raise not_found("Collaboration session not found.")
    if session[4] != "active":
        raise conflict("Collaboration session has ended.")
    if invitee == inviter:
        raise invalid_request("You cannot invite yourself.")
    expires_at = min(now + invite_ttl_ms, int(session[11]))
    _ensure_profile(txn, invitee, now)
    participant = _participant_row(txn, session_id, invitee)
    if participant is not None and participant[2] == "joined":
        raise conflict("That user is already in the collaboration session.")
    txn.execute(
        "SELECT invite_id FROM kinemium_collab_invites"
        " WHERE session_id = %s AND invitee_user_id = %s AND status = 'pending'",
        (session_id, invitee),
    )
    if txn.fetchone() is not None:
        raise conflict("That user already has a pending invitation.")
    txn.execute(
        "INSERT INTO kinemium_collab_invites"
        " (invite_id, session_id, inviter_user_id, invitee_user_id, status,"
        " created_at, updated_at, expires_at)"
        " VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s)",
        (invite_id, session_id, inviter, invitee, now, now, expires_at),
    )
    row = _invite_row(txn, invite_id)
    if row is None:
        raise not_found("Invitation not found.")
    return row


def _list_invites(
    txn: Any,
    *,
    session_id: str | None,
    invitee: str | None,
    game_id: str | None,
    status: str | None,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
    now: int,
) -> tuple[list[tuple], bool]:
    _expire_active_sessions(txn, now)
    sql = "SELECT " + _INVITE_ROW_COLUMNS + _INVITE_FROM + " WHERE 1 = 1"
    args: list[Any] = []
    if session_id is not None:
        sql += " AND i.session_id = %s"
        args.append(session_id)
    if invitee is not None:
        sql += " AND i.invitee_user_id = %s"
        args.append(invitee)
    if game_id is not None:
        sql += " AND s.game_id = %s"
        args.append(game_id)
    if status is not None:
        sql += " AND i.status = %s"
        args.append(status)
    if cursor_created is not None and cursor_id is not None:
        sql += " AND (i.created_at < %s OR (i.created_at = %s AND i.invite_id < %s))"
        args.extend([cursor_created, cursor_created, cursor_id])
    sql += " ORDER BY i.created_at DESC, i.invite_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _accept_invite(
    txn: Any,
    invite_id: str,
    invitee: str,
    game_id: str | None,
    now: int,
    for_update: str,
) -> tuple[tuple, tuple]:
    txn.execute(
        "SELECT " + _INVITE_COLUMNS + " FROM kinemium_collab_invites"
        " WHERE invite_id = %s" + for_update,
        (invite_id,),
    )
    invite = txn.fetchone()
    if invite is None or invite[3] != invitee:
        raise not_found("Invitation not found.")
    session = _session_row(txn, str(invite[1]), now, for_update)
    if session is None:
        raise not_found("Collaboration session not found.")
    if game_id is not None and session[1] != game_id:
        raise not_found("Invitation not found.")
    if invite[4] == "accepted":
        participant = _participant_row(txn, str(invite[1]), invitee)
        if participant is not None and participant[2] == "joined":
            row = _invite_row(txn, invite_id)
            if row is None:
                raise not_found("Invitation not found.")
            return row, session
        raise conflict("Invitation is no longer pending.")
    if invite[4] != "pending":
        raise conflict("Invitation is no longer pending.")
    if int(invite[7]) <= now:
        txn.execute(
            "UPDATE kinemium_collab_invites SET status = 'expired',"
            " updated_at = %s WHERE invite_id = %s",
            (now, invite_id),
        )
        raise conflict("Invitation has expired.")
    if session[4] != "active":
        raise conflict("Collaboration session has ended.")
    participant = _participant_row(txn, str(invite[1]), invitee)
    if participant is None or participant[2] != "joined":
        _insert_participant(
            txn,
            str(invite[1]),
            invitee,
            "guest",
            str(invite[2]),
            now,
            int(session[7]),
        )
    txn.execute(
        "UPDATE kinemium_collab_join_requests SET status = 'cancelled',"
        " updated_at = %s WHERE session_id = %s AND user_id = %s"
        " AND status = 'pending'",
        (now, invite[1], invitee),
    )
    txn.execute(
        "UPDATE kinemium_collab_invites SET status = 'accepted',"
        " updated_at = %s, responded_at = %s WHERE invite_id = %s",
        (now, now, invite_id),
    )
    row = _invite_row(txn, invite_id)
    updated_session = _session_row(txn, str(invite[1]), now)
    if row is None or updated_session is None:
        raise not_found("Invitation not found.")
    return row, updated_session


def _decline_invite(
    txn: Any,
    invite_id: str,
    invitee: str,
    game_id: str | None,
    now: int,
    for_update: str,
) -> tuple:
    txn.execute(
        "SELECT " + _INVITE_COLUMNS + " FROM kinemium_collab_invites"
        " WHERE invite_id = %s" + for_update,
        (invite_id,),
    )
    invite = txn.fetchone()
    if invite is None or invite[3] != invitee:
        raise not_found("Invitation not found.")
    session = _session_row(txn, str(invite[1]), now, for_update)
    if session is None:
        raise not_found("Collaboration session not found.")
    if game_id is not None and session[1] != game_id:
        raise not_found("Invitation not found.")
    if invite[4] == "declined":
        row = _invite_row(txn, invite_id)
        if row is None:
            raise not_found("Invitation not found.")
        return row
    if invite[4] != "pending":
        raise conflict("Invitation is no longer pending.")
    if int(invite[7]) <= now:
        txn.execute(
            "UPDATE kinemium_collab_invites SET status = 'expired',"
            " updated_at = %s WHERE invite_id = %s",
            (now, invite_id),
        )
        raise conflict("Invitation has expired.")
    txn.execute(
        "UPDATE kinemium_collab_invites SET status = 'declined',"
        " updated_at = %s, responded_at = %s WHERE invite_id = %s",
        (now, now, invite_id),
    )
    row = _invite_row(txn, invite_id)
    if row is None:
        raise not_found("Invitation not found.")
    return row


def _revoke_invite(
    txn: Any,
    session_id: str,
    invite_id: str,
    host: str,
    game_id: str | None,
    now: int,
    for_update: str,
) -> tuple:
    txn.execute(
        "SELECT " + _INVITE_COLUMNS + " FROM kinemium_collab_invites"
        " WHERE invite_id = %s AND session_id = %s" + for_update,
        (invite_id, session_id),
    )
    invite = txn.fetchone()
    if invite is None:
        raise not_found("Invitation not found.")
    session = _session_row(txn, session_id, now, for_update)
    if session is None or session[2] != host:
        raise not_found("Invitation not found.")
    if game_id is not None and session[1] != game_id:
        raise not_found("Invitation not found.")
    if invite[4] == "revoked":
        row = _invite_row(txn, invite_id)
        if row is None:
            raise not_found("Invitation not found.")
        return row
    if invite[4] != "pending":
        raise conflict("Invitation is no longer pending.")
    if session[4] != "active":
        raise conflict("Collaboration session has ended.")
    txn.execute(
        "UPDATE kinemium_collab_invites SET status = 'revoked',"
        " updated_at = %s WHERE invite_id = %s",
        (now, invite_id),
    )
    row = _invite_row(txn, invite_id)
    if row is None:
        raise not_found("Invitation not found.")
    return row


def _join_request_row(txn: Any, request_id: str) -> tuple | None:
    txn.execute(
        "SELECT "
        + _JOIN_REQUEST_ROW_COLUMNS
        + _JOIN_REQUEST_FROM
        + " WHERE r.request_id = %s",
        (request_id,),
    )
    return txn.fetchone()


def _join_by_code(
    txn: Any,
    code_hash: str,
    user_id: str,
    game_id: str | None,
    now: int,
    for_update: str,
) -> tuple[tuple, tuple | None, bool]:
    session = _session_row_by_code(txn, code_hash, now, for_update)
    if session is None:
        raise not_found("Invalid or expired collaboration code.")
    if game_id is not None and session[1] != game_id:
        raise not_found("Invalid or expired collaboration code.")
    if session[4] != "active":
        raise conflict("Collaboration session has ended.")
    _ensure_profile(txn, user_id, now)
    if user_id == session[2]:
        return session, None, True
    participant = _participant_row(txn, str(session[0]), user_id)
    if participant is not None and participant[2] == "joined":
        return session, None, True
    txn.execute(
        "SELECT request_id FROM kinemium_collab_join_requests"
        " WHERE session_id = %s AND user_id = %s AND status = 'pending'",
        (session[0], user_id),
    )
    existing = txn.fetchone()
    if existing is not None:
        row = _join_request_row(txn, str(existing[0]))
        if row is None:
            raise not_found("Join request not found.")
        return session, row, False
    if _participant_count(txn, str(session[0])) >= int(session[7]):
        raise conflict("Collaboration session is full.")
    request_id = "creq_" + uuid.uuid4().hex
    txn.execute(
        "INSERT INTO kinemium_collab_join_requests"
        " (request_id, session_id, user_id, status, created_at, updated_at)"
        " VALUES (%s, %s, %s, 'pending', %s, %s)",
        (request_id, session[0], user_id, now, now),
    )
    row = _join_request_row(txn, request_id)
    if row is None:
        raise not_found("Join request not found.")
    return session, row, True


def _list_join_requests(
    txn: Any,
    *,
    session_id: str | None,
    user_id: str | None,
    game_id: str | None,
    status: str | None,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
    now: int,
) -> tuple[list[tuple], bool]:
    _expire_active_sessions(txn, now)
    sql = "SELECT " + _JOIN_REQUEST_ROW_COLUMNS + _JOIN_REQUEST_FROM + " WHERE 1 = 1"
    args: list[Any] = []
    if session_id is not None:
        sql += " AND r.session_id = %s"
        args.append(session_id)
    if user_id is not None:
        sql += " AND r.user_id = %s"
        args.append(user_id)
    if game_id is not None:
        sql += " AND s.game_id = %s"
        args.append(game_id)
    if status is not None:
        sql += " AND r.status = %s"
        args.append(status)
    if cursor_created is not None and cursor_id is not None:
        sql += " AND (r.created_at < %s OR (r.created_at = %s AND r.request_id < %s))"
        args.extend([cursor_created, cursor_created, cursor_id])
    sql += " ORDER BY r.created_at DESC, r.request_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _approve_join_request(
    txn: Any,
    session_id: str,
    request_id: str,
    host: str,
    game_id: str | None,
    now: int,
    for_update: str,
) -> tuple[tuple, tuple]:
    txn.execute(
        "SELECT " + _JOIN_REQUEST_COLUMNS + " FROM kinemium_collab_join_requests"
        " WHERE request_id = %s AND session_id = %s" + for_update,
        (request_id, session_id),
    )
    request = txn.fetchone()
    if request is None:
        raise not_found("Join request not found.")
    session = _session_row(txn, session_id, now, for_update)
    if session is None or session[2] != host:
        raise not_found("Join request not found.")
    if game_id is not None and session[1] != game_id:
        raise not_found("Join request not found.")
    if request[3] == "approved":
        participant = _participant_row(txn, session_id, str(request[2]))
        if participant is not None and participant[2] == "joined":
            row = _join_request_row(txn, request_id)
            if row is None:
                raise not_found("Join request not found.")
            return row, session
        raise conflict("Join request is no longer pending.")
    if request[3] != "pending":
        raise conflict("Join request is no longer pending.")
    if session[4] != "active":
        raise conflict("Collaboration session has ended.")
    participant = _participant_row(txn, session_id, str(request[2]))
    if participant is None or participant[2] != "joined":
        _insert_participant(
            txn,
            session_id,
            str(request[2]),
            "guest",
            host,
            now,
            int(session[7]),
        )
    txn.execute(
        "UPDATE kinemium_collab_join_requests SET status = 'approved',"
        " updated_at = %s, decided_at = %s, decided_by = %s"
        " WHERE request_id = %s",
        (now, now, host, request_id),
    )
    row = _join_request_row(txn, request_id)
    updated_session = _session_row(txn, session_id, now)
    if row is None or updated_session is None:
        raise not_found("Join request not found.")
    return row, updated_session


def _deny_join_request(
    txn: Any,
    session_id: str,
    request_id: str,
    host: str,
    game_id: str | None,
    now: int,
    for_update: str,
) -> tuple:
    txn.execute(
        "SELECT " + _JOIN_REQUEST_COLUMNS + " FROM kinemium_collab_join_requests"
        " WHERE request_id = %s AND session_id = %s" + for_update,
        (request_id, session_id),
    )
    request = txn.fetchone()
    if request is None:
        raise not_found("Join request not found.")
    session = _session_row(txn, session_id, now, for_update)
    if session is None or session[2] != host:
        raise not_found("Join request not found.")
    if game_id is not None and session[1] != game_id:
        raise not_found("Join request not found.")
    if request[3] == "denied":
        row = _join_request_row(txn, request_id)
        if row is None:
            raise not_found("Join request not found.")
        return row
    if request[3] != "pending":
        raise conflict("Join request is no longer pending.")
    if session[4] != "active":
        raise conflict("Collaboration session has ended.")
    txn.execute(
        "UPDATE kinemium_collab_join_requests SET status = 'denied',"
        " updated_at = %s, decided_at = %s, decided_by = %s"
        " WHERE request_id = %s",
        (now, now, host, request_id),
    )
    row = _join_request_row(txn, request_id)
    if row is None:
        raise not_found("Join request not found.")
    return row


def _cancel_join_request(
    txn: Any,
    request_id: str,
    user_id: str,
    game_id: str | None,
    now: int,
    for_update: str,
) -> tuple:
    txn.execute(
        "SELECT " + _JOIN_REQUEST_COLUMNS + " FROM kinemium_collab_join_requests"
        " WHERE request_id = %s" + for_update,
        (request_id,),
    )
    request = txn.fetchone()
    if request is None or request[2] != user_id:
        raise not_found("Join request not found.")
    session = _session_row(txn, str(request[1]), now, for_update)
    if session is None:
        raise not_found("Join request not found.")
    if game_id is not None and session[1] != game_id:
        raise not_found("Join request not found.")
    if request[3] == "cancelled":
        row = _join_request_row(txn, request_id)
        if row is None:
            raise not_found("Join request not found.")
        return row
    if request[3] != "pending":
        raise conflict("Join request is no longer pending.")
    txn.execute(
        "UPDATE kinemium_collab_join_requests SET status = 'cancelled',"
        " updated_at = %s WHERE request_id = %s",
        (now, request_id),
    )
    row = _join_request_row(txn, request_id)
    if row is None:
        raise not_found("Join request not found.")
    return row


def _heartbeat(
    txn: Any,
    session_id: str,
    user_id: str,
    game_id: str | None,
    now: int,
    ttl_ms: int,
    for_update: str,
) -> tuple:
    session = _session_row(txn, session_id, now, for_update)
    if session is None:
        raise not_found("Collaboration session not found.")
    if game_id is not None and session[1] != game_id:
        raise not_found("Collaboration session not found.")
    if session[4] != "active":
        raise conflict("Collaboration session has ended.")
    participant = _participant_row(txn, session_id, user_id)
    if participant is None or participant[2] != "joined":
        raise forbidden("You are not participating in this collaboration session.")
    txn.execute(
        "UPDATE kinemium_collab_participants SET last_seen_at = %s"
        " WHERE session_id = %s AND user_id = %s",
        (now, session_id, user_id),
    )
    if session[2] == user_id:
        txn.execute(
            "UPDATE kinemium_collab_sessions SET last_heartbeat_at = %s,"
            " expires_at = %s, updated_at = %s WHERE session_id = %s",
            (now, now + ttl_ms, now, session_id),
        )
    updated = _session_row(txn, session_id, now)
    if updated is None:
        raise not_found("Collaboration session not found.")
    return updated


def _leave_session(
    txn: Any,
    session_id: str,
    user_id: str,
    game_id: str | None,
    now: int,
    for_update: str,
) -> tuple:
    session = _session_row(txn, session_id, now, for_update)
    if session is None:
        raise not_found("Collaboration session not found.")
    if game_id is not None and session[1] != game_id:
        raise not_found("Collaboration session not found.")
    participant = _participant_row(txn, session_id, user_id)
    if participant is None:
        raise not_found("Collaboration session not found.")
    if participant[1] == "host":
        raise forbidden("The host must stop the collaboration session.")
    if participant[2] == "joined":
        txn.execute(
            "UPDATE kinemium_collab_participants SET status = 'left',"
            " left_at = %s WHERE session_id = %s AND user_id = %s",
            (now, session_id, user_id),
        )
    updated = _session_row(txn, session_id, now)
    if updated is None:
        raise not_found("Collaboration session not found.")
    return updated


def _kick_participant(
    txn: Any,
    session_id: str,
    target: str,
    host: str,
    game_id: str | None,
    now: int,
    for_update: str,
) -> tuple:
    session = _session_row(txn, session_id, now, for_update)
    if session is None or session[2] != host:
        raise not_found("Collaboration session not found.")
    if game_id is not None and session[1] != game_id:
        raise not_found("Collaboration session not found.")
    if session[4] != "active":
        raise conflict("Collaboration session has ended.")
    if target == host:
        raise invalid_request("The host cannot be removed from the session.")
    participant = _participant_row(txn, session_id, target)
    if participant is None or participant[2] != "joined":
        raise not_found("Collaboration participant not found.")
    txn.execute(
        "UPDATE kinemium_collab_participants SET status = 'kicked',"
        " left_at = %s WHERE session_id = %s AND user_id = %s",
        (now, session_id, target),
    )
    txn.execute(
        "UPDATE kinemium_collab_join_requests SET status = 'cancelled',"
        " updated_at = %s WHERE session_id = %s AND user_id = %s"
        " AND status = 'pending'",
        (now, session_id, target),
    )
    updated = _session_row(txn, session_id, now)
    if updated is None:
        raise not_found("Collaboration session not found.")
    return updated


def _load_session_participant(
    txn: Any,
    session_id: str,
    user_id: str,
    game_id: str | None,
    now: int,
    require_joined: bool,
) -> tuple[tuple, tuple]:
    session = _session_row(txn, session_id, now)
    if session is None:
        raise not_found("Collaboration session not found.")
    if game_id is not None and session[1] != game_id:
        raise not_found("Collaboration session not found.")
    participant = _participant_row(txn, session_id, user_id)
    if participant is None:
        raise not_found("Collaboration session not found.")
    if require_joined and session[4] == "active" and participant[2] != "joined":
        raise forbidden("You are not participating in this collaboration session.")
    return session, participant


def _list_participants(txn: Any, session_id: str) -> list[tuple]:
    txn.execute(
        "SELECT " + _PARTICIPANT_COLUMNS + " FROM kinemium_collab_participants"
        " WHERE session_id = %s"
        " ORDER BY CASE role WHEN 'host' THEN 0 ELSE 1 END,"
        " joined_at ASC, user_id ASC",
        (session_id,),
    )
    return txn.fetchall()


def _signal_row(txn: Any, signal_id: str) -> tuple | None:
    txn.execute(
        "SELECT " + _SIGNAL_COLUMNS + " FROM kinemium_collab_signals"
        " WHERE signal_id = %s",
        (signal_id,),
    )
    return txn.fetchone()


def _insert_signal(
    txn: Any,
    session_id: str,
    user_id: str,
    game_id: str | None,
    to_user_id: str | None,
    kind: str,
    payload: str,
    now: int,
    ttl_ms: int,
    for_update: str,
) -> tuple:
    session = _session_row(txn, session_id, now, for_update)
    if session is None:
        raise not_found("Collaboration session not found.")
    if game_id is not None and session[1] != game_id:
        raise not_found("Collaboration session not found.")
    if session[4] != "active":
        raise conflict("Collaboration session has ended.")
    participant = _participant_row(txn, session_id, user_id)
    if participant is None or participant[2] != "joined":
        raise forbidden("You are not participating in this collaboration session.")
    if to_user_id is not None:
        if to_user_id == user_id:
            raise invalid_request("You cannot send a signal to yourself.")
        target = _participant_row(txn, session_id, to_user_id)
        if target is None or target[2] != "joined":
            raise not_found("Collaboration participant not found.")
    sequence = int(session[6])
    txn.execute(
        "UPDATE kinemium_collab_sessions SET next_signal_sequence = %s,"
        " updated_at = %s WHERE session_id = %s",
        (sequence + 1, now, session_id),
    )
    txn.execute(
        "UPDATE kinemium_collab_participants SET last_seen_at = %s"
        " WHERE session_id = %s AND user_id = %s",
        (now, session_id, user_id),
    )
    txn.execute(
        "DELETE FROM kinemium_collab_signals"
        " WHERE session_id = %s AND expires_at <= %s",
        (session_id, now),
    )
    signal_id = "csig_" + uuid.uuid4().hex
    txn.execute(
        "INSERT INTO kinemium_collab_signals"
        " (signal_id, session_id, sequence, from_user_id, to_user_id, kind,"
        " payload, created_at, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            signal_id,
            session_id,
            sequence,
            user_id,
            to_user_id,
            kind,
            payload,
            now,
            now + ttl_ms,
        ),
    )
    row = _signal_row(txn, signal_id)
    if row is None:
        raise not_found("Signal not found.")
    return row


def _list_signals(
    txn: Any,
    session_id: str,
    user_id: str,
    game_id: str | None,
    after: int,
    limit: int,
    now: int,
) -> tuple[list[tuple], bool, tuple]:
    session = _session_row(txn, session_id, now)
    if session is None:
        raise not_found("Collaboration session not found.")
    if game_id is not None and session[1] != game_id:
        raise not_found("Collaboration session not found.")
    participant = _participant_row(txn, session_id, user_id)
    if participant is None:
        raise forbidden("You are not participating in this collaboration session.")
    if session[4] == "active" and participant[2] != "joined":
        raise forbidden("You are not participating in this collaboration session.")
    if participant[2] == "joined":
        txn.execute(
            "UPDATE kinemium_collab_participants SET last_seen_at = %s"
            " WHERE session_id = %s AND user_id = %s",
            (now, session_id, user_id),
        )
    txn.execute(
        "SELECT " + _SIGNAL_COLUMNS + " FROM kinemium_collab_signals"
        " WHERE session_id = %s AND sequence > %s"
        " AND (to_user_id IS NULL OR to_user_id = %s)"
        " AND expires_at > %s ORDER BY sequence ASC LIMIT %s",
        (session_id, after, user_id, now, limit + 1),
    )
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit, session


class CollabService(Service):
    def register(self, resource: Any) -> None:
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/me/games/(?P<game_id>[^/]+)/collab-sessions$"
                )
            ],
            self.create_session,
            "KinemiumCollabSessionsServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/me/collab-sessions$")],
            self.list_sessions,
            "KinemiumCollabSessionsServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/me/collab-sessions/(?P<session_id>[^/]+)$")],
            self.stop_session,
            "KinemiumCollabSessionServlet",
        )
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/me/collab-sessions/(?P<session_id>[^/]+)/"
                    r"join-code$"
                )
            ],
            self.rotate_join_code,
            "KinemiumCollabJoinCodeServlet",
        )
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/me/collab-sessions/(?P<session_id>[^/]+)/"
                    r"invites$"
                )
            ],
            self.invite,
            "KinemiumCollabInvitesServlet",
        )
        resource.register_paths(
            "GET",
            [
                re.compile(
                    r"^/_kinemium/v1/me/collab-sessions/(?P<session_id>[^/]+)/"
                    r"invites$"
                )
            ],
            self.list_session_invites,
            "KinemiumCollabInvitesServlet",
        )
        resource.register_paths(
            "DELETE",
            [
                re.compile(
                    r"^/_kinemium/v1/me/collab-sessions/(?P<session_id>[^/]+)/"
                    r"invites/(?P<invite_id>[^/]+)$"
                )
            ],
            self.revoke_invite,
            "KinemiumCollabInviteServlet",
        )
        resource.register_paths(
            "GET",
            [
                re.compile(
                    r"^/_kinemium/v1/me/collab-sessions/(?P<session_id>[^/]+)/"
                    r"join-requests$"
                )
            ],
            self.list_session_join_requests,
            "KinemiumCollabJoinRequestsServlet",
        )
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/me/collab-sessions/(?P<session_id>[^/]+)/"
                    r"join-requests/(?P<request_id>[^/]+)/approve$"
                )
            ],
            self.approve_join_request,
            "KinemiumCollabJoinRequestServlet",
        )
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/me/collab-sessions/(?P<session_id>[^/]+)/"
                    r"join-requests/(?P<request_id>[^/]+)/deny$"
                )
            ],
            self.deny_join_request,
            "KinemiumCollabJoinRequestServlet",
        )
        resource.register_paths(
            "DELETE",
            [
                re.compile(
                    r"^/_kinemium/v1/me/collab-sessions/(?P<session_id>[^/]+)/"
                    r"participants/(?P<user_id>[^/]+)$"
                )
            ],
            self.kick_participant,
            "KinemiumCollabParticipantServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/me/collab-invites$")],
            self.list_my_invites,
            "KinemiumCollabInvitesServlet",
        )
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/me/collab-invites/(?P<invite_id>[^/]+)/accept$"
                )
            ],
            self.accept_invite,
            "KinemiumCollabInviteServlet",
        )
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/me/collab-invites/(?P<invite_id>[^/]+)/decline$"
                )
            ],
            self.decline_invite,
            "KinemiumCollabInviteServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/collab/join$")],
            self.join_by_code,
            "KinemiumCollabJoinServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/me/collab-join-requests$")],
            self.list_my_join_requests,
            "KinemiumCollabJoinRequestsServlet",
        )
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/me/collab-join-requests/"
                    r"(?P<request_id>[^/]+)/cancel$"
                )
            ],
            self.cancel_join_request,
            "KinemiumCollabJoinRequestServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/collab/sessions/(?P<session_id>[^/]+)$")],
            self.get_session,
            "KinemiumCollabSessionServlet",
        )
        resource.register_paths(
            "GET",
            [
                re.compile(
                    r"^/_kinemium/v1/collab/sessions/(?P<session_id>[^/]+)/"
                    r"participants$"
                )
            ],
            self.list_participants,
            "KinemiumCollabParticipantsServlet",
        )
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/collab/sessions/(?P<session_id>[^/]+)/"
                    r"heartbeat$"
                )
            ],
            self.heartbeat,
            "KinemiumCollabHeartbeatServlet",
        )
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/collab/sessions/(?P<session_id>[^/]+)/leave$"
                )
            ],
            self.leave_session,
            "KinemiumCollabLeaveServlet",
        )
        resource.register_paths(
            "POST",
            [
                re.compile(
                    r"^/_kinemium/v1/collab/sessions/(?P<session_id>[^/]+)/signals$"
                )
            ],
            self.post_signal,
            "KinemiumCollabSignalsServlet",
        )
        resource.register_paths(
            "GET",
            [
                re.compile(
                    r"^/_kinemium/v1/collab/sessions/(?P<session_id>[^/]+)/signals$"
                )
            ],
            self.get_signals,
            "KinemiumCollabSignalsServlet",
        )

    def _session_payload(
        self,
        row: tuple,
        join_code: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "sessionId": row[0],
            "gameId": row[1],
            "hostUserId": row[2],
            "name": row[3],
            "status": row[4],
            "nextSignalSequence": int(row[6]),
            "maxParticipants": row[7],
            "participantCount": int(row[15]),
            "createdAt": row[8],
            "updatedAt": row[9],
            "lastHeartbeatAt": row[10],
            "expiresAt": row[11],
            "endedAt": row[12],
            "endedBy": row[13],
            "gameName": row[14],
        }
        if join_code is not None:
            payload["joinCode"] = join_code
        if role is not None:
            payload["role"] = role
        return payload

    def _invite_payload(self, row: tuple) -> dict[str, Any]:
        return {
            "inviteId": row[0],
            "sessionId": row[1],
            "inviterUserId": row[2],
            "inviteeUserId": row[3],
            "status": row[4],
            "createdAt": row[5],
            "updatedAt": row[6],
            "expiresAt": row[7],
            "respondedAt": row[8],
            "gameId": row[9],
            "hostUserId": row[10],
            "sessionName": row[11],
            "sessionStatus": row[12],
            "sessionExpiresAt": row[13],
            "gameName": row[14],
        }

    def _join_request_payload(self, row: tuple) -> dict[str, Any]:
        return {
            "requestId": row[0],
            "sessionId": row[1],
            "userId": row[2],
            "status": row[3],
            "createdAt": row[4],
            "updatedAt": row[5],
            "decidedAt": row[6],
            "decidedBy": row[7],
            "gameId": row[8],
            "hostUserId": row[9],
            "sessionName": row[10],
            "sessionStatus": row[11],
            "sessionExpiresAt": row[12],
            "gameName": row[13],
        }

    def _participant_payload(self, row: tuple) -> dict[str, Any]:
        return {
            "userId": row[0],
            "role": row[1],
            "status": row[2],
            "invitedBy": row[3],
            "joinedAt": row[4],
            "lastSeenAt": row[5],
            "leftAt": row[6],
        }

    def _signal_payload(self, row: tuple) -> dict[str, Any]:
        return {
            "signalId": row[0],
            "sessionId": row[1],
            "sequence": int(row[2]),
            "fromUserId": row[3],
            "toUserId": row[4],
            "kind": row[5],
            "payload": json_loads(row[6]),
            "createdAt": row[7],
            "expiresAt": row[8],
        }

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

    def _validate_status(
        self, request: SynapseRequest, name: str, allowed: tuple[str, ...]
    ) -> str | None:
        status = query_value(request, name, None, 32)
        if status is not None and status not in allowed:
            raise invalid_request(f"Unknown {name}.")
        return status

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

    async def _session_for_host(
        self, request: SynapseRequest, session_id: str, scope: str
    ) -> tuple[Caller, tuple]:
        caller = await self.authenticate(request, scope, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(session_id, "sessionId")
        row = await self.db.run(
            "kinemium_get_collab_session",
            _session_row,
            session_id,
            now_ms(),
            "",
        )
        if row is None or row[2] != caller.user_id:
            raise not_found("Collaboration session not found.")
        if caller.game_id is not None and row[1] != caller.game_id:
            raise not_found("Collaboration session not found.")
        return caller, row

    async def _resolve_user(self, body: dict[str, Any]) -> str:
        user_id = optional_str(body, "userId", 255)
        username = optional_str(body, "username", 255)
        if (user_id is None) == (username is None):
            raise invalid_request("Provide exactly one of userId or username.")
        if user_id is not None:
            resolved = validate_matrix_user_id(user_id.strip())
        else:
            assert username is not None
            username = username.strip()
            if not username:
                raise invalid_request("username must not be blank.")
            check_str(username, "username", 1, 255)
            resolved = self.api.get_qualified_user_id(username)
            validate_matrix_user_id(resolved)
        canonical = await self.api.check_user_exists(resolved)
        if canonical is None:
            raise not_found("User not found.")
        return canonical

    def _new_join_code(self) -> str:
        return "cjoin_" + secrets.token_urlsafe(24)

    def _hash_join_code(self, code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    async def _notify(
        self,
        user_id: str,
        kind: str,
        actor: str | None,
        payload: dict[str, Any],
    ) -> None:
        if self.platform is not None:
            await self.platform.notify(user_id, kind, actor, None, payload)

    async def create_session(
        self, request: SynapseRequest, game_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _game = await self._game_for_owner(request, game_id, ACCOUNT_WRITE)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        unknown = sorted(set(body) - {"name", "maxParticipants"})
        if unknown:
            raise invalid_request(
                "Unknown collaboration session fields: " + ", ".join(unknown)
            )
        name = optional_str(body, "name", 100)
        if name is not None:
            name = name.strip()
            if not name:
                raise invalid_request("name must not be blank.")
        max_participants = optional_int(
            body, "maxParticipants", 2, self.config.max_collab_participants
        )
        if max_participants is None:
            max_participants = self.config.max_collab_participants
        now = now_ms()
        code = self._new_join_code()
        session_id = "collab_" + uuid.uuid4().hex
        expires_at = now + self.config.collab_session_ttl_seconds * 1000
        row = await self.db.run(
            "kinemium_create_collab_session",
            _create_session,
            session_id,
            game_id,
            caller.user_id,
            name or "",
            max_participants,
            self._hash_join_code(code),
            now,
            expires_at,
            self.db.for_update(),
        )
        return 201, self._session_payload(row, join_code=code, role="host")

    async def list_sessions(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        role = query_value(request, "role", "hosted", 16) or "hosted"
        if role not in SESSION_ROLES:
            raise invalid_request("role must be hosted or joined.")
        status = self._validate_status(request, "status", SESSION_STATUSES)
        game_id = query_value(request, "gameId", None, 128)
        if game_id is not None:
            validate_id(game_id, "gameId")
        if caller.game_id is not None:
            if game_id is not None and caller.game_id != game_id:
                raise not_found("Game not found.")
            game_id = caller.game_id
        limit = self.page_limit(request)
        cursor_created, cursor_id = self._cursor(request)
        if role == "hosted":
            rows, has_more = await self.db.run(
                "kinemium_list_collab_sessions_hosted",
                _list_sessions_host,
                caller.user_id,
                game_id,
                status,
                limit,
                cursor_created,
                cursor_id,
                now_ms(),
            )
            sessions = [self._session_payload(row, role="host") for row in rows]
        else:
            rows, has_more = await self.db.run(
                "kinemium_list_collab_sessions_joined",
                _list_sessions_joined,
                caller.user_id,
                game_id,
                status,
                limit,
                cursor_created,
                cursor_id,
                now_ms(),
            )
            sessions = [self._session_payload(row, role=str(row[16])) for row in rows]
        result: dict[str, Any] = {"sessions": sessions}
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][8], "id": rows[-1][0]}
            )
        return 200, result

    async def stop_session(
        self, request: SynapseRequest, session_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _row = await self._session_for_host(request, session_id, ACCOUNT_WRITE)
        now = now_ms()
        row, guests = await self.db.run(
            "kinemium_stop_collab_session",
            _stop_session,
            session_id,
            caller.user_id,
            now,
            self.db.for_update(),
        )
        for guest in guests:
            await self._notify(
                guest,
                "collab.session_ended",
                caller.user_id,
                {
                    "sessionId": row[0],
                    "gameId": row[1],
                    "gameName": row[14],
                    "sessionName": row[3],
                    "hostUserId": row[2],
                    "endedAt": row[12],
                },
            )
        return 200, self._session_payload(row, role="host")

    async def rotate_join_code(
        self, request: SynapseRequest, session_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _row = await self._session_for_host(request, session_id, ACCOUNT_WRITE)
        code = self._new_join_code()
        row = await self.db.run(
            "kinemium_rotate_collab_join_code",
            _rotate_join_code,
            session_id,
            caller.user_id,
            self._hash_join_code(code),
            now_ms(),
            self.db.for_update(),
        )
        return 200, self._session_payload(row, join_code=code, role="host")

    async def invite(
        self, request: SynapseRequest, session_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, session = await self._session_for_host(
            request, session_id, ACCOUNT_WRITE
        )
        body = parse_json_body(request, self.config.max_json_body_bytes)
        unknown = sorted(set(body) - {"userId", "username"})
        if unknown:
            raise invalid_request("Unknown invitation fields: " + ", ".join(unknown))
        invitee = await self._resolve_user(body)
        if invitee == caller.user_id:
            raise invalid_request("You cannot invite yourself.")
        if session[4] != "active":
            raise conflict("Collaboration session has ended.")
        now = now_ms()
        invite_id = "cinv_" + uuid.uuid4().hex
        row = await self.db.run(
            "kinemium_create_collab_invite",
            _insert_invite,
            invite_id,
            session_id,
            caller.user_id,
            invitee,
            self.config.collab_invite_ttl_seconds * 1000,
            now,
            self.db.for_update(),
        )
        await self._notify(
            invitee,
            "collab.invite",
            caller.user_id,
            {
                "inviteId": row[0],
                "sessionId": row[1],
                "gameId": row[9],
                "gameName": row[14],
                "sessionName": row[11],
                "hostUserId": row[10],
                "expiresAt": row[7],
            },
        )
        return 201, self._invite_payload(row)

    async def list_session_invites(
        self, request: SynapseRequest, session_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _session = await self._session_for_host(
            request, session_id, ACCOUNT_READ
        )
        status = self._validate_status(request, "status", INVITE_STATUSES)
        limit = self.page_limit(request)
        cursor_created, cursor_id = self._cursor(request)
        rows, has_more = await self.db.run(
            "kinemium_list_collab_invites",
            _list_invites,
            session_id=session_id,
            invitee=None,
            game_id=caller.game_id,
            status=status,
            limit=limit,
            cursor_created=cursor_created,
            cursor_id=cursor_id,
            now=now_ms(),
        )
        result: dict[str, Any] = {
            "invites": [self._invite_payload(row) for row in rows]
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][5], "id": rows[-1][0]}
            )
        return 200, result

    async def revoke_invite(
        self, request: SynapseRequest, session_id: str, invite_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _session = await self._session_for_host(
            request, session_id, ACCOUNT_WRITE
        )
        validate_id(invite_id, "inviteId")
        row = await self.db.run(
            "kinemium_revoke_collab_invite",
            _revoke_invite,
            session_id,
            invite_id,
            caller.user_id,
            caller.game_id,
            now_ms(),
            self.db.for_update(),
        )
        await self._notify(
            row[3],
            "collab.invite_revoked",
            caller.user_id,
            {
                "inviteId": row[0],
                "sessionId": row[1],
                "gameId": row[9],
                "gameName": row[14],
                "sessionName": row[11],
                "hostUserId": row[10],
            },
        )
        return 200, self._invite_payload(row)

    async def list_session_join_requests(
        self, request: SynapseRequest, session_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller, _session = await self._session_for_host(
            request, session_id, ACCOUNT_READ
        )
        status = self._validate_status(request, "status", JOIN_REQUEST_STATUSES)
        limit = self.page_limit(request)
        cursor_created, cursor_id = self._cursor(request)
        rows, has_more = await self.db.run(
            "kinemium_list_collab_join_requests",
            _list_join_requests,
            session_id=session_id,
            user_id=None,
            game_id=caller.game_id,
            status=status,
            limit=limit,
            cursor_created=cursor_created,
            cursor_id=cursor_id,
            now=now_ms(),
        )
        result: dict[str, Any] = {
            "joinRequests": [self._join_request_payload(row) for row in rows]
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][4], "id": rows[-1][0]}
            )
        return 200, result

    async def approve_join_request(
        self,
        request: SynapseRequest,
        session_id: str,
        request_id: str,
    ) -> tuple[int, dict[str, Any]]:
        caller, _session = await self._session_for_host(
            request, session_id, ACCOUNT_WRITE
        )
        validate_id(request_id, "requestId")
        row, session = await self.db.run(
            "kinemium_approve_collab_join_request",
            _approve_join_request,
            session_id,
            request_id,
            caller.user_id,
            caller.game_id,
            now_ms(),
            self.db.for_update(),
        )
        await self._notify(
            row[2],
            "collab.join_approved",
            caller.user_id,
            {
                "requestId": row[0],
                "sessionId": row[1],
                "gameId": row[8],
                "gameName": row[13],
                "sessionName": row[10],
                "hostUserId": row[9],
            },
        )
        return 200, {
            "joinRequest": self._join_request_payload(row),
            "session": self._session_payload(session, role="host"),
        }

    async def deny_join_request(
        self,
        request: SynapseRequest,
        session_id: str,
        request_id: str,
    ) -> tuple[int, dict[str, Any]]:
        caller, _session = await self._session_for_host(
            request, session_id, ACCOUNT_WRITE
        )
        validate_id(request_id, "requestId")
        row = await self.db.run(
            "kinemium_deny_collab_join_request",
            _deny_join_request,
            session_id,
            request_id,
            caller.user_id,
            caller.game_id,
            now_ms(),
            self.db.for_update(),
        )
        await self._notify(
            row[2],
            "collab.join_denied",
            caller.user_id,
            {
                "requestId": row[0],
                "sessionId": row[1],
                "gameId": row[8],
                "gameName": row[13],
                "sessionName": row[10],
                "hostUserId": row[9],
            },
        )
        return 200, self._join_request_payload(row)

    async def kick_participant(
        self,
        request: SynapseRequest,
        session_id: str,
        user_id: str,
    ) -> tuple[int, dict[str, Any]]:
        caller, _session = await self._session_for_host(
            request, session_id, ACCOUNT_WRITE
        )
        validate_matrix_user_id(user_id)
        row = await self.db.run(
            "kinemium_kick_collab_participant",
            _kick_participant,
            session_id,
            user_id,
            caller.user_id,
            caller.game_id,
            now_ms(),
            self.db.for_update(),
        )
        await self._notify(
            user_id,
            "collab.kicked",
            caller.user_id,
            {
                "sessionId": row[0],
                "gameId": row[1],
                "gameName": row[14],
                "sessionName": row[3],
                "hostUserId": row[2],
            },
        )
        return 200, self._session_payload(row, role="host")

    async def list_my_invites(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        status = self._validate_status(request, "status", INVITE_STATUSES)
        game_id = query_value(request, "gameId", None, 128)
        if game_id is not None:
            validate_id(game_id, "gameId")
        if caller.game_id is not None:
            if game_id is not None and caller.game_id != game_id:
                raise not_found("Game not found.")
            game_id = caller.game_id
        limit = self.page_limit(request)
        cursor_created, cursor_id = self._cursor(request)
        rows, has_more = await self.db.run(
            "kinemium_list_collab_invites",
            _list_invites,
            session_id=None,
            invitee=caller.user_id,
            game_id=game_id,
            status=status,
            limit=limit,
            cursor_created=cursor_created,
            cursor_id=cursor_id,
            now=now_ms(),
        )
        result: dict[str, Any] = {
            "invites": [self._invite_payload(row) for row in rows]
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][5], "id": rows[-1][0]}
            )
        return 200, result

    async def accept_invite(
        self, request: SynapseRequest, invite_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(invite_id, "inviteId")
        row, session = await self.db.run(
            "kinemium_accept_collab_invite",
            _accept_invite,
            invite_id,
            caller.user_id,
            caller.game_id,
            now_ms(),
            self.db.for_update(),
        )
        await self._notify(
            row[10],
            "collab.invite_accepted",
            caller.user_id,
            {
                "inviteId": row[0],
                "sessionId": row[1],
                "gameId": row[9],
                "gameName": row[14],
                "sessionName": row[11],
                "userId": caller.user_id,
            },
        )
        return 200, {
            "invite": self._invite_payload(row),
            "session": self._session_payload(session, role="guest"),
        }

    async def decline_invite(
        self, request: SynapseRequest, invite_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(invite_id, "inviteId")
        row = await self.db.run(
            "kinemium_decline_collab_invite",
            _decline_invite,
            invite_id,
            caller.user_id,
            caller.game_id,
            now_ms(),
            self.db.for_update(),
        )
        await self._notify(
            row[10],
            "collab.invite_declined",
            caller.user_id,
            {
                "inviteId": row[0],
                "sessionId": row[1],
                "gameId": row[9],
                "gameName": row[14],
                "sessionName": row[11],
                "userId": caller.user_id,
            },
        )
        return 200, self._invite_payload(row)

    async def join_by_code(self, request: SynapseRequest) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        unknown = sorted(set(body) - {"code"})
        if unknown:
            raise invalid_request("Unknown join fields: " + ", ".join(unknown))
        code = require_str(body, "code", 1, 256).strip()
        if not code:
            raise invalid_request("code must not be blank.")
        session, join_request, created = await self.db.run(
            "kinemium_join_collab_by_code",
            _join_by_code,
            self._hash_join_code(code),
            caller.user_id,
            caller.game_id,
            now_ms(),
            self.db.for_update(),
        )
        if session[4] != "active":
            raise conflict("Collaboration session has ended.")
        role = "host" if session[2] == caller.user_id else "guest"
        if join_request is None:
            return 200, {
                "joined": True,
                "session": self._session_payload(session, role=role),
                "joinRequest": None,
            }
        if created:
            await self._notify(
                session[2],
                "collab.join_request",
                caller.user_id,
                {
                    "requestId": join_request[0],
                    "sessionId": join_request[1],
                    "gameId": join_request[8],
                    "gameName": join_request[13],
                    "sessionName": join_request[10],
                    "userId": caller.user_id,
                },
            )
        return (201 if created else 200), {
            "joined": False,
            "session": self._session_payload(session, role=role),
            "joinRequest": self._join_request_payload(join_request),
        }

    async def list_my_join_requests(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        status = self._validate_status(request, "status", JOIN_REQUEST_STATUSES)
        game_id = query_value(request, "gameId", None, 128)
        if game_id is not None:
            validate_id(game_id, "gameId")
        if caller.game_id is not None:
            if game_id is not None and caller.game_id != game_id:
                raise not_found("Game not found.")
            game_id = caller.game_id
        limit = self.page_limit(request)
        cursor_created, cursor_id = self._cursor(request)
        rows, has_more = await self.db.run(
            "kinemium_list_collab_join_requests",
            _list_join_requests,
            session_id=None,
            user_id=caller.user_id,
            game_id=game_id,
            status=status,
            limit=limit,
            cursor_created=cursor_created,
            cursor_id=cursor_id,
            now=now_ms(),
        )
        result: dict[str, Any] = {
            "joinRequests": [self._join_request_payload(row) for row in rows]
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][4], "id": rows[-1][0]}
            )
        return 200, result

    async def cancel_join_request(
        self, request: SynapseRequest, request_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(request_id, "requestId")
        row = await self.db.run(
            "kinemium_cancel_collab_join_request",
            _cancel_join_request,
            request_id,
            caller.user_id,
            caller.game_id,
            now_ms(),
            self.db.for_update(),
        )
        await self._notify(
            row[9],
            "collab.join_request_cancelled",
            caller.user_id,
            {
                "requestId": row[0],
                "sessionId": row[1],
                "gameId": row[8],
                "gameName": row[13],
                "sessionName": row[10],
                "userId": caller.user_id,
            },
        )
        return 200, self._join_request_payload(row)

    async def get_session(
        self, request: SynapseRequest, session_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(session_id, "sessionId")
        session, participant = await self.db.run(
            "kinemium_get_collab_session_participant",
            _load_session_participant,
            session_id,
            caller.user_id,
            caller.game_id,
            now_ms(),
            False,
        )
        return 200, self._session_payload(session, role=str(participant[1]))

    async def list_participants(
        self, request: SynapseRequest, session_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(session_id, "sessionId")
        await self.db.run(
            "kinemium_get_collab_session_participant",
            _load_session_participant,
            session_id,
            caller.user_id,
            caller.game_id,
            now_ms(),
            True,
        )
        rows = await self.db.run(
            "kinemium_list_collab_participants",
            _list_participants,
            session_id,
        )
        return 200, {"participants": [self._participant_payload(row) for row in rows]}

    async def heartbeat(
        self, request: SynapseRequest, session_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(session_id, "sessionId")
        row = await self.db.run(
            "kinemium_collab_heartbeat",
            _heartbeat,
            session_id,
            caller.user_id,
            caller.game_id,
            now_ms(),
            self.config.collab_session_ttl_seconds * 1000,
            self.db.for_update(),
        )
        role = "host" if row[2] == caller.user_id else "guest"
        return 200, self._session_payload(row, role=role)

    async def leave_session(
        self, request: SynapseRequest, session_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(session_id, "sessionId")
        row = await self.db.run(
            "kinemium_leave_collab_session",
            _leave_session,
            session_id,
            caller.user_id,
            caller.game_id,
            now_ms(),
            self.db.for_update(),
        )
        await self._notify(
            row[2],
            "collab.participant_left",
            caller.user_id,
            {
                "sessionId": row[0],
                "gameId": row[1],
                "gameName": row[14],
                "sessionName": row[3],
                "hostUserId": row[2],
                "userId": caller.user_id,
            },
        )
        return 200, self._session_payload(row, role="guest")

    async def post_signal(
        self, request: SynapseRequest, session_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(session_id, "sessionId")
        body = parse_json_body(request, self.config.max_json_body_bytes)
        unknown = sorted(set(body) - {"kind", "payload", "toUserId"})
        if unknown:
            raise invalid_request("Unknown signal fields: " + ", ".join(unknown))
        kind = require_str(body, "kind", 1, 64)
        if not _SIGNAL_KIND_RE.fullmatch(kind):
            raise invalid_request("kind contains invalid characters.")
        payload = body.get("payload")
        if not isinstance(payload, dict):
            raise invalid_request("payload must be a JSON object.")
        validate_json_value(payload, self.config.max_collab_signal_bytes)
        to_user_id = optional_str(body, "toUserId", 255)
        if to_user_id is not None:
            to_user_id = to_user_id.strip()
            if not to_user_id:
                raise invalid_request("toUserId must not be blank.")
            validate_matrix_user_id(to_user_id)
        row = await self.db.run(
            "kinemium_create_collab_signal",
            _insert_signal,
            session_id,
            caller.user_id,
            caller.game_id,
            to_user_id,
            kind,
            json_dumps(payload),
            now_ms(),
            self.config.collab_signal_ttl_seconds * 1000,
            self.db.for_update(),
        )
        return 201, self._signal_payload(row)

    async def get_signals(
        self, request: SynapseRequest, session_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(session_id, "sessionId")
        after = query_int(request, "after", 0, 2**63 - 1, 0)
        if after is None:
            after = 0
        limit = self.page_limit(request)
        rows, has_more, session = await self.db.run(
            "kinemium_list_collab_signals",
            _list_signals,
            session_id,
            caller.user_id,
            caller.game_id,
            after,
            limit,
            now_ms(),
        )
        result: dict[str, Any] = {
            "signals": [self._signal_payload(row) for row in rows],
            "sessionStatus": session[4],
            "hasMore": has_more,
        }
        if rows:
            result["nextCursor"] = str(rows[-1][2])
        return 200, result
