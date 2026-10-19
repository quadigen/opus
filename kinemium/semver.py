from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_PARTIAL_RE = re.compile(
    r"^(?P<major>\d+|x|X|\*)"
    r"(?:\.(?P<minor>\d+|x|X|\*))?"
    r"(?:\.(?P<patch>\d+|x|X|\*))?"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?$"
)
_TERM_RE = re.compile(r"^(>=|<=|>|<|==|=|!=|\^|~>|~)?(.*)$")


class SemVerError(ValueError):
    pass


class ConstraintError(ValueError):
    pass


@dataclass(frozen=True)
class SemVer:
    original: str
    version: Version

    @property
    def is_prerelease(self) -> bool:
        return self.version.is_prerelease


@dataclass(frozen=True)
class _Partial:
    major: int | None
    minor: int | None
    patch: int | None
    pre: str | None

    def filled(self) -> str:
        if self.major is None:
            return "0.0.0"
        return f"{self.major}.{self.minor or 0}.{self.patch or 0}" + (
            f"-{self.pre}" if self.pre else ""
        )


def parse_semver(text: object) -> SemVer:
    if not isinstance(text, str):
        raise SemVerError("Version must be a string.")
    if len(text) > 128:
        raise SemVerError("Version is too long.")
    if not _SEMVER_RE.fullmatch(text):
        raise SemVerError(
            "Version must use strict semantic versioning (MAJOR.MINOR.PATCH)."
        )
    try:
        parsed = Version(text)
    except InvalidVersion:
        raise SemVerError("Invalid semantic version.") from None
    return SemVer(original=text, version=parsed)


def _parse_partial(text: str) -> _Partial:
    match = _PARTIAL_RE.fullmatch(text)
    if not match:
        raise ConstraintError(f"Unsupported version constraint: {text!r}")
    values: list[int | None] = []
    for name in ("major", "minor", "patch"):
        raw = match.group(name)
        if raw is None or raw in ("x", "X", "*"):
            values.append(None)
        else:
            if len(raw) > 1 and raw.startswith("0"):
                raise ConstraintError("Version components must not have leading zeros.")
            values.append(int(raw))
    pre = match.group("pre")
    if pre is not None:
        for identifier in pre.split("."):
            if not identifier:
                raise ConstraintError("Invalid prerelease identifier.")
            if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                raise ConstraintError("Numeric prerelease identifiers cannot have leading zeros.")
    if values[0] is None and (values[1] is not None or values[2] is not None):
        raise ConstraintError("Wildcards must be trailing.")
    if values[1] is None and values[2] is not None:
        raise ConstraintError("Wildcards must be trailing.")
    return _Partial(values[0], values[1], values[2], pre)


def _next_major(partial: _Partial) -> str:
    assert partial.major is not None
    return f"{partial.major + 1}.0.0"


def _next_minor(partial: _Partial) -> str:
    assert partial.major is not None
    return f"{partial.major}.{(partial.minor or 0) + 1}.0"


def _next_patch(partial: _Partial) -> str:
    assert partial.major is not None
    return f"{partial.major}.{partial.minor or 0}.{(partial.patch or 0) + 1}"


def _hyphen_specs(left: str, right: str) -> list[str]:
    left_partial = _parse_partial(left.strip())
    right_partial = _parse_partial(right.strip())
    if left_partial.major is None or right_partial.major is None:
        raise ConstraintError("Invalid hyphen version range.")
    result = [f">={left_partial.filled()}"]
    if right_partial.minor is None:
        result.append(f"<{_next_major(right_partial)}")
    elif right_partial.patch is None:
        result.append(f"<{_next_minor(right_partial)}")
    else:
        result.append(f"<={right_partial.filled()}")
    return result


