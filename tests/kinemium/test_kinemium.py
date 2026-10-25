from __future__ import annotations

import base64
import io
import json
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from twisted.internet import defer
from twisted.web.resource import Resource

import kinemium.storage
from kinemium.errors import KinemiumError
from kinemium.storage import validate_archive
from kinemium_module import KinemiumModule

from tests.unittest import HomeserverTestCase

MATRIX_TOKEN = "some_fake_token"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)
GIF_1X1 = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")

kinemium.storage.deferToThread = lambda f, *args, **kwargs: defer.succeed(
    f(*args, **kwargs)
)


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def multipart(
    fields: dict[str, str], filename: str, data: bytes
) -> tuple[bytes, bytes]:
    boundary = "----kinemiumtestboundary"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            ).encode()
        )
        chunks.append(value.encode())
        chunks.append(b"\r\n")
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
    )
    chunks.append(data)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/mixed; boundary={boundary}".encode()


class KinemiumTestCase(HomeserverTestCase):
    user_id = "@alice:test"

    def make_homeserver(self, reactor: Any, clock: Any) -> Any:
        hs = self.setup_test_homeserver(reactor=reactor, clock=clock)
        storage_path = tempfile.mkdtemp(prefix="kinemium-test-")
        self.storage_path = storage_path
        self.addCleanup(shutil.rmtree, storage_path, True)
        self.module = KinemiumModule(
            {
                "storage_path": storage_path,
                "server_worker_token": "test-worker-token-123456",
            },
            hs.get_module_api(),
        )
        self._admin = False

        async def is_user_admin(user_id: str) -> bool:
            return self._admin and user_id == self.user_id

        self.module.api.is_user_admin = is_user_admin  # type: ignore[method-assign]
        return hs

    def create_resource_dict(self) -> dict[str, Resource]:
        resources = super().create_resource_dict()
        resources.update(self.hs._module_web_resources)
        return resources

    def request(
        self,
        method: str,
        path: str,
        content: Any = b"",
        access_token: str | None = MATRIX_TOKEN,
        content_type: bytes | None = None,
        custom_headers: Any = None,
    ) -> Any:
        return self.make_request(
            method,
            path,
            content,
            access_token=access_token,
            shorthand=False,
            content_type=content_type,
            custom_headers=custom_headers,
        )

    def create_game(self, name: str = "Game") -> str:
        channel = self.request("POST", "/_kinemium/v1/me/games", {"name": name})
        self.assertEqual(channel.code, 201, channel.json_body)
        return channel.json_body["gameId"]

    def create_token(
        self, game_id: str, scopes: list[str] | None = None
    ) -> str:
        channel = self.request(
            "POST",
            "/_kinemium/v1/me/tokens",
            {
                "name": "CI",
                "scopes": scopes
                or [
                    "account:read",
                    "marketplace:read",
                    "marketplace:write",
                    "marketplace:acquire",
                ],
                "gameId": game_id,
            },
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        return channel.json_body["token"]

    def test_account_and_game_token(self) -> None:
        game_id = self.create_game()
        token = self.create_token(game_id)
        channel = self.request("GET", "/_kinemium/v1/me", access_token=token)
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["userId"], "@alice:test")
        channel = self.request("POST", "/_kinemium/v1/me/games", {"name": "Other"}, access_token=token)
        self.assertEqual(channel.code, 403, channel.json_body)
        channel = self.request(
            "GET", f"/_kinemium/v1/me?access_token={token}", access_token=None
        )
        self.assertEqual(channel.code, 401, channel.json_body)

    def test_game_token_scope_confinement(self) -> None:
        game_id = self.create_game()
        channel = self.request("POST", "/_kinemium/v1/me/games", {"name": "Other"})
        other_game_id = channel.json_body["gameId"]
        token = self.create_token(game_id, ["account:read", "account:write"])
        self.create_token(other_game_id, ["account:read"])

        channel = self.request("GET", "/_kinemium/v1/me/tokens", access_token=token)
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(
            [item["gameId"] for item in channel.json_body["tokens"]], [game_id]
        )

        channel = self.request("GET", "/_kinemium/v1/me/tokens")
        other_token_id = next(
            item["tokenId"]
            for item in channel.json_body["tokens"]
            if item["gameId"] == other_game_id
        )
        channel = self.request(
            "DELETE",
            f"/_kinemium/v1/me/tokens/{other_token_id}",
            access_token=token,
        )
        self.assertEqual(channel.code, 404, channel.json_body)

        channel = self.request("GET", f"/_kinemium/v1/me/games/{other_game_id}", access_token=token)
        self.assertEqual(channel.code, 404, channel.json_body)
        channel = self.request(
            "PATCH",
            f"/_kinemium/v1/me/games/{game_id}",
            {"name": "Renamed"},
            access_token=token,
        )
        self.assertEqual(channel.code, 403, channel.json_body)
        channel = self.request(
            "DELETE", f"/_kinemium/v1/me/games/{game_id}", access_token=token
        )
        self.assertEqual(channel.code, 403, channel.json_body)
        channel = self.request(
            "POST", "/_kinemium/v1/me/games", {"name": "New"}, access_token=token
        )
        self.assertEqual(channel.code, 403, channel.json_body)

    def test_security_headers_and_webhook_secret(self) -> None:
        channel = self.request("GET", "/_kinemium/v1/capabilities", access_token=None)
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(
            channel.headers.getRawHeaders(b"X-Content-Type-Options"), [b"nosniff"]
        )
        self.assertEqual(
            channel.headers.getRawHeaders(b"Cache-Control"), [b"no-store"]
        )
        channel = self.request(
            "POST",
            "/_kinemium/v1/me/webhooks",
            {"url": "http://127.0.0.1/hook", "events": ["*"]},
        )
        self.assertEqual(channel.code, 400, channel.json_body)
        channel = self.request(
            "POST",
            "/_kinemium/v1/me/webhooks",
            {"url": "http://localhost/hook", "events": ["*"]},
        )
        self.assertEqual(channel.code, 400, channel.json_body)
        channel = self.request(
            "POST",
            "/_kinemium/v1/me/webhooks",
            {"url": "https://example.com/hook", "events": ["*"]},
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        self.assertIn("secret", channel.json_body["webhook"])
        channel = self.request("GET", "/_kinemium/v1/me/webhooks")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertNotIn("secret", channel.json_body["webhooks"][0])

    def test_archive_validation_rejects_special_and_duplicate_files(self) -> None:
        def archive_with(mode: int, name: str) -> bytes:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                archive.writestr(info, b"data")
            return buffer.getvalue()

        with self.assertRaises(KinemiumError):
            validate_archive(
                archive_with(stat.S_IFLNK | 0o777, "link"), "pkg.zip", 100, 10000
            )
        with self.assertRaises(KinemiumError):
            validate_archive(
                archive_with(stat.S_IFIFO | 0o644, "pipe"), "pkg.zip", 100, 10000
            )
        with self.assertRaises(KinemiumError):
            validate_archive(
                make_zip({"src/a.luau": b"1", "src/A.luau": b"2"}),
                "pkg.zip",
                100,
                10000,
            )

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("src/")
            info.external_attr = (stat.S_IFDIR | 0o755) << 16
            archive.writestr(info, b"")
            archive.writestr("src/main.luau", b"return {}")
        self.assertEqual(
            validate_archive(buffer.getvalue(), "pkg.zip", 100, 10000), "zip"
        )

    def test_download_rejects_tampered_archive(self) -> None:
        self.publish_package()
        archives = [
            path for path in Path(self.storage_path).rglob("*") if path.is_file()
        ]
        self.assertEqual(len(archives), 1)
        archives[0].write_bytes(b"tampered")
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool/versions/1.2.0/download"
        )
        self.assertEqual(channel.code, 500, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "KINEMIUM_INTERNAL_ERROR")

    def test_cors_exposes_custom_headers(self) -> None:
        channel = self.request(
            "GET", "/_kinemium/v1/capabilities", access_token=None
        )
        self.assertEqual(channel.code, 200)
        expose = channel.headers.getRawHeaders(
            b"Access-Control-Expose-Headers", [b""]
        )[0]
        for header in (
            b"ETag",
            b"Digest",
            b"Retry-After",
            b"X-Kinemium-Yanked",
            b"X-Kinemium-Download-Count",
            b"X-RateLimit-Limit",
            b"X-RateLimit-Remaining",
            b"X-RateLimit-Reset",
            b"Idempotency-Replayed",
        ):
            self.assertIn(header, expose)

    def test_widget_html_and_svg(self) -> None:
        self.publish_package()
        game_id = self.create_game()
        channel = self.request(
            "GET",
            "/_kinemium/widget?title=Demo&packages=alice/tool,missing/pkg"
            "&users=@alice:test&users=@nobody:test"
            "&namespaces=alice&games=" + game_id,
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertIn(b"text/html", channel.headers.getRawHeaders(b"Content-Type", [b""])[0])
        body = channel.result["body"]
        self.assertIn(b"<h1>Demo</h1>", body)
        self.assertIn(b"alice/tool", body)
        self.assertIn(b"@alice:test", body)
        self.assertIn(b"Game", body)
        self.assertIn(b"Not found or not public.", body)
        policy = channel.headers.getRawHeaders(b"Content-Security-Policy", [b""])[0]
        self.assertIn(b"frame-ancestors *", policy)
        self.assertIsNone(channel.headers.getRawHeaders(b"X-Frame-Options"))

        channel = self.request(
            "GET",
            "/_kinemium/widget.svg?title=Demo&theme=dark&packages=alice/tool"
            "&users=@alice:test&namespaces=alice&games=" + game_id,
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.headers.getRawHeaders(b"Content-Type", [b""])[0],
            b"image/svg+xml; charset=utf-8",
        )
        body = channel.result["body"]
        self.assertTrue(body.startswith(b"<svg "))
        self.assertIn(b"alice/tool", body)
        self.assertIn(b"Demo", body)
        self.assertIn(b"#151518", body)

    def test_admin_review_page(self) -> None:
        channel = self.request(
            "GET", "/_kinemium/admin/review", access_token=None
        )
        self.assertEqual(channel.code, 200)
        self.assertIn(b"Package review", channel.result["body"])
        self.assertIn(b"All packages", channel.result["body"])
        self.assertIn(b"Download", channel.result["body"])
        policy = channel.headers.getRawHeaders(b"Content-Security-Policy")
        self.assertIsNotNone(policy)
        assert policy is not None
        self.assertIn(b"blob:", policy[0])
        channel = self.request("GET", "/_kinemium/admin", access_token=None)
        self.assertEqual(channel.code, 302)

    def set_admin(self, admin: bool) -> None:
        self._admin = admin

    def review_package(self, decision: str = "approve", note: str = "") -> None:
        self.set_admin(True)
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages/alice/tool/review",
            {"decision": decision, "note": note},
            access_token=MATRIX_TOKEN,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

    def publish_package(self, approve: bool = True) -> None:
        archive = make_zip({"src/main.luau": b"return {}\n"})
        manifest = {
            "name": "tool",
            "version": "1.2.0",
            "description": "A starter tool",
            "entrypoint": "src/main.luau",
            "engineConstraint": "^1.0.0",
            "type": "tool",
            "tags": ["starter", "tool"],
        }
        body, content_type = multipart(
            {"manifest": json.dumps(manifest)}, "tool.zip", archive
        )
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages",
            body,
            content_type=content_type,
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        self.assertEqual(channel.json_body["package"]["namespace"], "alice")
        self.assertEqual(channel.json_body["package"]["itemType"], "tool")
        self.assertEqual(channel.json_body["package"]["tags"], ["starter", "tool"])
        self.assertEqual(channel.json_body["package"]["reviewStatus"], "pending")
        if approve:
            self.review_package()

    def publish_version(self, version: str) -> Any:
        archive = make_zip({"src/main.luau": b"return 2\n"})
        manifest = {
            "name": "tool",
            "version": version,
            "entrypoint": "src/main.luau",
            "engineConstraint": "^1.0.0",
        }
        body, content_type = multipart(
            {"manifest": json.dumps(manifest)}, f"tool-{version}.zip", archive
        )
        return self.request(
            "POST",
            "/_kinemium/v1/packages",
            body,
            content_type=content_type,
        )

    def review_version(
        self, version: str, decision: str = "approve", note: str = ""
    ) -> Any:
        self.set_admin(True)
        return self.request(
            "POST",
            "/_kinemium/v1/admin/packages/alice/tool/versions/"
            + version
            + "/review",
            {"decision": decision, "note": note},
        )

    def test_packages_publish_resolve_and_download(self) -> None:
        self.publish_package()
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["latestVersion"], "1.2.0")
        self.assertEqual(channel.json_body["creator"]["userId"], "@alice:test")
        self.assertEqual(channel.json_body["reactions"]["likes"], 0)
        self.assertEqual(channel.json_body["installCount"], 0)
        self.assertEqual(channel.json_body["downloadCount"], 0)
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages/resolve",
            {
                "requirements": [
                    {"namespace": "alice", "name": "tool", "constraint": "^1.0.0"}
                ]
            },
            access_token=None,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"][0]["version"], "1.2.0")
        channel = self.request(
            "GET",
            "/_kinemium/v1/packages/alice/tool/versions/1.2.0/download",
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertTrue(channel.result)
        self.assertEqual(
            channel.headers.getRawHeaders(b"Access-Control-Allow-Origin"), [b"*"]
        )
        self.assertEqual(
            channel.headers.getRawHeaders(b"X-Kinemium-Download-Count"), [b"1"]
        )
        channel = self.request(
            "GET",
            "/_kinemium/v1/packages/alice/tool/versions/1.2.0/download",
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.headers.getRawHeaders(b"X-Kinemium-Download-Count"), [b"2"]
        )
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["downloadCount"], 2)
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages/alice/tool/versions/1.2.0/yank",
            {"yanked": True},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["version"]["yanked"])

    def test_package_review_queue_and_admin_review(self) -> None:
        self.publish_package(approve=False)
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace?q=starter", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"], [])
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 404, channel.json_body)
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=MATRIX_TOKEN
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["reviewStatus"], "pending")
        self.set_admin(False)
        channel = self.request(
            "GET", "/_kinemium/v1/packages/toreview", access_token=MATRIX_TOKEN
        )
        self.assertEqual(channel.code, 403, channel.json_body)
        self.set_admin(True)
        channel = self.request(
            "GET", "/_kinemium/v1/packages/toreview", access_token=MATRIX_TOKEN
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"][0]["name"], "tool")
        self.assertEqual(
            channel.json_body["packages"][0]["reviewStatus"], "pending"
        )
        self.set_admin(False)
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages/alice/tool/review",
            {"decision": "approve"},
            access_token=MATRIX_TOKEN,
        )
        self.assertEqual(channel.code, 403, channel.json_body)
        self.set_admin(True)
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages/alice/tool/review",
            {"decision": "reject", "note": "Please document the entrypoint."},
            access_token=MATRIX_TOKEN,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["package"]["reviewStatus"], "rejected")
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace?q=starter", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"], [])
        channel = self.request(
            "POST", "/_kinemium/v1/packages/alice/tool/submit"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["package"]["reviewStatus"], "pending")
        channel = self.request(
            "GET", "/_kinemium/v1/packages/toreview", access_token=MATRIX_TOKEN
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["packages"]), 1)
        self.review_package()
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace?q=starter", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"][0]["name"], "tool")

    def test_new_package_version_requires_review(self) -> None:
        self.publish_package()
        self.set_admin(False)

        channel = self.publish_version("1.3.0")
        self.assertEqual(channel.code, 201, channel.json_body)
        self.assertEqual(channel.json_body["package"]["reviewStatus"], "approved")
        self.assertEqual(channel.json_body["version"]["reviewStatus"], "pending")

        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["latestVersion"], "1.2.0")
        self.assertEqual(
            [item["version"] for item in channel.json_body["versions"]],
            ["1.2.0"],
        )
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=MATRIX_TOKEN
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["latestVersion"], "1.2.0")
        self.assertEqual(
            [item["version"] for item in channel.json_body["versions"]],
            ["1.3.0", "1.2.0"],
        )
        self.assertEqual(channel.json_body["versions"][0]["reviewStatus"], "pending")

        channel = self.request(
            "GET",
            "/_kinemium/v1/packages/alice/tool/versions/1.3.0/download",
            access_token=None,
        )
        self.assertEqual(channel.code, 404, channel.json_body)
        channel = self.request(
            "GET",
            "/_kinemium/v1/packages/alice/tool/versions/1.3.0/download",
        )
        self.assertEqual(channel.code, 200)
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages/resolve",
            {
                "requirements": [
                    {"namespace": "alice", "name": "tool", "constraint": "^1.0.0"}
                ]
            },
            access_token=None,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"][0]["version"], "1.2.0")

        channel = self.request("GET", "/_kinemium/v1/admin/package-versions")
        self.assertEqual(channel.code, 403, channel.json_body)
        self.set_admin(True)
        channel = self.request("GET", "/_kinemium/v1/admin/package-versions")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["versions"]), 1)
        pending = channel.json_body["versions"][0]
        self.assertEqual(pending["version"], "1.3.0")
        self.assertEqual(pending["reviewStatus"], "pending")
        self.assertEqual(pending["package"]["namespace"], "alice")
        self.assertEqual(pending["package"]["name"], "tool")

        channel = self.review_version(
            "1.3.0", "reject", "Please document the new behavior."
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["version"]["reviewStatus"], "rejected")
        self.assertEqual(
            channel.json_body["version"]["reviewNote"],
            "Please document the new behavior.",
        )
        channel = self.request(
            "GET", "/_kinemium/v1/admin/package-versions"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["versions"], [])
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["latestVersion"], "1.2.0")

        channel = self.publish_version("1.4.0")
        self.assertEqual(channel.code, 201, channel.json_body)
        self.assertEqual(channel.json_body["version"]["reviewStatus"], "pending")
        self.assertEqual(channel.json_body["package"]["reviewStatus"], "approved")
        channel = self.review_version("1.4.0")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["version"]["reviewStatus"], "approved")

        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["latestVersion"], "1.4.0")
        self.assertEqual(
            [item["version"] for item in channel.json_body["versions"]],
            ["1.4.0", "1.2.0"],
        )
        channel = self.request(
            "GET",
            "/_kinemium/v1/packages/alice/tool/versions/1.4.0/download",
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages/resolve",
            {
                "requirements": [
                    {"namespace": "alice", "name": "tool", "constraint": "^1.0.0"}
                ]
            },
            access_token=None,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"][0]["version"], "1.4.0")

    def test_package_icon_and_banner(self) -> None:
        self.publish_package(approve=False)
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages/alice/tool/icon",
            PNG_1X1,
            content_type=b"image/png",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        icon_url = channel.json_body["package"]["iconUrl"]
        self.assertTrue(icon_url.startswith("mxc://"))
        self.assertIsNone(channel.json_body["package"]["bannerUrl"])
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages/alice/tool/banner",
            PNG_1X1,
            content_type=b"image/png",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        banner_url = channel.json_body["package"]["bannerUrl"]
        self.assertTrue(banner_url.startswith("mxc://"))
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 404, channel.json_body)
        channel = self.request("GET", "/_kinemium/v1/packages/alice/tool")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["iconUrl"], icon_url)
        self.assertEqual(channel.json_body["bannerUrl"], banner_url)
        channel = self.request(
            "GET", "/_kinemium/v1/packages/toreview", access_token=MATRIX_TOKEN
        )
        self.assertEqual(channel.code, 403, channel.json_body)
        self.set_admin(True)
        channel = self.request(
            "GET", "/_kinemium/v1/packages/toreview", access_token=MATRIX_TOKEN
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        pending = channel.json_body["packages"][0]
        self.assertEqual(pending["iconUrl"], icon_url)
        self.assertEqual(pending["bannerUrl"], banner_url)
        channel = self.request(
            "DELETE", "/_kinemium/v1/packages/alice/tool/icon"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertIsNone(channel.json_body["package"]["iconUrl"])
        self.assertEqual(channel.json_body["package"]["bannerUrl"], banner_url)

    def test_admin_package_management(self) -> None:
        self.publish_package(approve=False)

        channel = self.request("GET", "/_kinemium/v1/admin/packages")
        self.assertEqual(channel.code, 403, channel.json_body)
        channel = self.request(
            "PATCH",
            "/_kinemium/v1/admin/packages/alice/tool",
            {"description": "Nope"},
        )
        self.assertEqual(channel.code, 403, channel.json_body)
        channel = self.request("DELETE", "/_kinemium/v1/admin/packages/alice/tool")
        self.assertEqual(channel.code, 403, channel.json_body)

        self.set_admin(True)
        channel = self.request("GET", "/_kinemium/v1/admin/packages")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["packages"]), 1)
        package = channel.json_body["packages"][0]
        self.assertEqual(package["namespace"], "alice")
        self.assertEqual(package["name"], "tool")
        self.assertEqual(package["reviewStatus"], "pending")
        self.assertEqual(package["latestVersion"], "1.2.0")
        self.assertEqual(package["installCount"], 0)

        channel = self.request(
            "GET", "/_kinemium/v1/admin/packages?q=starter&status=pending"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["packages"]), 1)
        channel = self.request(
            "GET", "/_kinemium/v1/admin/packages?status=approved"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"], [])
        channel = self.request(
            "GET", "/_kinemium/v1/admin/packages?status=bogus"
        )
        self.assertEqual(channel.code, 400, channel.json_body)

        channel = self.request(
            "GET",
            "/_kinemium/v1/packages/alice/tool/versions/1.2.0/download",
        )
        self.assertEqual(channel.code, 200)
        self.assertTrue(channel.result)

        channel = self.request(
            "PATCH",
            "/_kinemium/v1/admin/packages/alice/tool",
            {
                "description": "Updated by admin",
                "license": "MIT",
                "repository": "https://example.com/tool",
                "type": "plugin",
                "tags": ["admin", "edit"],
                "visibility": "unlisted",
                "deprecated": True,
            },
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        edited = channel.json_body
        self.assertEqual(edited["description"], "Updated by admin")
        self.assertEqual(edited["license"], "MIT")
        self.assertEqual(edited["repository"], "https://example.com/tool")
        self.assertEqual(edited["itemType"], "plugin")
        self.assertEqual(edited["tags"], ["admin", "edit"])
        self.assertEqual(edited["visibility"], "unlisted")
        self.assertTrue(edited["deprecated"])
        self.assertEqual(edited["reviewStatus"], "pending")

        channel = self.request(
            "PATCH",
            "/_kinemium/v1/admin/packages/alice/tool",
            {"license": None, "repository": None},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertIsNone(channel.json_body["license"])
        self.assertIsNone(channel.json_body["repository"])

        archives = [
            path for path in Path(self.storage_path).rglob("*") if path.is_file()
        ]
        self.assertEqual(len(archives), 1)
        channel = self.request(
            "DELETE", "/_kinemium/v1/admin/packages/alice/tool"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["deleted"])
        self.assertEqual(
            [path for path in Path(self.storage_path).rglob("*") if path.is_file()],
            [],
        )
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=MATRIX_TOKEN
        )
        self.assertEqual(channel.code, 404, channel.json_body)
        channel = self.request("GET", "/_kinemium/v1/admin/packages")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"], [])

    def test_admin_versions_and_full_package_edit(self) -> None:
        self.publish_package()
        self.set_admin(True)

        archive = make_zip({"src/main.luau": b"return 2\n"})
        manifest = {
            "name": "tool",
            "version": "1.3.0",
            "entrypoint": "src/main.luau",
            "engineConstraint": "^1.5.0",
        }
        body, content_type = multipart(
            {"manifest": json.dumps(manifest)}, "tool-1.3.0.zip", archive
        )
        channel = self.request(
            "POST",
            "/_kinemium/v1/admin/packages/alice/tool/versions",
            body,
            content_type=content_type,
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        self.assertEqual(channel.json_body["package"]["reviewStatus"], "approved")
        self.assertEqual(channel.json_body["version"]["version"], "1.3.0")
        self.assertEqual(channel.json_body["version"]["engineConstraint"], "^1.5.0")

        channel = self.request(
            "GET", "/_kinemium/v1/admin/packages/alice/tool/versions"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(
            [version["version"] for version in channel.json_body["versions"]],
            ["1.3.0", "1.2.0"],
        )

        channel = self.request(
            "PATCH",
            "/_kinemium/v1/admin/packages/alice/tool/versions/1.3.0",
            {
                "yanked": True,
                "engineConstraint": "^2.0.0",
                "publishedAt": 1234567890,
                "dependencies": {"bob/lib": "^1.0.0"},
            },
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["version"]["yanked"])
        self.assertEqual(channel.json_body["version"]["engineConstraint"], "^2.0.0")
        self.assertEqual(channel.json_body["version"]["publishedAt"], 1234567890)
        self.assertEqual(
            channel.json_body["version"]["dependencies"],
            [{"namespace": "bob", "name": "lib", "constraint": "^1.0.0"}],
        )

        channel = self.request(
            "PATCH",
            "/_kinemium/v1/admin/packages/alice/tool",
            {
                "description": "Admin full edit",
                "engineConstraint": "^3.0.0",
                "owner": "@bob:test",
                "reviewStatus": "approved",
                "reviewNote": "looks good",
                "reviewedBy": "@admin:test",
                "reviewedAt": 111,
                "submittedAt": 222,
                "publishedAt": 333,
                "createdAt": 444,
                "iconUrl": "mxc://test/icon",
                "bannerUrl": "mxc://test/banner",
                "visibility": "private",
                "deprecated": True,
            },
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        edited = channel.json_body
        self.assertEqual(edited["description"], "Admin full edit")
        self.assertEqual(edited["engineConstraint"], "^3.0.0")
        self.assertEqual(edited["owner"], "@bob:test")
        self.assertEqual(edited["reviewStatus"], "approved")
        self.assertEqual(edited["reviewNote"], "looks good")
        self.assertEqual(edited["reviewedBy"], "@admin:test")
        self.assertEqual(edited["reviewedAt"], 111)
        self.assertEqual(edited["submittedAt"], 222)
        self.assertEqual(edited["publishedAt"], 333)
        self.assertEqual(edited["createdAt"], 444)
        self.assertEqual(edited["iconUrl"], "mxc://test/icon")
        self.assertEqual(edited["bannerUrl"], "mxc://test/banner")
        self.assertEqual(edited["visibility"], "private")
        self.assertTrue(edited["deprecated"])

        channel = self.request(
            "POST",
            "/_kinemium/v1/packages/alice/tool/icon",
            PNG_1X1,
            content_type=b"image/png",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["package"]["iconUrl"].startswith("mxc://"))

        channel = self.request(
            "DELETE",
            "/_kinemium/v1/admin/packages/alice/tool/versions/1.3.0",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["deleted"])
        archives = [
            path for path in Path(self.storage_path).rglob("*") if path.is_file()
        ]
        self.assertEqual(len(archives), 1)
        channel = self.request(
            "GET", "/_kinemium/v1/admin/packages/alice/tool/versions"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(
            [version["version"] for version in channel.json_body["versions"]],
            ["1.2.0"],
        )

    def test_marketplace_reactions_and_comments(self) -> None:
        self.publish_package()
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace?q=starter", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"][0]["reactions"]["likes"], 0)

        channel = self.request(
            "POST",
            "/_kinemium/v1/marketplace/alice/tool/reaction",
            {"value": 1},
            access_token=None,
        )
        self.assertEqual(channel.code, 401, channel.json_body)
        channel = self.request(
            "POST",
            "/_kinemium/v1/marketplace/alice/tool/reaction",
            {"value": 1},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["reactions"]["likes"], 1)
        self.assertEqual(channel.json_body["reactions"]["mine"], 1)

        channel = self.request(
            "POST",
            "/_kinemium/v1/marketplace/alice/tool/reaction",
            {"value": 1},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["reactions"]["likes"], 1)

        channel = self.request(
            "POST",
            "/_kinemium/v1/marketplace/alice/tool/reaction",
            {"value": -1},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["reactions"]["likes"], 0)
        self.assertEqual(channel.json_body["reactions"]["dislikes"], 1)

        channel = self.request(
            "GET", "/_kinemium/v1/marketplace/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["reactions"]["score"], -1)
        self.assertIsNone(channel.json_body["reactions"]["mine"])
        self.assertEqual(channel.json_body["creator"]["userId"], "@alice:test")
        self.assertEqual(channel.json_body["downloadCount"], 0)

        channel = self.request(
            "POST",
            "/_kinemium/v1/marketplace/alice/tool/reaction",
            {"value": 0},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["reactions"]["likes"], 0)
        self.assertEqual(channel.json_body["reactions"]["dislikes"], 0)
        self.assertIsNone(channel.json_body["reactions"]["mine"])

        channel = self.request(
            "POST",
            "/_kinemium/v1/marketplace/alice/tool/comments",
            {"body": "  Nice tool  "},
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        comment_id = channel.json_body["comment"]["commentId"]
        self.assertEqual(channel.json_body["comment"]["body"], "Nice tool")

        channel = self.request(
            "POST",
            "/_kinemium/v1/marketplace/alice/tool/comments",
            {"body": "   "},
        )
        self.assertEqual(channel.code, 400, channel.json_body)

        channel = self.request(
            "GET",
            "/_kinemium/v1/marketplace/alice/tool/comments",
            access_token=None,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["count"], 1)
        self.assertEqual(channel.json_body["comments"][0]["commentId"], comment_id)
        self.assertEqual(
            channel.json_body["comments"][0]["userId"], "@alice:test"
        )

        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["commentCount"], 1)
        self.assertEqual(
            channel.json_body["comments"][0]["commentId"], comment_id
        )

    def test_marketplace_lists_and_reviews_packages(self) -> None:
        self.publish_package()
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace?q=starter", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"][0]["name"], "tool")
        self.assertEqual(channel.json_body["packages"][0]["itemType"], "tool")
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace?namespace=alice", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["packages"]), 1)
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace?namespace=other", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"], [])
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace?tag=starter", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["packages"]), 1)
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["latestVersion"], "1.2.0")
        self.assertEqual(channel.json_body["rating"]["count"], 0)
        channel = self.request(
            "POST", "/_kinemium/v1/marketplace/alice/tool/acquire"
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        channel = self.request(
            "POST",
            "/_kinemium/v1/marketplace/alice/tool/reviews",
            {"rating": 5, "body": "Great"},
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["rating"]["count"], 1)
        self.assertEqual(channel.json_body["acquisitionCount"], 1)
        self.assertEqual(channel.json_body["installCount"], 1)
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace?q=starter", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"][0]["installCount"], 1)
        channel = self.request(
            "GET",
            "/_kinemium/v1/marketplace/acquisitions",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(
            channel.json_body["acquisitions"][0]["package"]["name"], "tool"
        )
        channel = self.request(
            "PATCH",
            "/_kinemium/v1/packages/alice/tool",
            {"tags": ["starter", "updated"], "visibility": "unlisted"},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["tags"], ["starter", "updated"])
        self.assertEqual(channel.json_body["visibility"], "unlisted")
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace?tag=updated", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"], [])
        channel = self.request(
            "PATCH",
            "/_kinemium/v1/packages/alice/tool",
            {"visibility": "private"},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 404, channel.json_body)
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 404, channel.json_body)
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=MATRIX_TOKEN
        )
        self.assertEqual(channel.code, 200, channel.json_body)

    def test_games_crud_and_avatar_delete(self) -> None:
        game_id = self.create_game()
        channel = self.request(
            "PATCH", f"/_kinemium/v1/me/games/{game_id}", {"name": "Renamed"}
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["name"], "Renamed")
        channel = self.request("GET", f"/_kinemium/v1/me/games/{game_id}")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["name"], "Renamed")
        channel = self.request(
            "PATCH", f"/_kinemium/v1/me/games/{game_id}", {"name": " "}
        )
        self.assertEqual(channel.code, 400, channel.json_body)
        channel = self.request(
            "POST", "/_kinemium/v1/me/avatar", PNG_1X1, content_type=b"image/png"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["avatarUrl"].startswith("mxc://"))
        channel = self.request("DELETE", "/_kinemium/v1/me/avatar")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertIsNone(channel.json_body["avatarUrl"])
        channel = self.request("DELETE", f"/_kinemium/v1/me/games/{game_id}")
        self.assertEqual(channel.code, 200, channel.json_body)
        channel = self.request("GET", f"/_kinemium/v1/me/games/{game_id}")
        self.assertEqual(channel.code, 404, channel.json_body)

    def test_game_profile_customization(self) -> None:
        game_id = self.create_game()
        channel = self.request(
            "PATCH",
            f"/_kinemium/v1/me/games/{game_id}",
            {
                "description": "A tiny sandbox.",
                "website": "https://example.com/game",
                "bannerUrl": "mxc://test/banner",
            },
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["description"], "A tiny sandbox.")
        self.assertEqual(channel.json_body["website"], "https://example.com/game")
        self.assertEqual(channel.json_body["bannerUrl"], "mxc://test/banner")

        channel = self.request(
            "POST",
            f"/_kinemium/v1/me/games/{game_id}/icon",
            PNG_1X1,
            content_type=b"image/png",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        icon_url = channel.json_body["iconUrl"]
        self.assertTrue(icon_url.startswith("mxc://"))

        channel = self.request(
            "POST",
            f"/_kinemium/v1/me/games/{game_id}/banner",
            PNG_1X1,
            content_type=b"image/png",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["bannerUrl"].startswith("mxc://"))

        channel = self.request(
            "GET", f"/_kinemium/v1/games/{game_id}", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["ownerUserId"], self.user_id)
        self.assertEqual(channel.json_body["iconUrl"], icon_url)
        self.assertFalse(channel.json_body["canManage"])

        channel = self.request(
            "PATCH",
            f"/_kinemium/v1/me/games/{game_id}",
            {"website": None, "iconUrl": None},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertIsNone(channel.json_body["website"])
        self.assertIsNone(channel.json_body["iconUrl"])

        channel = self.request(
            "PATCH",
            f"/_kinemium/v1/me/games/{game_id}",
            {"unknown": True},
        )
        self.assertEqual(channel.code, 400, channel.json_body)

        channel = self.request("DELETE", f"/_kinemium/v1/me/games/{game_id}/banner")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertIsNone(channel.json_body["bannerUrl"])

        token = self.create_token(game_id, ["account:read", "account:write"])
        channel = self.request(
            "PATCH",
            f"/_kinemium/v1/me/games/{game_id}",
            {"description": "Updated by the game token."},
            access_token=token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(
            channel.json_body["description"], "Updated by the game token."
        )
        channel = self.request(
            "PATCH",
            f"/_kinemium/v1/me/games/{game_id}",
            {"name": "Token rename"},
            access_token=token,
        )
        self.assertEqual(channel.code, 403, channel.json_body)

    def test_game_server_hosting(self) -> None:
        game_id = self.create_game()
        metadata = json.dumps(
            {"region": "eu", "maxPlayers": 24, "note": "Weekend playtest"}
        )
        body, content_type = multipart(
            {"metadata": metadata}, "game.kine", b"KINE server payload"
        )
        channel = self.request(
            "POST",
            f"/_kinemium/v1/me/games/{game_id}/server-requests",
            body,
            content_type=content_type,
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        request_id = channel.json_body["requestId"]
        self.assertEqual(channel.json_body["status"], "pending")
        self.assertEqual(channel.json_body["region"], "eu")
        self.assertEqual(channel.json_body["maxPlayers"], 24)
        self.assertEqual(channel.json_body["fileName"], "game.kine")

        channel = self.request(
            "POST",
            f"/_kinemium/v1/me/games/{game_id}/server-requests",
            body,
            content_type=content_type,
        )
        self.assertEqual(channel.code, 409, channel.json_body)

        channel = self.request(
            "GET", f"/_kinemium/v1/me/games/{game_id}/server-requests"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["requests"]), 1)

        channel = self.request("GET", "/_kinemium/v1/admin/game-server-requests")
        self.assertEqual(channel.code, 403, channel.json_body)

        self._admin = True
        channel = self.request(
            "GET", "/_kinemium/v1/admin/game-server-requests?status=pending"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["requests"]), 1)

        channel = self.request(
            "GET",
            "/_kinemium/v1/admin/game-server-requests?status=pending",
            access_token=None,
            custom_headers=[
                (b"X-Kinemium-Worker-Token", b"test-worker-token-123456")
            ],
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["requests"]), 1)

        channel = self.request(
            "POST",
            f"/_kinemium/v1/admin/game-server-requests/{request_id}/claim",
            {"workerId": "worker-a"},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["status"], "claimed")
        self.assertEqual(channel.json_body["claimedBy"], "worker-a")
        download_url = channel.json_body["downloadUrl"]

        channel = self.request("GET", download_url)
        self.assertEqual(channel.code, 200, channel.result["body"])
        self.assertEqual(channel.result["body"], b"KINE server payload")

        channel = self.request(
            "POST",
            f"/_kinemium/v1/admin/game-server-requests/{request_id}/complete",
            {
                "workerId": "worker-a",
                "address": "127.0.0.1",
                "port": 7777,
                "region": "eu",
            },
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["removed"])
        server_id = channel.json_body["server"]["serverId"]
        self.assertEqual(channel.json_body["server"]["status"], "starting")
        self.assertEqual(channel.json_body["server"]["port"], 7777)

        channel = self.request(
            "GET",
            f"/_kinemium/v1/me/games/{game_id}/server-requests/{request_id}",
        )
        self.assertEqual(channel.code, 404, channel.json_body)

        channel = self.request(
            "GET", f"/_kinemium/v1/me/games/{game_id}/servers"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["servers"]), 1)
        self.assertEqual(channel.json_body["servers"][0]["gameName"], "Game")

        other_game_id = self.create_game("Other game")
        token = self.create_token(game_id, ["account:read"])
        channel = self.request(
            "GET", "/_kinemium/v1/me/servers", access_token=token
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(
            [item["gameId"] for item in channel.json_body["servers"]], [game_id]
        )
        channel = self.request(
            "GET",
            f"/_kinemium/v1/me/servers?gameId={other_game_id}",
            access_token=token,
        )
        self.assertEqual(channel.code, 404, channel.json_body)

        channel = self.request(
            "PATCH",
            f"/_kinemium/v1/admin/game-servers/{server_id}",
            {"status": "online"},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["status"], "online")
        self.assertIsNotNone(channel.json_body["lastHeartbeatAt"])

        channel = self.request(
            "GET", "/_kinemium/v1/admin/game-servers?status=online"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["servers"]), 1)

        channel = self.request(
            "DELETE", f"/_kinemium/v1/admin/game-servers/{server_id}"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        channel = self.request("GET", "/_kinemium/v1/admin/game-servers")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["servers"], [])

        channel = self.request("GET", "/_kinemium/v1/admin/audit")
        self.assertEqual(channel.code, 200, channel.json_body)
        actions = {item["action"] for item in channel.json_body["audit"]}
        self.assertIn("game_server.request_claim", actions)
        self.assertIn("game_server.request_complete", actions)
        self.assertIn("game_server.update", actions)
        self.assertIn("game_server.delete", actions)

        body, content_type = multipart(
            {"metadata": json.dumps({"region": "us"})},
            "second.kine",
            b"second payload",
        )
        channel = self.request(
            "POST",
            f"/_kinemium/v1/me/games/{game_id}/server-requests",
            body,
            content_type=content_type,
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        cancel_id = channel.json_body["requestId"]
        channel = self.request(
            "DELETE",
            f"/_kinemium/v1/me/games/{game_id}/server-requests/{cancel_id}",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        channel = self.request(
            "GET", f"/_kinemium/v1/me/games/{game_id}/server-requests"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["requests"], [])

        body, content_type = multipart({}, "not-a-place.txt", b"nope")
        channel = self.request(
            "POST",
            f"/_kinemium/v1/me/games/{game_id}/server-requests",
            body,
            content_type=content_type,
        )
        self.assertEqual(channel.code, 400, channel.json_body)

    def test_studio_collaboration(self) -> None:
        game_id = self.create_game("Studio")
        channel = self.request(
            "POST",
            f"/_kinemium/v1/me/games/{game_id}/collab-sessions",
            {"name": "Build", "maxParticipants": 4},
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        session = channel.json_body
        session_id = session["sessionId"]
        join_code = session["joinCode"]
        self.assertEqual(session["role"], "host")
        self.assertEqual(session["status"], "active")
        self.assertEqual(session["participantCount"], 1)

        channel = self.request(
            "POST",
            f"/_kinemium/v1/me/games/{game_id}/collab-sessions",
            {"name": "Second"},
        )
        self.assertEqual(channel.code, 409, channel.json_body)

        other_game_id = self.create_game("Other Studio")
        token = self.create_token(
            game_id, ["account:read", "account:write"]
        )
        channel = self.request(
            "POST",
            f"/_kinemium/v1/me/games/{other_game_id}/collab-sessions",
            {},
            access_token=token,
        )
        self.assertEqual(channel.code, 404, channel.json_body)

        channel = self.request("GET", "/_kinemium/v1/capabilities", access_token=None)
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["features"]["studioCollaboration"])
        self.assertEqual(
            channel.json_body["features"]["collabSignalTransport"],
            "http-polling",
        )

        bob_id = self.get_success(self.module.api.register_user("bob"))
        carol_id = self.get_success(self.module.api.register_user("carol"))
        original_user = self.helper.auth_user_id
        try:
            self.helper.auth_user_id = bob_id
            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/games/{game_id}/collab-sessions",
                {"name": "Nope"},
            )
            self.assertEqual(channel.code, 404, channel.json_body)

            self.helper.auth_user_id = original_user
            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/invites",
                {"username": "bob"},
            )
            self.assertEqual(channel.code, 201, channel.json_body)
            invite = channel.json_body
            invite_id = invite["inviteId"]
            self.assertEqual(invite["inviteeUserId"], bob_id)

            self.helper.auth_user_id = bob_id
            channel = self.request("GET", "/_kinemium/v1/me/collab-invites")
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(len(channel.json_body["invites"]), 1)
            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-invites/{invite_id}/accept",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["session"]["role"], "guest")

            channel = self.request(
                "POST",
                f"/_kinemium/v1/collab/sessions/{session_id}/signals",
                {"kind": "offer", "payload": {"sdp": "v=0"}},
            )
            self.assertEqual(channel.code, 201, channel.json_body)
            offer_sequence = channel.json_body["sequence"]

            channel = self.request(
                "GET",
                f"/_kinemium/v1/collab/sessions/{session_id}/signals?after=0",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["signals"][0]["kind"], "offer")

            self.helper.auth_user_id = original_user
            channel = self.request(
                "GET",
                f"/_kinemium/v1/collab/sessions/{session_id}/signals?after=0",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(len(channel.json_body["signals"]), 1)
            self.assertEqual(
                channel.json_body["signals"][0]["fromUserId"], bob_id
            )

            channel = self.request(
                "POST",
                f"/_kinemium/v1/collab/sessions/{session_id}/signals",
                {
                    "kind": "answer",
                    "payload": {"sdp": "v=0"},
                    "toUserId": bob_id,
                },
            )
            self.assertEqual(channel.code, 201, channel.json_body)

            self.helper.auth_user_id = bob_id
            channel = self.request(
                "GET",
                f"/_kinemium/v1/collab/sessions/{session_id}/signals"
                f"?after={offer_sequence}",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(
                [item["kind"] for item in channel.json_body["signals"]],
                ["answer"],
            )

            channel = self.request(
                "DELETE", f"/_kinemium/v1/me/collab-sessions/{session_id}"
            )
            self.assertEqual(channel.code, 404, channel.json_body)

            self.helper.auth_user_id = carol_id
            channel = self.request(
                "POST", "/_kinemium/v1/collab/join", {"code": join_code}
            )
            self.assertEqual(channel.code, 201, channel.json_body)
            self.assertFalse(channel.json_body["joined"])
            request_id = channel.json_body["joinRequest"]["requestId"]

            channel = self.request(
                "GET", "/_kinemium/v1/me/collab-join-requests"
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(len(channel.json_body["joinRequests"]), 1)

            self.helper.auth_user_id = original_user
            channel = self.request(
                "GET",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/join-requests",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(len(channel.json_body["joinRequests"]), 1)
            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/join-requests/"
                f"{request_id}/approve",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["session"]["participantCount"], 3)

            self.helper.auth_user_id = carol_id
            channel = self.request(
                "GET", "/_kinemium/v1/me/collab-sessions?role=joined"
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(len(channel.json_body["sessions"]), 1)
            self.assertEqual(
                channel.json_body["sessions"][0]["sessionId"], session_id
            )

            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/invites",
                {"username": "bob"},
            )
            self.assertEqual(channel.code, 404, channel.json_body)

            self.helper.auth_user_id = original_user
            channel = self.request(
                "DELETE",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/participants/"
                f"{carol_id}",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["participantCount"], 2)

            self.helper.auth_user_id = carol_id
            channel = self.request(
                "GET",
                f"/_kinemium/v1/collab/sessions/{session_id}/signals",
            )
            self.assertEqual(channel.code, 403, channel.json_body)

            self.helper.auth_user_id = original_user
            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/join-code",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            rotated_code = channel.json_body["joinCode"]
            self.assertNotEqual(rotated_code, join_code)

            self.helper.auth_user_id = carol_id
            channel = self.request(
                "POST", "/_kinemium/v1/collab/join", {"code": join_code}
            )
            self.assertEqual(channel.code, 404, channel.json_body)
            channel = self.request(
                "POST", "/_kinemium/v1/collab/join", {"code": rotated_code}
            )
            self.assertEqual(channel.code, 201, channel.json_body)
            cancel_id = channel.json_body["joinRequest"]["requestId"]
            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-join-requests/{cancel_id}/cancel",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["status"], "cancelled")

            self.helper.auth_user_id = original_user
            channel = self.request(
                "DELETE", f"/_kinemium/v1/me/collab-sessions/{session_id}"
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["status"], "ended")
            channel = self.request(
                "DELETE", f"/_kinemium/v1/me/collab-sessions/{session_id}"
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            channel = self.request(
                "GET",
                "/_kinemium/v1/me/collab-sessions?status=ended",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(len(channel.json_body["sessions"]), 1)

            self.helper.auth_user_id = bob_id
            channel = self.request(
                "GET", f"/_kinemium/v1/collab/sessions/{session_id}"
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["status"], "ended")
            channel = self.request(
                "GET",
                f"/_kinemium/v1/collab/sessions/{session_id}/signals",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["signals"], [])
            self.assertEqual(channel.json_body["sessionStatus"], "ended")
        finally:
            self.helper.auth_user_id = original_user

    def test_studio_collaboration_invites_and_heartbeat(self) -> None:
        game_id = self.create_game("Studio Two")
        channel = self.request(
            "POST",
            f"/_kinemium/v1/me/games/{game_id}/collab-sessions",
            {"maxParticipants": 2},
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        session_id = channel.json_body["sessionId"]
        join_code = channel.json_body["joinCode"]

        bob_id = self.get_success(self.module.api.register_user("bob"))
        carol_id = self.get_success(self.module.api.register_user("carol"))
        original_user = self.helper.auth_user_id
        try:
            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/invites",
                {"userId": "@missing:test"},
            )
            self.assertEqual(channel.code, 404, channel.json_body)

            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/invites",
                {"userId": bob_id},
            )
            self.assertEqual(channel.code, 201, channel.json_body)
            first_invite_id = channel.json_body["inviteId"]
            channel = self.request(
                "GET",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/invites",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(len(channel.json_body["invites"]), 1)

            self.helper.auth_user_id = bob_id
            channel = self.request("GET", "/_kinemium/v1/me/notifications")
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertIn(
                "collab.invite",
                [item["type"] for item in channel.json_body["notifications"]],
            )
            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-invites/{first_invite_id}/decline",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["status"], "declined")

            self.helper.auth_user_id = original_user
            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/invites",
                {"username": "bob"},
            )
            self.assertEqual(channel.code, 201, channel.json_body)
            second_invite_id = channel.json_body["inviteId"]
            channel = self.request(
                "DELETE",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/invites/"
                f"{second_invite_id}",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["status"], "revoked")

            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/invites",
                {"username": "bob"},
            )
            self.assertEqual(channel.code, 201, channel.json_body)
            third_invite_id = channel.json_body["inviteId"]

            self.helper.auth_user_id = bob_id
            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-invites/{third_invite_id}/accept",
            )
            self.assertEqual(channel.code, 200, channel.json_body)

            channel = self.request(
                "POST",
                f"/_kinemium/v1/collab/sessions/{session_id}/heartbeat",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["role"], "guest")

            self.helper.auth_user_id = original_user
            channel = self.request(
                "POST",
                f"/_kinemium/v1/collab/sessions/{session_id}/heartbeat",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["role"], "host")
            channel = self.request(
                "GET",
                f"/_kinemium/v1/collab/sessions/{session_id}/participants",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(len(channel.json_body["participants"]), 2)

            self.helper.auth_user_id = carol_id
            channel = self.request(
                "GET",
                f"/_kinemium/v1/collab/sessions/{session_id}/participants",
            )
            self.assertEqual(channel.code, 404, channel.json_body)
            channel = self.request(
                "POST", "/_kinemium/v1/collab/join", {"code": join_code}
            )
            self.assertEqual(channel.code, 409, channel.json_body)

            self.helper.auth_user_id = bob_id
            channel = self.request(
                "POST",
                f"/_kinemium/v1/collab/sessions/{session_id}/leave",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["participantCount"], 1)
            channel = self.request(
                "POST",
                f"/_kinemium/v1/collab/sessions/{session_id}/heartbeat",
            )
            self.assertEqual(channel.code, 403, channel.json_body)

            self.helper.auth_user_id = original_user
            channel = self.request(
                "GET",
                f"/_kinemium/v1/collab/sessions/{session_id}/participants",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            bob_participant = next(
                item
                for item in channel.json_body["participants"]
                if item["userId"] == bob_id
            )
            self.assertEqual(bob_participant["status"], "left")

            channel = self.request(
                "DELETE",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/participants/"
                f"{bob_id}",
            )
            self.assertEqual(channel.code, 404, channel.json_body)

            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/join-code",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            fresh_code = channel.json_body["joinCode"]
            self.helper.auth_user_id = carol_id
            channel = self.request(
                "POST", "/_kinemium/v1/collab/join", {"code": fresh_code}
            )
            self.assertEqual(channel.code, 201, channel.json_body)
            request_id = channel.json_body["joinRequest"]["requestId"]

            self.helper.auth_user_id = original_user
            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/join-requests/"
                f"{request_id}/deny",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["status"], "denied")

            self.helper.auth_user_id = carol_id
            channel = self.request(
                "GET",
                "/_kinemium/v1/me/collab-join-requests?status=denied",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(len(channel.json_body["joinRequests"]), 1)
        finally:
            self.helper.auth_user_id = original_user

    def test_studio_collaboration_session_expiry(self) -> None:
        from unittest import mock

        game_id = self.create_game("Expiry")
        channel = self.request(
            "POST",
            f"/_kinemium/v1/me/games/{game_id}/collab-sessions",
            {},
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        session_id = channel.json_body["sessionId"]
        join_code = channel.json_body["joinCode"]
        expires_at = channel.json_body["expiresAt"]

        with mock.patch(
            "kinemium.collab.now_ms", return_value=expires_at + 1
        ):
            channel = self.request(
                "GET", f"/_kinemium/v1/collab/sessions/{session_id}"
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["status"], "expired")
            self.assertEqual(channel.json_body["endedBy"], "system")

            channel = self.request(
                "POST", "/_kinemium/v1/collab/join", {"code": join_code}
            )
            self.assertEqual(channel.code, 409, channel.json_body)

            channel = self.request(
                "GET",
                f"/_kinemium/v1/collab/sessions/{session_id}/signals",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["sessionStatus"], "expired")
            self.assertEqual(channel.json_body["signals"], [])

        channel = self.request(
            "GET", "/_kinemium/v1/me/collab-sessions?status=expired"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["sessions"]), 1)

    def test_studio_collaboration_signal_expiry(self) -> None:
        from unittest import mock

        game_id = self.create_game("Signal Expiry")
        channel = self.request(
            "POST",
            f"/_kinemium/v1/me/games/{game_id}/collab-sessions",
            {},
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        session_id = channel.json_body["sessionId"]

        bob_id = self.get_success(self.module.api.register_user("bob"))
        original_user = self.helper.auth_user_id
        try:
            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-sessions/{session_id}/invites",
                {"userId": bob_id},
            )
            self.assertEqual(channel.code, 201, channel.json_body)
            invite_id = channel.json_body["inviteId"]

            self.helper.auth_user_id = bob_id
            channel = self.request(
                "POST",
                f"/_kinemium/v1/me/collab-invites/{invite_id}/accept",
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            channel = self.request(
                "POST",
                f"/_kinemium/v1/collab/sessions/{session_id}/signals",
                {"kind": "offer", "payload": {"sdp": "v=0"}},
            )
            self.assertEqual(channel.code, 201, channel.json_body)
            signal_expires_at = channel.json_body["expiresAt"]

            with mock.patch(
                "kinemium.collab.now_ms", return_value=signal_expires_at + 1
            ):
                channel = self.request(
                    "GET",
                    f"/_kinemium/v1/collab/sessions/{session_id}/signals",
                )
                self.assertEqual(channel.code, 200, channel.json_body)
                self.assertEqual(channel.json_body["sessionStatus"], "active")
                self.assertEqual(channel.json_body["signals"], [])

                channel = self.request(
                    "POST",
                    f"/_kinemium/v1/collab/sessions/{session_id}/signals",
                    {"kind": "answer", "payload": {"sdp": "v=0"}},
                )
                self.assertEqual(channel.code, 201, channel.json_body)
                self.assertEqual(channel.json_body["sequence"], 2)

                channel = self.request(
                    "GET",
                    f"/_kinemium/v1/collab/sessions/{session_id}/signals",
                )
                self.assertEqual(channel.code, 200, channel.json_body)
                self.assertEqual(
                    [item["kind"] for item in channel.json_body["signals"]],
                    ["answer"],
                )
        finally:
            self.helper.auth_user_id = original_user

    def test_package_lifecycle_extensions(self) -> None:
        self.publish_package()
        channel = self.request("GET", "/_kinemium/v1/packages/alice/tool/versions")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["versions"]), 1)
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool/manifest?version=1.2.0"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["manifest"]["entrypoint"], "src/main.luau")
        channel = self.request(
            "PUT",
            "/_kinemium/v1/packages/alice/tool/readme",
            {"readme": "# Tool\n\nHello."},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["readme"], "# Tool\n\nHello.")
        channel = self.request(
            "PUT",
            "/_kinemium/v1/packages/alice/tool/versions/1.2.0/changelog",
            {"changelog": "Initial release."},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["changelog"], "Initial release.")
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages/alice/tool/screenshots?caption=Editor",
            PNG_1X1,
            content_type=b"image/png",
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        screenshot_id = channel.json_body["screenshot"]["screenshotId"]
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool/screenshots"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["screenshots"]), 1)
        channel = self.request(
            "DELETE",
            f"/_kinemium/v1/packages/alice/tool/screenshots/{screenshot_id}",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        channel = self.request(
            "POST", "/_kinemium/v1/marketplace/alice/tool/acquire"
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        channel = self.request("GET", "/_kinemium/v1/packages/alice/tool/stats")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["totals"]["installs"], 1)
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages/alice/tool/archive",
            {"reason": "Superseded"},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertIsNotNone(channel.json_body["package"]["archivedAt"])
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 404, channel.json_body)
        channel = self.request("GET", "/_kinemium/v1/me/packages?archived=true")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["packages"]), 1)
        channel = self.request(
            "POST", "/_kinemium/v1/packages/alice/tool/restore"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertIsNone(channel.json_body["package"]["archivedAt"])
        channel = self.request(
            "POST", "/_kinemium/v1/namespaces", {"namespace": "alicegames"}
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        self.assertEqual(channel.json_body["owner"], "@alice:test")
        archive = make_zip({"src/main.luau": b"return {}\n"})
        manifest = {
            "namespace": "alicegames",
            "name": "custom",
            "version": "1.0.0",
            "entrypoint": "src/main.luau",
        }
        body, content_type = multipart(
            {"manifest": json.dumps(manifest)}, "custom.zip", archive
        )
        channel = self.request(
            "POST", "/_kinemium/v1/packages", body, content_type=content_type
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        self.assertEqual(channel.json_body["package"]["namespace"], "alicegames")
        channel = self.request("GET", "/_kinemium/v1/namespaces/alicegames")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packageCount"], 1)

    def test_marketplace_moderation_and_updates(self) -> None:
        self.publish_package()
        channel = self.request(
            "POST", "/_kinemium/v1/marketplace/alice/tool/acquire"
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        channel = self.request(
            "POST",
            "/_kinemium/v1/marketplace/alice/tool/comments",
            {"body": "First"},
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        comment_id = channel.json_body["comment"]["commentId"]
        channel = self.request(
            "PATCH",
            f"/_kinemium/v1/marketplace/alice/tool/comments/{comment_id}",
            {"body": "Edited"},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["comment"]["body"], "Edited")
        channel = self.request(
            "POST",
            "/_kinemium/v1/marketplace/alice/tool/reviews",
            {"rating": 4, "body": "Good"},
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        review_id = channel.json_body["review"]["reviewId"]
        channel = self.request(
            "DELETE",
            f"/_kinemium/v1/marketplace/alice/tool/reviews/{review_id}",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace/alice/tool"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["rating"]["count"], 0)
        channel = self.request(
            "DELETE",
            f"/_kinemium/v1/marketplace/alice/tool/comments/{comment_id}",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace/alice/tool/comments"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["comments"], [])
        channel = self.request(
            "PATCH",
            "/_kinemium/v1/marketplace/alice/tool/acquire",
            {"version": "1.2.0"},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(
            channel.json_body["acquisition"]["pinnedVersion"], "1.2.0"
        )
        channel = self.request("GET", "/_kinemium/v1/marketplace/updates")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertFalse(channel.json_body["updates"][0]["updateAvailable"])
        channel = self.request(
            "DELETE", "/_kinemium/v1/marketplace/alice/tool/acquire"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        channel = self.request("GET", "/_kinemium/v1/marketplace/acquisitions")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["acquisitions"], [])

    def test_platform_reports_notifications_webhooks_and_idempotency(self) -> None:
        channel = self.request(
            "GET", "/_kinemium/v1/capabilities", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["features"]["idempotency"])
        channel = self.request("GET", "/_kinemium/v1/openapi.json", access_token=None)
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertIn("/packages/{namespace}/{name}", channel.json_body["paths"])
        channel = self.request(
            "POST",
            "/_kinemium/v1/me/webhooks",
            {"url": "https://example.com/kinemium", "events": ["package.reviewed"]},
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        webhook_id = channel.json_body["webhook"]["webhookId"]
        self.assertTrue(channel.json_body["webhook"]["secret"].startswith("whsec_"))
        channel = self.request(
            "PATCH",
            f"/_kinemium/v1/me/webhooks/{webhook_id}",
            {"active": False},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertFalse(channel.json_body["webhook"]["active"])
        channel = self.request("GET", "/_kinemium/v1/me/webhooks")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["webhooks"]), 1)
        channel = self.request(
            "DELETE", f"/_kinemium/v1/me/webhooks/{webhook_id}"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        bob_id = self.get_success(self.module.api.register_user("bob"))
        self.get_success(
            self.module.platform.notify(
                bob_id, "test.event", self.user_id, None, {"value": 1}
            )
        )
        original_user = self.helper.auth_user_id
        self.helper.auth_user_id = bob_id
        try:
            channel = self.request("GET", "/_kinemium/v1/me/notifications")
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(len(channel.json_body["notifications"]), 1)
            notification_id = channel.json_body["notifications"][0]["notificationId"]
            channel = self.request(
                "POST",
                "/_kinemium/v1/me/notifications/read",
                {"ids": [notification_id]},
            )
            self.assertEqual(channel.code, 200, channel.json_body)
            self.assertEqual(channel.json_body["updated"], 1)
            channel = self.request(
                "DELETE", f"/_kinemium/v1/me/notifications/{notification_id}"
            )
            self.assertEqual(channel.code, 200, channel.json_body)
        finally:
            self.helper.auth_user_id = original_user
        self.publish_package()
        channel = self.request("GET", "/_kinemium/v1/packages/alice/tool")
        self.assertEqual(channel.code, 200, channel.json_body)
        package_id = channel.json_body["packageId"]
        channel = self.request(
            "POST",
            "/_kinemium/v1/reports",
            {
                "targetType": "package",
                "targetId": package_id,
                "namespace": "alice",
                "name": "tool",
                "reason": "spam",
            },
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        report_id = channel.json_body["report"]["reportId"]
        self.set_admin(True)
        channel = self.request("GET", "/_kinemium/v1/admin/reports?status=open")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["reports"]), 1)
        channel = self.request(
            "POST",
            f"/_kinemium/v1/admin/reports/{report_id}/resolve",
            {"status": "resolved", "note": "Handled"},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["report"]["status"], "resolved")
        channel = self.request("GET", "/_kinemium/v1/admin/audit")
        self.assertEqual(channel.code, 200, channel.json_body)
        actions = {item["action"] for item in channel.json_body["audit"]}
        self.assertIn("report.resolve", actions)
        self.assertIn("package.review", actions)
        archive = make_zip({"src/main.luau": b"return {}\n"})
        manifest = {
            "name": "idempotent",
            "version": "1.0.0",
            "entrypoint": "src/main.luau",
        }
        body, content_type = multipart(
            {"manifest": json.dumps(manifest)}, "idempotent.zip", archive
        )
        headers = [(b"Idempotency-Key", b"publish-idempotent")]
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages",
            body,
            content_type=content_type,
            custom_headers=headers,
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        first = channel.json_body["package"]["packageId"]
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages",
            body,
            content_type=content_type,
            custom_headers=headers,
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        self.assertEqual(channel.json_body["package"]["packageId"], first)
        self.assertEqual(
            channel.headers.getRawHeaders(b"Idempotency-Replayed"), [b"true"]
        )

    def create_space(self) -> str:
        room_id, _ = self.get_success(
            self.module.api.create_room(
                self.user_id,
                {
                    "visibility": "private",
                    "creation_content": {"type": "m.space"},
                },
                ratelimit=False,
            )
        )
        return room_id

    def test_profile_customization_and_media(self) -> None:
        channel = self.request(
            "PATCH",
            "/_kinemium/v1/me",
            {
                "bannerUrl": "mxc://test/banner",
                "accentColor": "#5865F2",
                "statusText": "Building Kinemium",
                "statusEmoji": "gear",
                "pronouns": "they/them",
            },
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["bannerUrl"], "mxc://test/banner")
        self.assertEqual(channel.json_body["accentColor"], "#5865F2")
        self.assertEqual(channel.json_body["statusText"], "Building Kinemium")
        self.assertEqual(channel.json_body["statusEmoji"], "gear")
        self.assertEqual(channel.json_body["pronouns"], "they/them")

        channel = self.request("PATCH", "/_kinemium/v1/me", {"accentColor": "blue"})
        self.assertEqual(channel.code, 400, channel.json_body)

        channel = self.request(
            "POST",
            "/_kinemium/v1/me/banner",
            PNG_1X1,
            content_type=b"image/png",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["bannerUrl"].startswith("mxc://"))

        channel = self.request(
            "POST",
            "/_kinemium/v1/me/avatar",
            PNG_1X1,
            content_type=b"image/png",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["avatarUrl"].startswith("mxc://"))

        channel = self.request(
            "POST",
            "/_kinemium/v1/me/banner",
            b"not an image",
            content_type=b"text/plain",
        )
        self.assertEqual(channel.code, 400, channel.json_body)

        channel = self.request("DELETE", "/_kinemium/v1/me/banner")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertIsNone(channel.json_body["bannerUrl"])

    def test_space_package_namespace_and_official(self) -> None:
        room_id = self.create_space()
        channel = self.request(
            "POST",
            "/_kinemium/v1/namespaces",
            {"namespace": "kinemiumteam", "spaceId": room_id},
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        self.assertEqual(channel.json_body["spaceId"], room_id)

        channel = self.request(
            "POST",
            "/_kinemium/v1/namespaces",
            {"namespace": "anotherteam", "spaceId": room_id},
        )
        self.assertEqual(channel.code, 409, channel.json_body)

        normal_room, _ = self.get_success(
            self.module.api.create_room(
                self.user_id, {"visibility": "private"}, ratelimit=False
            )
        )
        channel = self.request(
            "POST",
            "/_kinemium/v1/namespaces",
            {"namespace": "notaspace", "spaceId": normal_room},
        )
        self.assertEqual(channel.code, 403, channel.json_body)

        archive = make_zip({"src/main.luau": b"return {}\n"})
        manifest = {
            "spaceId": room_id,
            "name": "space-tool",
            "version": "1.0.0",
            "entrypoint": "src/main.luau",
        }
        body, content_type = multipart(
            {"manifest": json.dumps(manifest)}, "space-tool.zip", archive
        )
        channel = self.request(
            "POST", "/_kinemium/v1/packages", body, content_type=content_type
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        package = channel.json_body["package"]
        self.assertEqual(package["namespace"], "kinemiumteam")
        self.assertEqual(package["spaceId"], room_id)
        self.assertEqual(package["creator"]["spaceId"], room_id)
        self.assertFalse(package["official"])

        self.set_admin(True)
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages/kinemiumteam/space-tool/review",
            {"decision": "approve"},
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        channel = self.request(
            "PATCH",
            "/_kinemium/v1/admin/packages/kinemiumteam/space-tool",
            {"official": True},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["official"])
        self.assertEqual(channel.json_body["officialBy"], self.user_id)
        self.assertIsNotNone(channel.json_body["officialAt"])

        channel = self.request(
            "GET", "/_kinemium/v1/marketplace/kinemiumteam/space-tool"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["official"])
        self.assertTrue(channel.json_body["canManage"])

        channel = self.request(
            "GET", "/_kinemium/v1/marketplace?official=true", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["packages"]), 1)
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace?official=false", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"], [])

        self.set_admin(False)
        channel = self.request(
            "PATCH",
            "/_kinemium/v1/packages/kinemiumteam/space-tool",
            {"official": True},
        )
        self.assertEqual(channel.code, 400, channel.json_body)

    def test_space_customization_and_emojis(self) -> None:
        room_id = self.create_space()
        channel = self.request("GET", "/_kinemium/v1/spaces")
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["spaces"][0]["roomId"], room_id)
        self.assertTrue(channel.json_body["spaces"][0]["canCustomize"])

        channel = self.request(
            "PATCH",
            f"/_kinemium/v1/spaces/{room_id}/profile",
            {
                "name": "Kinemium Developers",
                "topic": "Engine development",
                "description": "A space for builders",
                "bannerUrl": "mxc://test/space-banner",
                "accentColor": "#23A55A",
            },
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["name"], "Kinemium Developers")
        self.assertEqual(channel.json_body["topic"], "Engine development")
        self.assertEqual(channel.json_body["description"], "A space for builders")
        self.assertEqual(channel.json_body["bannerUrl"], "mxc://test/space-banner")
        self.assertEqual(channel.json_body["accentColor"], "#23A55A")

        channel = self.request(
            "POST",
            f"/_kinemium/v1/spaces/{room_id}/avatar",
            PNG_1X1,
            content_type=b"image/png",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["avatarUrl"].startswith("mxc://"))

        channel = self.request(
            "POST",
            f"/_kinemium/v1/spaces/{room_id}/banner",
            PNG_1X1,
            content_type=b"image/png",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["bannerUrl"].startswith("mxc://"))

        body, content_type = multipart({"shortcode": "party"}, "party.gif", GIF_1X1)
        channel = self.request(
            "POST",
            f"/_kinemium/v1/spaces/{room_id}/emojis",
            body,
            content_type=content_type,
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        self.assertEqual(channel.json_body["emoji"]["shortcode"], "party")
        self.assertTrue(channel.json_body["emoji"]["animated"])
        self.assertTrue(channel.json_body["emoji"]["url"].startswith("mxc://"))

        channel = self.request(
            "GET", f"/_kinemium/v1/spaces/{room_id}/emojis"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["emojis"]), 1)

        body, content_type = multipart({"shortcode": "party"}, "party.gif", GIF_1X1)
        channel = self.request(
            "POST",
            f"/_kinemium/v1/spaces/{room_id}/emojis",
            body,
            content_type=content_type,
        )
        self.assertEqual(channel.code, 409, channel.json_body)

        channel = self.request(
            "DELETE", f"/_kinemium/v1/spaces/{room_id}/emojis/party"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["emojis"], [])

        normal_room, _ = self.get_success(
            self.module.api.create_room(
                self.user_id, {"visibility": "private"}, ratelimit=False
            )
        )
        channel = self.request(
            "GET", f"/_kinemium/v1/spaces/{normal_room}/profile"
        )
        self.assertEqual(channel.code, 404, channel.json_body)

        game_id = self.create_game()
        token = self.create_token(game_id)
        channel = self.request("GET", "/_kinemium/v1/spaces", access_token=token)
        self.assertEqual(channel.code, 403, channel.json_body)

        bob_id = self.get_success(self.module.api.register_user("bob"))
        original_user = self.helper.auth_user_id
        self.helper.auth_user_id = bob_id
        try:
            channel = self.request(
                "GET", f"/_kinemium/v1/spaces/{room_id}/profile"
            )
        finally:
            self.helper.auth_user_id = original_user
        self.assertEqual(channel.code, 403, channel.json_body)
