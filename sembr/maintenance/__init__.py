# SPDX-License-Identifier: Apache-2.0
"""Background maintenance jobs (reconcile + Qdrant TTL + dead_articles TTL) and
manual prune endpoints.

Three APScheduler jobs run on the cadence configured by
``settings.maintenance_interval_hours`` (default 24h), with start_date offsets
of 5 / 15 / 25 minutes so they don't all hit Qdrant in the same instant.
"""

from __future__ import annotations

# Re-exported for the maintenance modules' callers — imports route through this
# package so the dependency direction stays maintenance → vector_store.news.
from sembr.vector_store.news import md5_to_uuid, uuid_to_md5

from sembr.maintenance.dead_ttl import add_dead_ttl_job
from sembr.maintenance.qdrant_ttl import add_qdrant_ttl_job
from sembr.maintenance.reconcile import add_reconcile_job
from sembr.maintenance.tasks import sweep_expired as manual_prune_sweep_expired

__all__ = [
    "add_dead_ttl_job",
    "add_qdrant_ttl_job",
    "add_reconcile_job",
    "manual_prune_sweep_expired",
    "md5_to_uuid",
    "uuid_to_md5",
]