def _term_to_specs(term: str) -> tuple[list[str], bool]:
    term = term.strip()
    if not term:
        raise ConstraintError("Empty version constraint term.")
    if term in ("*", "x", "X", "latest"):
        return [], False
    match = _TERM_RE.fullmatch(term)
    if not match:
        raise ConstraintError(f"Unsupported version constraint: {term!r}")
    operator = match.group(1) or ""
    body = match.group(2).strip()
    if not body:
        raise ConstraintError("Missing version in constraint.")
    partial = _parse_partial(body)
    has_prerelease = partial.pre is not None
    if operator in ("^", "~", "~>"):
        if partial.major is None:
            raise ConstraintError("Caret and tilde constraints require a version.")
        lower = partial.filled()
        if operator == "^":
            if partial.major > 0 or partial.minor is None:
                upper = _next_major(partial)
            elif partial.minor > 0 or partial.patch is None:
                upper = _next_minor(partial)
            else:
                upper = _next_patch(partial)
        else:
            if partial.minor is None:
                upper = _next_major(partial)
            else:
                upper = _next_minor(partial)
        return [f">={lower}", f"<{upper}"], has_prerelease
    if operator in (">=", "<=", ">", "<", "==", "=", "!="):
        if partial.major is None:
            raise ConstraintError("Comparator requires a concrete version.")
        version = partial.filled()
        if operator in ("==", "="):
            if partial.minor is None:
                return [f">={version}", f"<{_next_major(partial)}"], has_prerelease
            if partial.patch is None:
                return [f">={version}", f"<{_next_minor(partial)}"], has_prerelease
            return [f"=={version}"], has_prerelease
        if operator == "!=":
            if partial.minor is None or partial.patch is None:
                raise ConstraintError("!= requires a full version.")
            return [f"!={version}"], has_prerelease
        return [f"{operator}{version}"], has_prerelease
    if partial.major is None:
        return [], False
    if partial.minor is None:
        return [f">={partial.major}.0.0", f"<{_next_major(partial)}"], False
    if partial.patch is None:
        return [
            f">={partial.major}.{partial.minor}.0",
            f"<{_next_minor(partial)}",
        ], False
    return [f"=={partial.filled()}"], has_prerelease


def _parse_alternative(text: str) -> tuple[SpecifierSet, bool]:
    text = text.strip()
    if not text or text in ("*", "x", "X", "latest"):
        return SpecifierSet(), False
    specs: list[str] = []
    allow_prerelease = False
    hyphen_pattern = re.compile(r"(?<!\S)(\S+)\s+-\s+(\S+)(?!\S)")
    for left, right in hyphen_pattern.findall(text):
        specs.extend(_hyphen_specs(left, right))
    text = hyphen_pattern.sub(" ", text)
    for term in text.split():
        term_specs, term_pre = _term_to_specs(term)
        specs.extend(term_specs)
        allow_prerelease = allow_prerelease or term_pre
    try:
        specifier_set = SpecifierSet(",".join(specs))
    except InvalidSpecifier:
        raise ConstraintError(f"Unsupported version constraint: {text!r}") from None
    if not specs and not allow_prerelease:
        return SpecifierSet(), False
    return specifier_set, allow_prerelease


@dataclass(frozen=True)
class Constraint:
    original: str
    alternatives: tuple[tuple[SpecifierSet, bool], ...]

    def matches(self, version: SemVer) -> bool:
        for specifier_set, allow_prerelease in self.alternatives:
            if not specifier_set and not allow_prerelease and version.is_prerelease:
                continue
            if specifier_set.contains(version.version, prereleases=allow_prerelease):
                return True
        return False


def parse_constraint(text: object) -> Constraint:
    if not isinstance(text, str):
        raise ConstraintError("Version constraint must be a string.")
    if len(text) > 512:
        raise ConstraintError("Version constraint is too long.")
    original = text.strip()
    if not original:
        raise ConstraintError("Version constraint must not be empty.")
    alternatives = []
    for part in original.split("||"):
        alternatives.append(_parse_alternative(part))
    return Constraint(original=original, alternatives=tuple(alternatives))


def sort_versions(versions: Iterable[SemVer]) -> list[SemVer]:
    return sorted(
        versions,
        key=lambda version: (version.version, version.original),
        reverse=True,
    )


def select_highest(
    versions: Iterable[SemVer], constraint: Constraint | None = None
) -> SemVer | None:
    candidates = [
        version
        for version in versions
        if constraint is None or constraint.matches(version)
    ]
    if not candidates:
        return None
    return sort_versions(candidates)[0]
