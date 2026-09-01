#!/usr/bin/env python3
"""Collect Japanese mobile-number candidates by four-digit mask."""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import logging
import math
import os
import random
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable, Collection, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    OpenerDirector,
    Request,
    build_opener,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MASKS_FILE = SCRIPT_DIR / "masks.txt"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "csv"
DEFAULT_RUN_DIR = SCRIPT_DIR / "run"
DEFAULT_DELAY_MIN = 1.1
DEFAULT_DELAY_MAX = 2.0
DEFAULT_TIMEOUT = 20.0
DEFAULT_TARGET_COVERAGE_BPS = 9_000
# Plan beyond the stop target so ordinary sampling variance does not turn the
# expected-value request estimate into a premature cutoff. Collection still
# stops as soon as the actual target is reached.
DEFAULT_PLANNING_COVERAGE_BPS = 9_900
DEFAULT_REQUEST_LIMIT = 5_000
DEFAULT_MASK_COOLDOWN = 30.0
DEFAULT_EFFECTIVE_BATCH = 2.5
MIN_PROBES = 5
EMPTY_PROBE_LIMIT = 5
COLD_MIN_RESPONSES = 15
COLD_NO_PROGRESS_LIMIT = 15
ONLINE_ESTIMATE_MIN_RESPONSES = 10
# ceil(log(0.01) / log(0.90)): 44 dry sampled slots make an unseen sampling
# mass of 10% or more less than 1% likely under the planning model.
WARM_NO_PROGRESS_SAMPLE_LIMIT = 44
PRIORITY_STARVATION_CYCLES = 2
SCAN_HISTORY_WINDOW = 5
SCAN_HISTORY_ROWS_PER_MASK = 30
# One initial request plus three retries for transient failures.
DEFAULT_REQUEST_ATTEMPTS = 4
DEFAULT_BACKOFF_CAP = 60.0
DEFAULT_RATE_LIMIT_DELAY = 30.0
MAX_RETRY_AFTER = 300.0
MAX_RESPONSE_BYTES = 1_000_000
API_URL_ENV = "PHONE_NUMBER_API_URL"
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
LIFECYCLE_EXCLUDED_FROM_COVERAGE = {
    "statistically_stale",
    "confirmed_unavailable",
    "legacy_history_unknown",
}
LIFECYCLE_STATUSES = {
    "retained",
    "possibly_unavailable",
    "statistically_stale",
    "confirmed_unavailable",
    "legacy_history_unknown",
}
MASK_RE = re.compile(r"[0-9]{4}")
PHONE_RE = re.compile(r"0[0-9]{10}")
ID_RE = re.compile(r"[0-9]+")
URL_RE = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)

FetchOffers = Callable[[object, str, str, float], list["PhoneOffer"]]
LOGGER = logging.getLogger("raku_mobi_bangou")
LOGGER.addHandler(logging.NullHandler())


class ResponseError(ValueError):
    """The endpoint returned JSON that does not match the expected contract."""


class FatalCollectionError(RuntimeError):
    """The run must stop to avoid unsafe or pointless further requests."""


class GlobalRequestLimitReached(RuntimeError):
    """The configured real-request budget was exhausted safely."""


