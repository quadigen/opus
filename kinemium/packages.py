from __future__ import annotations

import json
import re
import uuid
from collections import deque
from typing import Any

from synapse.http.site import SynapseRequest

from .auth import (
    PACKAGES_READ,
    PACKAGES_WRITE,
)
from .base import Service
from .errors import conflict, forbidden, invalid_request, not_found, payload_too_large
from .http import (
    content_type_for_filename,
    parse_json_body,
    parse_multipart,
    query_bool,
    query_value,
    send_file,
)
from .semver import (
    Constraint,
    ConstraintError,
    SemVer,
    SemVerError,
    parse_constraint,
    parse_semver,
)
from .storage import sanitize_filename, validate_archive
from .util import (
    decode_cursor,
    encode_cursor,
    json_loads,
    now_ms,
    validate_json_value,
)
from .validation import (
    check_str,
    require_list,
    validate_relative_path,
    validate_slug,
    validate_website,
)

PACKAGE_COLUMNS = (
    "package_id, namespace, name, owner_user_id, description, license,"
    " repository, engine_constraint, deprecated, deprecated_at,"
    " created_at, updated_at"
)
VERSION_COLUMNS = (
    "version_id, version, manifest, entrypoint, engine_constraint, digest,"
    " storage_key, size_bytes, yanked, published_at, package_id"
)
MAX_PACKAGE_VERSIONS_RETURNED = 100
MAX_MANIFEST_BYTES = 64 * 1024


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _namespace_for_user(user_id: str) -> str:
    localpart = user_id.split(":", 1)[0]
    if localpart.startswith("@"):
        localpart = localpart[1:]
    namespace = localpart.lower()
    try:
        return validate_slug(namespace, "namespace")
    except Exception:
        raise invalid_request(
            "Your user ID cannot be used as a package namespace."
        ) from None


def _package_row(txn: Any, namespace: str, name: str) -> tuple | None:
    txn.execute(
        f"SELECT {PACKAGE_COLUMNS} FROM kinemium_packages"
        " WHERE namespace = %s AND name = %s",
        (namespace, name),
    )
    return txn.fetchone()


def _lock_package(txn: Any, namespace: str, name: str, for_update: str) -> tuple | None:
    txn.execute(
        f"SELECT {PACKAGE_COLUMNS} FROM kinemium_packages"
        " WHERE namespace = %s AND name = %s" + for_update,
        (namespace, name),
    )
    return txn.fetchone()


def _version_row(txn: Any, package_id: str, version: str) -> tuple | None:
    txn.execute(
        f"SELECT {VERSION_COLUMNS} FROM kinemium_package_versions"
        " WHERE package_id = %s AND version = %s",
        (package_id, version),
    )
    return txn.fetchone()


def _version_rows(txn: Any, package_id: str) -> list[tuple]:
    txn.execute(
        f"SELECT {VERSION_COLUMNS} FROM kinemium_package_versions"
        " WHERE package_id = %s ORDER BY published_at DESC, version DESC"
        " LIMIT %s",
        (package_id, MAX_PACKAGE_VERSIONS_RETURNED),
    )
    return txn.fetchall()


def _all_yanked_versions(txn: Any, package_id: str) -> list[tuple]:
    txn.execute(
        "SELECT version FROM kinemium_package_versions"
        " WHERE package_id = %s AND yanked = FALSE",
        (package_id,),
    )
    return txn.fetchall()


def _dependencies(txn: Any, package_id: str) -> list[tuple]:
    txn.execute(
        "SELECT version, dependency_namespace, dependency_name, constraint_text"
        " FROM kinemium_package_dependencies WHERE package_id = %s",
        (package_id,),
    )
    return txn.fetchall()


def _version_dependencies(txn: Any, package_id: str, version: str) -> list[tuple]:
    txn.execute(
        "SELECT dependency_namespace, dependency_name, constraint_text"
        " FROM kinemium_package_dependencies"
        " WHERE package_id = %s AND version = %s"
        " ORDER BY dependency_namespace, dependency_name",
        (package_id, version),
    )
    return txn.fetchall()


