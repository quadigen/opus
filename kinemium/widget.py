from __future__ import annotations

import re
from datetime import datetime, timezone
from html import escape
from typing import Any
from urllib.parse import quote, urlparse

from synapse.api.errors import SynapseError
from synapse.http.site import SynapseRequest
from synapse.types import UserID

from .accounts import _get_game, _profile_row
from .base import Service
from .packages import _namespace_row, _package_row, package_is_visible

MAX_PER_KIND = 12
MAX_TOTAL = 40
MAX_TITLE = 80
MAX_IDENTIFIER = 255
ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
HTML_CSP = (
    b"default-src 'none'; style-src 'unsafe-inline';"
    b" script-src 'unsafe-inline'; img-src 'self' data:;"
    b" connect-src 'none'; frame-ancestors *; base-uri 'none'"
)
SVG_CSP = (
    b"default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:;"
    b" base-uri 'none'"
)


def _latest_version(txn: Any, package_id: str) -> str | None:
    txn.execute(
        "SELECT version FROM kinemium_package_versions"
        " WHERE package_id = %s AND yanked = FALSE"
        " AND review_status = 'approved'"
        " ORDER BY published_at DESC LIMIT 1",
        (package_id,),
    )
    row = txn.fetchone()
    return str(row[0]) if row is not None else None