class NoRedirectHandler(HTTPRedirectHandler):
    """Return redirect responses to the retry policy without following them."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


@dataclass(frozen=True)
class PhoneOffer:
    phone_number: str
    offer_id: str

    @property
    def row(self) -> tuple[str, str]:
        return self.phone_number, self.offer_id


@dataclass(frozen=True)
class AppendResult:
    new_rows: int
    new_phones: int


@dataclass
class MaskStats:
    historical_distinct_at_start: int = 0
    coverage_pool_at_start: int = 0
    successful_responses: int = 0
    http_requests: int = 0
    retries: int = 0
    response_phone_samples: int = 0
    empty_responses: int = 0
    observed_phones: set[str] = field(default_factory=set)
    observed_known_phones: set[str] = field(default_factory=set)
    observed_new_phones: set[str] = field(default_factory=set)
    target_coverage_bps: int = DEFAULT_TARGET_COVERAGE_BPS
    planning_coverage_bps: int = DEFAULT_PLANNING_COVERAGE_BPS
    estimated_request_budget: int = MIN_PROBES
    request_cap: int = MIN_PROBES
    round_limit: int = MIN_PROBES
    stop_reason: str = "round_limit"

    @property
    def comparable(self) -> bool:
        if self.coverage_pool_at_start <= 0:
            return False
        if (
            self.stop_reason == "empty_probe_limit"
            and self.successful_responses == EMPTY_PROBE_LIMIT
            and self.empty_responses == EMPTY_PROBE_LIMIT
        ):
            return True
        if self.successful_responses <= self.empty_responses:
            return False
        if self.stop_reason == "coverage_target":
            return bool(
                self.successful_responses >= MIN_PROBES
                and len(self.observed_known_phones) >= self.target_phone_count
            )
        return bool(
            self.stop_reason == "request_cap"
            and self.estimated_request_budget <= self.round_limit
            and self.request_cap == self.estimated_request_budget
            and self.successful_responses == self.request_cap
        )

    @property
    def achieved_coverage_bps(self) -> int | None:
        if self.coverage_pool_at_start == 0:
            return None
        return min(
            10_000,
            10_000
            * len(self.observed_known_phones)
            // self.coverage_pool_at_start,
        )

    @property
    def target_phone_count(self) -> int:
        return (
            self.coverage_pool_at_start * self.target_coverage_bps + 9_999
        ) // 10_000

    def row(self, mask: str) -> tuple[str, ...]:
        achieved = self.achieved_coverage_bps
        return (
            mask,
            str(self.historical_distinct_at_start),
            str(self.coverage_pool_at_start),
            str(self.successful_responses),
            str(self.http_requests),
            str(self.retries),
            str(self.response_phone_samples),
            str(self.empty_responses),
            str(len(self.observed_phones)),
            str(len(self.observed_known_phones)),
            str(len(self.observed_new_phones)),
            str(self.target_coverage_bps),
            str(self.planning_coverage_bps),
            "" if achieved is None else str(achieved),
            str(self.estimated_request_budget),
            str(self.request_cap),
            str(self.round_limit),
            self.stop_reason,
            "true" if self.comparable else "false",
        )


@dataclass(frozen=True)
class ScanHistoryRecord:
    observed_at: str
    values: tuple[str, ...]

    @property
    def mask(self) -> str:
        return self.values[0]

    def as_row(self) -> tuple[str, ...]:
        return (self.observed_at,) + self.values


@dataclass
class CollectionStats:
    rounds: int = 0
    requests: int = 0
    responses: int = 0
    retries: int = 0
    received: int = 0
    added_rows: int = 0
    added_phones: int = 0
    failed_requests: int = 0
    target_coverage_bps: int = DEFAULT_TARGET_COVERAGE_BPS
    planning_coverage_bps: int = DEFAULT_PLANNING_COVERAGE_BPS
    request_limit: int = DEFAULT_REQUEST_LIMIT
    deep_scan: bool = False
    deactivated_coverage: int = 0
    deactivated_empty_probe: int = 0
    deactivated_cold_saturated: int = 0
    deactivated_sampling_saturated: int = 0
    deactivated_request_cap: int = 0
    deactivated_round_limit: int = 0
    deactivated_global_limit: int = 0
    active_masks: int = 0
    elapsed_seconds: float = 0.0
    mask_stats: dict[str, MaskStats] = field(default_factory=dict)

    def as_dict(self) -> dict[str, int | float]:
        return {
            "rounds": self.rounds,
            "requests": self.requests,
            "responses": self.responses,
            "retries": self.retries,
            "received": self.received,
            "addedRows": self.added_rows,
            "addedPhones": self.added_phones,
            "failedRequests": self.failed_requests,
            "targetCoverageBps": self.target_coverage_bps,
            "planningCoverageBps": self.planning_coverage_bps,
            "requestLimit": self.request_limit,
            "deepScan": self.deep_scan,
            "deactivatedCoverage": self.deactivated_coverage,
            "deactivatedEmptyProbe": self.deactivated_empty_probe,
            "deactivatedColdSaturated": self.deactivated_cold_saturated,
            "deactivatedSamplingSaturated": self.deactivated_sampling_saturated,
            "deactivatedRequestCap": self.deactivated_request_cap,
            "deactivatedRoundLimit": self.deactivated_round_limit,
            "deactivatedGlobalLimit": self.deactivated_global_limit,
            "activeMasks": self.active_masks,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
        }


class UtcFormatter(logging.Formatter):
    converter = time.gmtime


class OfferStore:
    """Transactionally persist observations under an output-directory lock."""

    def __init__(self, output_dir: Path, masks: Sequence[str]) -> None:
        self.output_dir = output_dir
        self._pairs: dict[str, set[PhoneOffer]] = {}
        self._representatives: dict[str, tuple[str, str]] = {}
        self._observed: dict[str, tuple[str, str]] = {}
        self._id_owners: dict[str, str] = {}
        self._initial_phones: dict[str, frozenset[str]] = {}
        self._lock_fd: int | None = None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._acquire_lock()
            stored_masks: set[str] = set(masks)
            for path in self.output_dir.glob("*.csv"):
                if not MASK_RE.fullmatch(path.stem):
                    raise ValueError(
                        f"{path}: output CSV filename must be a four-digit mask"
                    )
                stored_masks.add(path.stem)

            for mask in sorted(stored_masks):
                path = self.path_for(mask)
                rows = read_existing(path, expected_mask=mask)
                self._pairs[mask] = set()
                for offer in rows:
                    self._register_existing(mask, offer, path)
                self._ensure_header(path)
            self._initial_phones = {
                mask: frozenset(offer.phone_number for offer in offers)
                for mask, offers in self._pairs.items()
            }
        except BaseException:
            self.close()
            raise

    def _acquire_lock(self) -> None:
        try:
            lock_fd = os.open(
                self.output_dir,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
        except OSError as exc:
            raise ValueError(
                f"cannot open output directory for locking {self.output_dir}: {exc}"
            ) from exc
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lock_fd)
            raise ValueError(
                f"output directory is already locked by another collector: "
                f"{self.output_dir}"
            ) from exc
        except OSError as exc:
            os.close(lock_fd)
            raise ValueError(
                f"cannot lock output directory {self.output_dir}: {exc}"
            ) from exc
        self._lock_fd = lock_fd

    def close(self) -> None:
        """Release the single-writer lock; calling this twice is harmless."""
        lock_fd = self._lock_fd
        if lock_fd is None:
            return
        self._lock_fd = None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    def __enter__(self) -> OfferStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @property
    def phone_count(self) -> int:
        return len(self._representatives)

    @property
    def observation_count(self) -> int:
        return sum(len(rows) for rows in self._pairs.values())

    @property
    def observed_phone_count(self) -> int:
        return len(self._observed)

    def initial_phones(self, mask: str) -> frozenset[str]:
        try:
            return self._initial_phones[mask]
        except KeyError as exc:
            raise ValueError(f"mask {mask} was not initialized in the output store") from exc

    def path_for(self, mask: str) -> Path:
        return self.output_dir / f"{mask}.csv"

    def _ensure_header(self, path: Path) -> None:
        if path.exists():
            return
        self._rewrite_mask(path, ())

    def _rewrite_mask(self, path: Path, offers: Iterable[PhoneOffer]) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="") as output:
                writer = csv.writer(output, lineterminator="\n")
                writer.writerow(("phoneNumber", "id"))
                writer.writerows(offer.row for offer in offers)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ValueError(f"cannot transactionally write {path}: {exc}") from exc

    def _register_existing(self, mask: str, offer: PhoneOffer, path: Path) -> None:
        if offer in self._pairs[mask]:
            raise ValueError(f"{path}: duplicate phoneNumber and id pair")
        owner = self._id_owners.setdefault(offer.offer_id, offer.phone_number)
        if owner != offer.phone_number:
            raise ValueError(
                f"{path}: offer id {offer.offer_id!r} belongs to another phone"
            )
        self._pairs[mask].add(offer)
        self._representatives.setdefault(
            offer.phone_number, (offer.offer_id, mask)
        )

    def append(self, mask: str, offers: Iterable[PhoneOffer]) -> AppendResult:
        if self._lock_fd is None:
            raise ValueError("output store is closed")
        if mask not in self._pairs:
            raise ValueError(f"mask {mask} was not initialized in the output store")

        pending: list[PhoneOffer] = []
        pending_set: set[PhoneOffer] = set()
        pending_owners: dict[str, str] = {}
        new_phone_numbers: set[str] = set()
        validated_offers: list[PhoneOffer] = []

        for offer in offers:
            if not offer.phone_number.endswith(mask):
                raise ValueError(
                    f"phone {offer.phone_number!r} does not end with mask {mask}"
                )
            owner = self._id_owners.get(offer.offer_id)
            pending_owner = pending_owners.get(offer.offer_id)
            if (owner is not None and owner != offer.phone_number) or (
                pending_owner is not None and pending_owner != offer.phone_number
            ):
                raise ValueError(
                    f"offer id {offer.offer_id!r} belongs to another phone"
                )
            pending_owners[offer.offer_id] = offer.phone_number
            validated_offers.append(offer)
            if offer in self._pairs[mask] or offer in pending_set:
                continue
            pending.append(offer)
            pending_set.add(offer)
            if offer.phone_number not in self._representatives:
                new_phone_numbers.add(offer.phone_number)

        if pending:
            path = self.path_for(mask)
            updated = sorted(
                self._pairs[mask] | pending_set,
                key=lambda offer: (
                    offer.phone_number,
                    len(offer.offer_id),
                    offer.offer_id,
                ),
            )
            self._rewrite_mask(path, updated)

            for offer in pending:
                self._pairs[mask].add(offer)
                self._id_owners.setdefault(offer.offer_id, offer.phone_number)
                self._representatives.setdefault(
                    offer.phone_number, (offer.offer_id, mask)
                )

        for offer in validated_offers:
            candidate = (offer.offer_id, mask)
            current = self._observed.get(offer.phone_number)
            candidate_key = (len(candidate[0]), candidate[0], candidate[1])
            current_key = (
                (len(current[0]), current[0], current[1])
                if current is not None
                else None
            )
            if current_key is None or candidate_key < current_key:
                self._observed[offer.phone_number] = candidate

        return AppendResult(len(pending), len(new_phone_numbers))

    def export_observed(self, path: Path) -> int:
        """Write one deterministic row for each phone seen in this invocation."""
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="") as output:
                writer = csv.writer(output, lineterminator="\n")
                writer.writerow(("phoneNumber", "id", "sourceMask"))
                for phone_number in sorted(self._observed):
                    offer_id, mask = self._observed[phone_number]
                    writer.writerow((phone_number, offer_id, mask))
            temporary.replace(path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ValueError(f"cannot write observed output {path}: {exc}") from exc
        return len(self._observed)

def read_deduplicated(path: Path) -> dict[str, tuple[str, str]]:
    try:
        source = path.open("r", encoding="utf-8", newline="")
    except OSError as exc:
        raise ValueError(f"cannot read deduplicated CSV {path}: {exc}") from exc

    records: dict[str, tuple[str, str]] = {}
    id_owners: dict[str, str] = {}
    with source:
        reader = csv.DictReader(source)
        if reader.fieldnames != ["phoneNumber", "id", "sourceMask"]:
            raise ValueError(
                f"{path}: expected CSV header 'phoneNumber,id,sourceMask', got "
                f"{reader.fieldnames!r}"
            )
        for line_number, row in enumerate(reader, start=2):
            if None in row or set(row) != {"phoneNumber", "id", "sourceMask"}:
                raise ValueError(f"{path}:{line_number}: malformed CSV row")
            phone_number = row["phoneNumber"]
            offer_id = row["id"]
            mask = row["sourceMask"]
            if (
                not PHONE_RE.fullmatch(phone_number)
                or not ID_RE.fullmatch(offer_id)
                or not MASK_RE.fullmatch(mask)
                or not phone_number.endswith(mask)
            ):
                raise ValueError(f"{path}:{line_number}: invalid deduplicated row")
            if phone_number in records:
                raise ValueError(f"{path}:{line_number}: duplicate phone number")
            owner = id_owners.setdefault(offer_id, phone_number)
            if owner != phone_number:
                raise ValueError(
                    f"{path}:{line_number}: offer id belongs to another phone"
                )
            records[phone_number] = (offer_id, mask)
    return records


def _same_existing_file(left: Path, right: Path) -> bool:
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False


def validate_runtime_paths(
    output_dir: Path,
    run_dir: Path,
    baseline_path: Path | None,
    lifecycle_path: Path | None = None,
    scan_history_path: Path | None = None,
) -> None:
    """Reject layouts that could overwrite an input during this run."""
    if output_dir == run_dir:
        raise ValueError("--output-dir and --run-dir must be different directories")
    inputs = (
        ("--baseline-csv", baseline_path),
        ("--lifecycle-csv", lifecycle_path),
        ("--scan-history", scan_history_path),
    )
    for option, input_path in inputs:
        if input_path is None:
            continue
        if input_path == output_dir or output_dir in input_path.parents:
            raise ValueError(f"{option} cannot be inside --output-dir")
        if input_path == run_dir or run_dir in input_path.parents:
            raise ValueError(f"{option} cannot be inside --run-dir")

        if output_dir.exists():
            for output_csv in output_dir.glob("*.csv"):
                if _same_existing_file(input_path, output_csv):
                    raise ValueError(f"{option} cannot alias an output CSV")

        if run_dir.exists():
            for existing_artifact in run_dir.rglob("*"):
                if existing_artifact.is_file() and _same_existing_file(
                    input_path, existing_artifact
                ):
                    raise ValueError(f"{option} cannot alias run artifact")


def write_run_diff(
    current_path: Path,
    baseline_records: Mapping[str, tuple[str, str]] | None,
    scanned_masks: Collection[str],
    output_path: Path,
    summary_path: Path,
) -> dict[str, object]:
    current = read_deduplicated(current_path)
    comparison_available = baseline_records is not None
    baseline = dict(baseline_records or {})
    scope = set(scanned_masks)
    if any(not MASK_RE.fullmatch(mask) for mask in scope):
        raise ValueError("scanned mask scope contains an invalid mask")
    for phone_number, (_offer_id, mask) in current.items():
        if mask not in scope:
            raise ValueError(
                f"{current_path}: phone {phone_number} belongs to unscanned mask {mask}"
            )

    rows: list[tuple[str, str, str, str, str]] = []
    for phone_number in sorted(set(current) - set(baseline)):
        current_id, mask = current[phone_number]
        rows.append(("added", phone_number, "", current_id, mask))
    if comparison_available:
        for phone_number in sorted(set(baseline) - set(current)):
            previous_id, mask = baseline[phone_number]
            change_type = "not_observed" if mask in scope else "not_scanned"
            rows.append((change_type, phone_number, previous_id, "", mask))
        for phone_number in sorted(set(current) & set(baseline)):
            previous_id, previous_mask = baseline[phone_number]
            current_id, mask = current[phone_number]
            if previous_mask != mask:
                raise ValueError(
                    f"phone {phone_number} changed source mask from "
                    f"{previous_mask} to {mask}"
                )
            if previous_id != current_id:
                rows.append(
                    ("id_changed", phone_number, previous_id, current_id, mask)
                )

    temporary_csv = output_path.with_name(f".{output_path.name}.tmp")
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with temporary_csv.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(
                (
                    "changeType",
                    "phoneNumber",
                    "previousId",
                    "currentId",
                    "sourceMask",
                )
            )
            writer.writerows(rows)
        temporary_csv.replace(output_path)
    except OSError as exc:
        try:
            temporary_csv.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(f"cannot write run diff {output_path}: {exc}") from exc

    counts: dict[str, object] = {
        "schemaVersion": 2,
        "generatedAt": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "comparisonAvailable": comparison_available,
        "scannedMaskCount": len(scope),
        "currentPhoneCount": len(current),
        "previousPhoneCount": len(baseline),
        "added": sum(row[0] == "added" for row in rows),
        "notObserved": sum(row[0] == "not_observed" for row in rows),
        "notScanned": sum(row[0] == "not_scanned" for row in rows),
        "idChanged": sum(row[0] == "id_changed" for row in rows),
        "unchanged": (
            sum(
                phone_number in baseline
                and baseline[phone_number][0] == current[phone_number][0]
                for phone_number in current
            )
            if comparison_available
            else 0
        ),
    }
    temporary_summary = summary_path.with_name(f".{summary_path.name}.tmp")
    try:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_summary.write_text(
            json.dumps(counts, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_summary.replace(summary_path)
    except OSError as exc:
        try:
            temporary_summary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(f"cannot write diff summary {summary_path}: {exc}") from exc
    return counts


def log_event(level: int, event: str, **fields: object) -> None:
    suffix = ""
    if fields:
        suffix = " " + json.dumps(fields, ensure_ascii=False, sort_keys=True)
    LOGGER.log(level, "%s%s", event, suffix)


def configure_logging(run_dir: Path) -> tuple[Path, Path]:
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    collector_log = logs_dir / "collector.log"
    errors_log = logs_dir / "errors.log"

    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()

    formatter = UtcFormatter(
        fmt="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    full_handler = logging.FileHandler(
        collector_log, mode="w", encoding="utf-8", delay=False
    )
    full_handler.setLevel(logging.DEBUG)
    full_handler.setFormatter(formatter)
    error_handler = logging.FileHandler(
        errors_log, mode="w", encoding="utf-8", delay=False
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)

    LOGGER.setLevel(logging.DEBUG)
    LOGGER.propagate = False
    LOGGER.addHandler(full_handler)
    LOGGER.addHandler(error_handler)
    return collector_log, errors_log


def validate_mask(raw_mask: str) -> str:
    mask = raw_mask.strip()
    if not MASK_RE.fullmatch(mask):
        raise ValueError(
            f"invalid mask {raw_mask!r}: exactly four decimal digits are required"
        )
    return mask


def validate_api_url(raw_url: str) -> str:
    api_url = raw_url.strip()
    parts = urlsplit(api_url)
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or "?" in api_url
        or "#" in api_url
        or not parts.path
    ):
        raise ValueError(
            "API URL must be an HTTPS URL with a path, without credentials, "
            "a query, or a fragment"
        )
    return api_url.rstrip("?")


def load_masks(path: Path) -> list[str]:
    """Read unique masks from the optional MASK | GOROAWASE format."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read masks file {path}: {exc}") from exc

    masks: list[str] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(lines, start=1):
        content = raw_line.partition("#")[0].strip()
        if not content:
            continue
        fields = [field.strip() for field in content.split("|")]
        if len(fields) > 2:
            raise ValueError(f"{path}:{line_number}: too many '|' separators")
        if len(fields) == 2 and not fields[1]:
            raise ValueError(f"{path}:{line_number}: empty goroawase reading")
        try:
            mask = validate_mask(fields[0])
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: {exc}") from exc
        if mask in seen:
            raise ValueError(f"{path}:{line_number}: duplicate mask {mask}")
        masks.append(mask)
        seen.add(mask)

    if not masks:
        raise ValueError(f"no masks found in {path}")
    return masks


