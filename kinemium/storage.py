from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import stat
import tarfile
import unicodedata
import uuid
import zipfile

from twisted.internet.threads import deferToThread

from .errors import internal_error, invalid_request, not_found, payload_too_large
from .util import now_ms

logger = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_UNSAFE_FILENAME_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_filename(filename: str | None, fallback: str = "download.bin") -> str:
    if not filename:
        return fallback
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = _UNSAFE_FILENAME_RE.sub("", name)
    if not name or name in (".", ".."):
        return fallback
    if len(name) > 255:
        stem, ext = os.path.splitext(name)
        name = stem[: 255 - len(ext)] + ext
    return name


def digest_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_archive_member(name: str) -> bool:
    if not name or len(name) > 1024 or "\x00" in name:
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return False
    if re.match(r"^[A-Za-z]:", normalized):
        return False
    if normalized.endswith("/"):
        normalized = normalized[:-1]
    parts = normalized.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _member_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _validate_zip(data: bytes, max_entries: int, max_uncompressed: int) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise invalid_request("The uploaded file is not a valid ZIP archive.") from None
    total = 0
    names: set[str] = set()
    infos = archive.infolist()
    if len(infos) > max_entries:
        raise invalid_request("Archive contains too many entries.")
    for info in infos:
        normalized = info.filename.replace("\\", "/")
        if not _safe_archive_member(normalized):
            raise invalid_request("Archive contains an unsafe path.")
        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if normalized.endswith("/") or file_type == stat.S_IFDIR:
            if info.file_size:
                raise invalid_request("Archive contains an invalid directory entry.")
            continue
        if info.flag_bits & 0x1:
            raise invalid_request("Encrypted archives are not supported.")
        if file_type not in (0, stat.S_IFREG):
            if file_type == stat.S_IFLNK:
                raise invalid_request("Archive contains a symbolic link.")
            raise invalid_request("Archive contains a special file.")
        key = _member_key(normalized)
        if key in names:
            raise invalid_request("Archive contains duplicate entries.")
        names.add(key)
        total += info.file_size
        if total > max_uncompressed:
            raise invalid_request("Archive expands beyond the configured limit.")


def _validate_tar(data: bytes, max_entries: int, max_uncompressed: int) -> None:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError:
        raise invalid_request("The uploaded file is not a valid tar archive.") from None
    total = 0
    names: set[str] = set()
    count = 0
    with archive:
        for member in archive:
            count += 1
            if count > max_entries:
                raise invalid_request("Archive contains too many entries.")
            normalized = member.name.replace("\\", "/")
            if not _safe_archive_member(normalized):
                raise invalid_request("Archive contains an unsafe path.")
            if member.issym() or member.islnk():
                raise invalid_request("Archive contains a link.")
            if member.isdir():
                continue
            if not member.isreg():
                raise invalid_request("Archive contains a special file.")
            key = _member_key(normalized)
            if key in names:
                raise invalid_request("Archive contains duplicate entries.")
            names.add(key)
            total += member.size
            if total > max_uncompressed:
                raise invalid_request("Archive expands beyond the configured limit.")


def validate_archive(
    data: bytes,
    filename: str | None,
    max_entries: int,
    max_uncompressed: int,
) -> str | None:
    name = (filename or "").lower()
    is_zip = data.startswith(b"PK\x03\x04") or name.endswith(".zip")
    is_tar = data.startswith(b"\x1f\x8b") or name.endswith(
        (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")
    )
    if is_zip:
        _validate_zip(data, max_entries, max_uncompressed)
        return "zip"
    if is_tar:
        _validate_tar(data, max_entries, max_uncompressed)
        return "tar"
    return None


class LocalStorage:
    def __init__(self, root: str, max_file_bytes: int) -> None:
        self.root = os.path.realpath(root)
        self.max_file_bytes = max_file_bytes

    def _path_for_key(self, key: str) -> str:
        if not _KEY_RE.fullmatch(key) or ".." in key.split("/"):
            raise ValueError("invalid storage key")
        path = os.path.realpath(os.path.join(self.root, key))
        if os.path.commonpath([self.root, path]) != self.root:
            raise ValueError("invalid storage key")
        return path

    def _write_sync(self, key: str, data: bytes) -> None:
        path = self._path_for_key(key)
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o750, exist_ok=True)
        temporary = os.path.join(directory, f".{uuid.uuid4().hex}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _read_sync(self, key: str) -> bytes:
        path = self._path_for_key(key)
        try:
            with open(path, "rb") as handle:
                data = handle.read(self.max_file_bytes + 1)
        except FileNotFoundError:
            raise not_found("The requested file was not found.") from None
        if len(data) > self.max_file_bytes:
            raise payload_too_large("Stored file exceeds the configured limit.")
        return data

    def _read_verified_sync(self, key: str, expected_digest: str) -> bytes:
        data = self._read_sync(key)
        if digest_hex(data) != expected_digest:
            logger.error("Integrity check failed for stored file %s", key)
            raise internal_error()
        return data

    def _delete_sync(self, key: str) -> None:
        path = self._path_for_key(key)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    async def write(self, key: str, data: bytes) -> tuple[str, int]:
        if len(data) > self.max_file_bytes:
            raise payload_too_large("The uploaded file exceeds the configured limit.")
        digest = await deferToThread(digest_hex, data)
        await deferToThread(self._write_sync, key, data)
        return digest, len(data)

    async def read(self, key: str) -> bytes:
        return await deferToThread(self._read_sync, key)

    async def read_verified(self, key: str, expected_digest: str) -> bytes:
        return await deferToThread(
            self._read_verified_sync, key, expected_digest
        )

    async def delete(self, key: str) -> None:
        await deferToThread(self._delete_sync, key)

    def new_key(self, prefix: str, identifier: str) -> str:
        return f"{prefix}/{identifier}/{now_ms()}_{uuid.uuid4().hex}"
