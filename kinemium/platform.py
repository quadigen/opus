from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import re
import socket
import uuid
from typing import Any
from urllib.parse import urlparse

from twisted.internet.threads import deferToThread

from synapse.http.site import SynapseRequest

from .auth import ACCOUNT_READ, ACCOUNT_WRITE, MARKETPLACE_WRITE
from .base import Service, idempotent
from .errors import conflict, invalid_request, not_found
from .http import parse_json_body, query_value
from .util import decode_cursor, encode_cursor, json_dumps, json_loads, now_ms
from .validation import check_str, optional_str, validate_id, validate_matrix_user_id

logger = logging.getLogger(__name__)

REPORT_TARGETS = ("package", "comment", "review", "user")
REPORT_STATUSES = ("open", "resolved", "dismissed")


def _audit_row(
    txn: Any,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any],
    now: int,
) -> tuple:
    audit_id = "aud_" + uuid.uuid4().hex
    details_json = json_dumps(details)
    txn.execute(
        "INSERT INTO kinemium_admin_audit"
        " (audit_id, actor_user_id, action, target_type, target_id,"
        " details, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (audit_id, actor, action, target_type, target_id, details_json, now),
    )
    return (audit_id, actor, action, target_type, target_id, details_json, now)


def _list_audit(
    txn: Any,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    sql = (
        "SELECT audit_id, actor_user_id, action, target_type, target_id,"
        " details, created_at FROM kinemium_admin_audit"
    )
    args: list[Any] = []
    if cursor_created is not None and cursor_id is not None:
        sql += " WHERE (created_at < %s OR (created_at = %s AND audit_id < %s))"
        args.extend([cursor_created, cursor_created, cursor_id])
    sql += " ORDER BY created_at DESC, audit_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _insert_report(
    txn: Any,
    target_type: str,
    target_id: str,
    package_id: str | None,
    reporter: str,
    reason: str,
    details: str,
    now: int,
) -> tuple:
    report_id = "rep_" + uuid.uuid4().hex
    txn.execute(
        "INSERT INTO kinemium_reports"
        " (report_id, target_type, target_id, package_id, reporter_user_id,"
        " reason, details, status, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', %s)",
        (report_id, target_type, target_id, package_id, reporter, reason, details, now),
    )
    return (
        report_id,
        target_type,
        target_id,
        package_id,
        reporter,
        reason,
        details,
        "open",
        now,
        None,
        None,
        "",
    )