def _search_packages(
    txn: Any,
    query: str | None,
    namespace: str | None,
    sort: str,
    include_yanked: bool,
    limit: int,
    cursor: dict[str, Any] | None,
) -> tuple[list[tuple], bool]:
    yank_filter = "" if include_yanked else " AND v.yanked = FALSE"
    latest = (
        "(SELECT v.version FROM kinemium_package_versions v"
        " WHERE v.package_id = p.package_id"
        + yank_filter
        + " ORDER BY v.published_at DESC, v.version DESC LIMIT 1)"
    )
    inner = (
        "SELECT p.package_id, p.namespace, p.name, p.owner_user_id,"
        " p.description, p.license, p.repository, p.engine_constraint,"
        " p.deprecated, p.deprecated_at, p.created_at, p.updated_at,"
        f" {latest} AS latest_version"
        " FROM kinemium_packages p WHERE 1=1"
    )
    args: list[Any] = []
    if query is not None:
        pattern = "%" + _escape_like(query.lower()) + "%"
        inner += (
            " AND (LOWER(p.namespace) LIKE %s ESCAPE '\\'"
            " OR LOWER(p.name) LIKE %s ESCAPE '\\'"
            " OR LOWER(p.description) LIKE %s ESCAPE '\\')"
        )
        args.extend([pattern, pattern, pattern])
    if namespace is not None:
        inner += " AND p.namespace = %s"
        args.append(namespace)
    sql = f"SELECT * FROM ({inner}) AS pkg"
    outer_args: list[Any] = []
    if not include_yanked:
        sql += " WHERE latest_version IS NOT NULL"
    if cursor is not None:
        if sort == "name":
            sql += " AND (namespace > %s OR (namespace = %s AND name > %s))"
            outer_args.extend(
                [cursor.get("namespace"), cursor.get("namespace"), cursor.get("name")]
            )
        else:
            sql += " AND (updated_at < %s OR (updated_at = %s AND package_id < %s))"
            outer_args.extend(
                [cursor.get("updatedAt"), cursor.get("updatedAt"), cursor.get("packageId")]
            )
    if sort == "name":
        sql += " ORDER BY namespace ASC, name ASC, package_id ASC LIMIT %s"
    else:
        sql += " ORDER BY updated_at DESC, package_id DESC LIMIT %s"
    txn.execute(sql, tuple(args + outer_args + [limit + 1]))
    rows = txn.fetchall()
    return rows[:limit], len(rows) > limit


def _create_package(
    txn: Any,
    owner: str,
    namespace: str,
    name: str,
    description: str,
    license_name: str | None,
    repository: str | None,
    engine_constraint: str | None,
    now: int,
    for_update: str,
) -> tuple:
    txn.execute(
        "INSERT INTO kinemium_profiles"
        " (user_id, bio, website, public_profile, public_marketplace,"
        " created_at, updated_at) VALUES (%s, '', NULL, TRUE, TRUE, %s, %s)"
        " ON CONFLICT (user_id) DO NOTHING",
        (owner, now, now),
    )
    txn.execute(
        "SELECT owner_user_id FROM kinemium_package_namespaces"
        " WHERE namespace = %s" + for_update,
        (namespace,),
    )
    namespace_row = txn.fetchone()
    if namespace_row is None:
        txn.execute(
            "INSERT INTO kinemium_package_namespaces"
            " (namespace, owner_user_id, created_at) VALUES (%s, %s, %s)",
            (namespace, owner, now),
        )
    elif namespace_row[0] != owner:
        raise forbidden("That package namespace belongs to another account.")
    existing = _lock_package(txn, namespace, name, for_update)
    if existing is None:
        package_id = "pkg_" + uuid.uuid4().hex
        txn.execute(
            f"INSERT INTO kinemium_packages ({PACKAGE_COLUMNS})"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                package_id,
                namespace,
                name,
                owner,
                description,
                license_name,
                repository,
                engine_constraint,
                False,
                None,
                now,
                now,
            ),
        )
        created = _package_row(txn, namespace, name)
        if created is None:
            raise not_found("Package not found.")
        return created
    if existing[3] != owner:
        raise forbidden("You do not own that package.")
    txn.execute(
        "UPDATE kinemium_packages SET description = %s, license = %s,"
        " repository = %s, engine_constraint = %s, updated_at = %s"
        " WHERE package_id = %s",
        (
            description if description else existing[4],
            license_name if license_name is not None else existing[5],
            repository if repository is not None else existing[6],
            engine_constraint if engine_constraint is not None else existing[7],
            now,
            existing[0],
        ),
    )
    updated = _package_row(txn, namespace, name)
    if updated is None:
        raise not_found("Package not found.")
    return updated


