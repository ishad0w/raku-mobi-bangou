#!/usr/bin/env python3
"""Maintain a history-aware phone lifecycle and its legacy catalog view."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence


PHONE_RE = re.compile(r"0[0-9]{10}")
MASK_RE = re.compile(r"[0-9]{4}")
ID_RE = re.compile(r"[0-9]+")
RUN_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

MISS_THRESHOLD = 3
RETENTION_PERIOD = timedelta(days=5)
MIN_NEGATIVE_LOG_LIKELIHOOD = math.log(10_000.0)
EVIDENCE_MODEL_VERSION = 1
WILSON_Z = 1.959963984540054
PLANNING_COVERAGE_FLOOR_BPS = 9_900
WARM_NO_PROGRESS_SAMPLE_LIMIT = 44

RUN_KINDS = {"scheduled_full", "manual_full", "manual_specialized"}
LIFECYCLE_STATUSES = {
    "retained",
    "possibly_unavailable",
    "statistically_stale",
    "confirmed_unavailable",
    "legacy_history_unknown",
}
CATALOG_INCLUDED_STATUSES = {"retained", "possibly_unavailable"}
COVERAGE_POOL_STATUSES = {"retained", "possibly_unavailable"}
TOMBSTONE_STATUSES = {
    "statistically_stale",
    "confirmed_unavailable",
}
PROVENANCE_VALUES = {"native", "legacy_catalog", "legacy_history"}

CATALOG_FIELDS = (
    "phoneNumber",
    "id",
    "sourceMask",
    "firstSeenAt",
    "lastSeenAt",
    "lastCheckedAt",
    "seenRuns",
    "consecutiveComparableMisses",
    "status",
)
COVERAGE_POOL_FIELDS = ("phoneNumber", "sourceMask")
LIFECYCLE_FIELDS = (
    "phoneNumber",
    "id",
    "sourceMask",
    "firstSeenAt",
    "lastSeenAt",
    "lastCheckedAt",
    "lastObservedRunKey",
    "seenRuns",
    "seenQualifiedDays",
    "resolvedSamplingMissDays",
    "consecutiveQualifiedMissDays",
    "lastQualifiedMissDate",
    "negativeLogMissLikelihood",
    "status",
    "statusChangedAt",
    "tombstonedAt",
    "tombstoneReason",
    "resurrectionCount",
    "lastResurrectedAt",
    "legacyComparableMisses",
    "provenance",
    "evidenceModelVersion",
)
MASK_SUMMARY_FIELDS = (
    "mask",
    "historicalDistinctAtStart",
    "coveragePoolAtStart",
    "successfulResponses",
    "httpRequests",
    "retries",
    "responsePhoneSamples",
    "emptyResponses",
    "observedPhoneCount",
    "observedKnownPhoneCount",
    "observedNewPhoneCount",
    "targetCoverageBps",
    "planningCoverageBps",
    "achievedCoverageBps",
    "estimatedRequestBudget",
    "requestCap",
    "roundLimit",
    "stopReason",
    "comparable",
)
MASK_DAY_FIELDS = (
    "evidenceDate",
    "mask",
    "runKey",
    "runKind",
    "historicalDistinctAtStart",
    "coveragePoolAtStart",
    "successfulResponses",
    "observedPhoneCount",
    "observedKnownPhoneCount",
    "stopReason",
    "comparable",
    "qualified",
    "qualificationReason",
    "inclusionLowerBound",
    "recordedAt",
    "evidenceModelVersion",
)
EVENT_FIELDS = (
    "eventId",
    "eventAt",
    "evidenceDate",
    "runKey",
    "phoneNumber",
    "id",
    "sourceMask",
    "eventType",
    "fromStatus",
    "toStatus",
    "reason",
    "evidenceModelVersion",
)
STOP_REASONS = {
    "coverage_target",
    "empty_probe_limit",
    "cold_start_saturated",
    "sampling_saturated",
    "request_cap",
    "round_limit",
    "global_request_limit",
    "collection_fatal",
}
EVENT_TYPES = {
    "added",
    "status_changed",
    "tombstoned",
    "reappeared",
    "identity_changed",
}


class CatalogError(ValueError):
    """Catalog inputs or outputs are inconsistent."""


@dataclass(frozen=True)
class CatalogRecord:
    phone_number: str
    offer_id: str
    source_mask: str
    first_seen_at: datetime
    last_seen_at: datetime
    last_checked_at: datetime
    seen_runs: int
    consecutive_misses: int
    status: str

    def row(self) -> tuple[str, ...]:
        return (
            self.phone_number,
            self.offer_id,
            self.source_mask,
            format_timestamp(self.first_seen_at),
            format_timestamp(self.last_seen_at),
            format_timestamp(self.last_checked_at),
            str(self.seen_runs),
            str(self.consecutive_misses),
            self.status,
        )


@dataclass(frozen=True)
class LifecycleRecord:
    phone_number: str
    offer_id: str
    source_mask: str
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    last_checked_at: datetime | None
    last_observed_run_key: str
    seen_runs: int
    seen_qualified_days: int
    resolved_sampling_miss_days: int
    consecutive_qualified_miss_days: int
    last_qualified_miss_date: date | None
    negative_log_miss_likelihood: float
    status: str
    status_changed_at: datetime | None
    tombstoned_at: datetime | None
    tombstone_reason: str
    resurrection_count: int
    last_resurrected_at: datetime | None
    legacy_comparable_misses: int
    provenance: str
    evidence_model_version: int

    def row(self) -> tuple[str, ...]:
        return (
            self.phone_number,
            self.offer_id,
            self.source_mask,
            optional_timestamp(self.first_seen_at),
            optional_timestamp(self.last_seen_at),
            optional_timestamp(self.last_checked_at),
            self.last_observed_run_key,
            str(self.seen_runs),
            str(self.seen_qualified_days),
            str(self.resolved_sampling_miss_days),
            str(self.consecutive_qualified_miss_days),
            optional_date(self.last_qualified_miss_date),
            format_score(self.negative_log_miss_likelihood),
            self.status,
            optional_timestamp(self.status_changed_at),
            optional_timestamp(self.tombstoned_at),
            self.tombstone_reason,
            str(self.resurrection_count),
            optional_timestamp(self.last_resurrected_at),
            str(self.legacy_comparable_misses),
            self.provenance,
            str(self.evidence_model_version),
        )

    def catalog_record(self) -> CatalogRecord | None:
        if self.status not in CATALOG_INCLUDED_STATUSES:
            return None
        if (
            self.first_seen_at is None
            or self.last_seen_at is None
            or self.last_checked_at is None
            or self.seen_runs < 1
        ):
            raise CatalogError(
                f"lifecycle phone {self.phone_number}: active view lacks history"
            )
        return CatalogRecord(
            phone_number=self.phone_number,
            offer_id=self.offer_id,
            source_mask=self.source_mask,
            first_seen_at=self.first_seen_at,
            last_seen_at=self.last_seen_at,
            last_checked_at=self.last_checked_at,
            seen_runs=self.seen_runs,
            consecutive_misses=self.consecutive_qualified_miss_days,
            status=(
                "possibly_unavailable"
                if self.status == "possibly_unavailable"
                else "active"
            ),
        )


@dataclass(frozen=True)
class MaskEvidence:
    mask: str
    historical_distinct_at_start: int
    coverage_pool_at_start: int
    successful_responses: int
    http_requests: int
    retries: int
    response_phone_samples: int
    empty_responses: int
    observed_phone_count: int
    observed_known_phone_count: int
    observed_new_phone_count: int
    target_coverage_bps: int
    planning_coverage_bps: int
    achieved_coverage_bps: int | None
    estimated_request_budget: int
    request_cap: int
    round_limit: int
    stop_reason: str
    comparable: bool


def parse_timestamp(value: str, label: str) -> datetime:
    if not TIMESTAMP_RE.fullmatch(value):
        raise CatalogError(f"{label}: timestamp must use UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise CatalogError(f"{label}: invalid timestamp") from exc


def parse_optional_timestamp(value: str, label: str) -> datetime | None:
    return None if value == "" else parse_timestamp(value, label)


def format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise CatalogError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def optional_timestamp(value: datetime | None) -> str:
    return "" if value is None else format_timestamp(value)


def parse_date(value: str, label: str) -> date:
    if not DATE_RE.fullmatch(value):
        raise CatalogError(f"{label}: date must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CatalogError(f"{label}: invalid date") from exc


def parse_optional_date(value: str, label: str) -> date | None:
    return None if value == "" else parse_date(value, label)


def optional_date(value: date | None) -> str:
    return "" if value is None else value.isoformat()


def parse_nonnegative_int(value: str, label: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise CatalogError(f"{label}: expected a non-negative integer")
    return int(value)


def parse_positive_int(value: str, label: str) -> int:
    parsed = parse_nonnegative_int(value, label)
    if parsed < 1:
        raise CatalogError(f"{label}: expected a positive integer")
    return parsed


def parse_score(value: str, label: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise CatalogError(f"{label}: invalid score") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise CatalogError(f"{label}: score must be finite and non-negative")
    return parsed


def format_score(value: float) -> str:
    if not math.isfinite(value) or value < 0:
        raise CatalogError("score must be finite and non-negative")
    return f"{value:.12f}"


def validate_identity(phone: str, offer_id: str, mask: str, label: str) -> None:
    if (
        not PHONE_RE.fullmatch(phone)
        or not ID_RE.fullmatch(offer_id)
        or not MASK_RE.fullmatch(mask)
        or not phone.endswith(mask)
    ):
        raise CatalogError(f"{label}: invalid phone number, id, or source mask")


def read_dict_rows(path: Path, fields: Sequence[str], label: str) -> list[dict[str, str]]:
    try:
        source = path.open("r", encoding="utf-8", newline="")
    except OSError as exc:
        raise CatalogError(f"cannot read {label} {path}: {exc}") from exc
    with source:
        reader = csv.DictReader(source)
        if reader.fieldnames != list(fields):
            raise CatalogError(f"{path}: unsupported {label} CSV header")
        rows: list[dict[str, str]] = []
        for line_number, row in enumerate(reader, start=2):
            if None in row or set(row) != set(fields):
                raise CatalogError(f"{path}:{line_number}: malformed {label} row")
            rows.append(dict(row))
        return rows


def load_current(path: Path) -> dict[str, tuple[str, str]]:
    records: dict[str, tuple[str, str]] = {}
    id_owners: dict[str, str] = {}
    for line_number, row in enumerate(
        read_dict_rows(path, ("phoneNumber", "id", "sourceMask"), "current snapshot"),
        start=2,
    ):
        phone, offer_id, mask = row["phoneNumber"], row["id"], row["sourceMask"]
        validate_identity(phone, offer_id, mask, f"{path}:{line_number}")
        if phone in records:
            raise CatalogError(f"{path}:{line_number}: duplicate phone number")
        owner = id_owners.setdefault(offer_id, phone)
        if owner != phone:
            raise CatalogError(f"{path}:{line_number}: id belongs to another phone")
        records[phone] = (offer_id, mask)
    return records


def load_coverage_pool(path: Path) -> dict[str, str]:
    records: dict[str, str] = {}
    for line_number, row in enumerate(
        read_dict_rows(path, COVERAGE_POOL_FIELDS, "coverage pool"), start=2
    ):
        label = f"{path}:{line_number}"
        phone = row["phoneNumber"]
        mask = row["sourceMask"]
        if (
            not PHONE_RE.fullmatch(phone)
            or not MASK_RE.fullmatch(mask)
            or not phone.endswith(mask)
        ):
            raise CatalogError(f"{label}: invalid coverage-pool identity")
        if phone in records:
            raise CatalogError(f"{label}: duplicate coverage-pool phone")
        records[phone] = mask
    return records


def load_catalog(path: Path | None) -> dict[str, CatalogRecord]:
    if path is None:
        return {}
    records: dict[str, CatalogRecord] = {}
    id_owners: dict[str, str] = {}
    for line_number, row in enumerate(
        read_dict_rows(path, CATALOG_FIELDS, "catalog"), start=2
    ):
        label = f"{path}:{line_number}"
        phone, offer_id, mask = row["phoneNumber"], row["id"], row["sourceMask"]
        validate_identity(phone, offer_id, mask, label)
        first = parse_timestamp(row["firstSeenAt"], label)
        last = parse_timestamp(row["lastSeenAt"], label)
        checked = parse_timestamp(row["lastCheckedAt"], label)
        seen_runs = parse_positive_int(row["seenRuns"], label)
        misses = parse_nonnegative_int(row["consecutiveComparableMisses"], label)
        expected = "possibly_unavailable" if misses >= MISS_THRESHOLD else "active"
        if row["status"] != expected or not first <= last <= checked:
            raise CatalogError(f"{label}: inconsistent catalog lifecycle")
        if phone in records:
            raise CatalogError(f"{label}: duplicate phone number")
        owner = id_owners.setdefault(offer_id, phone)
        if owner != phone:
            raise CatalogError(f"{label}: id belongs to another phone")
        records[phone] = CatalogRecord(
            phone, offer_id, mask, first, last, checked, seen_runs, misses, expected
        )
    return records


def validate_lifecycle_record(record: LifecycleRecord, label: str) -> None:
    validate_identity(record.phone_number, record.offer_id, record.source_mask, label)
    if record.status not in LIFECYCLE_STATUSES:
        raise CatalogError(f"{label}: invalid lifecycle status")
    if record.provenance not in PROVENANCE_VALUES:
        raise CatalogError(f"{label}: invalid lifecycle provenance")
    if record.evidence_model_version != EVIDENCE_MODEL_VERSION:
        raise CatalogError(f"{label}: unsupported evidence model")
    if record.last_observed_run_key and not RUN_KEY_RE.fullmatch(
        record.last_observed_run_key
    ):
        raise CatalogError(f"{label}: invalid last observed run key")
    if record.consecutive_qualified_miss_days == 0:
        if record.last_qualified_miss_date is not None:
            raise CatalogError(f"{label}: miss date without a miss streak")
        if record.negative_log_miss_likelihood != 0:
            raise CatalogError(f"{label}: likelihood without a miss streak")
    elif record.last_qualified_miss_date is None:
        raise CatalogError(f"{label}: miss streak lacks its last date")
    known_dates = [
        value
        for value in (
            record.first_seen_at,
            record.last_seen_at,
            record.last_checked_at,
        )
        if value is not None
    ]
    if known_dates and len(known_dates) != 3:
        raise CatalogError(f"{label}: observation timestamps must be all present")
    if len(known_dates) == 3 and not known_dates[0] <= known_dates[1] <= known_dates[2]:
        raise CatalogError(f"{label}: lifecycle timestamps are out of order")
    if record.status in CATALOG_INCLUDED_STATUSES and (
        len(known_dates) != 3 or record.seen_runs < 1
    ):
        raise CatalogError(f"{label}: catalog-eligible lifecycle lacks observations")
    if record.status == "possibly_unavailable" and (
        record.consecutive_qualified_miss_days < MISS_THRESHOLD
    ):
        raise CatalogError(f"{label}: possible status lacks qualified misses")
    if record.status == "retained" and (
        record.consecutive_qualified_miss_days >= MISS_THRESHOLD
    ):
        raise CatalogError(f"{label}: retained status contradicts miss streak")
    if record.status in {"statistically_stale", "confirmed_unavailable"}:
        if record.tombstoned_at is None or not record.tombstone_reason:
            raise CatalogError(f"{label}: tombstone metadata is missing")
    elif record.tombstoned_at is not None or record.tombstone_reason:
        raise CatalogError(f"{label}: non-tombstone has tombstone metadata")
    if record.resurrection_count == 0 and record.last_resurrected_at is not None:
        raise CatalogError(f"{label}: resurrection timestamp without count")
    if record.resurrection_count > 0 and record.last_resurrected_at is None:
        raise CatalogError(f"{label}: resurrection count lacks timestamp")
    if record.status == "legacy_history_unknown" and record.provenance != "legacy_history":
        raise CatalogError(f"{label}: legacy history status has wrong provenance")
    if record.status == "statistically_stale":
        if (
            record.consecutive_qualified_miss_days < 5
            or record.negative_log_miss_likelihood < MIN_NEGATIVE_LOG_LIKELIHOOD
            or record.last_seen_at is None
            or record.tombstoned_at is None
            or record.tombstoned_at - record.last_seen_at < RETENTION_PERIOD
        ):
            raise CatalogError(f"{label}: statistical tombstone lacks required evidence")


def load_lifecycle(path: Path | None) -> dict[str, LifecycleRecord]:
    if path is None:
        return {}
    records: dict[str, LifecycleRecord] = {}
    id_owners: dict[str, str] = {}
    for line_number, row in enumerate(
        read_dict_rows(path, LIFECYCLE_FIELDS, "lifecycle"), start=2
    ):
        label = f"{path}:{line_number}"
        record = LifecycleRecord(
            phone_number=row["phoneNumber"],
            offer_id=row["id"],
            source_mask=row["sourceMask"],
            first_seen_at=parse_optional_timestamp(row["firstSeenAt"], label),
            last_seen_at=parse_optional_timestamp(row["lastSeenAt"], label),
            last_checked_at=parse_optional_timestamp(row["lastCheckedAt"], label),
            last_observed_run_key=row["lastObservedRunKey"],
            seen_runs=parse_nonnegative_int(row["seenRuns"], label),
            seen_qualified_days=parse_nonnegative_int(row["seenQualifiedDays"], label),
            resolved_sampling_miss_days=parse_nonnegative_int(
                row["resolvedSamplingMissDays"], label
            ),
            consecutive_qualified_miss_days=parse_nonnegative_int(
                row["consecutiveQualifiedMissDays"], label
            ),
            last_qualified_miss_date=parse_optional_date(
                row["lastQualifiedMissDate"], label
            ),
            negative_log_miss_likelihood=parse_score(
                row["negativeLogMissLikelihood"], label
            ),
            status=row["status"],
            status_changed_at=parse_optional_timestamp(row["statusChangedAt"], label),
            tombstoned_at=parse_optional_timestamp(row["tombstonedAt"], label),
            tombstone_reason=row["tombstoneReason"],
            resurrection_count=parse_nonnegative_int(row["resurrectionCount"], label),
            last_resurrected_at=parse_optional_timestamp(
                row["lastResurrectedAt"], label
            ),
            legacy_comparable_misses=parse_nonnegative_int(
                row["legacyComparableMisses"], label
            ),
            provenance=row["provenance"],
            evidence_model_version=parse_positive_int(
                row["evidenceModelVersion"], label
            ),
        )
        validate_lifecycle_record(record, label)
        if record.phone_number in records:
            raise CatalogError(f"{label}: duplicate lifecycle phone")
        owner = id_owners.setdefault(record.offer_id, record.phone_number)
        if owner != record.phone_number:
            raise CatalogError(f"{label}: id belongs to another lifecycle phone")
        records[record.phone_number] = record
    return records


def _expected_comparable(evidence: MaskEvidence) -> bool:
    nonempty_responses = evidence.successful_responses - evidence.empty_responses
    target_count = (
        evidence.coverage_pool_at_start * evidence.target_coverage_bps + 9_999
    ) // 10_000
    coverage_stop = (
        evidence.stop_reason == "coverage_target"
        and evidence.successful_responses >= 5
        and evidence.observed_known_phone_count >= target_count
    )
    request_cap_stop = (
        evidence.stop_reason == "request_cap"
        and evidence.estimated_request_budget <= evidence.round_limit
        and evidence.request_cap == evidence.estimated_request_budget
        and evidence.successful_responses == evidence.request_cap
    )
    empty_probe_stop = (
        evidence.stop_reason == "empty_probe_limit"
        and evidence.successful_responses == 5
        and evidence.empty_responses == 5
    )
    return (
        evidence.coverage_pool_at_start > 0
        and (
            empty_probe_stop
            or (nonempty_responses > 0 and (coverage_stop or request_cap_stop))
        )
    )


def load_mask_summary(path: Path) -> tuple[list[str], dict[str, MaskEvidence]]:
    masks: list[str] = []
    evidence_by_mask: dict[str, MaskEvidence] = {}
    for line_number, row in enumerate(
        read_dict_rows(path, MASK_SUMMARY_FIELDS, "mask summary"), start=2
    ):
        label = f"{path}:{line_number}"
        mask = row["mask"]
        if not MASK_RE.fullmatch(mask) or mask in evidence_by_mask:
            raise CatalogError(f"{label}: invalid or duplicate mask")
        integer_names = (
            "historicalDistinctAtStart",
            "coveragePoolAtStart",
            "successfulResponses",
            "httpRequests",
            "retries",
            "responsePhoneSamples",
            "emptyResponses",
            "observedPhoneCount",
            "observedKnownPhoneCount",
            "observedNewPhoneCount",
            "targetCoverageBps",
            "planningCoverageBps",
            "estimatedRequestBudget",
            "requestCap",
            "roundLimit",
        )
        values = {name: parse_nonnegative_int(row[name], label) for name in integer_names}
        if row["comparable"] not in {"true", "false"}:
            raise CatalogError(f"{label}: invalid comparable flag")
        if row["stopReason"] not in STOP_REASONS:
            raise CatalogError(f"{label}: invalid stop reason")
        coverage_pool = values["coveragePoolAtStart"]
        achieved_raw = row["achievedCoverageBps"]
        if coverage_pool == 0:
            if achieved_raw != "":
                raise CatalogError(f"{label}: zero pool requires blank achieved coverage")
            achieved = None
        else:
            achieved = parse_nonnegative_int(achieved_raw, label)
            expected_achieved = (
                10_000 * values["observedKnownPhoneCount"] // coverage_pool
            )
            if achieved != expected_achieved:
                raise CatalogError(f"{label}: achieved coverage arithmetic mismatch")
        evidence = MaskEvidence(
            mask=mask,
            historical_distinct_at_start=values["historicalDistinctAtStart"],
            coverage_pool_at_start=coverage_pool,
            successful_responses=values["successfulResponses"],
            http_requests=values["httpRequests"],
            retries=values["retries"],
            response_phone_samples=values["responsePhoneSamples"],
            empty_responses=values["emptyResponses"],
            observed_phone_count=values["observedPhoneCount"],
            observed_known_phone_count=values["observedKnownPhoneCount"],
            observed_new_phone_count=values["observedNewPhoneCount"],
            target_coverage_bps=values["targetCoverageBps"],
            planning_coverage_bps=values["planningCoverageBps"],
            achieved_coverage_bps=achieved,
            estimated_request_budget=values["estimatedRequestBudget"],
            request_cap=values["requestCap"],
            round_limit=values["roundLimit"],
            stop_reason=row["stopReason"],
            comparable=row["comparable"] == "true",
        )
        arithmetic_valid = (
            evidence.coverage_pool_at_start <= evidence.historical_distinct_at_start
            and evidence.http_requests
            == evidence.successful_responses + evidence.retries
            and evidence.empty_responses <= evidence.successful_responses
            and evidence.observed_phone_count
            == evidence.observed_known_phone_count + evidence.observed_new_phone_count
            and evidence.observed_known_phone_count <= evidence.coverage_pool_at_start
            and evidence.response_phone_samples >= evidence.observed_phone_count
            and 0 < evidence.target_coverage_bps < 10_000
            and evidence.planning_coverage_bps
            == max(
                evidence.target_coverage_bps,
                PLANNING_COVERAGE_FLOOR_BPS,
            )
            and evidence.round_limit > 0
            and evidence.request_cap
            == min(evidence.estimated_request_budget, evidence.round_limit)
            and (
                evidence.stop_reason != "sampling_saturated"
                or (
                    evidence.coverage_pool_at_start > 0
                    and evidence.successful_responses >= 5
                    and evidence.response_phone_samples
                    >= WARM_NO_PROGRESS_SAMPLE_LIMIT
                )
            )
        )
        if not arithmetic_valid:
            raise CatalogError(f"{label}: inconsistent mask summary arithmetic")
        if evidence.comparable != _expected_comparable(evidence):
            raise CatalogError(f"{label}: comparable flag contradicts strict evidence")
        masks.append(mask)
        evidence_by_mask[mask] = evidence
    if not masks:
        raise CatalogError(f"{path}: no mask rows")
    return masks, evidence_by_mask


def wilson_lower(successes: int, trials: int) -> float:
    if trials <= 0 or successes <= 0:
        return 0.0
    if successes > trials:
        raise CatalogError("Wilson successes exceed trials")
    proportion = successes / trials
    z2 = WILSON_Z * WILSON_Z
    denominator = 1.0 + z2 / trials
    center = proportion + z2 / (2.0 * trials)
    margin = WILSON_Z * math.sqrt(
        proportion * (1.0 - proportion) / trials + z2 / (4.0 * trials * trials)
    )
    return max(0.0, min(1.0, (center - margin) / denominator))


def evidence_inclusion_lower(evidence: MaskEvidence) -> float:
    """Return the conservative per-phone detection bound for a qualified day."""
    if (
        evidence.stop_reason == "empty_probe_limit"
        and evidence.successful_responses == 5
        and evidence.empty_responses == 5
    ):
        # Five independent availability probes all returned an empty candidate
        # set. For a warm mask this is whole-pool evidence, not a random
        # three-number sample, so use the collector's conservative target.
        return evidence.target_coverage_bps / 10_000
    return wilson_lower(
        evidence.observed_known_phone_count,
        evidence.coverage_pool_at_start,
    )


def load_mask_days(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    rows = read_dict_rows(path, MASK_DAY_FIELDS, "mask days")
    seen_run_masks: set[tuple[str, str]] = set()
    qualified_days: set[tuple[str, str]] = set()
    for line_number, row in enumerate(rows, start=2):
        label = f"{path}:{line_number}"
        parse_date(row["evidenceDate"], label)
        parse_timestamp(row["recordedAt"], label)
        if not MASK_RE.fullmatch(row["mask"]) or not RUN_KEY_RE.fullmatch(row["runKey"]):
            raise CatalogError(f"{label}: invalid mask-day identity")
        if row["runKind"] not in RUN_KINDS:
            raise CatalogError(f"{label}: invalid run kind")
        if row["comparable"] not in {"true", "false"} or row["qualified"] not in {
            "true",
            "false",
        }:
            raise CatalogError(f"{label}: invalid mask-day boolean")
        if row["stopReason"] not in STOP_REASONS:
            raise CatalogError(f"{label}: invalid mask-day stop reason")
        inclusion = parse_score(row["inclusionLowerBound"], label)
        if inclusion > 1:
            raise CatalogError(f"{label}: inclusion lower bound exceeds one")
        if row["qualified"] == "true" and not (
            row["runKind"] == "scheduled_full"
            and row["comparable"] == "true"
            and row["qualificationReason"] == "scheduled_full_comparable"
        ):
            raise CatalogError(f"{label}: qualified mask-day lacks strict evidence")
        if parse_positive_int(row["evidenceModelVersion"], label) != EVIDENCE_MODEL_VERSION:
            raise CatalogError(f"{label}: unsupported mask-day evidence model")
        key = (row["runKey"], row["mask"])
        if key in seen_run_masks:
            raise CatalogError(f"{label}: duplicate run/mask evidence")
        seen_run_masks.add(key)
        if row["qualified"] == "true":
            day_key = (row["evidenceDate"], row["mask"])
            if day_key in qualified_days:
                raise CatalogError(f"{label}: duplicate qualified mask/day")
            qualified_days.add(day_key)
    return rows


def load_events(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    rows = read_dict_rows(path, EVENT_FIELDS, "lifecycle events")
    seen: set[str] = set()
    for line_number, row in enumerate(rows, start=2):
        label = f"{path}:{line_number}"
        if not re.fullmatch(r"[0-9a-f]{64}", row["eventId"]):
            raise CatalogError(f"{label}: invalid event id")
        if row["eventId"] in seen:
            raise CatalogError(f"{label}: duplicate event id")
        seen.add(row["eventId"])
        parse_timestamp(row["eventAt"], label)
        parse_date(row["evidenceDate"], label)
        if not RUN_KEY_RE.fullmatch(row["runKey"]):
            raise CatalogError(f"{label}: invalid event run key")
        validate_identity(row["phoneNumber"], row["id"], row["sourceMask"], label)
        if row["eventType"] not in EVENT_TYPES:
            raise CatalogError(f"{label}: invalid event type")
        if row["fromStatus"] and row["fromStatus"] not in LIFECYCLE_STATUSES:
            raise CatalogError(f"{label}: invalid event source status")
        if row["toStatus"] and row["toStatus"] not in LIFECYCLE_STATUSES:
            raise CatalogError(f"{label}: invalid event target status")
        expected_event_id = lifecycle_event_id(
            row["runKey"],
            row["phoneNumber"],
            row["eventType"],
            row["fromStatus"],
            row["toStatus"],
        )
        if row["eventId"] != expected_event_id:
            raise CatalogError(f"{label}: event id does not match its fields")
        if parse_positive_int(row["evidenceModelVersion"], label) != EVIDENCE_MODEL_VERSION:
            raise CatalogError(f"{label}: unsupported event evidence model")
    return rows


def write_csv_atomic(
    path: Path, fields: Sequence[str], rows: Iterable[Sequence[str]]
) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(fields)
            writer.writerows(rows)
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise CatalogError(f"cannot write {path}: {exc}") from exc


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise CatalogError(f"cannot write {path}: {exc}") from exc


def lifecycle_event_id(
    run_key: str,
    phone_number: str,
    event_type: str,
    from_status: str,
    to_status: str,
) -> str:
    identity = "|".join(
        (run_key, phone_number, event_type, from_status, to_status)
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _event(
    *,
    observed_at: datetime,
    evidence_date: date,
    run_key: str,
    record: LifecycleRecord,
    event_type: str,
    from_status: str,
    to_status: str,
    reason: str,
) -> dict[str, str]:
    return {
        "eventId": lifecycle_event_id(
            run_key,
            record.phone_number,
            event_type,
            from_status,
            to_status,
        ),
        "eventAt": format_timestamp(observed_at),
        "evidenceDate": evidence_date.isoformat(),
        "runKey": run_key,
        "phoneNumber": record.phone_number,
        "id": record.offer_id,
        "sourceMask": record.source_mask,
        "eventType": event_type,
        "fromStatus": from_status,
        "toStatus": to_status,
        "reason": reason,
        "evidenceModelVersion": str(EVIDENCE_MODEL_VERSION),
    }


def _dict_row(fields: Sequence[str], row: dict[str, str]) -> tuple[str, ...]:
    return tuple(row[field] for field in fields)


def _mask_day_row(
    *,
    evidence: MaskEvidence,
    evidence_date: date,
    run_key: str,
    run_kind: str,
    qualified: bool,
    reason: str,
    inclusion_lower: float,
    observed_at: datetime,
) -> dict[str, str]:
    return {
        "evidenceDate": evidence_date.isoformat(),
        "mask": evidence.mask,
        "runKey": run_key,
        "runKind": run_kind,
        "historicalDistinctAtStart": str(evidence.historical_distinct_at_start),
        "coveragePoolAtStart": str(evidence.coverage_pool_at_start),
        "successfulResponses": str(evidence.successful_responses),
        "observedPhoneCount": str(evidence.observed_phone_count),
        "observedKnownPhoneCount": str(evidence.observed_known_phone_count),
        "stopReason": evidence.stop_reason,
        "comparable": "true" if evidence.comparable else "false",
        "qualified": "true" if qualified else "false",
        "qualificationReason": reason,
        "inclusionLowerBound": format_score(inclusion_lower),
        "recordedAt": format_timestamp(observed_at),
        "evidenceModelVersion": str(EVIDENCE_MODEL_VERSION),
    }


def update_catalog(
    *,
    current_path: Path,
    coverage_pool_path: Path,
    previous_path: Path | None,
    previous_lifecycle_path: Path | None,
    previous_mask_days_path: Path | None,
    previous_events_path: Path | None,
    mask_summary_path: Path,
    observed_at: datetime,
    evidence_date: date,
    run_key: str,
    run_kind: str,
    catalog_output: Path,
    lifecycle_output: Path,
    mask_days_output: Path,
    events_output: Path,
    summary_output: Path,
) -> dict[str, object]:
    if observed_at.tzinfo is None:
        raise CatalogError("observed_at must be timezone-aware")
    observed_at = observed_at.astimezone(timezone.utc).replace(microsecond=0)
    if run_kind not in RUN_KINDS or not RUN_KEY_RE.fullmatch(run_key):
        raise CatalogError("invalid run kind or run key")
    previous_inputs = (
        previous_path,
        previous_lifecycle_path,
        previous_mask_days_path,
        previous_events_path,
    )
    if any(path is None for path in previous_inputs) and any(
        path is not None for path in previous_inputs
    ):
        raise CatalogError("previous catalog and lifecycle ledgers must be supplied together")
    output_paths = {
        path.resolve()
        for path in (
            catalog_output,
            lifecycle_output,
            mask_days_output,
            events_output,
            summary_output,
        )
    }
    if len(output_paths) != 5:
        raise CatalogError("catalog output paths must be distinct")
    input_paths = {
        current_path.resolve(),
        coverage_pool_path.resolve(),
        mask_summary_path.resolve(),
    }
    input_paths.update(
        path.resolve()
        for path in (
            previous_path,
            previous_lifecycle_path,
            previous_mask_days_path,
            previous_events_path,
        )
        if path is not None
    )
    if output_paths & input_paths:
        raise CatalogError("catalog outputs must not overwrite an input file")

    current = load_current(current_path)
    coverage_pool = load_coverage_pool(coverage_pool_path)
    old_catalog = load_catalog(previous_path)
    lifecycle = (
        load_lifecycle(previous_lifecycle_path)
        if previous_lifecycle_path is not None
        else {}
    )
    if previous_lifecycle_path is not None and previous_path is not None:
        projected = {
            phone: projected_record
            for phone, lifecycle_record in lifecycle.items()
            if (projected_record := lifecycle_record.catalog_record()) is not None
        }
        if {phone: record.row() for phone, record in projected.items()} != {
            phone: record.row() for phone, record in old_catalog.items()
        }:
            raise CatalogError("previous catalog is not the lifecycle active projection")
    mask_days = load_mask_days(previous_mask_days_path)
    events = load_events(previous_events_path)
    masks, evidence_by_mask = load_mask_summary(mask_summary_path)
    scope = set(masks)
    existing_run_rows = [row for row in mask_days if row["runKey"] == run_key]
    if existing_run_rows:
        raise CatalogError(f"run key {run_key!r} has already been processed")
    if any(mask not in scope for _phone, (_offer_id, mask) in current.items()):
        raise CatalogError("current snapshot contains a phone outside scan scope")
    if any(mask not in scope for mask in coverage_pool.values()):
        raise CatalogError("coverage pool contains a phone outside scan scope")

    historical_by_mask = {mask: 0 for mask in scope}
    for record in lifecycle.values():
        if record.source_mask in scope:
            historical_by_mask[record.source_mask] += 1
    current_by_mask = {mask: 0 for mask in scope}
    for phone, (_offer_id, mask) in current.items():
        current_by_mask[mask] += 1
    coverage_by_mask = {mask: 0 for mask in scope}
    for mask in coverage_pool.values():
        coverage_by_mask[mask] += 1
    if previous_lifecycle_path is not None:
        expected_coverage_pool = {
            phone: record.source_mask
            for phone, record in lifecycle.items()
            if record.source_mask in scope
            and record.status in COVERAGE_POOL_STATUSES
        }
        if coverage_pool != expected_coverage_pool:
            raise CatalogError(
                "coverage pool is not the exact lifecycle active projection"
            )
    for mask, evidence in evidence_by_mask.items():
        observed_known = sum(
            phone in coverage_pool
            for phone, (_offer_id, source_mask) in current.items()
            if source_mask == mask
        )
        if (
            (
                previous_lifecycle_path is not None
                and evidence.historical_distinct_at_start
                != historical_by_mask[mask]
            )
            or evidence.coverage_pool_at_start != coverage_by_mask[mask]
            or evidence.observed_phone_count != current_by_mask[mask]
            or evidence.observed_known_phone_count != observed_known
            or evidence.observed_new_phone_count
            != current_by_mask[mask] - observed_known
        ):
            raise CatalogError(
                f"mask {mask}: summary contradicts lifecycle/pool/current data"
            )

    existing_qualified_days = {
        (row["evidenceDate"], row["mask"])
        for row in mask_days
        if row["qualified"] == "true"
    }
    qualified_masks = set()
    for mask, evidence in evidence_by_mask.items():
        day_key = (evidence_date.isoformat(), mask)
        if run_kind != "scheduled_full":
            qualified, reason = False, "positive_only_run_kind"
        elif not evidence.comparable:
            qualified, reason = False, "insufficient_scan_evidence"
        elif day_key in existing_qualified_days:
            qualified, reason = False, "duplicate_evidence_day"
        else:
            qualified, reason = True, "scheduled_full_comparable"
            qualified_masks.add(mask)
            existing_qualified_days.add(day_key)
        inclusion = evidence_inclusion_lower(evidence)
        mask_days.append(
            _mask_day_row(
                evidence=evidence,
                evidence_date=evidence_date,
                run_key=run_key,
                run_kind=run_kind,
                qualified=qualified,
                reason=reason,
                inclusion_lower=inclusion,
                observed_at=observed_at,
            )
        )

    added = reobserved = resurrected = qualified_misses = tombstoned = 0
    updated: dict[str, LifecycleRecord] = {}
    for phone, old in lifecycle.items():
        current_identity = current.get(phone)
        if current_identity is not None:
            offer_id, mask = current_identity
            if mask != old.source_mask:
                raise CatalogError(f"phone {phone}: source mask changed")
            previous_status = old.status
            was_tombstone = previous_status in TOMBSTONE_STATUSES
            identity_changed = offer_id != old.offer_id
            first_seen = old.first_seen_at or observed_at
            resolved = old.resolved_sampling_miss_days
            if old.consecutive_qualified_miss_days:
                resolved += old.consecutive_qualified_miss_days
            seen_qualified = old.seen_qualified_days + (
                1 if mask in qualified_masks else 0
            )
            new = replace(
                old,
                offer_id=offer_id,
                first_seen_at=first_seen,
                last_seen_at=observed_at,
                last_checked_at=observed_at,
                last_observed_run_key=run_key,
                seen_runs=old.seen_runs + 1,
                seen_qualified_days=seen_qualified,
                resolved_sampling_miss_days=resolved,
                consecutive_qualified_miss_days=0,
                last_qualified_miss_date=None,
                negative_log_miss_likelihood=0.0,
                status="retained",
                status_changed_at=(
                    observed_at if previous_status != "retained" else old.status_changed_at
                ),
                tombstoned_at=None,
                tombstone_reason="",
                resurrection_count=old.resurrection_count + (1 if was_tombstone else 0),
                last_resurrected_at=(observed_at if was_tombstone else old.last_resurrected_at),
            )
            updated[phone] = new
            reobserved += 1
            if identity_changed:
                events.append(
                    _event(
                        observed_at=observed_at,
                        evidence_date=evidence_date,
                        run_key=run_key,
                        record=new,
                        event_type="identity_changed",
                        from_status=previous_status,
                        to_status="retained",
                        reason="observed_with_new_offer_id",
                    )
                )
            if was_tombstone:
                resurrected += 1
                events.append(
                    _event(
                        observed_at=observed_at,
                        evidence_date=evidence_date,
                        run_key=run_key,
                        record=new,
                        event_type="reappeared",
                        from_status=previous_status,
                        to_status="retained",
                        reason="observed_after_tombstone",
                    )
                )
            elif previous_status in {
                "possibly_unavailable",
                "legacy_history_unknown",
            }:
                events.append(
                    _event(
                        observed_at=observed_at,
                        evidence_date=evidence_date,
                        run_key=run_key,
                        record=new,
                        event_type="status_changed",
                        from_status=previous_status,
                        to_status="retained",
                        reason=(
                            "legacy_history_observed"
                            if previous_status == "legacy_history_unknown"
                            else "reobserved"
                        ),
                    )
                )
            continue

        if (
            old.source_mask not in scope
            or old.source_mask not in qualified_masks
            or phone not in coverage_pool
        ):
            updated[phone] = old
            continue
        if old.status in TOMBSTONE_STATUSES:
            updated[phone] = old
            continue
        evidence = evidence_by_mask[old.source_mask]
        effective_inclusion = evidence_inclusion_lower(evidence)
        contribution = (
            -math.log1p(-effective_inclusion)
            if 0.0 < effective_inclusion < 1.0
            else 0.0
        )
        misses = old.consecutive_qualified_miss_days + 1
        score = old.negative_log_miss_likelihood + contribution
        new_status = (
            "possibly_unavailable" if misses >= MISS_THRESHOLD else "retained"
        )
        can_tombstone = (
            old.last_seen_at is not None
            and observed_at - old.last_seen_at >= RETENTION_PERIOD
            and misses >= 5
            and score >= MIN_NEGATIVE_LOG_LIKELIHOOD
        )
        if can_tombstone:
            new_status = "statistically_stale"
        new = replace(
            old,
            last_checked_at=observed_at,
            consecutive_qualified_miss_days=misses,
            last_qualified_miss_date=evidence_date,
            negative_log_miss_likelihood=score,
            status=new_status,
            status_changed_at=(
                observed_at if new_status != old.status else old.status_changed_at
            ),
            tombstoned_at=(observed_at if can_tombstone else None),
            tombstone_reason=(
                "qualified_sampling_bayes_factor_10000" if can_tombstone else ""
            ),
        )
        updated[phone] = new
        qualified_misses += 1
        if new_status != old.status:
            event_type = "tombstoned" if can_tombstone else "status_changed"
            events.append(
                _event(
                    observed_at=observed_at,
                    evidence_date=evidence_date,
                    run_key=run_key,
                    record=new,
                    event_type=event_type,
                    from_status=old.status,
                    to_status=new_status,
                    reason=(
                        "sampling_evidence_threshold" if can_tombstone else "miss_threshold"
                    ),
                )
            )
        if can_tombstone:
            tombstoned += 1
    lifecycle = updated

    for phone, (offer_id, mask) in current.items():
        if phone in lifecycle:
            continue
        record = LifecycleRecord(
            phone_number=phone,
            offer_id=offer_id,
            source_mask=mask,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
            last_checked_at=observed_at,
            last_observed_run_key=run_key,
            seen_runs=1,
            seen_qualified_days=1 if mask in qualified_masks else 0,
            resolved_sampling_miss_days=0,
            consecutive_qualified_miss_days=0,
            last_qualified_miss_date=None,
            negative_log_miss_likelihood=0.0,
            status="retained",
            status_changed_at=observed_at,
            tombstoned_at=None,
            tombstone_reason="",
            resurrection_count=0,
            last_resurrected_at=None,
            legacy_comparable_misses=0,
            provenance="native",
            evidence_model_version=EVIDENCE_MODEL_VERSION,
        )
        lifecycle[phone] = record
        added += 1
        events.append(
            _event(
                observed_at=observed_at,
                evidence_date=evidence_date,
                run_key=run_key,
                record=record,
                event_type="added",
                from_status="",
                to_status="retained",
                reason="first_observation",
            )
        )

    # Global ID ownership includes tombstones.
    id_owners: dict[str, str] = {}
    for record in lifecycle.values():
        validate_lifecycle_record(record, f"lifecycle phone {record.phone_number}")
        owner = id_owners.setdefault(record.offer_id, record.phone_number)
        if owner != record.phone_number:
            raise CatalogError("offer id belongs to multiple lifecycle phones")

    projected_records = {
        phone: projected
        for phone, record in lifecycle.items()
        if (projected := record.catalog_record()) is not None
    }
    write_csv_atomic(
        catalog_output,
        CATALOG_FIELDS,
        (projected_records[phone].row() for phone in sorted(projected_records)),
    )
    write_csv_atomic(
        lifecycle_output,
        LIFECYCLE_FIELDS,
        (lifecycle[phone].row() for phone in sorted(lifecycle)),
    )
    write_csv_atomic(
        mask_days_output,
        MASK_DAY_FIELDS,
        (_dict_row(MASK_DAY_FIELDS, row) for row in sorted(mask_days, key=lambda item: (item["recordedAt"], item["runKey"], item["mask"]))),
    )
    write_csv_atomic(
        events_output,
        EVENT_FIELDS,
        (_dict_row(EVENT_FIELDS, row) for row in sorted(events, key=lambda item: (item["eventAt"], item["eventId"]))),
    )
    summary: dict[str, object] = {
        "schemaVersion": 3,
        "generatedAt": format_timestamp(observed_at),
        "historyMode": "cache" if previous_lifecycle_path is not None else "empty",
        "evidenceDate": evidence_date.isoformat(),
        "runKey": run_key,
        "runKind": run_kind,
        "evidenceModelVersion": EVIDENCE_MODEL_VERSION,
        "missThreshold": MISS_THRESHOLD,
        "retentionDays": RETENTION_PERIOD.days,
        "minimumNegativeLogMissLikelihood": round(MIN_NEGATIVE_LOG_LIKELIHOOD, 12),
        "scannedMaskCount": len(scope),
        "comparableMaskCount": sum(item.comparable for item in evidence_by_mask.values()),
        "qualifiedMaskCount": len(qualified_masks),
        "previousActiveCount": len(old_catalog),
        "currentObservedCount": len(current),
        "added": added,
        "reobserved": reobserved,
        "resurrected": resurrected,
        "qualifiedMisses": qualified_misses,
        "possiblyUnavailable": sum(
            record.status == "possibly_unavailable" for record in lifecycle.values()
        ),
        "tombstoned": tombstoned,
        "activeCount": len(projected_records),
        "lifecycleCount": len(lifecycle),
    }
    write_json_atomic(summary_output, summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--coverage-pool", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--previous-lifecycle", type=Path)
    parser.add_argument("--previous-mask-days", type=Path)
    parser.add_argument("--previous-events", type=Path)
    parser.add_argument("--mask-summary", type=Path, required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--evidence-date", required=True)
    parser.add_argument("--run-key", required=True)
    parser.add_argument("--run-kind", choices=sorted(RUN_KINDS), required=True)
    parser.add_argument("--catalog-output", type=Path, required=True)
    parser.add_argument("--lifecycle-output", type=Path, required=True)
    parser.add_argument("--mask-days-output", type=Path, required=True)
    parser.add_argument("--events-output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        update_catalog(
            current_path=args.current,
            coverage_pool_path=args.coverage_pool,
            previous_path=args.previous,
            previous_lifecycle_path=args.previous_lifecycle,
            previous_mask_days_path=args.previous_mask_days,
            previous_events_path=args.previous_events,
            mask_summary_path=args.mask_summary,
            observed_at=parse_timestamp(args.observed_at, "--observed-at"),
            evidence_date=parse_date(args.evidence_date, "--evidence-date"),
            run_key=args.run_key,
            run_kind=args.run_kind,
            catalog_output=args.catalog_output,
            lifecycle_output=args.lifecycle_output,
            mask_days_output=args.mask_days_output,
            events_output=args.events_output,
            summary_output=args.summary_output,
        )
    except (CatalogError, OSError, UnicodeError, csv.Error) as exc:
        print(f"catalog error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
