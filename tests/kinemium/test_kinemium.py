from __future__ import annotations

import base64
import io
import json
import shutil
import tempfile
import zipfile
from typing import Any

from twisted.internet import defer
from twisted.web.resource import Resource

import kinemium.storage
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
        self.addCleanup(shutil.rmtree, storage_path, True)
        self.module = KinemiumModule(
            {"storage_path": storage_path}, hs.get_module_api()
        )
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
    ) -> Any:
        return self.make_request(
            method,
            path,
            content,
            access_token=access_token,
            shorthand=False,
            content_type=content_type,
        )

    def create_game(self) -> str:
        channel = self.request("POST", "/_kinemium/v1/me/games", {"name": "Game"})
        self.assertEqual(channel.code, 201, channel.json_body)
        return channel.json_body["gameId"]

    def create_token(self, game_id: str) -> str:
        channel = self.request(
            "POST",
            "/_kinemium/v1/me/tokens",
            {
                "name": "CI",
                "scopes": ["account:read", "datastores:read", "datastores:write"],
                "gameId": game_id,
            },
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        return channel.json_body["token"]

    def create_store(self, game_id: str) -> str:
        channel = self.request(
            "POST",
            "/_kinemium/v1/datastores",
            {"gameId": game_id, "name": "Scores"},
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        return channel.json_body["storeId"]

    def test_account_and_game_token(self) -> None:
        game_id = self.create_game()
        token = self.create_token(game_id)
        channel = self.request("GET", "/_kinemium/v1/me", access_token=token)
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["userId"], "@alice:test")
        channel = self.request("POST", "/_kinemium/v1/me/games", {"name": "Other"}, access_token=token)
        self.assertEqual(channel.code, 403, channel.json_body)

    def test_datastore_entries_and_batches(self) -> None:
        game_id = self.create_game()
        token = self.create_token(game_id)
        store_id = self.create_store(game_id)
        channel = self.request(
            "PUT",
            f"/_kinemium/v1/datastores/{store_id}/entries/player",
            {"value": {"score": 1}},
            access_token=token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["version"], 1)
        channel = self.request(
            "PUT",
            f"/_kinemium/v1/datastores/{store_id}/entries/player",
            {"value": {"score": 2}, "expectedVersion": 0},
            access_token=token,
        )
        self.assertEqual(channel.code, 409, channel.json_body)
        channel = self.request(
            "PATCH",
            f"/_kinemium/v1/datastores/{store_id}/entries/counter",
            {"operation": "increment", "amount": 3},
            access_token=token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["value"], 3)
        channel = self.request(
            "POST",
            f"/_kinemium/v1/datastores/{store_id}/entries/batch-write",
            {
                "entries": [
                    {"key": "a", "value": 1},
                    {"key": "b", "value": 2},
                ]
            },
            access_token=token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        channel = self.request(
            "POST",
            f"/_kinemium/v1/datastores/{store_id}/entries/batch-read",
            {"keys": ["b", "missing", "a"]},
            access_token=token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        entries = channel.json_body["entries"]
        self.assertEqual([entry["key"] for entry in entries], ["b", "missing", "a"])
        self.assertIsNone(entries[1]["entry"])
        channel = self.request(
            "PUT",
            f"/_kinemium/v1/datastores/{store_id}/ordered-entries/top",
            {"score": 10, "value": "alice"},
            access_token=token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        channel = self.request(
            "GET",
            f"/_kinemium/v1/datastores/{store_id}/ordered-entries",
            access_token=token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["entries"][0]["score"], 10)

    def test_packages_publish_resolve_and_download(self) -> None:
        archive = make_zip({"src/main.luau": b"return {}\n"})
        manifest = {
            "name": "tool",
            "version": "1.2.0",
            "description": "A test package",
            "entrypoint": "src/main.luau",
            "engineConstraint": "^1.0.0",
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
        channel = self.request("GET", "/_kinemium/v1/packages?q=tool", access_token=None)
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["packages"][0]["name"], "tool")
        channel = self.request(
            "GET", "/_kinemium/v1/packages/alice/tool", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["latestVersion"], "1.2.0")
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
        channel = self.request(
            "POST",
            "/_kinemium/v1/packages/alice/tool/versions/1.2.0/yank",
            {"yanked": True},
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["version"]["yanked"])

    def test_marketplace_acquisition_and_review(self) -> None:
        channel = self.request(
            "POST",
            "/_kinemium/v1/marketplace/items",
            {
                "name": "Starter Model",
                "description": "A model",
                "itemType": "model",
                "tags": ["starter", "model"],
            },
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        item_id = channel.json_body["itemId"]
        body, content_type = multipart(
            {"version": "1.0.0", "changelog": "Initial"},
            "model.bin",
            b"model-data",
        )
        channel = self.request(
            "POST",
            f"/_kinemium/v1/marketplace/items/{item_id}/versions",
            body,
            content_type=content_type,
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        channel = self.request(
            "POST", f"/_kinemium/v1/marketplace/items/{item_id}/publish"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        channel = self.request(
            "GET", "/_kinemium/v1/marketplace?q=starter", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["items"][0]["itemId"], item_id)
        channel = self.request(
            "POST", f"/_kinemium/v1/marketplace/items/{item_id}/acquire"
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        channel = self.request(
            "POST",
            f"/_kinemium/v1/marketplace/items/{item_id}/reviews",
            {"rating": 5, "body": "Great"},
        )
        self.assertEqual(channel.code, 201, channel.json_body)
        channel = self.request(
            "GET", f"/_kinemium/v1/marketplace/items/{item_id}", access_token=None
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["rating"]["count"], 1)
        self.assertEqual(channel.json_body["acquisitionCount"], 1)
        channel = self.request(
            "GET",
            "/_kinemium/v1/marketplace/acquisitions",
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["acquisitions"][0]["itemId"], item_id)

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