def _publish_version(
    txn: Any,
    package: tuple,
    version: str,
    manifest_json: str,
    entrypoint: str | None,
    engine_constraint: str | None,
    digest: str,
    storage_key: str,
    size: int,
    dependencies: list[tuple[str, str, str]],
    maximum: int,
    now: int,
    for_update: str,
) -> tuple:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    if locked[3] != package[3]:
        raise forbidden("You do not own that package.")
    txn.execute(
        "SELECT COUNT(*) FROM kinemium_package_versions WHERE package_id = %s",
        (package[0],),
    )
    count = txn.fetchone()
    if count is not None and int(count[0]) >= maximum:
        raise conflict("Package version limit reached.", {"reason": "quota_exceeded"})
    if _version_row(txn, package[0], version) is not None:
        raise conflict("That package version already exists.")
    version_id = "pkgv_" + uuid.uuid4().hex
    txn.execute(
        f"INSERT INTO kinemium_package_versions ({VERSION_COLUMNS})"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            version_id,
            version,
            manifest_json,
            entrypoint,
            engine_constraint,
            digest,
            storage_key,
            size,
            False,
            now,
            package[0],
        ),
    )
    for dependency_namespace, dependency_name, constraint_text in dependencies:
        txn.execute(
            "INSERT INTO kinemium_package_dependencies"
            " (package_id, version, dependency_namespace, dependency_name,"
            " constraint_text) VALUES (%s, %s, %s, %s, %s)",
            (
                package[0],
                version,
                dependency_namespace,
                dependency_name,
                constraint_text,
            ),
        )
    txn.execute(
        "UPDATE kinemium_packages SET updated_at = %s WHERE package_id = %s",
        (now, package[0]),
    )
    row = _version_row(txn, package[0], version)
    if row is None:
        raise not_found("Package version not found.")
    return row


def _set_yanked(
    txn: Any,
    package: tuple,
    version: str,
    yanked: bool,
    for_update: str,
) -> tuple:
    locked = _lock_package(txn, package[1], package[2], for_update)
    if locked is None:
        raise not_found("Package not found.")
    if locked[3] != package[3]:
        raise forbidden("You do not own that package.")
    row = _version_row(txn, package[0], version)
    if row is None:
        raise not_found("Package version not found.")
    txn.execute(
        "UPDATE kinemium_package_versions SET yanked = %s"
        " WHERE package_id = %s AND version = %s",
        (yanked, package[0], version),
    )
    updated = _version_row(txn, package[0], version)
    if updated is None:
        raise not_found("Package version not found.")
    return updated


def _update_package_metadata(
    txn: Any,
    package: tuple,
    description: str | None,
    license_name: str | None,
    repository: str | None,
    deprecated: bool | None,
    now: int,
) -> tuple:
    txn.execute(
        "UPDATE kinemium_packages SET description = %s, license = %s,"
        " repository = %s, deprecated = %s, deprecated_at = %s, updated_at = %s"
        " WHERE package_id = %s",
        (
            description if description is not None else package[4],
            license_name if license_name is not None else package[5],
            repository if repository is not None else package[6],
            deprecated if deprecated is not None else package[8],
            now if deprecated else (None if deprecated is False else package[9]),
            now,
            package[0],
        ),
    )
    updated = _package_row(txn, package[1], package[2])
    if updated is None:
        raise not_found("Package not found.")
    return updated


