from __future__ import annotations

from pathlib import Path

from twisted.web.resource import Resource

from synapse.http.site import SynapseRequest

_PAGE = (Path(__file__).parent / "static" / "admin-review.html").read_bytes()


class AdminReviewResource(Resource):
    isLeaf = True

    def render_GET(self, request: SynapseRequest) -> bytes:
        path = request.path.decode("latin-1").rstrip("/")
        if path == "/_kinemium/admin":
            request.setResponseCode(302)
            request.setHeader(b"Location", b"/_kinemium/admin/review")
            return b""
        if path != "/_kinemium/admin/review":
            request.setResponseCode(404)
            request.setHeader(b"Content-Type", b"text/plain; charset=utf-8")
            return b"Not found"
        request.setResponseCode(200)
        request.setHeader(b"Content-Type", b"text/html; charset=utf-8")
        request.setHeader(b"Cache-Control", b"no-store")
        request.setHeader(b"X-Content-Type-Options", b"nosniff")
        request.setHeader(b"X-Frame-Options", b"DENY")
        request.setHeader(b"Referrer-Policy", b"no-referrer")
        request.setHeader(
            b"Content-Security-Policy",
            b"default-src 'none'; script-src 'unsafe-inline';"
            b" style-src 'unsafe-inline'; connect-src 'self';"
            b" img-src 'self' data: blob:; base-uri 'none'; form-action 'self';"
            b" frame-ancestors 'none'",
        )
        return _PAGE
