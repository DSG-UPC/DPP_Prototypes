"""
Client for the metrics-dashboard service (see metrics-dashboard/), the
timing-measurement sink. Mirrors registry_client.py's shape
(neocyclehub calling out to a separate service, best-effort, never raises).

It is a separate service with its own volume rather than a table in
neocyclehub's own database because `docker compose down -v` already wipes neocyclehub's volume, and
that shouldn't also erase the measurement history.
"""
import logging
import time
from contextlib import contextmanager

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def send_sample(operation, duration_ms, mode="passive", **metadata):
    """
    Best-effort POST of one timing sample. Public (not underscore-prefixed)
    because some call sites — e.g. the chain-walk view, where chain_depth/
    company_count are only known *after* _build_chain() returns — need to
    time manually and send one complete sample, rather than using
    record_timing()'s context-manager shape below.
    """
    payload = {
        "operation": operation,
        "mode": mode,
        "source": settings.METRICS_SOURCE_NAME,
        "duration_ms": round(duration_ms, 3),
        **{k: v for k, v in metadata.items() if v is not None},
    }
    try:
        requests.post(
            f"{settings.METRICS_DASHBOARD_URL.rstrip('/')}/api/samples/",
            json=payload, timeout=3,
        )
    except requests.RequestException as e:
        logger.warning("Failed to record timing sample for %s: %s", operation, e)


@contextmanager
def record_timing(operation, mode="passive", **metadata):
    """
    Times the wrapped block and best-effort POSTs the result to
    metrics-dashboard. A dashboard outage never blocks the timed operation
    itself — same non-blocking convention as register_dpp()/anchor_dpp().
    Use this when all metadata is known *before* the block runs; use
    send_sample() directly otherwise (see its docstring).

    Usage:
        with record_timing("issue_dpp", cred_type="DPP", actor_name=actor.name):
            ... the thing being timed ...
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        send_sample(operation, duration_ms, mode=mode, **metadata)