def _available_versions(
    txn: Any, namespace: str, name: str
) -> tuple[str | None, list[tuple[str, SemVer]]]:
    txn.execute(
        "SELECT p.package_id, v.version FROM kinemium_packages p"
        " JOIN kinemium_package_versions v ON v.package_id = p.package_id"
        " WHERE p.namespace = %s AND p.name = %s AND v.yanked = FALSE"
        " ORDER BY v.published_at DESC, v.version DESC",
        (namespace, name),
    )
    rows = txn.fetchall()
    if not rows:
        return None, []
    package_id = rows[0][0]
    versions: list[tuple[str, SemVer]] = []
    for _package_id, raw_version in rows:
        try:
            versions.append((raw_version, parse_semver(raw_version)))
        except SemVerError:
            continue
    return package_id, versions


def _resolve(
    txn: Any, requirements: list[tuple[str, str, Constraint]], maximum: int
) -> list[tuple[str, str, str, list[tuple[str, str, str]]]]:
    constraints: dict[tuple[str, str], list[Constraint]] = {}
    queue: deque[tuple[str, str]] = deque()
    for namespace, name, constraint in requirements:
        key = (namespace, name)
        constraints.setdefault(key, []).append(constraint)
        queue.append(key)
    selected: dict[tuple[str, str], tuple[str, str, str, list[tuple[str, str, str]]]] = {}
    iterations = 0
    while queue:
        iterations += 1
        if iterations > maximum * 20:
            raise conflict("Dependency graph is too complex.")
        namespace, name = queue.popleft()
        key = (namespace, name)
        package_id, versions = _available_versions(txn, namespace, name)
        if package_id is None:
            raise conflict(
                "Package is not available.",
                {"reason": "unresolved", "namespace": namespace, "name": name},
            )
        matching = [
            (raw, parsed)
            for raw, parsed in versions
            if all(constraint.matches(parsed) for constraint in constraints[key])
        ]
        if not matching:
            raise conflict(
                "No package version satisfies the constraints.",
                {"reason": "unresolved", "namespace": namespace, "name": name},
            )
        best_raw, _best = max(matching, key=lambda item: item[1].version)
        previous = selected.get(key)
        if previous is not None and previous[2] == best_raw:
            continue
        dependencies: list[tuple[str, str, str]] = []
        for dependency_namespace, dependency_name, constraint_text in _version_dependencies(
            txn, package_id, best_raw
        ):
            try:
                dependency_constraint = parse_constraint(constraint_text)
            except ConstraintError:
                raise conflict(
                    "Package metadata contains an invalid dependency constraint."
                ) from None
            dependencies.append(
                (
                    dependency_namespace,
                    dependency_name,
                    constraint_text,
                )
            )
            dependency_key = (dependency_namespace, dependency_name)
            constraints.setdefault(dependency_key, []).append(dependency_constraint)
            queue.append(dependency_key)
        selected[key] = (namespace, name, best_raw, dependencies)
        if len(selected) > maximum:
            raise conflict(
                "Dependency resolution exceeded the configured package limit.",
                {"reason": "quota_exceeded"},
            )
    result = sorted(selected.values(), key=lambda item: (item[0], item[1]))
    return result


