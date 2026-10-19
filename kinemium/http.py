from __future__ import annotations

import base64
import json
import logging
import urllib.parse
from typing import Any

from twisted.python import failure

from synapse.api.errors import SynapseError
from synapse.http.server import JsonResource, respond_with_json, return_json_error
from synapse.http.site import SynapseRequest

from .config import KinemiumConfig
from .errors import (
    INTERNAL_ERROR,
    invalid_request,
    not_found,
    payload_too_large,
)

logger = logging.getLogger(__name__)


def read_body(request: SynapseRequest, max_bytes: int) -> bytes:
    header = request.getHeader(b"Content-Length")
    if header is not None:
        try:
            length = int(header)
        except ValueError:
            raise invalid_request("Invalid Content-Length header.") from None
        if length < 0 or length > max_bytes:
            raise payload_too_large("Request body exceeds the configured limit.")
    content = getattr(request, "content", None)
    if content is None:
        raise invalid_request("Request body is unavailable.")
    try:
        content.seek(0)
    except (AttributeError, OSError):
        pass
    data = content.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise payload_too_large("Request body exceeds the configured limit.")
    return data


def _content_type(request: SynapseRequest) -> str:
    raw = request.getHeader(b"Content-Type") or b""
    text = raw.decode("latin-1") if isinstance(raw, bytes) else raw
    return text.split(";", 1)[0].strip().lower()


def parse_json_body(
    request: SynapseRequest,
    max_bytes: int,
    allow_empty: bool = False,
) -> dict[str, Any]:
    content_type = _content_type(request)
    if content_type and content_type not in ("application/json", "text/json"):
        raise invalid_request("Content-Type must be application/json.")
    data = read_body(request, max_bytes)
    if not data:
        if allow_empty:
            return {}
        raise invalid_request("A JSON request body is required.")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise invalid_request("Request body is not valid JSON.") from None
    if not isinstance(value, dict):
        raise invalid_request("Request body must be a JSON object.")
    return value


def parse_multipart(
    request: SynapseRequest,
    max_bytes: int,
) -> tuple[dict[str, str], bytes | None, str | None]:
    raw_content_type = request.getHeader(b"Content-Type") or b""
    content_type = (
        raw_content_type.decode("latin-1")
        if isinstance(raw_content_type, bytes)
        else raw_content_type
    )
    boundary = None
    for part in content_type.split(";")[1:]:
        key_text, _, value_text = part.strip().partition("=")
        if key_text.lower() == "boundary":
            boundary = value_text.strip().strip('"')
            break
    if not boundary:
        raise invalid_request("Multipart requests require a boundary.")
    try:
        delimiter = b"--" + boundary.encode("latin-1")
    except UnicodeEncodeError:
        raise invalid_request("Invalid multipart boundary.") from None
    raw = read_body(request, max_bytes)
    fields: dict[str, str] = {}
    file_data: bytes | None = None
    filename: str | None = None
    segments = raw.split(delimiter)
    if len(segments) < 2:
        raise invalid_request("Malformed multipart request.")
    for segment in segments[1:]:
        if segment.startswith(b"--"):
            break
        if segment.startswith(b"\r\n"):
            segment = segment[2:]
        if segment.endswith(b"\r\n"):
            segment = segment[:-2]
        header_blob, separator, body = segment.partition(b"\r\n\r\n")
        if not separator:
            continue
        headers: dict[str, str] = {}
        for line in header_blob.split(b"\r\n"):
            header_name, sep, header_value = line.partition(b":")
            if not sep:
                continue
            headers[
                header_name.strip().lower().decode("latin-1")
            ] = header_value.strip().decode("latin-1")
        disposition = headers.get("content-disposition", "")
        parameters: dict[str, str] = {}
        for piece in disposition.split(";")[1:]:
            key_text, _, value_text = piece.strip().partition("=")
            parameters[key_text.lower()] = value_text.strip().strip('"')
        field_name = parameters.get("name")
        if not field_name:
            continue
        part_filename = parameters.get("filename")
        if part_filename is not None:
            if file_data is not None:
                raise invalid_request("Only one uploaded file is supported.")
            file_data = body
            filename = part_filename
        else:
            try:
                fields[field_name] = body.decode("utf-8")
            except UnicodeDecodeError:
                raise invalid_request("Multipart fields must be UTF-8.") from None
            if len(fields) > 20:
                raise invalid_request("Too many multipart fields.")
    return fields, file_data, filename


def query_value(
    request: SynapseRequest,
    name: str,
    default: str | None = None,
    max_length: int = 1024,
) -> str | None:
    raw = request.args.get(name.encode("utf-8")) if request.args else None
    values: list[bytes] = list(raw) if raw else []
    if not values:
        return default
    if len(values) > 1:
        raise invalid_request(f"Query parameter {name} must appear once.")
    try:
        value = values[0].decode("utf-8")
    except UnicodeDecodeError:
        raise invalid_request(f"Query parameter {name} must be UTF-8.") from None
    if len(value) > max_length:
        raise invalid_request(f"Query parameter {name} is too long.")
    return value


