from __future__ import annotations

import io
from dataclasses import dataclass

from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from synapse.types import UserID

from .errors import invalid_request, payload_too_large
from .http import read_body


@dataclass(frozen=True)
class ImageData:
    data: bytes
    content_type: str
    animated: bool

    @property
    def extension(self) -> str:
        return {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/gif": "gif",
            "image/webp": "webp",
        }[self.content_type]


def detect_image(data: bytes) -> ImageData | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ImageData(data, "image/png", False)
    if data.startswith(b"\xff\xd8\xff"):
        return ImageData(data, "image/jpeg", False)
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ImageData(data, "image/gif", True)
    if (
        len(data) >= 12
        and data[:4] == b"RIFF"
        and data[8:12] == b"WEBP"
    ):
        return ImageData(data, "image/webp", False)
    return None


def read_image(request: SynapseRequest, max_bytes: int) -> ImageData:
    data = read_body(request, max_bytes)
    if not data:
        raise invalid_request("An image file is required.")
    image = detect_image(data)
    if image is None:
        raise invalid_request("Only PNG, JPEG, GIF, and WebP images are supported.")
    if len(data) > max_bytes:
        raise payload_too_large("Image exceeds the configured limit.")
    return image


async def store_image(
    api: ModuleApi,
    user_id: str,
    image: ImageData,
    name: str,
) -> str:
    media_repository = api._hs.get_media_repository()
    uri = await media_repository.create_or_update_content(
        media_type=image.content_type,
        upload_name=f"{name}.{image.extension}",
        content=io.BytesIO(image.data),
        content_length=len(image.data),
        auth_user=UserID.from_string(user_id),
    )
    return str(uri)