class PackageService(Service):
    def register(self, resource: Any) -> None:
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages$")],
            self.search_packages,
            "KinemiumPackagesServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/packages$")],
            self.publish_package,
            "KinemiumPackagesServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/packages/resolve$")],
            self.resolve_packages,
            "KinemiumPackageResolveServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/versions/(?P<version>[^/]+)/download$")],
            self.download_version,
            "KinemiumPackageDownloadServlet",
        )
        resource.register_paths(
            "POST",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/versions/(?P<version>[^/]+)/yank$")],
            self.yank_version,
            "KinemiumPackageVersionServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)/versions/(?P<version>[^/]+)$")],
            self.get_version,
            "KinemiumPackageVersionServlet",
        )
        resource.register_paths(
            "PATCH",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)$")],
            self.patch_package,
            "KinemiumPackageServlet",
        )
        resource.register_paths(
            "GET",
            [re.compile(r"^/_kinemium/v1/packages/(?P<namespace>[^/]+)/(?P<name>[^/]+)$")],
            self.get_package,
            "KinemiumPackageServlet",
        )

    def _package_payload(self, row: tuple) -> dict[str, Any]:
        return {
            "packageId": row[0],
            "namespace": row[1],
            "name": row[2],
            "owner": row[3],
            "description": row[4],
            "license": row[5],
            "repository": row[6],
            "engineConstraint": row[7],
            "deprecated": bool(row[8]),
            "deprecatedAt": row[9],
            "createdAt": row[10],
            "updatedAt": row[11],
        }

    def _summary_payload(self, row: tuple) -> dict[str, Any]:
        payload = self._package_payload(row[:12])
        payload["latestVersion"] = row[12]
        return payload

    def _version_payload(
        self, row: tuple, dependencies: list[tuple]
    ) -> dict[str, Any]:
        return {
            "version": row[1],
            "manifest": json_loads(row[2]),
            "entrypoint": row[3],
            "engineConstraint": row[4],
            "digest": row[5],
            "size": int(row[7]),
            "yanked": bool(row[8]),
            "publishedAt": row[9],
            "dependencies": [
                {
                    "namespace": dependency[0],
                    "name": dependency[1],
                    "constraint": dependency[2],
                }
                for dependency in dependencies
            ],
        }

    async def _package_and_versions(
        self, namespace: str, name: str
    ) -> tuple[tuple, list[tuple], dict[str, list[tuple]]]:
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        versions = await self.db.run(
            "kinemium_get_package_versions", _version_rows, package[0]
        )
        dependency_rows = await self.db.run(
            "kinemium_get_package_dependencies", _dependencies, package[0]
        )
        grouped: dict[str, list[tuple]] = {}
        for dependency in dependency_rows:
            grouped.setdefault(dependency[0], []).append(dependency[1:])
        return package, versions, grouped

    async def search_packages(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        limit = self.page_limit(request)
        query = query_value(request, "q", None, 100)
        if query is not None:
            query = query.strip()
            if not query:
                query = None
        namespace = query_value(request, "namespace", None, 64)
        if namespace is not None:
            validate_slug(namespace, "namespace")
        sort = query_value(request, "sort", "recent", 16)
        if sort not in ("recent", "created", "name"):
            raise invalid_request("sort must be recent, created, or name.")
        include_yanked = bool(query_bool(request, "includeYanked", False))
        cursor_raw = query_value(request, "cursor", None, 2048)
        cursor = None
        if cursor_raw is not None:
            cursor = decode_cursor(cursor_raw)
            if sort == "name":
                if not isinstance(cursor.get("namespace"), str) or not isinstance(
                    cursor.get("name"), str
                ):
                    raise invalid_request("Invalid pagination cursor.")
            elif not isinstance(cursor.get("updatedAt"), int) or not isinstance(
                cursor.get("packageId"), str
            ):
                raise invalid_request("Invalid pagination cursor.")
        rows, has_more = await self.db.run(
            "kinemium_search_packages",
            _search_packages,
            query,
            namespace,
            sort,
            include_yanked,
            limit,
            cursor,
        )
        result: dict[str, Any] = {
            "packages": [self._summary_payload(row) for row in rows],
            "sort": sort,
        }
        if has_more and rows:
            if sort == "name":
                result["nextCursor"] = encode_cursor(
                    {"namespace": rows[-1][1], "name": rows[-1][2]}
                )
            else:
                result["nextCursor"] = encode_cursor(
                    {"updatedAt": rows[-1][11], "packageId": rows[-1][0]}
                )
        return 200, result

    def _parse_manifest(
        self, fields: dict[str, str]
    ) -> tuple[
        dict[str, Any],
        str,
        str,
        str,
        str | None,
        str | None,
        str | None,
        str | None,
        list[tuple[str, str, str]],
        str | None,
    ]:
        raw_manifest = fields.get("manifest")
        if raw_manifest is not None:
            try:
                manifest = json.loads(raw_manifest)
            except json.JSONDecodeError:
                raise invalid_request("manifest must be valid JSON.") from None
            if not isinstance(manifest, dict):
                raise invalid_request("manifest must be a JSON object.")
        else:
            manifest = {}
            for key in (
                "namespace",
                "name",
                "version",
                "description",
                "license",
                "repository",
                "engineConstraint",
                "entrypoint",
            ):
                if key in fields:
                    manifest[key] = fields[key]
            if "dependencies" in fields:
                try:
                    manifest["dependencies"] = json.loads(fields["dependencies"])
                except json.JSONDecodeError:
                    raise invalid_request("dependencies must be valid JSON.") from None
        validate_json_value(manifest, MAX_MANIFEST_BYTES)
        name = require_manifest_str(manifest, "name", 1, 64)
        validate_slug(name, "name")
        version = require_manifest_str(manifest, "version", 1, 128)
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        description = optional_manifest_str(manifest, "description", 4000) or ""
        license_name = optional_manifest_str(manifest, "license", 128)
        repository = optional_manifest_str(manifest, "repository", 512)
        if repository is not None:
            repository = validate_website(repository)
        engine_constraint = optional_manifest_str(
            manifest, "engineConstraint", 512
        )
        if engine_constraint is not None:
            try:
                parse_constraint(engine_constraint)
            except ConstraintError as error:
                raise invalid_request(str(error)) from None
        entrypoint = optional_manifest_str(manifest, "entrypoint", 255)
        if entrypoint is not None:
            entrypoint = validate_relative_path(entrypoint, "entrypoint")
        dependencies = self._parse_dependencies(manifest.get("dependencies"))
        namespace = optional_manifest_str(manifest, "namespace", 64)
        return (
            manifest,
            name,
            version,
            description,
            license_name,
            repository,
            engine_constraint,
            entrypoint,
            dependencies,
            namespace,
        )

    def _parse_dependencies(self, value: Any) -> list[tuple[str, str, str]]:
        items: list[tuple[str, str, str]] = []
        if value is None:
            return items
        if isinstance(value, dict):
            for key, constraint in value.items():
                if not isinstance(key, str) or not isinstance(constraint, str):
                    raise invalid_request(
                        "dependencies must map package names to constraints."
                    )
                namespace, separator, name = key.partition("/")
                if not separator:
                    raise invalid_request(
                        "Dependency keys must use namespace/name format."
                    )
                items.append((namespace, name, constraint))
        elif isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    raise invalid_request("dependencies must contain objects.")
                dependency_namespace = item.get("namespace")
                dependency_name = item.get("name")
                dependency_constraint = item.get("constraint")
                if (
                    not isinstance(dependency_namespace, str)
                    or not isinstance(dependency_name, str)
                    or not isinstance(dependency_constraint, str)
                ):
                    raise invalid_request(
                        "Dependencies require namespace, name, and constraint strings."
                    )
                items.append(
                    (dependency_namespace, dependency_name, dependency_constraint)
                )
        else:
            raise invalid_request("dependencies must be an object or array.")
        if len(items) > self.config.max_dependency_count:
            raise invalid_request("Too many dependencies.")
        seen: set[tuple[str, str]] = set()
        result: list[tuple[str, str, str]] = []
        for namespace, name, constraint in items:
            validate_slug(namespace, "dependency namespace")
            validate_slug(name, "dependency name")
            try:
                parse_constraint(constraint)
            except ConstraintError as error:
                raise invalid_request(str(error)) from None
            key = (namespace, name)
            if key in seen:
                raise invalid_request("Dependencies must not contain duplicates.")
            seen.add(key)
            result.append((namespace, name, constraint))
        return result

    async def publish_package(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        fields, archive, filename = parse_multipart(
            request, self.config.max_json_body_bytes
        )
        if archive is None:
            raise invalid_request("An archive file is required.")
        if len(archive) > self.config.max_archive_bytes:
            raise payload_too_large(
                "The uploaded file exceeds the configured limit."
            )
        archive_kind = validate_archive(
            archive,
            filename,
            self.config.max_archive_entries,
            self.config.max_archive_uncompressed_bytes,
        )
        if archive_kind is None:
            raise invalid_request("Packages must be ZIP or tar archives.")
        manifest, name, version, description, license_name, repository, engine_constraint, entrypoint, dependencies, manifest_namespace = self._parse_manifest(fields)
        namespace = _namespace_for_user(caller.user_id)
        if manifest_namespace is not None and manifest_namespace != namespace:
            raise forbidden("You may only publish under your own namespace.")
        storage_key = self.storage.new_key(
            "packages", f"{namespace}/{name}/{version}"
        )
        digest, size = await self.storage.write(storage_key, archive)
        try:
            package = await self.db.run(
                "kinemium_create_package",
                _create_package,
                caller.user_id,
                namespace,
                name,
                description,
                license_name,
                repository,
                engine_constraint,
                now_ms(),
                self.db.for_update(),
            )
            row = await self.db.run(
                "kinemium_publish_version",
                _publish_version,
                package,
                version,
                self.db.json(manifest),
                entrypoint,
                engine_constraint,
                digest,
                storage_key,
                size,
                dependencies,
                self.config.max_package_versions,
                now_ms(),
                self.db.for_update(),
            )
        except Exception:
            await self.storage.delete(storage_key)
            raise
        published: tuple | None = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if published is None:
            raise not_found("Package not found.")
        dependencies = await self.db.run(
            "kinemium_get_version_dependencies",
            _version_dependencies,
            published[0],
            version,
        )
        return 201, {
            "package": self._package_payload(package),
            "version": self._version_payload(row, dependencies),
            "archive": {
                "format": archive_kind,
                "filename": sanitize_filename(filename, f"{name}-{version}.zip"),
                "size": size,
                "digest": digest,
            },
        }

    async def get_package(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        package, versions, grouped = await self._package_and_versions(
            namespace, name
        )
        latest = next((row for row in versions if not row[8]), None)
        if latest is None and versions:
            latest = versions[0]
        payload = self._package_payload(package)
        payload["latestVersion"] = latest[1] if latest is not None else None
        payload["versions"] = [
            self._version_payload(row, grouped.get(row[1], [])) for row in versions
        ]
        return 200, payload

    async def get_version(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        version: str,
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        row = await self.db.run(
            "kinemium_get_version", _version_row, package[0], version
        )
        if row is None:
            raise not_found("Package version not found.")
        dependencies = await self.db.run(
            "kinemium_get_version_dependencies", _version_dependencies, package[0], version
        )
        return 200, {
            "package": self._package_payload(package),
            "version": self._version_payload(row, dependencies),
        }

    async def download_version(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        version: str,
    ) -> None:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        row = await self.db.run(
            "kinemium_get_version", _version_row, package[0], version
        )
        if row is None:
            raise not_found("Package version not found.")
        data = await self.storage.read(row[6])
        if data.startswith(b"PK\x03\x04"):
            filename = f"{name}-{version}.zip"
        elif data.startswith(b"\x1f\x8b"):
            filename = f"{name}-{version}.tar.gz"
        else:
            filename = f"{name}-{version}.tar"
        extra_headers = {"X-Kinemium-Yanked": "true" if row[8] else "false"}
        send_file(
            request,
            data,
            filename,
            content_type_for_filename(filename),
            row[5],
            extra_headers,
        )

    async def yank_version(
        self,
        request: SynapseRequest,
        namespace: str,
        name: str,
        version: str,
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        try:
            parse_semver(version)
        except SemVerError as error:
            raise invalid_request(str(error)) from None
        body = parse_json_body(request, self.config.max_json_body_bytes)
        yanked = body.get("yanked")
        if not isinstance(yanked, bool):
            raise invalid_request("yanked must be a boolean.")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        if package[3] != caller.user_id:
            raise forbidden("You do not own that package.")
        row = await self.db.run(
            "kinemium_set_yanked",
            _set_yanked,
            package,
            version,
            yanked,
            self.db.for_update(),
        )
        dependencies = await self.db.run(
            "kinemium_get_version_dependencies", _version_dependencies, package[0], version
        )
        return 200, {
            "package": self._package_payload(package),
            "version": self._version_payload(row, dependencies),
        }

    async def patch_package(
        self, request: SynapseRequest, namespace: str, name: str
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_WRITE, required=True)
        assert caller is not None
        self.enforce_rate_limit(request, caller)
        validate_slug(namespace, "namespace")
        validate_slug(name, "name")
        package = await self.db.run(
            "kinemium_get_package", _package_row, namespace, name
        )
        if package is None:
            raise not_found("Package not found.")
        if package[3] != caller.user_id:
            raise forbidden("You do not own that package.")
        body = parse_json_body(request, self.config.max_json_body_bytes)
        allowed = {"description", "license", "repository", "deprecated"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise invalid_request("Unknown package fields: " + ", ".join(unknown))
        description = None
        license_name = None
        repository = None
        deprecated = None
        if "description" in body:
            value = body.get("description")
            if not isinstance(value, str):
                raise invalid_request("description must be a string.")
            description = check_str(value, "description", 0, 4000)
        if "license" in body:
            value = body.get("license")
            license_name = None if value is None else check_str(value, "license", 0, 128)
        if "repository" in body:
            value = body.get("repository")
            if value is not None and not isinstance(value, str):
                raise invalid_request("repository must be a string or null.")
            repository = validate_website(value)
        if "deprecated" in body:
            if not isinstance(body.get("deprecated"), bool):
                raise invalid_request("deprecated must be a boolean.")
            deprecated = body["deprecated"]
        updated = await self.db.run(
            "kinemium_update_package",
            _update_package_metadata,
            package,
            description,
            license_name,
            repository,
            deprecated,
            now_ms(),
        )
        return 200, self._package_payload(updated)

    async def resolve_packages(
        self, request: SynapseRequest
    ) -> tuple[int, dict[str, Any]]:
        caller = await self.authenticate(request, PACKAGES_READ, required=False)
        self.enforce_rate_limit(request, caller)
        body = parse_json_body(request, self.config.max_json_body_bytes)
        requirements = require_list(
            body, "requirements", self.config.max_resolve_packages
        )
        parsed: list[tuple[str, str, Constraint]] = []
        for requirement in requirements:
            if not isinstance(requirement, dict):
                raise invalid_request("requirements must contain objects.")
            namespace = requirement.get("namespace")
            name = requirement.get("name")
            constraint_text = requirement.get("constraint", "*")
            if (
                not isinstance(namespace, str)
                or not isinstance(name, str)
                or not isinstance(constraint_text, str)
            ):
                raise invalid_request(
                    "Requirements need namespace, name, and constraint strings."
                )
            validate_slug(namespace, "namespace")
            validate_slug(name, "name")
            try:
                constraint = parse_constraint(constraint_text)
            except ConstraintError as error:
                raise invalid_request(str(error)) from None
            parsed.append((namespace, name, constraint))
        rows = await self.db.run(
            "kinemium_resolve_packages",
            _resolve,
            parsed,
            self.config.max_resolve_packages,
        )
        return 200, {
            "packages": [
                {
                    "namespace": row[0],
                    "name": row[1],
                    "version": row[2],
                    "dependencies": [
                        {
                            "namespace": dependency[0],
                            "name": dependency[1],
                            "constraint": dependency[2],
                        }
                        for dependency in row[3]
                    ],
                }
                for row in rows
            ]
        }


def require_manifest_str(
    manifest: dict[str, Any], name: str, minimum: int, maximum: int
) -> str:
    value = manifest.get(name)
    if not isinstance(value, str):
        raise invalid_request(f"manifest.{name} must be a string.")
    try:
        return check_str(value, f"manifest.{name}", minimum, maximum)
    except TypeError:
        raise invalid_request(f"manifest.{name} must be a string.") from None


def optional_manifest_str(
    manifest: dict[str, Any], name: str, maximum: int
) -> str | None:
    value = manifest.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise invalid_request(f"manifest.{name} must be a string or null.")
    return check_str(value, f"manifest.{name}", 0, maximum)
