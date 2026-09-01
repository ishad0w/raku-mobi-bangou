#!/usr/bin/env python3
"""Validate the versioned collection-state cache."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Sequence


SCHEMA_VERSION = 3
EVIDENCE_MODEL_VERSION = 1
MIN_NEGATIVE_LOG_LIKELIHOOD = math.log(10_000.0)
PLANNING_COVERAGE_FLOOR_BPS = 9_900
PHONE_RE = re.compile(r"0[0-9]{10}")
MASK_RE = re.compile(r"[0-9]{4}")
ID_RE = re.compile(r"[0-9]+")
RUN_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
SCOPE_RE = re.compile(r"(?:full|specialized-[0-9a-f]{64})")
TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
PER_MASK_FIELDS = ("phoneNumber", "id")
AGGREGATE_FIELDS = ("phoneNumber", "id", "sourceMask")
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
SCAN_HISTORY_FIELDS = ("observedAt",) + MASK_SUMMARY_FIELDS
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
LIFECYCLE_STATUSES = {
    "retained",
    "possibly_unavailable",
    "statistically_stale",
    "confirmed_unavailable",
    "legacy_history_unknown",
}
CATALOG_INCLUDED_STATUSES = {"retained", "possibly_unavailable"}
PROVENANCE_VALUES = {"native", "legacy_catalog", "legacy_history"}
RUN_KINDS = {"scheduled_full", "manual_full", "manual_specialized"}
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
WARM_NO_PROGRESS_SAMPLE_LIMIT = 44
EVENT_TYPES = {
    "added",
    "status_changed",
    "tombstoned",
    "reappeared",
    "identity_changed",
}


class StateCacheError(ValueError):
    """The cached state is incomplete, incompatible, or inconsistent."""


def _valid_timestamp(value: str) -> bool:
    if not TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _valid_optional_timestamp(value: str) -> bool:
    return value == "" or _valid_timestamp(value)


def _valid_date(value: str) -> bool:
    if not DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _nonnegative(value: str) -> bool:
    return bool(re.fullmatch(r"0|[1-9][0-9]*", value))


def _positive(value: str) -> bool:
    return bool(re.fullmatch(r"[1-9][0-9]*", value))


def _valid_score(value: str) -> bool:
    try:
        score = float(value)
    except ValueError:
        return False
    return math.isfinite(score) and score >= 0


def _valid_identity(phone: str, offer_id: str, mask: str) -> bool:
    return bool(
        PHONE_RE.fullmatch(phone)
        and ID_RE.fullmatch(offer_id)
        and MASK_RE.fullmatch(mask)
        and phone.endswith(mask)
    )


def _valid_mask_summary_row(row: Sequence[str]) -> bool:
    if len(row) != len(MASK_SUMMARY_FIELDS):
        return False
    if (
        not MASK_RE.fullmatch(row[0])
        or row[17] not in STOP_REASONS
        or row[18] not in {"true", "false"}
    ):
        return False
    integer_indexes = tuple(range(1, 13)) + (14, 15, 16)
    if not all(_nonnegative(row[index]) for index in integer_indexes):
        return False
    (
        historical,
        pool,
        successful,
        requests,
        retries,
        samples,
        empty,
        observed,
        known,
        new,
        target,
        planning_target,
    ) = (int(row[index]) for index in range(1, 13))
    estimate, cap, round_limit = (int(row[index]) for index in (14, 15, 16))
    if pool == 0:
        achieved_valid = row[13] == ""
    else:
        achieved_valid = _nonnegative(row[13]) and int(row[13]) == 10_000 * known // pool
    target_count = (pool * target + 9_999) // 10_000
    expected_comparable = (
        pool > 0
        and (
            row[17] == "empty_probe_limit"
            and successful == 5
            and empty == 5
            or successful > empty
            and (
                (
                    row[17] == "coverage_target"
                    and successful >= 5
                    and known >= target_count
                )
                or (
                    row[17] == "request_cap"
                    and estimate <= round_limit
                    and cap == estimate
                    and successful == cap
                )
            )
        )
    )
    return (
        pool <= historical
        and requests == successful + retries
        and empty <= successful
        and observed == known + new
        and known <= pool
        and samples >= observed
        and 0 < target < 10_000
        and planning_target == max(target, PLANNING_COVERAGE_FLOOR_BPS)
        and round_limit > 0
        and cap == min(estimate, round_limit)
        and achieved_valid
        and (
            row[17] != "sampling_saturated"
            or (
                pool > 0
                and successful >= 5
                and samples >= WARM_NO_PROGRESS_SAMPLE_LIMIT
            )
        )
        and (row[18] == "true") == expected_comparable
    )


def read_rows(path: Path, fields: Sequence[str]) -> list[tuple[str, ...]]:
    if not path.is_file():
        raise StateCacheError(f"missing state file: {path}")
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header != list(fields):
                raise StateCacheError(f"unexpected CSV header: {path}")
            rows = [tuple(row) for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise StateCacheError(f"cannot read state CSV {path}: {type(exc).__name__}") from exc
    if any(len(row) != len(fields) for row in rows):
        raise StateCacheError(f"malformed CSV row: {path}")
    return rows


def validate_unique_rows(
    rows: Sequence[tuple[str, ...]],
    *,
    path: Path,
    validate_row: Callable[[tuple[str, ...]], bool],
) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    for row in rows:
        if not validate_row(row):
            raise StateCacheError(f"invalid state row: {path}")
        if row in result:
            raise StateCacheError(f"duplicate state row: {path}")
        result.add(row)
    return result


def list_scopes(state_dir: Path) -> list[str]:
    scopes_dir = state_dir / "scopes"
    if not scopes_dir.is_dir():
        raise StateCacheError("state scopes directory is missing")
    try:
        scopes = sorted(path.name for path in scopes_dir.iterdir())
    except OSError as exc:
        raise StateCacheError("cannot list cached scopes") from exc
    if not scopes or any(not SCOPE_RE.fullmatch(scope) for scope in scopes):
        raise StateCacheError("state cache contains no scopes or an invalid scope")
    return scopes


def _load_manifest_payload(state_dir: Path) -> dict[str, object]:
    path = state_dir / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateCacheError("state manifest is missing or invalid") from exc
    if not isinstance(payload, dict):
        raise StateCacheError("state manifest must be an object")
    return payload


def load_manifest(state_dir: Path) -> list[str]:
    payload = _load_manifest_payload(state_dir)
    if set(payload) != {"schemaVersion", "scopes"}:
        raise StateCacheError("state manifest has unexpected fields")
    if payload["schemaVersion"] != SCHEMA_VERSION:
        raise StateCacheError("state manifest schema is incompatible")
    scopes = payload["scopes"]
    if (
        not isinstance(scopes, list)
        or not scopes
        or any(not isinstance(scope, str) for scope in scopes)
        or scopes != sorted(set(scopes))
        or any(not SCOPE_RE.fullmatch(scope) for scope in scopes)
    ):
        raise StateCacheError("state manifest scopes must be non-empty, unique, and sorted")
    return scopes


def _validate_scan_history(path: Path, masks: set[str]) -> None:
    rows = read_rows(path, SCAN_HISTORY_FIELDS)
    previous_key: tuple[str, str] | None = None
    per_mask: dict[str, int] = defaultdict(int)
    seen: set[tuple[str, str]] = set()
    for row in rows:
        observed_at, mask = row[0], row[1]
        if (
            not _valid_timestamp(observed_at)
            or mask not in masks
            or not _valid_mask_summary_row(row[1:])
        ):
            raise StateCacheError(f"invalid state row: {path}")
        key = (observed_at, mask)
        if key in seen or (previous_key is not None and key < previous_key):
            raise StateCacheError(f"scan history is duplicated or unsorted: {path}")
        seen.add(key)
        previous_key = key
        per_mask[mask] += 1
        if per_mask[mask] > 30:
            raise StateCacheError(f"scan history exceeds retention: {path}")


def validate_scope(state_dir: Path, scope_name: str) -> set[tuple[str, str, str]]:
    scope_dir = state_dir / "scopes" / scope_name
    try:
        entries = {path.name for path in scope_dir.iterdir()}
    except OSError as exc:
        raise StateCacheError(f"cannot list state scope: {scope_name}") from exc
    expected = {"all_numbers.csv", "csv", "scan_history.csv"}
    if entries != expected:
        raise StateCacheError(f"state scope has unexpected entries: {scope_name}")

    csv_dir = scope_dir / "csv"
    try:
        csv_entries = sorted(csv_dir.iterdir()) if csv_dir.is_dir() else []
    except OSError as exc:
        raise StateCacheError(f"cannot list per-mask state: {scope_name}") from exc
    unexpected = [
        path
        for path in csv_entries
        if path.name != ".collector.lock"
        and (not path.is_file() or path.suffix != ".csv" or not MASK_RE.fullmatch(path.stem))
    ]
    if unexpected:
        raise StateCacheError(f"unexpected per-mask state file: {unexpected[0]}")
    lock_path = csv_dir / ".collector.lock"
    if lock_path.exists() and (not lock_path.is_file() or lock_path.stat().st_size != 0):
        raise StateCacheError(f"invalid collector lock file: {lock_path}")
    csv_files = [path for path in csv_entries if path.name != ".collector.lock"]
    if not csv_files:
        raise StateCacheError(f"state scope has no per-mask CSV files: {scope_name}")

    history: set[tuple[str, str, str]] = set()
    id_owners: dict[str, str] = {}
    for path in csv_files:
        rows = validate_unique_rows(
            read_rows(path, PER_MASK_FIELDS),
            path=path,
            validate_row=lambda row: bool(
                PHONE_RE.fullmatch(row[0])
                and row[0].endswith(path.stem)
                and ID_RE.fullmatch(row[1])
            ),
        )
        for phone, offer_id in rows:
            owner = id_owners.setdefault(offer_id, phone)
            if owner != phone:
                raise StateCacheError(
                    f"offer id belongs to multiple phones in scope: {scope_name}"
                )
            history.add((phone, offer_id, path.stem))

    aggregate_path = scope_dir / "all_numbers.csv"
    aggregate_rows = read_rows(aggregate_path, AGGREGATE_FIELDS)
    aggregate = validate_unique_rows(
        aggregate_rows,
        path=aggregate_path,
        validate_row=lambda row: _valid_identity(row[0], row[1], row[2]),
    )
    if len({row[0] for row in aggregate_rows}) != len(aggregate_rows):
        raise StateCacheError(f"duplicate phone in current aggregate: {scope_name}")
    if not aggregate.issubset(history):
        raise StateCacheError(
            f"current aggregate contains rows absent from per-mask history: {scope_name}"
        )
    _validate_scan_history(
        scope_dir / "scan_history.csv", {path.stem for path in csv_files}
    )
    return history


def _validate_catalog(path: Path) -> list[tuple[str, ...]]:
    rows = read_rows(path, CATALOG_FIELDS)
    phones: set[str] = set()
    id_owners: dict[str, str] = {}
    for row in rows:
        phone, offer_id, mask = row[:3]
        misses_valid = _nonnegative(row[7])
        misses = int(row[7]) if misses_valid else -1
        expected = "possibly_unavailable" if misses >= 3 else "active"
        valid = (
            _valid_identity(phone, offer_id, mask)
            and all(_valid_timestamp(value) for value in row[3:6])
            and row[3] <= row[4] <= row[5]
            and _positive(row[6])
            and misses_valid
            and row[8] == expected
        )
        if not valid or phone in phones:
            raise StateCacheError(f"invalid state row: {path}")
        phones.add(phone)
        owner = id_owners.setdefault(offer_id, phone)
        if owner != phone:
            raise StateCacheError("offer id belongs to multiple phones in catalog state")
    return rows


def _validate_lifecycle(
    path: Path,
) -> tuple[list[tuple[str, ...]], dict[str, tuple[str, ...]], set[tuple[str, str, str]]]:
    rows = read_rows(path, LIFECYCLE_FIELDS)
    by_phone: dict[str, tuple[str, ...]] = {}
    identities: set[tuple[str, str, str]] = set()
    id_owners: dict[str, str] = {}
    for row in rows:
        phone, offer_id, mask = row[:3]
        integers = row[7:11] + (row[17], row[19], row[21])
        try:
            score = float(row[12])
        except ValueError:
            score = -1
        timestamps = row[3:6]
        all_times = all(_valid_timestamp(value) for value in timestamps)
        no_times = all(value == "" for value in timestamps)
        misses = int(row[10]) if _nonnegative(row[10]) else -1
        status = row[13]
        tombstone = status in {"statistically_stale", "confirmed_unavailable"}
        statistically_stale_valid = (
            status != "statistically_stale"
            or (
                misses >= 5
                and _nonnegative(row[8])
                and score >= MIN_NEGATIVE_LOG_LIKELIHOOD
                and _valid_timestamp(row[4])
                and _valid_timestamp(row[15])
                and (
                    datetime.strptime(row[15], "%Y-%m-%dT%H:%M:%SZ")
                    - datetime.strptime(row[4], "%Y-%m-%dT%H:%M:%SZ")
                ).days
                >= 5
            )
        )
        valid = (
            _valid_identity(phone, offer_id, mask)
            and (all_times or no_times)
            and (not all_times or row[3] <= row[4] <= row[5])
            and (row[6] == "" or RUN_KEY_RE.fullmatch(row[6]))
            and all(_nonnegative(value) for value in integers[:-1])
            and row[21] == str(EVIDENCE_MODEL_VERSION)
            and math.isfinite(score)
            and score >= 0
            and status in LIFECYCLE_STATUSES
            and _valid_optional_timestamp(row[14])
            and _valid_optional_timestamp(row[15])
            and _valid_optional_timestamp(row[18])
            and (row[11] == "" if misses == 0 else _valid_date(row[11]))
            and (score == 0 if misses == 0 else True)
            and row[20] in PROVENANCE_VALUES
            and ((row[15] != "" and row[16] != "") if tombstone else (row[15] == "" and row[16] == ""))
            and (status != "legacy_history_unknown" or row[20] == "legacy_history")
            and (status not in CATALOG_INCLUDED_STATUSES or (all_times and int(row[7]) >= 1))
            and (status != "retained" or misses < 3)
            and (status != "possibly_unavailable" or misses >= 3)
            and ((int(row[17]) == 0 and row[18] == "") or (int(row[17]) > 0 and row[18] != ""))
            and statistically_stale_valid
        )
        if not valid or phone in by_phone:
            raise StateCacheError(f"invalid state row: {path}")
        by_phone[phone] = row
        owner = id_owners.setdefault(offer_id, phone)
        if owner != phone:
            raise StateCacheError("offer id belongs to multiple lifecycle phones")
        identities.add((phone, offer_id, mask))
    return rows, by_phone, identities


def _catalog_projection(row: tuple[str, ...]) -> tuple[str, ...] | None:
    status = row[13]
    if status not in CATALOG_INCLUDED_STATUSES:
        return None
    misses = row[10]
    return (
        row[0],
        row[1],
        row[2],
        row[3],
        row[4],
        row[5],
        row[7],
        misses,
        "possibly_unavailable" if status == "possibly_unavailable" else "active",
    )


def _validate_mask_days(path: Path) -> None:
    rows = read_rows(path, MASK_DAY_FIELDS)
    run_masks: set[tuple[str, str]] = set()
    qualified_days: set[tuple[str, str]] = set()
    previous: tuple[str, str, str] | None = None
    for row in rows:
        key = (row[14], row[2], row[1])
        counts_valid = all(_nonnegative(value) for value in row[4:9])
        historical, pool, successful, observed, known = (
            (int(value) for value in row[4:9])
            if counts_valid
            else (-1, -1, -1, -1, -1)
        )
        inclusion_valid = _valid_score(row[13]) and float(row[13]) <= 1
        qualification_valid = (
            row[11] != "true"
            or (
                row[3] == "scheduled_full"
                and row[10] == "true"
                and row[12] == "scheduled_full_comparable"
            )
        )
        valid = (
            _valid_date(row[0])
            and MASK_RE.fullmatch(row[1])
            and RUN_KEY_RE.fullmatch(row[2])
            and row[3] in RUN_KINDS
            and counts_valid
            and pool <= historical
            and known <= pool
            and known <= observed
            and row[9] in STOP_REASONS
            and row[10] in {"true", "false"}
            and row[11] in {"true", "false"}
            and bool(row[12])
            and inclusion_valid
            and qualification_valid
            and _valid_timestamp(row[14])
            and row[15] == str(EVIDENCE_MODEL_VERSION)
        )
        run_key = (row[2], row[1])
        if not valid or run_key in run_masks or (previous is not None and key < previous):
            raise StateCacheError(f"invalid state row: {path}")
        run_masks.add(run_key)
        previous = key
        if row[11] == "true":
            day_key = (row[0], row[1])
            if day_key in qualified_days:
                raise StateCacheError(f"duplicate qualified mask/day: {path}")
            qualified_days.add(day_key)


def _validate_events(
    path: Path,
    lifecycle: dict[str, tuple[str, ...]],
    history: set[tuple[str, str, str]],
) -> None:
    rows = read_rows(path, EVENT_FIELDS)
    ids: set[str] = set()
    previous: tuple[str, str] | None = None
    for row in rows:
        key = (row[1], row[0])
        expected_event_id = hashlib.sha256(
            "|".join((row[3], row[4], row[7], row[8], row[9])).encode("utf-8")
        ).hexdigest()
        valid = (
            re.fullmatch(r"[0-9a-f]{64}", row[0])
            and row[0] == expected_event_id
            and _valid_timestamp(row[1])
            and _valid_date(row[2])
            and RUN_KEY_RE.fullmatch(row[3])
            and _valid_identity(row[4], row[5], row[6])
            and (row[4], row[5], row[6]) in history
            and row[4] in lifecycle
            and row[7] in EVENT_TYPES
            and (row[8] == "" or row[8] in LIFECYCLE_STATUSES)
            and (row[9] == "" or row[9] in LIFECYCLE_STATUSES)
            and bool(row[10])
            and row[11] == str(EVIDENCE_MODEL_VERSION)
        )
        if not valid or row[0] in ids or (previous is not None and key < previous):
            raise StateCacheError(f"invalid state row: {path}")
        ids.add(row[0])
        previous = key


def _inspect_root(state_dir: Path, expected: set[str]) -> None:
    if not state_dir.is_dir():
        raise StateCacheError("state directory is missing")
    try:
        if any(path.is_symlink() for path in state_dir.rglob("*")):
            raise StateCacheError("state cache must not contain symbolic links")
        entries = {path.name for path in state_dir.iterdir()}
    except OSError as exc:
        raise StateCacheError("cannot inspect state directory") from exc
    if entries != expected:
        raise StateCacheError("state cache has unexpected root entries")


def validate_state(state_dir: Path) -> None:
    _inspect_root(
        state_dir,
        {
            "catalog.csv",
            "lifecycle.csv",
            "mask_days.csv",
            "lifecycle_events.csv",
            "manifest.json",
            "scopes",
        },
    )
    scopes = load_manifest(state_dir)
    if scopes != list_scopes(state_dir):
        raise StateCacheError("state manifest does not match cached scopes")
    history: set[tuple[str, str, str]] = set()
    for scope in scopes:
        history.update(validate_scope(state_dir, scope))
    global_id_owners: dict[str, str] = {}
    global_phone_masks: dict[str, str] = {}
    for phone, offer_id, mask in history:
        owner = global_id_owners.setdefault(offer_id, phone)
        if owner != phone:
            raise StateCacheError("offer id belongs to multiple phones across scopes")
        existing_mask = global_phone_masks.setdefault(phone, mask)
        if existing_mask != mask:
            raise StateCacheError("phone belongs to multiple masks across scopes")

    lifecycle_rows, lifecycle, lifecycle_identities = _validate_lifecycle(
        state_dir / "lifecycle.csv"
    )
    if not lifecycle_identities.issubset(history):
        raise StateCacheError("lifecycle contains rows absent from cached history")
    history_phone_masks = {(phone, mask) for phone, _offer_id, mask in history}
    lifecycle_phone_masks = {
        (phone, row[2]) for phone, row in lifecycle.items()
    }
    if lifecycle_phone_masks != history_phone_masks:
        raise StateCacheError(
            "cached per-mask history and lifecycle phone sets disagree"
        )
    catalog_rows = _validate_catalog(state_dir / "catalog.csv")
    projection = sorted(
        projected
        for row in lifecycle_rows
        if (projected := _catalog_projection(row)) is not None
    )
    if catalog_rows != projection:
        raise StateCacheError("catalog is not the lifecycle active projection")
    _validate_mask_days(state_dir / "mask_days.csv")
    _validate_events(state_dir / "lifecycle_events.csv", lifecycle, history)


def write_manifest(state_dir: Path) -> None:
    scopes = list_scopes(state_dir)
    for required in ("catalog.csv", "lifecycle.csv", "mask_days.csv", "lifecycle_events.csv"):
        if not (state_dir / required).is_file():
            raise StateCacheError(f"cannot write manifest before {required} exists")
    for scope in scopes:
        if not (state_dir / "scopes" / scope / "scan_history.csv").is_file():
            raise StateCacheError("cannot write manifest before scan history exists")
    path = state_dir / "manifest.json"
    temporary = state_dir / ".manifest.json.tmp"
    payload = {"schemaVersion": SCHEMA_VERSION, "scopes": scopes}
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise StateCacheError("cannot write state manifest") from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "write-manifest"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--state-dir", type=Path, default=Path("state"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "validate":
            validate_state(args.state_dir)
        else:
            write_manifest(args.state_dir)
    except (StateCacheError, OSError, UnicodeError, csv.Error) as exc:
        print(f"state cache error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