def query_int(
    request: SynapseRequest,
    name: str,
    minimum: int,
    maximum: int,
    default: int | None = None,
) -> int | None:
    value = query_value(request, name, None)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise invalid_request(f"Query parameter {name} must be an integer.") from None
    if parsed < minimum or parsed > maximum:
        raise invalid_request(
            f"Query parameter {name} must be between {minimum} and {maximum}."
        )
    return parsed


def query_bool(
    request: SynapseRequest,
    name: str,
    default: bool | None = None,
) -> bool | None:
    value = query_value(request, name, None)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise invalid_request(f"Query parameter {name} must be a boolean.")


def _content_disposition(filename: str) -> str:
    fallback = "".join(
        character if 32 <= ord(character) < 127 and character not in '"\\' else "_"
        for character in filename
    )
    quoted = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quoted}"


def send_file(
    request: SynapseRequest,
    data: bytes,
    filename: str,
    content_type: str,
    digest_hex: str,
    extra_headers: dict[str, str] | None = None,
) -> None:
    request.setResponseCode(200)
    request.setHeader(b"Content-Type", content_type.encode("latin-1"))
    request.setHeader(
        b"Content-Disposition", _content_disposition(filename).encode("utf-8")
    )
    request.setHeader(b"Content-Length", str(len(data)).encode("ascii"))
    digest_bytes = bytes.fromhex(digest_hex)
    request.setHeader(
        b"Digest",
        b"sha-256=" + base64.b64encode(digest_bytes),
    )
    request.setHeader(b"ETag", f'"sha256:{digest_hex}"'.encode("ascii"))
    request.setHeader(b"Cache-Control", b"private, max-age=60")
    if extra_headers:
        for name, value in extra_headers.items():
            request.setHeader(name.encode("latin-1"), value.encode("latin-1"))
    request.write(data)
    request.finish()


def content_type_for_filename(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith(".zip"):
        return "application/zip"
    if lowered.endswith((".tar.gz", ".tgz")):
        return "application/gzip"
    if lowered.endswith(".tar"):
        return "application/x-tar"
    if lowered.endswith((".luau", ".lua")):
        return "text/plain; charset=utf-8"
    if lowered.endswith(".json"):
        return "application/json"
    return "application/octet-stream"


class KinemiumResource(JsonResource):
    def __init__(self, hs: Any, config: KinemiumConfig) -> None:
        super().__init__(hs)
        self.config = config

    def _send_error_response(
        self, f: failure.Failure, request: SynapseRequest
    ) -> None:
        if f.check(SynapseError):
            error: SynapseError = f.value
            if error.code == 404:
                error = not_found()
            elif error.code == 405:
                error = invalid_request("Method not allowed for this endpoint.")
            elif error.code >= 500 and error.errcode != INTERNAL_ERROR:
                logger.error(
                    "Kinemium request failed",
                    exc_info=(f.type, f.value, f.getTracebackObject()),
                )
                return_json_error(
                    failure.Failure(
                        SynapseError(
                            500,
                            "Internal server error.",
                            INTERNAL_ERROR,
                        )
                    ),
                    request,
                    self.hs.config,
                )
                return
            return_json_error(failure.Failure(error), request, self.hs.config)
            return
        logger.error(
            "Kinemium request failed",
            exc_info=(f.type, f.value, f.getTracebackObject()),
        )
        respond_with_json(
            request,
            500,
            {"errcode": INTERNAL_ERROR, "error": "Internal server error."},
            send_cors=self.config.enable_cors,
        )

    def register_options(self) -> None:
        import re

        self.register_paths(
            "OPTIONS",
            [re.compile(r"^/_kinemium/v1(?:/.*)?$")],
            self._handle_options,
            "KinemiumOptionsServlet",
        )

    def _handle_options(self, request: SynapseRequest) -> None:
        request.setResponseCode(204)
        request.setHeader(b"Content-Length", b"0")
        if self.config.enable_cors:
            request.setHeader(b"Access-Control-Allow-Origin", b"*")
            request.setHeader(
                b"Access-Control-Allow-Methods",
                b"GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS",
            )
            request.setHeader(
                b"Access-Control-Allow-Headers",
                b"Authorization, Content-Type, If-Match",
            )
            request.setHeader(
                b"Access-Control-Expose-Headers",
                b"ETag, Digest, Retry-After, X-Kinemium-Yanked",
            )
            request.setHeader(b"Access-Control-Max-Age", b"86400")
        request.finish()