def _list_reports(
    txn: Any,
    status: str | None,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    sql = (
        "SELECT report_id, target_type, target_id, package_id, reporter_user_id,"
        " reason, details, status, created_at, resolved_by, resolved_at,"
        " resolution_note FROM kinemium_reports"
    )
    clauses: list[str] = []
    args: list[Any] = []
    if status is not None:
        clauses.append("status = %s")
        args.append(status)
    if cursor_created is not None and cursor_id is not None:
        clauses.append("(created_at < %s OR (created_at = %s AND report_id < %s))")
        args.extend([cursor_created, cursor_created, cursor_id])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, report_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _resolve_report(
    txn: Any,
    report_id: str,
    status: str,
    note: str,
    resolver: str,
    now: int,
) -> tuple:
    txn.execute(
        "SELECT report_id FROM kinemium_reports WHERE report_id = %s",
        (report_id,),
    )
    if txn.fetchone() is None:
        raise not_found("Report not found.")
    txn.execute(
        "UPDATE kinemium_reports SET status = %s, resolved_by = %s,"
        " resolved_at = %s, resolution_note = %s WHERE report_id = %s",
        (status, resolver, now, note, report_id),
    )
    txn.execute(
        "SELECT report_id, target_type, target_id, package_id, reporter_user_id,"
        " reason, details, status, created_at, resolved_by, resolved_at,"
        " resolution_note FROM kinemium_reports WHERE report_id = %s",
        (report_id,),
    )
    row = txn.fetchone()
    if row is None:
        raise not_found("Report not found.")
    return row


def _insert_notification(
    txn: Any,
    user_id: str,
    kind: str,
    actor: str | None,
    package_id: str | None,
    payload: dict[str, Any],
    now: int,
) -> tuple:
    notification_id = "ntf_" + uuid.uuid4().hex
    payload_json = json_dumps(payload)
    txn.execute(
        "INSERT INTO kinemium_notifications"
        " (notification_id, user_id, type, actor_user_id, package_id,"
        " payload, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (notification_id, user_id, kind, actor, package_id, payload_json, now),
    )
    return (notification_id, user_id, kind, actor, package_id, payload_json, now, None)


def _list_notifications(
    txn: Any,
    user_id: str,
    unread_only: bool,
    limit: int,
    cursor_created: int | None,
    cursor_id: str | None,
) -> tuple[list[tuple], bool]:
    sql = (
        "SELECT notification_id, user_id, type, actor_user_id, package_id,"
        " payload, created_at, read_at FROM kinemium_notifications"
        " WHERE user_id = %s"
    )
    args: list[Any] = [user_id]
    if unread_only:
        sql += " AND read_at IS NULL"
    if cursor_created is not None and cursor_id is not None:
        sql += " AND (created_at < %s OR (created_at = %s AND notification_id < %s))"
        args.extend([cursor_created, cursor_created, cursor_id])
    sql += " ORDER BY created_at DESC, notification_id DESC LIMIT %s"
    args.append(limit + 1)
    txn.execute(sql, tuple(args))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _mark_notifications(
    txn: Any, user_id: str, ids: list[str], now: int
) -> int:
    if ids:
        placeholders = ", ".join(["%s"] * len(ids))
        txn.execute(
            f"UPDATE kinemium_notifications SET read_at = COALESCE(read_at, %s)"
            f" WHERE user_id = %s AND notification_id IN ({placeholders})",
            (now, user_id, *ids),
        )
    else:
        txn.execute(
            "UPDATE kinemium_notifications SET read_at = COALESCE(read_at, %s)"
            " WHERE user_id = %s AND read_at IS NULL",
            (now, user_id),
        )
    return int(txn.rowcount or 0)


def _delete_notification(
    txn: Any, user_id: str, notification_id: str
) -> None:
    txn.execute(
        "DELETE FROM kinemium_notifications"
        " WHERE notification_id = %s AND user_id = %s",
        (notification_id, user_id),
    )
    if int(txn.rowcount or 0) == 0:
        raise not_found("Notification not found.")


def _webhook_rows(txn: Any, user_id: str) -> list[tuple]:
    txn.execute(
        "SELECT webhook_id, user_id, url, secret, events, active, created_at,"
        " updated_at, last_delivery_at, last_error FROM kinemium_webhooks"
        " WHERE user_id = %s ORDER BY created_at DESC, webhook_id DESC",
        (user_id,),
    )
    return txn.fetchall()


def _webhook_row(txn: Any, user_id: str, webhook_id: str) -> tuple | None:
    txn.execute(
        "SELECT webhook_id, user_id, url, secret, events, active, created_at,"
        " updated_at, last_delivery_at, last_error FROM kinemium_webhooks"
        " WHERE webhook_id = %s AND user_id = %s",
        (webhook_id, user_id),
    )
    return txn.fetchone()


def _create_webhook(
    txn: Any,
    user_id: str,
    url: str,
    secret: str,
    events: list[str],
    now: int,
    maximum: int,
    for_update: str,
) -> tuple:
    txn.execute(
        "SELECT user_id FROM kinemium_profiles WHERE user_id = %s" + for_update,
        (user_id,),
    )
    txn.execute(
        "SELECT COUNT(*) FROM kinemium_webhooks WHERE user_id = %s",
        (user_id,),
    )
    count_row = txn.fetchone()
    if count_row is not None and int(count_row[0]) >= maximum:
        raise conflict("Webhook limit reached.")
    webhook_id = "whk_" + uuid.uuid4().hex
    events_json = json_dumps(events)
    txn.execute(
        "INSERT INTO kinemium_webhooks"
        " (webhook_id, user_id, url, secret, events, active, created_at,"
        " updated_at) VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s)",
        (webhook_id, user_id, url, secret, events_json, now, now),
    )
    return (webhook_id, user_id, url, secret, events_json, True, now, now, None, None)


def _update_webhook(
    txn: Any,
    user_id: str,
    webhook_id: str,
    url: str | None,
    events: list[str] | None,
    active: bool | None,
    now: int,
) -> tuple:
    row = _webhook_row(txn, user_id, webhook_id)
    if row is None:
        raise not_found("Webhook not found.")
    txn.execute(
        "UPDATE kinemium_webhooks SET url = %s, events = %s, active = %s,"
        " updated_at = %s WHERE webhook_id = %s AND user_id = %s",
        (
            url if url is not None else row[2],
            json_dumps(events) if events is not None else row[4],
            active if active is not None else bool(row[5]),
            now,
            webhook_id,
            user_id,
        ),
    )
    updated = _webhook_row(txn, user_id, webhook_id)
    if updated is None:
        raise not_found("Webhook not found.")
    return updated


def _delete_webhook(txn: Any, user_id: str, webhook_id: str) -> None:
    txn.execute(
        "DELETE FROM kinemium_webhooks WHERE webhook_id = %s AND user_id = %s",
        (webhook_id, user_id),
    )
    if int(txn.rowcount or 0) == 0:
        raise not_found("Webhook not found.")


def _matching_webhooks(txn: Any, user_id: str, event: str) -> list[tuple]:
    txn.execute(
        "SELECT webhook_id, url, secret, events FROM kinemium_webhooks"
        " WHERE user_id = %s AND active = TRUE",
        (user_id,),
    )
    result = []
    for row in txn.fetchall():
        try:
            events = json_loads(row[3])
        except Exception:
            continue
        if isinstance(events, list) and ("*" in events or event in events):
            result.append(row)
    return result


def _create_delivery(
    txn: Any, webhook_id: str, event: str, payload: str, now: int
) -> str:
    delivery_id = "dlv_" + uuid.uuid4().hex
    txn.execute(
        "INSERT INTO kinemium_webhook_deliveries"
        " (delivery_id, webhook_id, event_type, payload, status, created_at)"
        " VALUES (%s, %s, %s, %s, 'pending', %s)",
        (delivery_id, webhook_id, event, payload, now),
    )
    return delivery_id


def _finish_delivery(
    txn: Any,
    delivery_id: str,
    webhook_id: str,
    status: str,
    response_code: int | None,
    error: str | None,
    now: int,
) -> None:
    txn.execute(
        "UPDATE kinemium_webhook_deliveries SET status = %s, attempts = attempts + 1,"
        " response_code = %s, delivered_at = %s WHERE delivery_id = %s",
        (status, response_code, now, delivery_id),
    )
    txn.execute(
        "UPDATE kinemium_webhooks SET last_delivery_at = %s, last_error = %s"
        " WHERE webhook_id = %s",
        (now, error, webhook_id),
    )


_BLOCKED_WEBHOOK_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".corp",
    ".arpa",
)