def make_opener() -> OpenerDirector:
    """Create one cookie-preserving HTTP client for the whole run."""
    return build_opener(HTTPCookieProcessor(CookieJar()), NoRedirectHandler())


def parse_offers(payload: object, mask: str) -> list[PhoneOffer]:
    if not isinstance(payload, dict):
        raise ResponseError("top-level JSON value is not an object")

    raw_offers = payload.get("randomPhoneNumbers")
    if not isinstance(raw_offers, list):
        raise ResponseError("randomPhoneNumbers is not an array")

    offers: list[PhoneOffer] = []
    for index, item in enumerate(raw_offers):
        if not isinstance(item, dict):
            raise ResponseError(f"randomPhoneNumbers[{index}] is not an object")

        phone_number = item.get("phoneNumber")
        offer_id = item.get("id")
        if not isinstance(phone_number, str) or not PHONE_RE.fullmatch(phone_number):
            raise ResponseError(
                f"randomPhoneNumbers[{index}].phoneNumber is not an 11-digit "
                "Japanese mobile number"
            )
        if not phone_number.endswith(mask):
            raise ResponseError(
                f"phone number {phone_number!r} does not end with mask {mask!r}"
            )
        if not isinstance(offer_id, str) or not ID_RE.fullmatch(offer_id):
            raise ResponseError(
                f"randomPhoneNumbers[{index}].id is not a decimal string"
            )
        offers.append(PhoneOffer(phone_number=phone_number, offer_id=offer_id))

    return offers


