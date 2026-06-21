# SPDX-License-Identifier: Apache-2.0
"""Token gate for /dashboard and /api/dashboard.

Single shared token via env DASHBOARD_TOKEN. Empty token = pass-through (no auth).
Token comparison uses secrets.compare_digest (timing-safe). Login page and vendor
JS are always exempt so the user can bootstrap the cookie.
"""

from __future__ import annotations

import logging
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from sembr.config import get_settings

logger = logging.getLogger(__name__)

_HEADER_NAME = "X-Dashboard-Token"
_COOKIE_NAME = "sembr_dashboard_token"
_LOGIN_PATH = "/dashboard/login.html"
_VENDOR_PREFIX = "/dashboard/vendor/"
# Trailing-slash prefixes prevent accidental capture of a future
# `/dashboard-status` or `/api/dashboard-stats` business route under the gate.
# Bare paths still need exact-match coverage for `/dashboard` and `/api/dashboard`.
_PROTECTED_PREFIXES = (
    "/dashboard/",
    "/api/dashboard/",
    "/api/prompts/",
    "/api/settings/",
    "/api/external/",
    # map sub-feature: extract-sources / extractions endpoints live under
    # /api/intents/* (not /intents/*) so an unauthenticated fetch gets a 401
    # JSON rather than a 302 redirect to the login page.
    "/api/intents/",
    "/intents/",
    "/feeds/",
)
_PROTECTED_EXACT = frozenset(
    {
        "/dashboard",
        "/api/dashboard",
        "/api/prompts",
        "/api/settings",
        "/intents",
        "/feeds",
    }
)
# Endpoints that must remain reachable without a token to bootstrap the UI:
#   /api/dashboard/config — frontend calls it on first load to know whether
#   auth is required and what poll interval to use.
_AUTH_FREE_API_PATHS = frozenset({"/api/dashboard/config"})
# Static assets the login page itself loads before the cookie is set. Without
# these, an enabled DASHBOARD_TOKEN deployment would 302 the login page's own
# CSS / favicon to /login.html, leaving the user staring at an un-styled page.
_LOGIN_ASSETS = frozenset({"/dashboard/style.css", "/dashboard/favicon.svg"})


class DashboardTokenMiddleware(BaseHTTPMiddleware):
    """Per-request gate. No-op when DASHBOARD_TOKEN is empty.

    Lookup order for the supplied token:
      1. X-Dashboard-Token header (set by bundled JS for /api/* fetch)
      2. sembr_dashboard_token cookie (set by login flow for static asset GETs)
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path not in _PROTECTED_EXACT and not any(
            path.startswith(p) for p in _PROTECTED_PREFIXES
        ):
            return await call_next(request)

        token = get_settings().dashboard_token.get_secret_value()
        if not token:
            return await call_next(request)

        if path == _LOGIN_PATH or path.startswith(_VENDOR_PREFIX):
            return await call_next(request)
        if path in _LOGIN_ASSETS:
            return await call_next(request)
        if path in _AUTH_FREE_API_PATHS:
            return await call_next(request)

        provided = request.headers.get(_HEADER_NAME) or request.cookies.get(_COOKIE_NAME)
        if provided and secrets.compare_digest(provided, token):
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(_LOGIN_PATH, status_code=302)
