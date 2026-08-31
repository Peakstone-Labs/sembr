# SPDX-License-Identifier: Apache-2.0
"""Background maintenance jobs (reconcile + Qdrant TTL + dead_articles TTL +
derived-field backfill) and manual prune endpoints.

Three APScheduler jobs run on the cadence configured by
``settings.maintenance_interval_hours`` (default 24h), with start_date offsets
of 5 / 15 / 25 minutes so they don't all hit Qdrant in the same instant. The
derived-field backfill runs on its own faster cadence — see
``derived_backfill.py`` for why it cannot share the 24h one.
"""

from __future__ import annotations

from sembr.maintenance.dead_ttl import add_dead_ttl_job
from sembr.maintenance.derived_backfill import (
    add_news_derived_backfill_job,
    initialise_pending_flag,
)
from sembr.maintenance.qdrant_ttl import add_qdrant_ttl_job
from sembr.maintenance.reconcile import add_reconcile_job
from sembr.maintenance.tasks import sweep_expired as manual_prune_sweep_expired

# Re-exported for the maintenance modules' callers — imports route through this
# package so the dependency direction stays maintenance → vector_store.news.
from sembr.vector_store.news import md5_to_uuid, uuid_to_md5

__all__ = [
    "add_dead_ttl_job",
    "add_news_derived_backfill_job",
    "add_qdrant_ttl_job",
    "add_reconcile_job",
    "initialise_pending_flag",
    "manual_prune_sweep_expired",
    "md5_to_uuid",
    "uuid_to_md5",
]
