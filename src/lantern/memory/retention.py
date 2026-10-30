"""Deterministic, inspectable retention planning for Lantern recovery points."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Sequence


DEFAULT_DAILY_POINTS = 7
DEFAULT_WEEKLY_POINTS = 4
DEFAULT_MONTHLY_POINTS = 3
MAX_CONTENT_RETENTION_DAYS = 93

RetentionAction = Literal["keep", "prune", "review"]


@dataclass(frozen=True, slots=True)
class RecoveryPoint:
    """Minimum verified metadata needed to plan encrypted-backup retention."""

    point_id: str
    created_at: datetime
    verified: bool = True


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    point_id: str
    created_at: datetime
    action: RetentionAction
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    evaluated_at: datetime
    decisions: tuple[RetentionDecision, ...]

    @property
    def keep_ids(self) -> tuple[str, ...]:
        return tuple(
            decision.point_id
            for decision in self.decisions
            if decision.action == "keep"
        )

    @property
    def prune_ids(self) -> tuple[str, ...]:
        return tuple(
            decision.point_id
            for decision in self.decisions
            if decision.action == "prune"
        )

    @property
    def review_ids(self) -> tuple[str, ...]:
        return tuple(
            decision.point_id
            for decision in self.decisions
            if decision.action == "review"
        )


def _require_aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _require_nonnegative(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def plan_backup_retention(
    recovery_points: Sequence[RecoveryPoint],
    *,
    now: datetime,
    keep_daily: int = DEFAULT_DAILY_POINTS,
    keep_weekly: int = DEFAULT_WEEKLY_POINTS,
    keep_monthly: int = DEFAULT_MONTHLY_POINTS,
    max_content_age_days: int = MAX_CONTENT_RETENTION_DAYS,
) -> RetentionPlan:
    """Return a deterministic plan without deleting any recovery point.

    The newest verified point in each selected UTC day, ISO week, and UTC
    month is retained. Selected bucket sets are combined. Points beyond the
    content-age ceiling cannot be retained by a cadence bucket, although the
    newest verified point is always protected so a retention run cannot erase
    the final known-good recovery point. Unverified and future-dated points
    require review and are never automatic prune candidates.
    """

    _require_nonnegative(keep_daily, "keep_daily")
    _require_nonnegative(keep_weekly, "keep_weekly")
    _require_nonnegative(keep_monthly, "keep_monthly")
    _require_nonnegative(max_content_age_days, "max_content_age_days")
    evaluated_at = _require_aware_utc(now, "now")
    ceiling = timedelta(days=max_content_age_days)

    normalized: list[RecoveryPoint] = []
    seen_ids: set[str] = set()
    for point in recovery_points:
        point_id = point.point_id.strip()
        if not point_id:
            raise ValueError("recovery point IDs must not be empty")
        if point_id in seen_ids:
            raise ValueError(f"duplicate recovery point ID: {point_id}")
        seen_ids.add(point_id)
        normalized.append(
            RecoveryPoint(
                point_id=point_id,
                created_at=_require_aware_utc(
                    point.created_at, f"created_at for {point_id}"
                ),
                verified=point.verified,
            )
        )

    ordered = sorted(
        normalized,
        key=lambda point: (point.created_at, point.point_id),
        reverse=True,
    )
    eligible = [
        point
        for point in ordered
        if point.verified
        and point.created_at <= evaluated_at
        and evaluated_at - point.created_at <= ceiling
    ]
    verified_not_future = [
        point
        for point in ordered
        if point.verified and point.created_at <= evaluated_at
    ]
    newest_verified_id = (
        verified_not_future[0].point_id if verified_not_future else None
    )

    reasons_by_id: dict[str, set[str]] = {}

    def retain_by_bucket(
        limit: int,
        reason: str,
        bucket,
    ) -> None:
        selected_buckets: set[object] = set()
        for point in eligible:
            value = bucket(point.created_at)
            if value in selected_buckets:
                continue
            if len(selected_buckets) >= limit:
                break
            selected_buckets.add(value)
            reasons_by_id.setdefault(point.point_id, set()).add(reason)

    retain_by_bucket(
        keep_daily,
        "daily",
        lambda created: created.date(),
    )
    retain_by_bucket(
        keep_weekly,
        "weekly",
        lambda created: created.isocalendar()[:2],
    )
    retain_by_bucket(
        keep_monthly,
        "monthly",
        lambda created: (created.year, created.month),
    )

    if newest_verified_id is not None:
        reasons_by_id.setdefault(newest_verified_id, set()).add(
            "newest_verified"
        )

    decisions: list[RetentionDecision] = []
    for point in ordered:
        reasons = reasons_by_id.get(point.point_id, set())
        if not point.verified:
            action: RetentionAction = "review"
            reasons = {"unverified"}
        elif point.created_at > evaluated_at:
            action = "review"
            reasons = {"future_timestamp"}
        elif point.point_id == newest_verified_id:
            action = "keep"
        elif evaluated_at - point.created_at > ceiling:
            action = "prune"
            reasons = {"content_age_ceiling"}
        elif reasons:
            action = "keep"
        else:
            action = "prune"
            reasons = {"outside_retention_buckets"}

        decisions.append(
            RetentionDecision(
                point_id=point.point_id,
                created_at=point.created_at,
                action=action,
                reasons=tuple(sorted(reasons)),
            )
        )

    return RetentionPlan(
        evaluated_at=evaluated_at,
        decisions=tuple(decisions),
    )