def _format_date(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return ""
    date = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return f"{date:%b} {date.day}, {date:%Y}"


def _mxc_thumbnail(value: str | None, size: int = 96) -> str | None:
    if not value or not value.startswith("mxc://"):
        return None
    parsed = urlparse(value)
    server = parsed.netloc
    media_id = parsed.path.lstrip("/")
    if not server or not media_id:
        return None
    return (
        f"/_matrix/media/v3/thumbnail/{quote(server, safe='')}/"
        f"{quote(media_id, safe='')}?width={size}&height={size}&method=crop"
    )


def _fit(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


def _wrap(text: str, width: int, lines: int) -> list[str]:
    words = " ".join(text.split()).split(" ")
    result: list[str] = []
    current = ""
    for word in words:
        if not word:
            continue
        candidate = word if not current else current + " " + word
        if len(candidate) <= width:
            current = candidate
            continue
        if current:
            result.append(current)
        if len(word) > width:
            current = word[: width - 1] + "…"
            break
        current = word
        if len(result) == lines:
            break
    if current and len(result) < lines:
        result.append(current)
    if len(result) > lines:
        result = result[:lines]
    return result or [""]


class WidgetService(Service):
    def register(self, resource: Any) -> None:
        resource.register_paths(
            "GET",
            [
                re.compile(r"^/_kinemium/widget$"),
                re.compile(r"^/widget$"),
            ],
            self.render_html,
            "KinemiumWidgetServlet",
        )
        resource.register_paths(
            "GET",
            [
                re.compile(r"^/_kinemium/widget\.svg$"),
                re.compile(r"^/widget\.svg$"),
            ],
            self.render_svg,
            "KinemiumWidgetSvgServlet",
        )

    def _query_values(self, request: SynapseRequest, names: tuple[str, ...]) -> list[str]:
        result: list[str] = []
        for name in names:
            raw_values: list[bytes] = (
                request.args.get(name.encode("utf-8"), []) if request.args else []
            )
            for raw in raw_values:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                for part in text.split(","):
                    value = part.strip()
                    if value and len(value) <= MAX_IDENTIFIER and value not in result:
                        result.append(value)
        return result[:MAX_PER_KIND]

    def _query_one(
        self, request: SynapseRequest, name: str, default: str
    ) -> str:
        raw_values: list[bytes] = (
            request.args.get(name.encode("utf-8"), []) if request.args else []
        )
        if not raw_values:
            return default
        try:
            text = raw_values[0].decode("utf-8").strip()
        except UnicodeDecodeError:
            return default
        return text[:MAX_TITLE] or default

    async def _matrix_profile(self, user_id: str) -> tuple[str | None, str | None]:
        try:
            parsed = UserID.from_string(user_id)
        except Exception:
            return None, None
        if parsed.domain != self.api.server_name:
            return None, None
        try:
            profile = await self.api.get_profile_for_user(parsed.localpart)
        except SynapseError:
            return None, None
        return profile.display_name, profile.avatar_url

    def _unavailable(self, kind: str, identifier: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "ok": False,
            "title": identifier,
            "subtitle": "Not found or not public.",
            "meta": "",
            "badge": kind.capitalize(),
            "initial": identifier[:1].upper() or "?",
            "accent": None,
            "icon": None,
        }

    async def _package_item(self, namespace: str, name: str) -> dict[str, Any]:
        identifier = f"{namespace}/{name}"
        row = await self.db.run(
            "kinemium_widget_package", _package_row, namespace, name
        )
        if row is None or not package_is_visible(row, None):
            return self._unavailable("package", identifier)
        latest = await self.db.run(
            "kinemium_widget_latest_version", _latest_version, row[0]
        )
        description = str(row[4] or "").strip()
        item_type = str(row[12] or "package")
        return {
            "kind": "package",
            "ok": True,
            "title": identifier,
            "subtitle": description or "No description provided.",
            "meta": f"v{latest}" if latest else "No published version",
            "badge": item_type.capitalize(),
            "initial": name[:1].upper() or "?",
            "accent": None,
            "icon": _mxc_thumbnail(row[22] if isinstance(row[22], str) else None),
        }

    async def _user_item(self, user_id: str) -> dict[str, Any]:
        row = await self.db.run("kinemium_widget_profile", _profile_row, user_id)
        if row is None or not row[3]:
            return self._unavailable("user", user_id)
        display_name, avatar_url = await self._matrix_profile(user_id)
        localpart = user_id.split(":", 1)[0].lstrip("@")
        title = display_name or localpart or user_id
        subtitle = str(row[1] or "").strip() or str(row[7] or "").strip()
        return {
            "kind": "user",
            "ok": True,
            "title": title,
            "subtitle": subtitle or "Kinemium developer",
            "meta": user_id,
            "badge": "User",
            "initial": title[:1].upper() or "?",
            "accent": row[6] if isinstance(row[6], str) else None,
            "icon": _mxc_thumbnail(avatar_url),
        }

    async def _namespace_item(self, namespace: str) -> dict[str, Any]:
        row = await self.db.run(
            "kinemium_widget_namespace", _namespace_row, namespace
        )
        if row is None:
            return self._unavailable("namespace", namespace)
        count = int(row[3])
        return {
            "kind": "namespace",
            "ok": True,
            "title": namespace,
            "subtitle": f"{count} package{'s' if count != 1 else ''}",
            "meta": str(row[1]),
            "badge": "Namespace",
            "initial": namespace[:1].upper() or "?",
            "accent": None,
            "icon": None,
        }

    async def _game_item(self, game_id: str) -> dict[str, Any]:
        row = await self.db.run("kinemium_widget_game", _get_game, game_id)
        if row is None:
            return self._unavailable("game", game_id)
        return {
            "kind": "game",
            "ok": True,
            "title": str(row[2]),
            "subtitle": f"Game by {row[1]}",
            "meta": f"Created {_format_date(row[3])}",
            "badge": "Game",
            "initial": str(row[2])[:1].upper() or "?",
            "accent": None,
            "icon": None,
        }

    async def _collect(
        self, request: SynapseRequest
    ) -> tuple[str, list[tuple[str, list[dict[str, Any]]]]]:
        title = self._query_one(request, "title", "Kinemium")
        packages = self._query_values(request, ("packages", "plugins"))
        users = self._query_values(request, ("users",))
        namespaces = self._query_values(request, ("namespaces",))
        games = self._query_values(request, ("games",))
        remaining = MAX_TOTAL

        groups: list[tuple[str, list[dict[str, Any]]]] = []

        package_items: list[dict[str, Any]] = []
        for identifier in packages:
            if remaining <= 0:
                break
            remaining -= 1
            parts = identifier.split("/")
            if len(parts) != 2 or not all(parts):
                item = self._unavailable("package", identifier)
            else:
                item = await self._package_item(parts[0], parts[1])
            package_items.append(item)
        if package_items:
            groups.append(("Packages", package_items))

        user_items: list[dict[str, Any]] = []
        for identifier in users:
            if remaining <= 0:
                break
            remaining -= 1
            user_items.append(await self._user_item(identifier))
        if user_items:
            groups.append(("Users", user_items))

        namespace_items: list[dict[str, Any]] = []
        for identifier in namespaces:
            if remaining <= 0:
                break
            remaining -= 1
            namespace_items.append(await self._namespace_item(identifier))
        if namespace_items:
            groups.append(("Namespaces", namespace_items))

        game_items: list[dict[str, Any]] = []
        for identifier in games:
            if remaining <= 0:
                break
            remaining -= 1
            game_items.append(await self._game_item(identifier))
        if game_items:
            groups.append(("Games", game_items))
        return title, groups

    def _write(
        self,
        request: SynapseRequest,
        content_type: str,
        body: bytes,
        csp: bytes,
    ) -> None:
        request.setResponseCode(200)
        request.setHeader(b"Content-Type", content_type.encode("latin-1"))
        request.setHeader(b"Content-Length", str(len(body)).encode("ascii"))
        request.setHeader(b"Cache-Control", b"public, max-age=60")
        request.setHeader(b"X-Content-Type-Options", b"nosniff")
        request.setHeader(b"Referrer-Policy", b"no-referrer")
        request.setHeader(b"Content-Security-Policy", csp)
        request.setHeader(b"Access-Control-Allow-Origin", b"*")
        request.write(body)
        request.finish()

    def _icon_html(self, item: dict[str, Any]) -> str:
        accent = item.get("accent")
        style = (
            f' style="--icon-accent:{escape(accent)}"'
            if isinstance(accent, str) and ACCENT_RE.fullmatch(accent)
            else ""
        )
        image = item.get("icon")
        image_html = (
            '<img src="'
            + escape(str(image), quote=True)
            + '" alt="" loading="lazy" decoding="async"'
            + ' onerror="this.remove()">'
            if image
            else ""
        )
        return (
            f'<div class="icon"{style}><span>{escape(str(item["initial"]))}</span>'
            f"{image_html}</div>"
        )

    def _card_html(self, item: dict[str, Any]) -> str:
        kind = str(item["kind"])
        badge = escape(str(item["badge"]))
        return (
            f'<article class="card {escape(kind)}">'
            f"{self._icon_html(item)}"
            '<div class="body">'
            f'<div class="title">{escape(str(item["title"]))}</div>'
            f'<div class="subtitle">{escape(str(item["subtitle"]))}</div>'
            f'<div class="meta"><span>{escape(str(item["meta"]))}</span>'
            f'<span class="badge">{badge}</span></div>'
            "</div></article>"
        )

    def _html_document(
        self, title: str, groups: list[tuple[str, list[dict[str, Any]]]]
    ) -> bytes:
        total = sum(len(items) for _, items in groups)
        sections: list[str] = []
        for label, items in groups:
            cards = "".join(self._card_html(item) for item in items)
            sections.append(
                '<section class="group">'
                f'<div class="group-head"><h2>{escape(label)}</h2>'
                f'<span>{len(items)}</span></div>'
                f'<div class="grid">{cards}</div></section>'
            )
        body = "".join(sections) or (
            '<div class="empty">Add <code>packages</code>, <code>users</code>, '
            "<code>namespaces</code>, or <code>games</code> to the widget URL.</div>"
        )
        document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{escape(title)}</title>
<style>
:root {{
  color-scheme: light dark;
  --bg: #ffffff;
  --card: #f6f6f7;
  --text: #17171a;
  --muted: #6b6b73;
  --line: #e3e3e7;
  --accent: #5b5bd6;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #151518;
    --card: #1d1d22;
    --text: #f2f2f3;
    --muted: #9a9aa3;
    --line: #2a2a30;
  }}
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; background: var(--bg); color: var(--text); }}
body {{
  font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased;
  padding: 18px;
}}
header {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 16px; }}
h1 {{ margin: 0; font-size: 18px; line-height: 1.2; letter-spacing: -0.02em; }}
header .count {{ color: var(--muted); font-size: 12px; white-space: nowrap; }}
.group {{ margin-top: 16px; }}
.group:first-of-type {{ margin-top: 0; }}
.group-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 8px; }}
.group-head h2 {{ margin: 0; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }}
.group-head span {{ color: var(--muted); font-size: 11px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 10px; }}
.card {{
  display: flex; align-items: flex-start; gap: 11px; min-width: 0;
  background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 12px;
}}
.icon {{
  position: relative; display: grid; place-items: center; flex: none;
  width: 38px; height: 38px; overflow: hidden; border-radius: 9px;
  background: color-mix(in srgb, var(--icon-accent, var(--accent)) 18%, var(--card));
  color: var(--text); font-size: 15px; font-weight: 700;
}}
.icon img {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }}
.body {{ min-width: 0; flex: 1; }}
.title {{ font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.subtitle {{ color: var(--muted); font-size: 12px; margin-top: 1px; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }}
.meta {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 7px; color: var(--muted); font-size: 10.5px; }}
.meta > span:first-child {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.badge {{ flex: none; border: 1px solid var(--line); border-radius: 999px; padding: 1px 7px; font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.04em; }}
.empty {{ border: 1px dashed var(--line); border-radius: 12px; padding: 28px 18px; text-align: center; color: var(--muted); }}
code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }}
@media (max-width: 460px) {{
  body {{ padding: 13px; }}
  .grid {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>
<header><h1>{escape(title)}</h1><span class="count">{total} item{'s' if total != 1 else ''}</span></header>
{body}
<script>
if (window.parent !== window) {{
  const send = () => window.parent.postMessage(
    {{ type: "kinemium-widget-height", height: document.documentElement.scrollHeight }},
    "*"
  );
  new ResizeObserver(send).observe(document.body);
  send();
}}
</script>
</body>
</html>
"""
        return document.encode("utf-8")

    async def render_html(self, request: SynapseRequest) -> None:
        self.enforce_rate_limit(request, None)
        title, groups = await self._collect(request)
        self._write(
            request,
            "text/html; charset=utf-8",
            self._html_document(title, groups),
            HTML_CSP,
        )

    def _svg_card(
        self,
        item: dict[str, Any],
        x: int,
        y: int,
        width: int,
        height: int,
        text_width: int,
    ) -> str:
        accent = item.get("accent")
        accent = accent if isinstance(accent, str) and ACCENT_RE.fullmatch(accent) else "#5b5bd6"
        subtitle = _wrap(str(item["subtitle"]), text_width, 2)
        subtitle_lines = "".join(
            f'<text x="50" y="{45 + index * 14}" class="sub">{escape(line)}</text>'
            for index, line in enumerate(subtitle)
        )
        return (
            f'<g transform="translate({x},{y})">'
            f'<rect width="{width}" height="{height}" rx="12" class="card"/>'
            f'<circle cx="25" cy="25" r="13" fill="{escape(accent)}" opacity="0.18"/>'
            f'<text x="25" y="30" text-anchor="middle" class="initial">'
            f'{escape(str(item["initial"]))}</text>'
            f'<text x="48" y="25" class="title">{escape(_fit(str(item["title"]), text_width))}</text>'
            f'<text x="{width - 12}" y="25" text-anchor="end" class="badge">'
            f'{escape(str(item["badge"]).upper())}</text>'
            f"{subtitle_lines}"
            f'<text x="48" y="{height - 12}" class="meta">'
            f'{escape(_fit(str(item["meta"]), text_width))}</text>'
            f"</g>"
        )

    def _svg_document(
        self,
        title: str,
        groups: list[tuple[str, list[dict[str, Any]]]],
        width: int,
        theme: str,
    ) -> bytes:
        padding = 20
        header_height = 38
        group_gap = 16
        heading_height = 20
        card_gap = 10
        card_height = 88
        columns = 2 if width >= 620 else 1
        card_width = (
            width - padding * 2 - card_gap * (columns - 1)
        ) // columns
        text_width = max(16, int((card_width - 62) / 6.4))

        rendered_groups: list[str] = []
        cursor = padding + header_height
        for label, items in groups:
            rendered_groups.append(
                f'<text x="{padding}" y="{cursor + 10}" class="group">'
                f"{escape(label.upper())}</text>"
            )
            cursor += heading_height
            for index, item in enumerate(items):
                row = index // columns
                column = index % columns
                x = padding + column * (card_width + card_gap)
                y = cursor + row * (card_height + card_gap)
                rendered_groups.append(
                    self._svg_card(item, x, y, card_width, card_height, text_width)
                )
            rows = (len(items) + columns - 1) // columns
            cursor += rows * card_height + max(0, rows - 1) * card_gap + group_gap

        height = cursor - group_gap + padding
        if not groups:
            rendered_groups.append(
                f'<text x="{padding}" y="{cursor + 16}" class="empty">'
                "Add packages, users, namespaces, or games to the widget URL.</text>"
            )
            height = cursor + 42

        fixed_theme = ""
        if theme in ("light", "dark"):
            if theme == "dark":
                fixed_theme = (
                    ":root{--bg:#151518;--card:#1d1d22;--text:#f2f2f3;"
                    "--muted:#9a9aa3;--line:#2a2a30;}"
                )
            else:
                fixed_theme = (
                    ":root{--bg:#ffffff;--card:#f6f6f7;--text:#17171a;"
                    "--muted:#6b6b73;--line:#e3e3e7;}"
                )
        media = ""
        if theme not in ("light", "dark"):
            media = (
                "@media (prefers-color-scheme:dark){:root{--bg:#151518;"
                "--card:#1d1d22;--text:#f2f2f3;--muted:#9a9aa3;--line:#2a2a30;}}"
            )
        total = sum(len(items) for _, items in groups)
        document = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">
<title>{escape(title)}</title>
<style>
:root{{--bg:#ffffff;--card:#f6f6f7;--text:#17171a;--muted:#6b6b73;--line:#e3e3e7;}}
{media}
{fixed_theme}
text{{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;}}
.bg{{fill:var(--bg);}}
.card{{fill:var(--card);stroke:var(--line);stroke-width:1;}}
.title{{fill:var(--text);font-size:13px;font-weight:650;}}
.sub{{fill:var(--muted);font-size:10.5px;}}
.meta{{fill:var(--muted);font-size:9.5px;}}
.badge{{fill:var(--muted);font-size:8.5px;letter-spacing:1px;}}
.initial{{fill:var(--text);font-size:11px;font-weight:700;}}
.group{{fill:var(--muted);font-size:9.5px;font-weight:650;letter-spacing:1.2px;}}
.empty{{fill:var(--muted);font-size:11px;}}
</style>
<rect class="bg" width="{width}" height="{height}" rx="14"/>
<text x="{padding}" y="{padding + 17}" class="title" font-size="16">{escape(title)}</text>
<text x="{width - padding}" y="{padding + 16}" text-anchor="end" class="meta">{total} item{'s' if total != 1 else ''}</text>
{''.join(rendered_groups)}
</svg>
"""
        return document.encode("utf-8")

    async def render_svg(self, request: SynapseRequest) -> None:
        self.enforce_rate_limit(request, None)
        title, groups = await self._collect(request)
        raw_width = self._query_one(request, "width", "720")
        try:
            width = max(320, min(1200, int(raw_width)))
        except ValueError:
            width = 720
        theme = self._query_one(request, "theme", "auto").lower()
        if theme not in ("auto", "light", "dark"):
            theme = "auto"
        self._write(
            request,
            "image/svg+xml; charset=utf-8",
            self._svg_document(title, groups, width, theme),
            SVG_CSP,
        )