def _public_ip_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_global


def _validate_webhook_url(value: str) -> str:
    url = check_str(value, "url", 1, 2048)
    if any(character.isspace() or ord(character) < 32 for character in url):
        raise invalid_request("url must not contain whitespace or control characters.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise invalid_request("url must be an absolute http or https URL.")
    if parsed.username or parsed.password:
        raise invalid_request("url must not include credentials.")
    host = parsed.hostname.lower().rstrip(".")
    blocked = (
        host == "localhost"
        or host.endswith(_BLOCKED_WEBHOOK_SUFFIXES)
        or "." not in host and ":" not in host
    )
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        address = None
    if address is not None:
        blocked = blocked or not address.is_global
    if blocked:
        raise invalid_request("url must point to a public host.")
    return url


async def _host_resolves_public(host: str, port: int) -> bool:
    host = host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        return address.is_global
    if host.endswith(_BLOCKED_WEBHOOK_SUFFIXES) or "." not in host:
        return False
    try:
        results = await deferToThread(
            socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM
        )
    except OSError:
        return False
    if not results:
        return False
    for result in results:
        if not _public_ip_address(str(result[4][0])):
            return False
    return True


def _report_payload(row: tuple) -> dict[str, Any]:
    return {
        "reportId": row[0],
        "targetType": row[1],
        "targetId": row[2],
        "packageId": row[3],
        "reporter": row[4],
        "reason": row[5],
        "details": row[6],
        "status": row[7],
        "createdAt": row[8],
        "resolvedBy": row[9],
        "resolvedAt": row[10],
        "resolutionNote": row[11],
    }


def _notification_payload(row: tuple) -> dict[str, Any]:
    return {
        "notificationId": row[0],
        "userId": row[1],
        "type": row[2],
        "actor": row[3],
        "packageId": row[4],
        "payload": json_loads(row[5]),
        "createdAt": row[6],
        "readAt": row[7],
    }


def _webhook_payload(
    row: tuple, include_secret: bool = False
) -> dict[str, Any]:
    payload = {
        "webhookId": row[0],
        "userId": row[1],
        "url": row[2],
        "events": json_loads(row[4]),
        "active": bool(row[5]),
        "createdAt": row[6],
        "updatedAt": row[7],
        "lastDeliveryAt": row[8],
        "lastError": row[9],
    }
    if include_secret:
        payload["secret"] = row[3]
    return payload


class PlatformService(Service):
    def register(self, resource: Any) -> None:
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/capabilities$")],
            self.capabilities,
            "KinemiumCapabilitiesServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/openapi.json$")],
            self.openapi,
            "KinemiumOpenAPIServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/reports$")],
            self.create_report,
            "KinemiumReportsServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/admin/reports$")],
            self.list_reports,
            "KinemiumAdminReportsServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/admin/reports/(?P<report_id>[^/]+)/resolve$")],
            self.resolve_report,
            "KinemiumAdminReportServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/admin/audit$")],
            self.list_audit,
            "KinemiumAdminAuditServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/me/notifications$")],
            self.list_notifications,
            "KinemiumNotificationsServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/me/notifications/read$")],
            self.mark_notifications,
            "KinemiumNotificationReadServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/me/notifications/(?P<notification_id>[^/]+)$")],
            self.delete_notification,
            "KinemiumNotificationServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/me/webhooks$")],
            self.list_webhooks,
            "KinemiumWebhooksServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/me/webhooks$")],
            self.create_webhook,
            "KinemiumWebhooksServlet",
        )
        resource.register_paths(
            "PATCH",
            [re.compile(r"^/_kinemium/v1/me/webhooks/(?P<webhook_id>[^/]+)$")],
            self.update_webhook,
            "KinemiumWebhookServlet",
        )
        resource.register_paths(
            "DELETE",
            [re.compile(r"^/_kinemium/v1/me/webhooks/(?P<webhook_id>[^/]+)$")],
            self.delete_webhook,
            "KinemiumWebhookServlet",
        )

    async def capabilities(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, None, required=False)
        self.enforce_rate_limit(request, caller)
        return 200, {
            "apiVersion": "v1",
            "packageFormat": ["zip", "tar", "tar.gz"],
            "scopes": [
                "account:read",
                "account:write",
                "marketplace:read",
                "marketplace:write",
                "marketplace:acquire",
                "packages:read",
                "packages:write",
                "spaces:read",
                "spaces:write",
                "*",
            ],
            "limits": {
                "maxArchiveBytes": self.config.max_archive_bytes,
                "maxJsonBodyBytes": self.config.max_json_body_bytes,
                "maxMediaBytes": self.config.max_media_bytes,
                "maxApiTokensPerUser": self.config.max_api_tokens_per_user,
                "maxWebhooksPerUser": self.config.max_webhooks_per_user,
                "maxCollabParticipants": self.config.max_collab_participants,
                "collabSessionTtlSeconds": self.config.collab_session_ttl_seconds,
                "collabInviteTtlSeconds": self.config.collab_invite_ttl_seconds,
                "collabSignalTtlSeconds": self.config.collab_signal_ttl_seconds,
                "maxCollabSignalBytes": self.config.max_collab_signal_bytes,
                "maxPackageVersions": self.config.max_package_versions,
                "paginationDefaultLimit": self.config.pagination_default_limit,
                "paginationMaxLimit": self.config.pagination_max_limit,
                "rateLimitPerSecond": self.config.rate_limit_per_second,
                "rateLimitBurst": self.config.rate_limit_burst,
            },
            "features": {
                "packageLifecycle": True,
                "packageStats": True,
                "versionReviews": True,
                "packageScreenshots": True,
                "acquisitionPinning": True,
                "moderationReports": True,
                "adminAudit": True,
                "notifications": True,
                "webhooks": True,
                "idempotency": True,
                "customNamespaces": True,
                "ownershipTransfer": True,
                "gameProfiles": True,
                "gameServerHosting": True,
                "serverWorkers": True,
                "studioCollaboration": True,
                "collabInvites": True,
                "collabJoinRequests": True,
                "collabSignaling": True,
                "collabSignalTransport": "http-polling",
                "openapi": True,
            },
        }

    async def openapi(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        self.enforce_rate_limit(request, None)
        paths: dict[str, Any] = {}
        operations = {
            "/me": ("get", "Get current profile"),
            "/me/packages": ("get", "List owned packages"),
            "/me/games": ("get", "List owned games"),
            "/games/{gameId}": ("get", "Get a public game profile"),
            "/me/games/{gameId}/server-requests": (
                "get",
                "List free server hosting requests",
            ),
            "/me/games/{gameId}/servers": ("get", "List provisioned game servers"),
            "/me/servers": ("get", "List all provisioned game servers"),
            "/me/games/{gameId}/collab-sessions": (
                "post",
                "Start a Studio collaboration session",
            ),
            "/me/collab-sessions": ("get", "List collaboration sessions"),
            "/me/collab-sessions/{sessionId}": (
                "delete",
                "Stop a collaboration session",
            ),
            "/me/collab-sessions/{sessionId}/join-code": (
                "post",
                "Rotate a collaboration session join code",
            ),
            "/me/collab-sessions/{sessionId}/invites": (
                "post",
                "Invite an Opus user to a collaboration session",
            ),
            "/me/collab-invites": ("get", "List collaboration invitations"),
            "/me/collab-invites/{inviteId}/accept": (
                "post",
                "Accept a collaboration invitation",
            ),
            "/me/collab-join-requests": (
                "get",
                "List your collaboration join requests",
            ),
            "/collab/join": ("post", "Request to join a collaboration session"),
            "/collab/sessions/{sessionId}": (
                "get",
                "Get a collaboration session",
            ),
            "/collab/sessions/{sessionId}/participants": (
                "get",
                "List collaboration participants",
            ),
            "/collab/sessions/{sessionId}/heartbeat": (
                "post",
                "Refresh a collaboration participant heartbeat",
            ),
            "/collab/sessions/{sessionId}/signals": (
                "post",
                "Send a WebRTC signaling message",
            ),
            "/me/notifications": ("get", "List notifications"),
            "/me/webhooks": ("get", "List webhooks"),
            "/packages": ("post", "Publish a package"),
            "/packages/resolve": ("post", "Resolve dependencies"),
            "/packages/{namespace}/{name}": ("get", "Get a package"),
            "/packages/{namespace}/{name}/versions": ("get", "List package versions"),
            "/packages/{namespace}/{name}/manifest": ("get", "Get package manifest"),
            "/packages/{namespace}/{name}/readme": ("get", "Get package readme"),
            "/packages/{namespace}/{name}/dependents": ("get", "List reverse dependencies"),
            "/packages/{namespace}/{name}/stats": ("get", "Get package statistics"),
            "/packages/{namespace}/{name}/screenshots": ("get", "List screenshots"),
            "/packages/{namespace}/{name}/versions/{version}/changelog": ("get", "Get changelog"),
            "/marketplace": ("get", "Browse marketplace"),
            "/marketplace/acquisitions": ("get", "List acquisitions"),
            "/marketplace/updates": ("get", "List available updates"),
            "/marketplace/{namespace}/{name}": ("get", "Get marketplace package"),
            "/namespaces": ("get", "List owned namespaces"),
            "/reports": ("post", "Submit a report"),
            "/admin/reports": ("get", "List reports"),
            "/admin/audit": ("get", "List admin audit events"),
            "/admin/game-server-requests": (
                "get",
                "List server hosting requests for the provisioning worker",
            ),
            "/admin/game-servers": ("get", "List provisioned game servers"),
            "/capabilities": ("get", "Get API capabilities"),
            "/openapi.json": ("get", "Get this document"),
        }
        for path, (method, summary) in operations.items():
            paths[path] = {
                method: {
                    "summary": summary,
                    "security": [{"matrixToken": []}, {"apiToken": []}],
                    "responses": {"200": {"description": "Success"}},
                }
            }
        return 200, {
            "openapi": "3.1.0",
            "info": {
                "title": "Kinemium developer platform API",
                "version": "1.0.0",
                "description": "Developer platform API served by the Kinemium Synapse module.",
            },
            "servers": [{"url": "/_kinemium/v1"}],
            "components": {
                "securitySchemes": {
                    "matrixToken": {"type": "http", "scheme": "bearer"},
                    "apiToken": {"type": "http", "scheme": "bearer"},
                }
            },
            "paths": paths,
        }

    async def record_audit(
        self,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = await self.db.run(
            "kinemium_record_audit",
            _audit_row,
            actor,
            action,
            target_type,
            target_id,
            details or {},
            now_ms(),
        )
        return {
            "auditId": row[0],
            "actor": row[1],
            "action": row[2],
            "targetType": row[3],
            "targetId": row[4],
            "details": json_loads(row[5]),
            "createdAt": row[6],
        }

    async def list_audit(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        limit = self.page_limit(request)
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_created = None
        cursor_id = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_created = cursor.get("createdAt")
            cursor_id = cursor.get("auditId")
            if not isinstance(cursor_created, int) or not isinstance(cursor_id, str):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_list_audit",
            _list_audit,
            limit,
            cursor_created,
            cursor_id,
        )
        result: dict[str, Any] = {
            "audit": [
                {
                    "auditId": row[0],
                    "actor": row[1],
                    "action": row[2],
                    "targetType": row[3],
                    "targetId": row[4],
                    "details": json_loads(row[5]),
                    "createdAt": row[6],
                }
                for row in rows
            ]
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][6], "auditId": rows[-1][0]}
            )
        return 200, result

    @idempotent
    async def create_report(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, MARKETPLACE_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {"targetType", "targetId", "namespace", "name", "reason", "details"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown report fields: " + ", ".join(unknown))
        target_type = body.get("targetType")
        if target_type not in REPORT_TARGETS:
            raise invalid_request("targetType must be package, comment, review, or user.")
        target_id = body.get("targetId")
        if not isinstance(target_id, str) or not target_id.strip():
            raise invalid_request("targetId is required.")
        target_id = check_str(target_id.strip(), "targetId", 1, 255)
        reason = body.get("reason")
        if not isinstance(reason, str):
            raise invalid_request("reason is required.")
        reason = check_str(reason.strip(), "reason", 1, 64)
        details = optional_str(body, "details", 4000) or ""
        package_id: str | None = None
        if target_type == "user":
            validate_matrix_user_id(target_id)
        else:
            namespace = body.get("namespace")
            name = body.get("name")
            if not isinstance(namespace, str) or not isinstance(name, str):
                raise invalid_request("namespace and name are required for package content.")
            row = await self.db.run(
                "kinemium_report_package",
                _report_package,
                namespace,
                name,
            )
            if row is None:
                raise not_found("Package not found.")
            package_id = row[0]
            if target_type == "package":
                if target_id != row[0]:
                    raise invalid_request("targetId must match the packageId.")
            elif target_type == "comment":
                exists = await self.db.run(
                    "kinemium_report_comment",
                    _report_comment,
                    row[0],
                    target_id,
                )
                if not exists:
                    raise not_found("Comment not found.")
            elif target_type == "review":
                exists = await self.db.run(
                    "kinemium_report_review",
                    _report_review,
                    row[0],
                    target_id,
                )
                if not exists:
                    raise not_found("Review not found.")
        row = await self.db.run(
            "kinemium_create_report",
            _insert_report,
            target_type,
            target_id,
            package_id,
            caller.user_id,
            reason,
            details,
            now_ms(),
        )
        return 201, {"report": _report_payload(row)}

    async def list_reports(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        status = query_value(request, "status", None, 16)
        if status is not None and status not in REPORT_STATUSES:
            raise invalid_request("status must be open, resolved, or dismissed.")
        limit = self.page_limit(request)
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_created = None
        cursor_id = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_created = cursor.get("createdAt")
            cursor_id = cursor.get("reportId")
            if not isinstance(cursor_created, int) or not isinstance(cursor_id, str):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_list_reports",
            _list_reports,
            status,
            limit,
            cursor_created,
            cursor_id,
        )
        result: dict[str, Any] = {"reports": [_report_payload(row) for row in rows]}
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][8], "reportId": rows[-1][0]}
            )
        return 200, result

    async def resolve_report(
        self, request: SynapseRequest, report_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.require_admin(request)
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {"status", "note"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown report fields: " + ", ".join(unknown))
        status = body.get("status")
        if status not in ("resolved", "dismissed"):
            raise invalid_request("status must be resolved or dismissed.")
        note = optional_str(body, "note", 4000) or ""
        row = await self.db.run(
            "kinemium_resolve_report",
            _resolve_report,
            report_id,
            status,
            note,
            caller.user_id,
            now_ms(),
        )
        await self.record_audit(
            caller.user_id,
            "report.resolve",
            "report",
            report_id,
            {"status": status, "note": note},
        )
        return 200, {"report": _report_payload(row)}

    async def notify(
        self,
        user_id: str,
        kind: str,
        actor: str | None,
        package_id: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if user_id == actor:
            return None
        now = now_ms()
        row = await self.db.run(
            "kinemium_create_notification",
            _insert_notification,
            user_id,
            kind,
            actor,
            package_id,
            payload,
            now,
        )
        try:
            await self.dispatch_webhook(user_id, kind, payload)
        except Exception:
            logger.warning("Unable to dispatch webhook", exc_info=True)
        return _notification_payload(row)

    async def list_notifications(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        limit = self.page_limit(request)
        unread_only = (query_value(request, "unread", "false", 8) or "").lower() == "true"
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor_created = None
        cursor_id = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            cursor_created = cursor.get("createdAt")
            cursor_id = cursor.get("notificationId")
            if not isinstance(cursor_created, int) or not isinstance(cursor_id, str):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_list_notifications",
            _list_notifications,
            caller.user_id,
            unread_only,
            limit,
            cursor_created,
            cursor_id,
        )
        result: dict[str, Any] = {
            "notifications": [_notification_payload(row) for row in rows]
        }
        if has_more and rows:
            result["nextCursor"] = encode_cursor(
                {"createdAt": rows[-1][6], "notificationId": rows[-1][0]}
            )
        return 200, result

    async def mark_notifications(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        unknown = sorted(set(body) - {"ids"})
        if unknown:
            raise invalid_request("Unknown notification fields: " + ", ".join(unknown))
        ids = body.get("ids", [])
        if (
            not isinstance(ids, list)
            or len(ids) > 1000
            or not all(isinstance(item, str) for item in ids)
        ):
            raise invalid_request("ids must be an array of up to 1000 notification IDs.")
        updated = await self.db.run(
            "kinemium_mark_notifications",
            _mark_notifications,
            caller.user_id,
            ids,
            now_ms(),
        )
        return 200, {"updated": updated}

    async def delete_notification(
        self, request: SynapseRequest, notification_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(notification_id, "notificationId")
        await self.db.run(
            "kinemium_delete_notification",
            _delete_notification,
            caller.user_id,
            notification_id,
        )
        return 200, {"deleted": True, "notificationId": notification_id}

    async def list_webhooks(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_READ, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        rows = await self.db.run(
            "kinemium_list_webhooks", _webhook_rows, caller.user_id
        )
        return 200, {"webhooks": [_webhook_payload(row) for row in rows]}

    async def create_webhook(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {"url", "events"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown webhook fields: " + ", ".join(unknown))
        url = _validate_webhook_url(body.get("url", ""))
        events_raw = body.get("events", ["*"])
        if not isinstance(events_raw, list) or not all(
            isinstance(item, str) for item in events_raw
        ):
            raise invalid_request("events must be an array of strings.")
        if len(events_raw) > 50:
            raise invalid_request("events must contain at most 50 entries.")
        events = []
        for item in events_raw:
            event = check_str(item.strip(), "event", 1, 64)
            if event not in events:
                events.append(event)
        if not events:
            raise invalid_request("events must not be empty.")
        secret = "whsec_" + uuid.uuid4().hex + uuid.uuid4().hex
        row = await self.db.run(
            "kinemium_create_webhook",
            _create_webhook,
            caller.user_id,
            url,
            secret,
            events,
            now_ms(),
            self.config.max_webhooks_per_user,
            self.db.for_update(),
        )
        return 201, {"webhook": _webhook_payload(row, include_secret=True)}

    async def update_webhook(
        self, request: SynapseRequest, webhook_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(webhook_id, "webhookId")
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {"url", "events", "active"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown webhook fields: " + ", ".join(unknown))
        url = _validate_webhook_url(body["url"]) if "url" in body else None
        events: list[str] | None = None
        if "events" in body:
            value = body.get("events")
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise invalid_request("events must be an array of strings.")
            events = [check_str(item.strip(), "event", 1, 64) for item in value]
            if not events:
                raise invalid_request("events must not be empty.")
        active = None
        if "active" in body:
            if not isinstance(body.get("active"), bool):
                raise invalid_request("active must be a boolean.")
            active = body["active"]
        row = await self.db.run(
            "kinemium_update_webhook",
            _update_webhook,
            caller.user_id,
            webhook_id,
            url,
            events,
            active,
            now_ms(),
        )
        return 200, {"webhook": _webhook_payload(row)}

    async def delete_webhook(
        self, request: SynapseRequest, webhook_id: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, ACCOUNT_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_id(webhook_id, "webhookId")
        await self.db.run(
            "kinemium_delete_webhook",
            _delete_webhook,
            caller.user_id,
            webhook_id,
        )
        return 200, {"deleted": True, "webhookId": webhook_id}

    async def dispatch_webhook(
        self, user_id: str, event: str, payload: dict[str, Any]
    ) -> None:
        hooks = await self.db.run(
            "kinemium_matching_webhooks", _matching_webhooks, user_id, event
        )
        if not hooks:
            return
        now = now_ms()
        body = json_dumps(
            {"event": event, "createdAt": now, "data": payload}
        )
        for webhook_id, url, secret, _events in hooks:
            delivery_id = await self.db.run(
                "kinemium_create_delivery",
                _create_delivery,
                webhook_id,
                event,
                body,
                now,
            )
            from twisted.internet import defer

            defer.ensureDeferred(
                self._deliver(webhook_id, url, secret, event, body, delivery_id)
            )

    async def _deliver(
        self,
        webhook_id: str,
        url: str,
        secret: str,
        event: str,
        body: str,
        delivery_id: str,
    ) -> None:
        signature = "sha256=" + hmac.new(
            secret.encode(), body.encode(), hashlib.sha256
        ).hexdigest()
        status = "failed"
        response_code = None
        error = None
        try:
            import treq

            parsed = urlparse(url)
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if not host or not await _host_resolves_public(host, port):
                raise ValueError("Webhook host does not resolve to a public address.")
            headers: dict[bytes | str, bytes | str] = {
                b"Content-Type": b"application/json",
                b"User-Agent": b"Kinemium-Webhooks/1.0",
                b"X-Kinemium-Event": event.encode(),
                b"X-Kinemium-Signature": signature.encode(),
            }
            response = await treq.post(
                url,
                data=body.encode(),
                headers=headers,
                timeout=10,
                allow_redirects=False,
            )
            response_code = int(response.code)
            if response_code < 200 or response_code >= 300:
                error = f"HTTP {response_code}"
            else:
                status = "delivered"
        except Exception as exc:
            error = str(exc)[:500]
            logger.warning("Webhook delivery failed: %s", exc)
        try:
            await self.db.run(
                "kinemium_finish_delivery",
                _finish_delivery,
                delivery_id,
                webhook_id,
                status,
                response_code,
                error,
                now_ms(),
            )
        except Exception:
            logger.warning("Unable to record webhook delivery", exc_info=True)


def _report_package(txn: Any, namespace: str, name: str) -> tuple | None:
    txn.execute(
        "SELECT package_id FROM kinemium_packages"
        " WHERE namespace = %s AND name = %s",
        (namespace, name),
    )
    return txn.fetchone()


def _report_comment(txn: Any, package_id: str, comment_id: str) -> bool:
    txn.execute(
        "SELECT 1 FROM kinemium_package_comments"
        " WHERE comment_id = %s AND package_id = %s AND deleted_at IS NULL",
        (comment_id, package_id),
    )
    return txn.fetchone() is not None


def _report_review(txn: Any, package_id: str, review_id: str) -> bool:
    txn.execute(
        "SELECT 1 FROM kinemium_marketplace_reviews"
        " WHERE review_id = %s AND package_id = %s AND deleted_at IS NULL",
        (review_id, package_id),
    )
    return txn.fetchone() is not None