def fetch_offers(
    opener: object,
    api_url: str,
    mask: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[PhoneOffer]:
    if not isinstance(opener, OpenerDirector):
        raise TypeError("default fetcher requires an OpenerDirector")
    url = f"{api_url}?{urlencode({'mask': mask})}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "raku-mobi-bangou/0.2",
        },
        method="GET",
    )
    with opener.open(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ResponseError(
                f"response exceeds the {MAX_RESPONSE_BYTES}-byte safety limit"
            )
        try:
            decoded = body.decode(charset)
        except LookupError as exc:
            raise ResponseError("response declares an unsupported charset") from exc
        payload = json.loads(decoded)
    return parse_offers(payload, mask)


def read_existing(
    path: Path,
    expected_mask: str | None = None,
) -> list[PhoneOffer]:
    if not path.exists():
        return []

    try:
        source = path.open("r", encoding="utf-8", newline="")
    except OSError as exc:
        raise ValueError(f"cannot read output file {path}: {exc}") from exc

    existing: list[PhoneOffer] = []
    seen: set[PhoneOffer] = set()
    with source:
        reader = csv.DictReader(source)
        if reader.fieldnames != ["phoneNumber", "id"]:
            raise ValueError(
                f"{path}: expected CSV header 'phoneNumber,id', got "
                f"{reader.fieldnames!r}"
            )
        for line_number, row in enumerate(reader, start=2):
            if None in row or set(row) != {"phoneNumber", "id"}:
                raise ValueError(f"{path}:{line_number}: malformed CSV row")
            phone_number = row["phoneNumber"]
            offer_id = row["id"]
            if not PHONE_RE.fullmatch(phone_number) or not ID_RE.fullmatch(offer_id):
                raise ValueError(f"{path}:{line_number}: invalid phone number or id")
            if expected_mask is not None and not phone_number.endswith(expected_mask):
                raise ValueError(
                    f"{path}:{line_number}: phone does not match mask {expected_mask}"
                )
            offer = PhoneOffer(phone_number, offer_id)
            if offer in seen:
                raise ValueError(
                    f"{path}:{line_number}: duplicate phoneNumber and id pair"
                )
            seen.add(offer)
            existing.append(offer)
    return existing


def _parse_nonnegative_int(value: str, label: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise ValueError(f"{label}: expected a non-negative integer")
    return int(value)


def load_lifecycle_statuses(path: Path) -> dict[str, tuple[str, str]]:
    """Load the strict lifecycle identity and status fields needed for scanning."""
    try:
        source = path.open("r", encoding="utf-8", newline="")
    except OSError as exc:
        raise ValueError(f"cannot read lifecycle CSV {path}: {exc}") from exc

    records: dict[str, tuple[str, str]] = {}
    with source:
        reader = csv.DictReader(source)
        if reader.fieldnames != list(LIFECYCLE_FIELDS):
            raise ValueError(f"{path}: unsupported lifecycle CSV header")
        for line_number, row in enumerate(reader, start=2):
            if None in row or set(row) != set(LIFECYCLE_FIELDS):
                raise ValueError(f"{path}:{line_number}: malformed lifecycle row")
            phone = row["phoneNumber"]
            mask = row["sourceMask"]
            status = row["status"]
            if (
                not PHONE_RE.fullmatch(phone)
                or not MASK_RE.fullmatch(mask)
                or not phone.endswith(mask)
                or status not in LIFECYCLE_STATUSES
            ):
                raise ValueError(f"{path}:{line_number}: invalid lifecycle identity")
            if phone in records:
                raise ValueError(f"{path}:{line_number}: duplicate lifecycle phone")
            records[phone] = (mask, status)
    return records


def coverage_pools(
    masks: Sequence[str],
    store: OfferStore,
    lifecycle: Mapping[str, tuple[str, str]] | None,
) -> dict[str, frozenset[str]]:
    """Build the exact global active pool used as the scan denominator."""
    result: dict[str, frozenset[str]] = {}
    for mask in masks:
        initial = store.initial_phones(mask)
        if lifecycle is None:
            result[mask] = initial
            continue
        for phone in initial:
            lifecycle_identity = lifecycle.get(phone)
            if lifecycle_identity is None:
                raise ValueError(
                    f"stored history phone {phone} is absent from lifecycle"
                )
            if lifecycle_identity[0] != mask:
                raise ValueError(
                    f"lifecycle phone {phone} belongs to mask "
                    f"{lifecycle_identity[0]}, expected {mask}"
                )
        result[mask] = frozenset(
            phone
            for phone, (source_mask, status) in lifecycle.items()
            if source_mask == mask
            and status not in LIFECYCLE_EXCLUDED_FROM_COVERAGE
        )
    return result


def historical_pools(
    masks: Sequence[str],
    store: OfferStore,
    lifecycle: Mapping[str, tuple[str, str]] | None,
) -> dict[str, frozenset[str]]:
    """Build each mask's complete at-start identity universe."""
    result: dict[str, frozenset[str]] = {}
    for mask in masks:
        phones = set(store.initial_phones(mask))
        if lifecycle is not None:
            phones.update(
                phone
                for phone, (source_mask, _status) in lifecycle.items()
                if source_mask == mask
            )
        result[mask] = frozenset(phones)
    return result


def write_coverage_pool(
    path: Path,
    masks: Sequence[str],
    pools: Mapping[str, Collection[str]],
) -> None:
    """Persist the exact identity-level denominator consumed by this run."""
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(COVERAGE_POOL_FIELDS)
            for mask in sorted(masks):
                for phone in sorted(pools.get(mask, ())):
                    if not PHONE_RE.fullmatch(phone) or not phone.endswith(mask):
                        raise ValueError(
                            f"coverage pool phone {phone!r} does not match mask {mask}"
                        )
                    writer.writerow((phone, mask))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(f"cannot write coverage pool {path}: {exc}") from exc


def load_scan_history(path: Path | None) -> list[ScanHistoryRecord]:
    if path is None:
        return []
    try:
        source = path.open("r", encoding="utf-8", newline="")
    except OSError as exc:
        raise ValueError(f"cannot read scan history {path}: {exc}") from exc

    records: list[ScanHistoryRecord] = []
    seen: set[tuple[str, str]] = set()
    per_mask_counts: dict[str, int] = {}
    previous_key: tuple[str, str] | None = None
    with source:
        reader = csv.DictReader(source)
        if reader.fieldnames != list(SCAN_HISTORY_FIELDS):
            raise ValueError(f"{path}: unsupported scan history CSV header")
        for line_number, row in enumerate(reader, start=2):
            if None in row or set(row) != set(SCAN_HISTORY_FIELDS):
                raise ValueError(f"{path}:{line_number}: malformed scan history row")
            try:
                datetime.strptime(row["observedAt"], "%Y-%m-%dT%H:%M:%SZ")
            except ValueError as exc:
                raise ValueError(
                    f"{path}:{line_number}: invalid observedAt timestamp"
                ) from exc
            mask = row["mask"]
            if not MASK_RE.fullmatch(mask):
                raise ValueError(f"{path}:{line_number}: invalid mask")
            key = (row["observedAt"], mask)
            if key in seen:
                raise ValueError(f"{path}:{line_number}: duplicate scan history row")
            if previous_key is not None and key < previous_key:
                raise ValueError(f"{path}:{line_number}: scan history is not sorted")
            seen.add(key)
            previous_key = key

            numeric = {
                field: _parse_nonnegative_int(row[field], f"{path}:{line_number}")
                for field in MASK_SUMMARY_FIELDS[1:]
                if field
                not in {"achievedCoverageBps", "stopReason", "comparable"}
            }
            achieved_raw = row["achievedCoverageBps"]
            achieved = (
                None
                if achieved_raw == ""
                else _parse_nonnegative_int(achieved_raw, f"{path}:{line_number}")
            )
            if row["stopReason"] not in STOP_REASONS:
                raise ValueError(f"{path}:{line_number}: invalid stop reason")
            if row["comparable"] not in {"true", "false"}:
                raise ValueError(f"{path}:{line_number}: invalid comparable flag")
            historical = numeric["historicalDistinctAtStart"]
            pool = numeric["coveragePoolAtStart"]
            responses = numeric["successfulResponses"]
            requests = numeric["httpRequests"]
            retries = numeric["retries"]
            observed = numeric["observedPhoneCount"]
            known = numeric["observedKnownPhoneCount"]
            new = numeric["observedNewPhoneCount"]
            estimate = numeric["estimatedRequestBudget"]
            cap = numeric["requestCap"]
            round_limit = numeric["roundLimit"]
            expected_achieved = None if pool == 0 else min(10_000, 10_000 * known // pool)
            target = numeric["targetCoverageBps"]
            planning_target = numeric["planningCoverageBps"]
            if not (
                pool <= historical
                and known <= pool
                and observed == known + new
                and numeric["responsePhoneSamples"] >= observed
                and numeric["emptyResponses"] <= responses
                and requests == responses + retries
                and cap == min(estimate, round_limit)
                and achieved == expected_achieved
                and 1 <= target < 10_000
                and planning_target
                == max(target, DEFAULT_PLANNING_COVERAGE_BPS)
                and round_limit > 0
            ):
                raise ValueError(f"{path}:{line_number}: inconsistent scan history row")
            target_count = (
                pool * target + 9_999
            ) // 10_000
            coverage_stop = (
                row["stopReason"] == "coverage_target"
                and responses >= MIN_PROBES
                and known >= target_count
            )
            request_cap_stop = (
                row["stopReason"] == "request_cap"
                and estimate <= round_limit
                and cap == estimate
                and responses == cap
            )
            empty_probe_stop = (
                row["stopReason"] == "empty_probe_limit"
                and responses == EMPTY_PROBE_LIMIT
                and numeric["emptyResponses"] == EMPTY_PROBE_LIMIT
            )
            sampling_saturated_stop = (
                row["stopReason"] == "sampling_saturated"
                and pool > 0
                and responses >= MIN_PROBES
                and numeric["responsePhoneSamples"]
                >= WARM_NO_PROGRESS_SAMPLE_LIMIT
            )
            if (
                row["stopReason"] == "sampling_saturated"
                and not sampling_saturated_stop
            ):
                raise ValueError(
                    f"{path}:{line_number}: inconsistent sampling saturation"
                )
            expected_comparable = bool(
                pool > 0
                and (
                    empty_probe_stop
                    or (
                        responses > numeric["emptyResponses"]
                        and (coverage_stop or request_cap_stop)
                    )
                )
            )
            if (row["comparable"] == "true") != expected_comparable:
                raise ValueError(
                    f"{path}:{line_number}: inconsistent scan history comparable flag"
                )
            per_mask_counts[mask] = per_mask_counts.get(mask, 0) + 1
            if per_mask_counts[mask] > SCAN_HISTORY_ROWS_PER_MASK:
                raise ValueError(f"{path}: too many scan history rows for mask {mask}")
            values = tuple(row[field] for field in MASK_SUMMARY_FIELDS)
            records.append(ScanHistoryRecord(row["observedAt"], values))
    return records


def _lower_quartile(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a lower quartile of no values")
    return ordered[max(0, math.ceil(0.25 * len(ordered)) - 1)]


def estimate_request_budget(
    *,
    pool_size: int,
    target_coverage_bps: int,
    round_limit: int,
    history: Sequence[ScanHistoryRecord],
) -> int:
    """Estimate total successful responses using conservative sampling hazard."""
    if pool_size <= 0:
        return round_limit
    recent = list(history[-SCAN_HISTORY_WINDOW:])
    empirical_hazards: list[float] = []
    batch_sizes: list[float] = []
    indexes = {field: index for index, field in enumerate(MASK_SUMMARY_FIELDS)}
    for record in recent:
        values = record.values
        responses = int(values[indexes["successfulResponses"]])
        prior_pool = int(values[indexes["coveragePoolAtStart"]])
        known = int(values[indexes["observedKnownPhoneCount"]])
        samples = int(values[indexes["responsePhoneSamples"]])
        if responses <= 0:
            continue
        if samples > 0:
            batch_sizes.append(samples / responses)
        if prior_pool > 0 and 0 < known < prior_pool:
            empirical_hazards.append(-math.log1p(-known / prior_pool) / responses)

    batch = _lower_quartile(batch_sizes) if batch_sizes else DEFAULT_EFFECTIVE_BATCH
    iid_probability = min(batch / pool_size, 0.99)
    iid_hazard = -math.log1p(-iid_probability)
    planning_hazard = (
        min(iid_hazard, _lower_quartile(empirical_hazards))
        if empirical_hazards
        else iid_hazard
    )
    if planning_hazard <= 0:
        return round_limit
    target = target_coverage_bps / 10_000
    return max(MIN_PROBES, math.ceil(-math.log1p(-target) / planning_hazard))


def historical_new_phone_yield(
    history: Sequence[ScanHistoryRecord],
) -> float:
    """Return the recent first-seen-phone yield per successful response."""
    indexes = {field: index for index, field in enumerate(MASK_SUMMARY_FIELDS)}
    responses = sum(
        int(record.values[indexes["successfulResponses"]])
        for record in history[-SCAN_HISTORY_WINDOW:]
    )
    if responses == 0:
        return 0.0
    new_phones = sum(
        int(record.values[indexes["observedNewPhoneCount"]])
        for record in history[-SCAN_HISTORY_WINDOW:]
    )
    return new_phones / responses


def expected_useful_gain(
    mask_stats: MaskStats,
    prior_new_phone_yield: float = 0.0,
) -> float:
    """Estimate useful distinct phones returned by the next response."""
    responses = mask_stats.successful_responses
    batch = (
        mask_stats.response_phone_samples / responses
        if responses > 0 and mask_stats.response_phone_samples > 0
        else DEFAULT_EFFECTIVE_BATCH
    )
    pool = mask_stats.coverage_pool_at_start
    known = len(mask_stats.observed_known_phones)
    deficit = max(0, mask_stats.target_phone_count - known)
    if pool > 0 and deficit > 0:
        unseen_probability = max(0, pool - known) / pool
        target_deficit = deficit / max(1, mask_stats.target_phone_count)
        observed_distinct_yield = len(mask_stats.observed_phones) / max(1, responses)
        sampling_efficiency = min(1.0, observed_distinct_yield / max(batch, 1.0))
        expected_known = min(
            float(deficit),
            batch * unseen_probability * target_deficit * sampling_efficiency,
        )
    else:
        expected_known = 0.0
    current_new_yield = (
        len(mask_stats.observed_new_phones) / responses if responses > 0 else 0.0
    )
    return expected_known + current_new_yield + prior_new_phone_yield


def write_scan_history(
    path: Path,
    previous: Sequence[ScanHistoryRecord],
    masks: Sequence[str],
    stats: CollectionStats,
    observed_at: str,
) -> None:
    combined = list(previous)
    combined.extend(
        ScanHistoryRecord(observed_at, stats.mask_stats[mask].row(mask))
        for mask in masks
    )
    retained: list[ScanHistoryRecord] = []
    by_mask: dict[str, list[ScanHistoryRecord]] = {}
    for record in combined:
        by_mask.setdefault(record.mask, []).append(record)
    for records in by_mask.values():
        retained.extend(records[-SCAN_HISTORY_ROWS_PER_MASK:])
    retained.sort(key=lambda record: (record.observed_at, record.mask))

    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(SCAN_HISTORY_FIELDS)
            writer.writerows(record.as_row() for record in retained)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(f"cannot write scan history {path}: {exc}") from exc


def retry_after_seconds(error: HTTPError, now: float | None = None) -> float | None:
    raw_value = error.headers.get("Retry-After") if error.headers else None
    if not raw_value:
        return None
    raw_value = raw_value.strip()
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(raw_value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    reference = time.time() if now is None else now
    return max(0.0, parsed.timestamp() - reference)


def sanitized_error(error: BaseException) -> str:
    if isinstance(error, HTTPError):
        reason = URL_RE.sub("[redacted-url]", str(error.reason))
        return f"HTTP {error.code} {reason}"
    if isinstance(error, TimeoutError):
        return "request timed out"
    if isinstance(error, URLError):
        return f"network error ({type(error.reason).__name__})"
    if isinstance(error, HTTPException):
        return f"HTTP protocol error ({type(error).__name__})"
    if isinstance(error, OSError):
        return f"network error ({type(error).__name__})"
    if isinstance(error, json.JSONDecodeError):
        return "response is not valid JSON"
    if isinstance(error, UnicodeError):
        return "response text encoding is invalid"
    return URL_RE.sub("[redacted-url]", str(error))


def retryable_error(error: BaseException) -> bool:
    if isinstance(error, HTTPError):
        return error.code in {408, 425, 429} or 500 <= error.code <= 599
    return isinstance(
        error,
        (
            URLError,
            TimeoutError,
            HTTPException,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
        ),
    )


def backoff_seconds(
    error: BaseException,
    attempt: int,
    random_delay: Callable[[float, float], float],
) -> float:
    if isinstance(error, HTTPError):
        retry_after = retry_after_seconds(error)
        if retry_after is not None:
            if retry_after > MAX_RETRY_AFTER:
                raise FatalCollectionError(
                    "server requested a Retry-After interval longer than this run "
                    "will wait"
                )
            return retry_after

    if isinstance(error, HTTPError) and error.code == 429:
        ceiling = min(
            DEFAULT_BACKOFF_CAP,
            DEFAULT_RATE_LIMIT_DELAY * (2 ** (attempt - 1)),
        )
        return random_delay(ceiling * 0.8, ceiling)

    ceiling = min(DEFAULT_BACKOFF_CAP, float(2**attempt))
    return random_delay(ceiling / 2.0, ceiling)


def collect(
    masks: Sequence[str],
    output_dir: Path,
    rounds: int,
    delay_min: float,
    delay_max: float,
    timeout: float,
    *,
    api_url: str,
    target_coverage_bps: int = DEFAULT_TARGET_COVERAGE_BPS,
    request_limit: int = DEFAULT_REQUEST_LIMIT,
    deep_scan: bool = False,
    mask_cooldown: float = DEFAULT_MASK_COOLDOWN,
    request_attempts: int = DEFAULT_REQUEST_ATTEMPTS,
    historical_pool_by_mask: Mapping[str, Collection[str]] | None = None,
    coverage_pool_by_mask: Mapping[str, Collection[str]] | None = None,
    scan_history: Sequence[ScanHistoryRecord] = (),
    opener: object | None = None,
    fetch: FetchOffers | None = None,
    store: OfferStore | None = None,
    stats: CollectionStats | None = None,
    sleep: Callable[[float], None] = time.sleep,
    random_delay: Callable[[float, float], float] = random.uniform,
    monotonic: Callable[[], float] = time.monotonic,
) -> CollectionStats:
    """Collect sequentially while owning any store created by this call."""
    owned_store = store is None
    offer_store = store or OfferStore(output_dir, masks)
    try:
        return _collect_locked(
            masks=masks,
            output_dir=output_dir,
            rounds=rounds,
            delay_min=delay_min,
            delay_max=delay_max,
            timeout=timeout,
            api_url=api_url,
            target_coverage_bps=target_coverage_bps,
            request_limit=request_limit,
            deep_scan=deep_scan,
            mask_cooldown=mask_cooldown,
            request_attempts=request_attempts,
            historical_pool_by_mask=historical_pool_by_mask,
            coverage_pool_by_mask=coverage_pool_by_mask,
            scan_history=scan_history,
            opener=opener,
            fetch=fetch,
            store=offer_store,
            stats=stats,
            sleep=sleep,
            random_delay=random_delay,
            monotonic=monotonic,
        )
    finally:
        if owned_store:
            offer_store.close()


def _collect_locked(
    masks: Sequence[str],
    output_dir: Path,
    rounds: int,
    delay_min: float,
    delay_max: float,
    timeout: float,
    *,
    api_url: str,
    target_coverage_bps: int,
    request_limit: int,
    deep_scan: bool,
    mask_cooldown: float,
    request_attempts: int,
    historical_pool_by_mask: Mapping[str, Collection[str]] | None,
    coverage_pool_by_mask: Mapping[str, Collection[str]] | None,
    scan_history: Sequence[ScanHistoryRecord],
    opener: object | None,
    fetch: FetchOffers | None,
    store: OfferStore,
    stats: CollectionStats | None,
    sleep: Callable[[float], None],
    random_delay: Callable[[float, float], float],
    monotonic: Callable[[], float],
) -> CollectionStats:
    """Collect sequentially; routine details go to files, not the console."""
    del output_dir  # The validated, locked store owns all persistent output.
    client = opener or make_opener()
    fetcher = fetch or fetch_offers
    offer_store = store
    active_masks = list(masks)
    round_limit = rounds
    if round_limit < 1:
        raise ValueError("round limit must be at least 1")
    if not 1 <= target_coverage_bps < 10_000:
        raise ValueError("target coverage must be between 1 and 9999 basis points")
    if request_limit < 1:
        raise ValueError("request limit must be at least 1")
    if mask_cooldown < 0 or not math.isfinite(mask_cooldown):
        raise ValueError("mask cooldown must be a finite non-negative number")

    history_by_mask: dict[str, list[ScanHistoryRecord]] = {}
    for record in scan_history:
        history_by_mask.setdefault(record.mask, []).append(record)
    pool_sets: dict[str, frozenset[str]] = {}
    cold_no_progress = {mask: 0 for mask in masks}
    warm_no_progress_samples = {mask: 0 for mask in masks}
    consecutive_empty = {mask: 0 for mask in masks}
    last_logical_request_at: dict[str, float] = {}
    mask_order = {mask: index for index, mask in enumerate(masks)}
    prior_new_yields = {
        mask: historical_new_phone_yield(history_by_mask.get(mask, ()))
        for mask in masks
    }
    priority_turn = 0
    last_priority_turn = {mask: 0 for mask in masks}
    stats = stats or CollectionStats()
    stats.target_coverage_bps = target_coverage_bps
    planning_coverage_bps = max(
        target_coverage_bps,
        DEFAULT_PLANNING_COVERAGE_BPS,
    )
    stats.planning_coverage_bps = planning_coverage_bps
    stats.request_limit = request_limit
    stats.deep_scan = deep_scan
    for mask in masks:
        initial = offer_store.initial_phones(mask)
        supplied_history = (
            initial
            if historical_pool_by_mask is None
            else frozenset(historical_pool_by_mask.get(mask, initial))
        )
        supplied_pool = (
            initial
            if coverage_pool_by_mask is None
            else frozenset(coverage_pool_by_mask.get(mask, initial))
        )
        if not initial <= supplied_history or not supplied_pool <= supplied_history:
            raise ValueError(
                f"historical/coverage pool for mask {mask} is inconsistent"
            )
        if any(
            not PHONE_RE.fullmatch(phone) or not phone.endswith(mask)
            for phone in supplied_history
        ):
            raise ValueError(f"historical pool for mask {mask} has invalid identity")
        pool_sets[mask] = supplied_pool
        estimated = estimate_request_budget(
            pool_size=len(supplied_pool),
            target_coverage_bps=planning_coverage_bps,
            round_limit=round_limit,
            history=history_by_mask.get(mask, ()),
        )
        mask_stats = stats.mask_stats.setdefault(mask, MaskStats())
        mask_stats.historical_distinct_at_start = len(supplied_history)
        mask_stats.coverage_pool_at_start = len(supplied_pool)
        mask_stats.target_coverage_bps = target_coverage_bps
        mask_stats.planning_coverage_bps = planning_coverage_bps
        mask_stats.estimated_request_budget = estimated
        mask_stats.round_limit = round_limit
        mask_stats.request_cap = min(estimated, round_limit)
        mask_stats.stop_reason = "round_limit"
    stats.active_masks = len(active_masks)
    request_count = 0
    start_time = monotonic()

    def run_request(mask: str, required_wait: float = 0.0) -> list[PhoneOffer]:
        nonlocal request_count
        if stats.requests >= request_limit:
            raise GlobalRequestLimitReached
        if request_count:
            ordinary_wait = random_delay(delay_min, delay_max)
            delay = max(ordinary_wait, required_wait)
            log_event(
                logging.DEBUG,
                "request_wait",
                mask=mask,
                seconds=round(delay, 3),
            )
            sleep(delay)
        request_count += 1
        stats.requests += 1
        stats.mask_stats[mask].http_requests += 1
        try:
            return fetcher(client, api_url, mask, timeout)
        finally:
            stats.elapsed_seconds = monotonic() - start_time

    log_event(
        logging.INFO,
        "collection_started",
        masks=len(masks),
        rounds=rounds,
        delayMin=delay_min,
        delayMax=delay_max,
        timeout=timeout,
        targetCoverageBps=target_coverage_bps,
        planningCoverageBps=planning_coverage_bps,
        requestLimit=request_limit,
        deepScan=deep_scan,
        maskCooldown=mask_cooldown,
        roundLimit=round_limit,
        requestAttempts=request_attempts,
    )

    def deactivate(mask: str, reason: str) -> None:
        if mask not in active_masks:
            return
        active_masks.remove(mask)
        mask_stats = stats.mask_stats[mask]
        mask_stats.stop_reason = reason
        if reason == "coverage_target":
            stats.deactivated_coverage += 1
        elif reason == "empty_probe_limit":
            stats.deactivated_empty_probe += 1
        elif reason == "cold_start_saturated":
            stats.deactivated_cold_saturated += 1
        elif reason == "sampling_saturated":
            stats.deactivated_sampling_saturated += 1
        elif reason == "request_cap":
            stats.deactivated_request_cap += 1
        elif reason == "round_limit":
            stats.deactivated_round_limit += 1
        elif reason == "global_request_limit":
            stats.deactivated_global_limit += 1
        stats.active_masks = len(active_masks)
        log_event(
            logging.INFO,
            "mask_deactivated",
            mask=mask,
            reason=reason,
            responses=mask_stats.successful_responses,
            knownObserved=len(mask_stats.observed_known_phones),
            coverageBps=mask_stats.achieved_coverage_bps,
            requestCap=mask_stats.request_cap,
            samplingSlotsSinceProgress=warm_no_progress_samples[mask],
        )

    def select_priority_mask() -> tuple[str, float]:
        """Choose useful work without hammering or starving any active mask."""
        nonlocal priority_turn
        now = monotonic()
        waits = {
            mask: max(
                0.0,
                mask_cooldown - (now - last_logical_request_at[mask]),
            )
            if mask in last_logical_request_at
            else 0.0
            for mask in active_masks
        }
        ready = [mask for mask in active_masks if waits[mask] <= 0.0]
        if ready:
            candidates = ready
        else:
            earliest_wait = min(waits.values())
            candidates = [
                mask
                for mask in active_masks
                if math.isclose(waits[mask], earliest_wait, abs_tol=1e-9)
            ]

        next_turn = priority_turn + 1
        starvation_limit = max(
            1,
            PRIORITY_STARVATION_CYCLES * len(active_masks),
        )
        starved = [
            mask
            for mask in candidates
            if next_turn - last_priority_turn[mask] >= starvation_limit
        ]

        def score(mask: str) -> float:
            gain = expected_useful_gain(
                stats.mask_stats[mask],
                prior_new_yields[mask],
            )
            plateau_discount = 1.0 + (
                warm_no_progress_samples[mask]
                / WARM_NO_PROGRESS_SAMPLE_LIMIT
            )
            return gain / plateau_discount

        if starved:
            selected = max(
                starved,
                key=lambda mask: (
                    next_turn - last_priority_turn[mask],
                    -mask_order[mask],
                ),
            )
        else:
            selected = max(
                candidates,
                key=lambda mask: (
                    score(mask),
                    next_turn - last_priority_turn[mask],
                    -mask_order[mask],
                ),
            )
        selected_age = next_turn - last_priority_turn[selected]
        priority_turn = next_turn
        last_priority_turn[selected] = priority_turn
        log_event(
            logging.DEBUG,
            "priority_mask_selected",
            mask=selected,
            expectedUsefulGain=round(score(selected), 6),
            starvationOverride=selected in starved,
            starvationAge=selected_age,
            cooldownWait=round(waits[selected], 3),
        )
        return selected, waits[selected]

    try:
        while active_masks:
            round_requests_start = stats.requests
            round_responses_start = stats.responses
            round_retries_start = stats.retries
            round_received_start = stats.received
            round_added_start = stats.added_phones
            active_at_round_start = len(active_masks)

            bootstrap_masks = [
                mask
                for mask in active_masks
                if stats.mask_stats[mask].successful_responses < MIN_PROBES
            ]
            priority_phase = not bootstrap_masks
            scheduled_masks: list[str | None] = (
                list(bootstrap_masks)
                if bootstrap_masks
                else [None] * len(active_masks)
            )

            # Every active mask gets its first MIN_PROBES in fair input order.
            # Only the remaining budget is allocated by expected useful gain.
            for scheduled_mask in scheduled_masks:
                if not active_masks:
                    break
                if priority_phase:
                    mask, required_wait = select_priority_mask()
                else:
                    if scheduled_mask not in active_masks:
                        continue
                    mask = scheduled_mask
                    required_wait = 0.0
                offers: list[PhoneOffer] | None = None
                if not priority_phase:
                    prior_request = last_logical_request_at.get(mask)
                    now = monotonic()
                    if prior_request is not None:
                        required_wait = max(
                            0.0,
                            mask_cooldown - (now - prior_request),
                        )

                for attempt in range(1, request_attempts + 1):
                    try:
                        offers = run_request(mask, required_wait)
                        stats.responses += 1
                        break
                    except GlobalRequestLimitReached:
                        raise
                    except (
                        HTTPError,
                        HTTPException,
                        URLError,
                        TimeoutError,
                        OSError,
                        UnicodeError,
                        json.JSONDecodeError,
                        ResponseError,
                    ) as error:
                        description = sanitized_error(error)
                        try:
                            if not retryable_error(error):
                                log_event(
                                    logging.CRITICAL,
                                    "non_retryable_response",
                                    mask=mask,
                                    attempt=attempt,
                                    error=description,
                                )
                                raise FatalCollectionError(
                                    f"mask {mask}: non-retryable endpoint response: "
                                    f"{description}"
                                ) from error

                            if attempt >= request_attempts:
                                stats.failed_requests += 1
                                exhausted_fields: dict[str, object] = {
                                    "mask": mask,
                                    "attempts": request_attempts,
                                    "error": description,
                                }
                                if isinstance(error, HTTPError):
                                    retry_after = retry_after_seconds(error)
                                    if retry_after is not None:
                                        exhausted_fields["retryAfterSeconds"] = round(
                                            retry_after, 3
                                        )
                                log_event(
                                    logging.CRITICAL,
                                    "request_exhausted",
                                    **exhausted_fields,
                                )
                                raise FatalCollectionError(
                                    f"mask {mask}: request exhausted after "
                                    f"{request_attempts} attempts: {description}"
                                ) from error

                            required_wait = backoff_seconds(error, attempt, random_delay)
                            stats.retries += 1
                            stats.mask_stats[mask].retries += 1
                            log_event(
                                logging.WARNING,
                                "request_retry",
                                mask=mask,
                                attempt=attempt,
                                maxAttempts=request_attempts,
                                error=description,
                                backoffSeconds=round(required_wait, 3),
                            )
                        finally:
                            if isinstance(error, HTTPError):
                                error.close()

                if offers is None:
                    raise FatalCollectionError(
                        f"mask {mask}: request ended without a response"
                    )
                last_logical_request_at[mask] = monotonic()

                response_phones = {offer.phone_number for offer in offers}
                mask_stats = stats.mask_stats[mask]
                newly_observed = response_phones - mask_stats.observed_phones
                try:
                    appended = offer_store.append(mask, offers)
                except ValueError as exc:
                    log_event(
                        logging.CRITICAL,
                        "csv_write_failed",
                        mask=mask,
                        error=str(exc),
                    )
                    raise FatalCollectionError(str(exc)) from exc

                stats.received += len(offers)
                stats.added_rows += appended.new_rows
                stats.added_phones += appended.new_phones
                mask_stats.successful_responses += 1
                stats.rounds = max(
                    stats.rounds,
                    mask_stats.successful_responses,
                )
                mask_stats.response_phone_samples += len(response_phones)
                if mask_stats.coverage_pool_at_start > 0:
                    if newly_observed:
                        warm_no_progress_samples[mask] = 0
                    else:
                        warm_no_progress_samples[mask] += len(response_phones)
                if not response_phones:
                    mask_stats.empty_responses += 1
                    consecutive_empty[mask] += 1
                else:
                    consecutive_empty[mask] = 0
                mask_stats.observed_phones.update(response_phones)
                known = response_phones & pool_sets[mask]
                mask_stats.observed_known_phones.update(known)
                mask_stats.observed_new_phones.update(response_phones - pool_sets[mask])
                log_event(
                    logging.INFO,
                    "response_processed",
                    mask=mask,
                    received=len(offers),
                    sampledPhones=len(response_phones),
                    newThisRun=len(newly_observed),
                    knownObserved=len(mask_stats.observed_known_phones),
                    addedRows=appended.new_rows,
                    addedPhones=appended.new_phones,
                )

                if (
                    mask_stats.coverage_pool_at_start > 0
                    and mask_stats.successful_responses >= MIN_PROBES
                    and len(mask_stats.observed_known_phones)
                    >= mask_stats.target_phone_count
                ):
                    deactivate(mask, "coverage_target")
                    continue

                if consecutive_empty[mask] >= EMPTY_PROBE_LIMIT:
                    deactivate(mask, "empty_probe_limit")
                    continue

                if (
                    not deep_scan
                    and mask_stats.coverage_pool_at_start > 0
                    and mask_stats.successful_responses >= MIN_PROBES
                    and warm_no_progress_samples[mask]
                    >= WARM_NO_PROGRESS_SAMPLE_LIMIT
                ):
                    deactivate(mask, "sampling_saturated")
                    continue

                if mask_stats.coverage_pool_at_start == 0:
                    if response_phones:
                        if newly_observed:
                            cold_no_progress[mask] = 0
                        else:
                            cold_no_progress[mask] += 1
                    if (
                        mask_stats.successful_responses >= COLD_MIN_RESPONSES
                        and cold_no_progress[mask] >= COLD_NO_PROGRESS_LIMIT
                    ):
                        deactivate(mask, "cold_start_saturated")
                        continue
                elif (
                    mask_stats.successful_responses >= ONLINE_ESTIMATE_MIN_RESPONSES
                    and 0 < len(mask_stats.observed_known_phones)
                    < mask_stats.coverage_pool_at_start
                ):
                    coverage = (
                        len(mask_stats.observed_known_phones)
                        / mask_stats.coverage_pool_at_start
                    )
                    online_hazard = -math.log1p(-coverage) / mask_stats.successful_responses
                    online_estimate = max(
                        MIN_PROBES,
                        math.ceil(
                            -math.log1p(-planning_coverage_bps / 10_000)
                            / online_hazard
                        ),
                    )
                    if online_estimate > mask_stats.estimated_request_budget:
                        mask_stats.estimated_request_budget = online_estimate
                        mask_stats.request_cap = min(online_estimate, round_limit)

                if mask_stats.successful_responses >= mask_stats.request_cap:
                    if mask_stats.coverage_pool_at_start == 0:
                        reason = "round_limit"
                    else:
                        reason = (
                            "request_cap"
                            if mask_stats.estimated_request_budget <= round_limit
                            else "round_limit"
                        )
                    deactivate(mask, reason)

            stats.active_masks = len(active_masks)
            stats.elapsed_seconds = monotonic() - start_time
            print(
                f"round {stats.rounds}/{round_limit}: requests "
                f"{stats.requests - round_requests_start}, responses "
                f"{stats.responses - round_responses_start}, received "
                f"{stats.received - round_received_start}, new "
                f"{stats.added_phones - round_added_start}, retries "
                f"{stats.retries - round_retries_start}, deactivated "
                f"{active_at_round_start - len(active_masks)}, active "
                f"{len(active_masks)}",
                flush=True,
            )
    except GlobalRequestLimitReached:
        for mask in list(active_masks):
            deactivate(mask, "global_request_limit")
        log_event(
            logging.WARNING,
            "global_request_limit_reached",
            requestLimit=request_limit,
        )
    except BaseException:
        for mask in active_masks:
            stats.mask_stats[mask].stop_reason = "collection_fatal"
        raise

    stats.active_masks = len(active_masks)
    stats.elapsed_seconds = monotonic() - start_time
    log_event(logging.INFO, "collection_finished", **stats.as_dict())
    return stats


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than 0")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite non-negative number")
    return parsed


def coverage_bps(value: str) -> int:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be 0.80 or 0.90") from exc
    if parsed not in {0.8, 0.9}:
        raise argparse.ArgumentTypeError("must be 0.80 or 0.90")
    return round(parsed * 10_000)


def write_mask_summary(
    path: Path,
    masks: Sequence[str],
    stats: CollectionStats,
) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(MASK_SUMMARY_FIELDS)
            for mask in sorted(masks):
                mask_stats = stats.mask_stats.get(mask, MaskStats())
                writer.writerow(mask_stats.row(mask))
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(f"cannot write mask summary {path}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect Japanese mobile-number candidates into one CSV file per "
            "four-digit mask."
        )
    )
    parser.add_argument(
        "masks",
        nargs="*",
        metavar="MASK",
        help="four-digit masks; when omitted, read --masks-file",
    )
    parser.add_argument(
        "--rounds",
        type=positive_int,
        default=1,
        metavar="N",
        help="hard successful-response cap per mask (default: 1)",
    )
    parser.add_argument(
        "--api-url",
        help=f"HTTPS JSON endpoint; defaults to the {API_URL_ENV} environment variable",
    )
    parser.add_argument(
        "--masks-file",
        type=Path,
        default=DEFAULT_MASKS_FILE,
        help=f"default mask list (default: {DEFAULT_MASKS_FILE})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"directory for MASK.csv files (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=DEFAULT_RUN_DIR,
        help=f"directory for combined CSV and logs (default: {DEFAULT_RUN_DIR})",
    )
    parser.add_argument(
        "--baseline-csv",
        type=Path,
        help="previous all_numbers.csv used to create a cross-run diff",
    )
    parser.add_argument(
        "--lifecycle-csv",
        type=Path,
        help="optional lifecycle state used to exclude stale coverage targets",
    )
    parser.add_argument(
        "--scan-history",
        type=Path,
        help="optional prior per-mask adaptive scan history",
    )
    parser.add_argument(
        "--target-coverage",
        type=coverage_bps,
        default=DEFAULT_TARGET_COVERAGE_BPS,
        metavar="FRACTION",
        help="warm-mask coverage target: 0.80 or 0.90 (default: 0.90)",
    )
    parser.add_argument(
        "--request-limit",
        type=positive_int,
        default=DEFAULT_REQUEST_LIMIT,
        metavar="N",
        help=f"hard real HTTP request limit (default: {DEFAULT_REQUEST_LIMIT})",
    )
    parser.add_argument(
        "--deep-scan",
        action="store_true",
        help="continue warm masks past repeat-pool sampling saturation",
    )
    parser.add_argument(
        "--mask-cooldown",
        type=nonnegative_float,
        default=DEFAULT_MASK_COOLDOWN,
        metavar="SECONDS",
        help=(
            "minimum time between logical requests for the same mask "
            f"(default: {DEFAULT_MASK_COOLDOWN})"
        ),
    )
    parser.add_argument(
        "--delay-min",
        type=positive_float,
        default=DEFAULT_DELAY_MIN,
        metavar="SECONDS",
        help=f"minimum inter-request delay (default: {DEFAULT_DELAY_MIN})",
    )
    parser.add_argument(
        "--delay-max",
        type=positive_float,
        default=DEFAULT_DELAY_MAX,
        metavar="SECONDS",
        help=f"maximum inter-request delay (default: {DEFAULT_DELAY_MAX})",
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"HTTP timeout (default: {DEFAULT_TIMEOUT})",
    )
    return parser


def write_summary(
    path: Path,
    *,
    status: str,
    exit_code: int,
    mask_count: int,
    stats: CollectionStats | None,
    store: OfferStore | None,
) -> None:
    payload: dict[str, object] = {
        "schemaVersion": 2,
        "finishedAt": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "status": status,
        "exitCode": exit_code,
        "maskCount": mask_count,
        "uniquePhoneCount": store.phone_count if store else 0,
        "observedPhoneCount": store.observed_phone_count if store else 0,
        "observationCount": store.observation_count if store else 0,
        "collection": stats.as_dict() if stats else None,
    }
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
        raise ValueError(f"cannot write run summary {path}: {exc}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        run_dir = args.run_dir.resolve()
        output_dir = args.output_dir.resolve()
        baseline_path = args.baseline_csv.resolve() if args.baseline_csv else None
        lifecycle_path = (
            args.lifecycle_csv.resolve() if args.lifecycle_csv else None
        )
        scan_history_path = (
            args.scan_history.resolve() if args.scan_history else None
        )
        validate_runtime_paths(
            output_dir,
            run_dir,
            baseline_path,
            lifecycle_path,
            scan_history_path,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"fatal: {sanitized_error(exc)}", flush=True)
        return 1

    try:
        collector_log, _errors_log = configure_logging(run_dir)
    except OSError as exc:
        print(f"fatal: cannot initialize run logs: {exc}", flush=True)
        return 1

    masks: list[str] = []
    store: OfferStore | None = None
    stats: CollectionStats | None = None
    baseline_records: dict[str, tuple[str, str]] | None = None
    lifecycle_records: dict[str, tuple[str, str]] | None = None
    historical_pool_by_mask: dict[str, frozenset[str]] | None = None
    coverage_pool_by_mask: dict[str, frozenset[str]] | None = None
    previous_scan_history: list[ScanHistoryRecord] = []
    exit_code = 1
    status = "fatal"
    terminal_error: str | None = None
    completion_message: str | None = None

    try:
        if args.masks:
            masks = list(dict.fromkeys(validate_mask(mask) for mask in args.masks))
        else:
            masks = load_masks(args.masks_file)

        raw_api_url = args.api_url or os.environ.get(API_URL_ENV, "")
        if not raw_api_url:
            raise ValueError(
                f"set {API_URL_ENV} or pass --api-url before starting collection"
            )
        api_url = validate_api_url(raw_api_url)
        if args.delay_min > args.delay_max:
            raise ValueError("--delay-min cannot be greater than --delay-max")

        if baseline_path is not None:
            baseline_records = read_deduplicated(baseline_path)
        if lifecycle_path is not None:
            lifecycle_records = load_lifecycle_statuses(lifecycle_path)
        previous_scan_history = load_scan_history(scan_history_path)
        store = OfferStore(output_dir, masks)
        historical_pool_by_mask = historical_pools(masks, store, lifecycle_records)
        coverage_pool_by_mask = coverage_pools(masks, store, lifecycle_records)
        write_coverage_pool(
            run_dir / "coverage_pool.csv",
            masks,
            coverage_pool_by_mask,
        )
        print(
            f"start: masks {len(masks)}, rounds "
            f"{args.rounds}, existing "
            f"{store.phone_count}",
            flush=True,
        )
        stats = CollectionStats(
            active_masks=len(masks),
            mask_stats={mask: MaskStats() for mask in masks},
        )
        stats = collect(
            masks=masks,
            output_dir=output_dir,
            rounds=args.rounds,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
            timeout=args.timeout,
            api_url=api_url,
            target_coverage_bps=args.target_coverage,
            request_limit=args.request_limit,
            deep_scan=args.deep_scan,
            mask_cooldown=args.mask_cooldown,
            historical_pool_by_mask=historical_pool_by_mask,
            coverage_pool_by_mask=coverage_pool_by_mask,
            scan_history=previous_scan_history,
            store=store,
            stats=stats,
        )

        status = "success"
        exit_code = 0
        completion_message = (
            f"finished: rounds {stats.rounds}, requests {stats.requests}, "
            f"new {stats.added_phones}, total {store.phone_count}"
        )
    except KeyboardInterrupt:
        status = "interrupted"
        exit_code = 130
        log_event(logging.WARNING, "run_interrupted")
        terminal_error = "fatal: interrupted; completed writes are preserved"
    except (ValueError, FatalCollectionError) as exc:
        status = "fatal"
        exit_code = 1
        description = sanitized_error(exc)
        log_event(logging.CRITICAL, "run_fatal", error=description)
        terminal_error = f"fatal: {description}"
    except Exception as exc:  # Defensive boundary: diagnostics must survive.
        status = "fatal"
        exit_code = 1
        description = sanitized_error(exc)
        log_event(
            logging.CRITICAL,
            "unexpected_fatal",
            errorType=type(exc).__name__,
            error=description,
            traceback=traceback.format_tb(exc.__traceback__),
        )
        terminal_error = (
            f"fatal: unexpected {type(exc).__name__}; see {collector_log}"
        )
    finally:
        if store is not None:
            try:
                all_numbers_path = run_dir / "all_numbers.csv"
                exported = store.export_observed(all_numbers_path)
                log_event(
                    logging.INFO,
                    "observed_export_written",
                    phoneCount=exported,
                )
                if stats is not None:
                    write_mask_summary(
                        run_dir / "mask_summary.csv",
                        masks,
                        stats,
                    )
                diff_counts = write_run_diff(
                    current_path=all_numbers_path,
                    baseline_records=baseline_records,
                    scanned_masks=masks,
                    output_path=run_dir / "diff.csv",
                    summary_path=run_dir / "diff_summary.json",
                )
                log_event(logging.INFO, "cross_run_diff_written", **diff_counts)
                if status == "success" and stats is not None:
                    observed_at = (
                        datetime.now(timezone.utc)
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z")
                    )
                    write_scan_history(
                        run_dir / "scan_history.csv",
                        previous_scan_history,
                        masks,
                        stats,
                        observed_at,
                    )
            except ValueError as exc:
                status = "fatal"
                exit_code = 1
                log_event(
                    logging.CRITICAL,
                    "run_artifact_export_failed",
                    error=sanitized_error(exc),
                )
                terminal_error = f"fatal: {sanitized_error(exc)}"
        if store is not None:
            try:
                store.close()
            except OSError as exc:
                status = "fatal"
                exit_code = 1
                log_event(
                    logging.CRITICAL,
                    "output_lock_release_failed",
                    error=sanitized_error(exc),
                )
                terminal_error = "fatal: cannot release output-directory lock"
        try:
            write_summary(
                run_dir / "summary.json",
                status=status,
                exit_code=exit_code,
                mask_count=len(masks),
                stats=stats,
                store=store,
            )
        except ValueError as exc:
            status = "fatal"
            exit_code = 1
            log_event(
                logging.CRITICAL,
                "summary_write_failed",
                error=sanitized_error(exc),
            )
            terminal_error = f"fatal: {sanitized_error(exc)}"
        logging.shutdown()

    if exit_code == 0 and completion_message is not None:
        print(completion_message, flush=True)
    elif terminal_error is not None:
        print(terminal_error, flush=True)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
