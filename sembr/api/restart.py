"""Dashboard restart endpoint — POST /api/dashboard/restart (design D1).

Single-purpose router: triggers the same "double restart" (rsshub
force-recreate + api SIGTERM-self) that ``/api/settings/save`` performs
when env keys change, but as the user-facing button on the dashboard
container panel.

Auth model intentionally matches ``/api/settings/save``:
``Depends(require_header_token)`` so an attacker cannot CSRF the endpoint
by stealing the dashboard cookie. Cookie-only browsers are still gated by
``DashboardTokenMiddleware``; the explicit header check defends against
the cross-origin form-POST class.

Response shape mirrors ``SaveResponse``: rsshub failure becomes a 200 with
``rsshub_restart_failed=True`` (and the error string) so the api self-restart
still proceeds and the frontend can surface a non-blocking warning toast —
matches the contract the dashboard already speaks to ``/api/settings/save``.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from sembr.api.settings import require_header_token
from sembr.api.settings_restart import RestartController

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


class RestartResponse(BaseModel):
    """200 response body for POST /api/dashboard/restart.

    ``rsshub_restart_failed`` mirrors the ``SaveResponse`` flag so the
    dashboard's existing toast/error path can render warnings without a
    second response shape.
    """

    rsshub_restart_failed: bool = False
    rsshub_error: str | None = None


@router.post(
    "/restart",
    response_model=RestartResponse,
    dependencies=[Depends(require_header_token)],
)
async def post_restart() -> RestartResponse:
    """Trigger api + rsshub double-restart.

    Ordering matches ``settings.save_settings`` (settings.py:481-501):
    ``await rsshub_restart`` first, then schedule the api SIGTERM. Reversing
    the order would let the SIGTERM fire while the response was still being
    written, dropping the connection before the JSON body reached the browser.
    """
    rc = RestartController()
    rsshub_restart_failed = False
    rsshub_error: str | None = None
    try:
        await rc.restart_rsshub()
    except Exception as exc:  # noqa: BLE001 — degrade rsshub failure to a flag
        logger.error("rsshub restart failed (continuing): %s", exc, exc_info=True)
        rsshub_restart_failed = True
        rsshub_error = str(exc)

    rc.schedule_self_restart()

    return RestartResponse(
        rsshub_restart_failed=rsshub_restart_failed,
        rsshub_error=rsshub_error,
    )
