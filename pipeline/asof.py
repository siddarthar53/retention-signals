"""As-of availability and feature-source audit rules (design v0.2, section 4; injection cases).

A source row may feed a feature at scoring date `as_of` only if BOTH its event date and its
recorded_at are on or before as_of. Late-recorded rows remain valid history; only their inclusion in
an earlier feature snapshot is rejected (FUTURE_LEAK). A missing survey answer may never be turned
into a low engagement value (MISSING_AS_NEGATIVE).
"""
from __future__ import annotations

import datetime as dt


def _as_date(v) -> dt.date:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def is_available(event_date, recorded_at, as_of) -> bool:
    """True if the row was both effective and recorded by the end of as_of (UTC dates)."""
    a = _as_date(as_of)
    return _as_date(event_date) <= a and _as_date(recorded_at) <= a


def audit_feature_source(event_date, recorded_at, as_of) -> str | None:
    """Return 'FUTURE_LEAK' if a feature snapshot for as_of includes this source row illegally."""
    return None if is_available(event_date, recorded_at, as_of) else "FUTURE_LEAK"


def audit_engagement_feature(survey_status: str | None, engagement_feature) -> str | None:
    """Return 'MISSING_AS_NEGATIVE' if an engagement feature value exists without an answered survey
    (fewer than 3 items answered means there is no engagement index to use)."""
    has_index = survey_status in ("complete", "partial")
    if engagement_feature is not None and not has_index:
        return "MISSING_AS_NEGATIVE"
    return None
