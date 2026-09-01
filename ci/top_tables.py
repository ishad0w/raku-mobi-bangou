#!/usr/bin/env python3
"""Prepare and validate the release ranking tables."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


RAW_PHONE_RE = re.compile(r"0[0-9]{10}")
FORMATTED_PHONE_RE = re.compile(r"0[0-9]{2}-[0-9]{4}-[0-9]{4}")
MASK_RE = re.compile(r"[0-9]{4}")
ID_RE = re.compile(r"[0-9]+")
KANA_PREFIX_RE = re.compile(r"[ぁ-んァ-ヶ]")
TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
CANDIDATE_ID_RE = re.compile(r"[TGN][0-9]{3}")

DIGIT_JA = {
    "0": "ぜろ",
    "1": "いち",
    "2": "に",
    "3": "さん",
    "4": "よん",
    "5": "ご",
    "6": "ろく",
    "7": "なな",
    "8": "はち",
    "9": "きゅう",
}
MORA_COUNT = {
    "0": 2,
    "1": 2,
    "2": 1,
    "3": 2,
    "4": 2,
    "5": 1,
    "6": 2,
    "7": 2,
    "8": 2,
    "9": 2,
}
BASE_DIRECT_CANDIDATES = 100
MAX_DIRECT_CANDIDATES = 200
MAX_STRONG_PATTERN_CANDIDATES = 100
MAX_BALANCED_ECHO_CANDIDATES = 100
MAX_GOROAWASE_CANDIDATES = 100
MAX_NEWLY_FOUND_CANDIDATES = 100
DIVERSE_CANDIDATES_PER_MASK = 1
RANKING_LIMIT = 30
NEWLY_FOUND_RANKING_LIMIT = 10
CURRENT_FIELDS = ("phoneNumber", "id", "sourceMask")
DIFF_FIELDS = (
    "changeType",
    "phoneNumber",
    "previousId",
    "currentId",
    "sourceMask",
)
CURRENT_SOURCE_KIND = "currentSnapshot"
CONTEXT_SCHEMA_VERSION = 4
QUALITY_ORDER_BAND_SIZE = RANKING_LIMIT


class DataError(ValueError):
    """Input data cannot produce a trustworthy release table."""


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def formatted_phone(raw_number: str) -> str:
    if not RAW_PHONE_RE.fullmatch(raw_number):
        raise DataError(f"invalid phone number: {raw_number!r}")
    return f"{raw_number[:3]}-{raw_number[3:7]}-{raw_number[7:]}"


def raw_phone(formatted: str) -> str:
    if not FORMATTED_PHONE_RE.fullmatch(formatted):
        raise DataError(
            f"phone number must use 0XX-XXXX-XXXX format: {formatted!r}"
        )
    return formatted.replace("-", "")


def standard_reading(raw_number: str) -> str:
    if not RAW_PHONE_RE.fullmatch(raw_number):
        raise DataError(f"invalid phone number: {raw_number!r}")
    groups = (raw_number[:3], raw_number[3:7], raw_number[7:])
    return "｜".join("・".join(DIGIT_JA[digit] for digit in group) for group in groups)


def flow_reading(raw_number: str) -> str:
    if not RAW_PHONE_RE.fullmatch(raw_number):
        raise DataError(f"invalid phone number: {raw_number!r}")
    groups = (raw_number[:3], raw_number[3:7], raw_number[7:])
    return "｜".join("".join(DIGIT_JA[digit] for digit in group) for group in groups)


def load_mask_readings(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DataError(f"cannot read masks file {path}: {exc}") from exc

    readings: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        content = line.partition("#")[0].strip()
        if not content:
            continue
        fields = [field.strip() for field in content.split("|")]
        if len(fields) > 2:
            raise DataError(f"{path}:{line_number}: too many '|' separators")
        mask = fields[0]
        reading = fields[1] if len(fields) == 2 else ""
        if not MASK_RE.fullmatch(mask):
            raise DataError(f"{path}:{line_number}: invalid mask {mask!r}")
        if len(fields) == 2 and not reading:
            raise DataError(f"{path}:{line_number}: empty goroawase reading")
        if reading and (
            not KANA_PREFIX_RE.match(reading)
            or any(character in "[]<>`" for character in reading)
        ):
            raise DataError(f"{path}:{line_number}: invalid goroawase reading")
        if mask in readings:
            raise DataError(f"{path}:{line_number}: duplicate mask {mask}")
        readings[mask] = reading
    return readings


def parse_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        raise DataError(f"{label}: timestamp must use UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise DataError(f"{label}: invalid timestamp") from exc


def nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DataError(f"{label} must be a non-negative integer")
    return value


def read_current_snapshot(
    path: Path,
    mask_readings: dict[str, str],
    *,
    allowed_source_masks: set[str] | None = None,
    allow_unlisted_masks: bool = False,
    minimum_records: int = RANKING_LIMIT,
) -> tuple[list[dict[str, str]], str]:
    """Load the exact current-run snapshot and return its byte-level digest."""
    try:
        source_bytes = path.read_bytes()
        source_text = source_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise DataError(f"cannot read current snapshot {path}: {exc}") from exc

    digest = hashlib.sha256(source_bytes).hexdigest()
    records: list[dict[str, str]] = []
    seen_phones: set[str] = set()
    id_owners: dict[str, str] = {}

    source = io.StringIO(source_text, newline="")
    reader = csv.DictReader(source)
    if reader.fieldnames != list(CURRENT_FIELDS):
        raise DataError(
            f"{path}: expected CSV header {','.join(CURRENT_FIELDS)}; "
            f"got {reader.fieldnames!r}"
        )
    for line_number, row in enumerate(reader, start=2):
        label = f"{path}:{line_number}"
        if None in row or set(row) != set(CURRENT_FIELDS):
            raise DataError(f"{label}: malformed current snapshot row")
        phone = row["phoneNumber"]
        offer_id = row["id"]
        source_mask = row["sourceMask"]
        if (
            not RAW_PHONE_RE.fullmatch(phone)
            or not ID_RE.fullmatch(offer_id)
            or not MASK_RE.fullmatch(source_mask)
            or not phone.endswith(source_mask)
        ):
            raise DataError(f"{label}: invalid current snapshot identity")
        if phone in seen_phones:
            raise DataError(f"{label}: duplicate current snapshot phone")
        seen_phones.add(phone)
        owner = id_owners.setdefault(offer_id, phone)
        if owner != phone:
            raise DataError(f"{label}: offer id belongs to another phone")
        if allowed_source_masks is not None:
            if source_mask not in allowed_source_masks:
                raise DataError(f"{label}: phone belongs to an unscanned mask")
        elif not allow_unlisted_masks and source_mask not in mask_readings:
            raise DataError(f"{label}: source mask is absent from the masks file")
        records.append(
            {
                "phoneNumber": formatted_phone(phone),
                "offerId": offer_id,
                "sourceMask": source_mask,
                "standardReading": standard_reading(phone),
            }
        )

    if len(records) < minimum_records:
        raise DataError(
            f"at least {minimum_records} unique phone numbers are required; "
            f"found {len(records)}"
        )
    return sorted(records, key=lambda item: item["phoneNumber"]), digest


def common_prefix_length(left: str, right: str) -> int:
    return next(
        (index for index, pair in enumerate(zip(left, right)) if pair[0] != pair[1]),
        min(len(left), len(right)),
    )


def common_suffix_length(left: str, right: str) -> int:
    return common_prefix_length(left[::-1], right[::-1])


def mora_pattern(block: str) -> tuple[int, ...]:
    return tuple(MORA_COUNT[digit] for digit in block)


def block_features(label: str, block: str) -> tuple[int, list[str]]:
    signals: list[str] = []
    score = 0
    pattern = mora_pattern(block)
    if len(set(block)) == 1:
        return -10, [f"{label}: four identical digits may be miscounted"]
    if block[:2] == block[2:]:
        signals.append(f"{label}: ABAB")
        score += 5
    if block == block[::-1]:
        signals.append(f"{label}: palindrome")
        score += 5
    if block[0] == block[1] and block[2] == block[3]:
        signals.append(f"{label}: AABB")
        score += 3
    if len(set(block)) <= 2:
        signals.append(f"{label}: at most two distinct digits")
        score += 1
    if (
        pattern[0] == pattern[2]
        and pattern[1] == pattern[3]
        and pattern[0] != pattern[1]
    ):
        signals.append(
            f"{label}: alternating mora cadence {'-'.join(map(str, pattern))}"
        )
        score += 8
    if (
        pattern[0] == pattern[3]
        and pattern[1] == pattern[2]
        and pattern[0] != pattern[1]
    ):
        signals.append(
            f"{label}: mirrored mora cadence {'-'.join(map(str, pattern))}"
        )
        score += 5
    return score, signals


def is_strong_pattern(block: str) -> bool:
    return (
        block[:2] == block[2:]
        or block == block[::-1]
        or (block[0] == block[1] and block[2] == block[3])
    )


def is_balanced_echo(record: dict[str, str]) -> bool:
    phone = raw_phone(record["phoneNumber"])
    first = phone[3:7]
    second = phone[7:]
    first_mora = sum(MORA_COUNT[digit] for digit in first)
    second_mora = sum(MORA_COUNT[digit] for digit in second)
    aligned = sum(left == right for left, right in zip(first, second))
    return first_mora == second_mora and aligned >= 2


def stable_hash_key(*parts: str) -> bytes:
    """Return a reproducible, non-lexical ordering key."""
    return hashlib.sha256("\0".join(parts).encode("utf-8")).digest()


def balanced_stable_interleave(
    ranked_records: Sequence[dict[str, str]],
    *,
    band_size: int = QUALITY_ORDER_BAND_SIZE,
) -> list[dict[str, str]]:
    """Balance masks inside fixed quality bands without changing membership."""
    if band_size < 1:
        raise DataError("quality-order band size must be positive")

    ordered: list[dict[str, str]] = []
    for band_number, start in enumerate(range(0, len(ranked_records), band_size)):
        band = ranked_records[start : start + band_size]
        by_mask: dict[str, list[dict[str, str]]] = {}
        for record in band:
            by_mask.setdefault(record["sourceMask"], []).append(record)
        mask_order = sorted(
            by_mask,
            key=lambda mask: (stable_hash_key(str(band_number), mask), mask),
        )
        position = 0
        while True:
            added = False
            for mask in mask_order:
                records = by_mask[mask]
                if position < len(records):
                    ordered.append(records[position])
                    added = True
            if not added:
                break
            position += 1
    return ordered


def direct_features(record: dict[str, str]) -> tuple[int, list[str]]:
    phone = raw_phone(record["phoneNumber"])
    first = phone[3:7]
    second = phone[7:]
    prefix = common_prefix_length(first, second)
    suffix = common_suffix_length(first, second)
    aligned = sum(left == right for left, right in zip(first, second))
    first_block_score, first_signals = block_features("first block", first)
    second_block_score, second_signals = block_features("second block", second)
    signals = first_signals + second_signals

    if prefix:
        signals.append(f"shared prefix: {prefix} digit(s)")
    if suffix:
        signals.append(f"shared suffix: {suffix} digit(s)")
    if aligned:
        signals.append(f"same aligned digits: {aligned}/4")

    first_pattern = mora_pattern(first)
    second_pattern = mora_pattern(second)
    first_mora = sum(first_pattern)
    second_mora = sum(second_pattern)
    if first_mora == second_mora:
        signals.append(f"equal mora count: {first_mora}+{second_mora}")
    if first_pattern == second_pattern:
        signals.append(
            "parallel mora patterns: " + "-".join(map(str, first_pattern))
        )
    if first == second:
        signals.append("exact four-digit block repetition")
    elif aligned >= 3:
        signals.append("near-identical blocks may be misheard")

    score = first_block_score + second_block_score
    score += prefix * 2 + suffix * 3 + aligned * 2
    score += 3 if first_mora == second_mora else 0
    score += 7 if first_pattern == second_pattern else 0
    score += 10 if first == second else 0
    score -= 6 if first != second and aligned >= 3 else 0
    return score, signals


def direct_candidate(record: dict[str, str]) -> dict[str, object]:
    _score, signals = direct_features(record)
    phone = raw_phone(record["phoneNumber"])
    return {
        "phoneNumber": record["phoneNumber"],
        "standardReading": record["standardReading"],
        "flowReading": flow_reading(phone),
        "firstMoraPattern": list(mora_pattern(phone[3:7])),
        "secondMoraPattern": list(mora_pattern(phone[7:])),
        "soundSignals": signals,
    }


def goroawase_candidate(
    record: dict[str, str],
    mask_readings: dict[str, str],
) -> dict[str, object]:
    phone = raw_phone(record["phoneNumber"])
    first = phone[3:7]
    second = phone[7:]
    _score, signals = direct_features(record)
    standard_groups = record["standardReading"].split("｜")
    first_hint = mask_readings.get(first, "")
    second_hint = mask_readings.get(second, "")
    suggested_reading = "｜".join(
        (
            standard_groups[0],
            first_hint or standard_groups[1],
            second_hint or standard_groups[2],
        )
    )
    forbidden = set("|[]<>\r\n") | {chr(96)}
    if (
        suggested_reading == record["standardReading"]
        or len(suggested_reading) > 200
        or suggested_reading.count("｜") != 2
        or any(character in forbidden for character in suggested_reading)
    ):
        raise DataError(
            f"{record['phoneNumber']}: cannot build a safe goroawase reading"
        )
    return {
        "phoneNumber": record["phoneNumber"],
        "standardReading": record["standardReading"],
        "suggestedReading": suggested_reading,
        "firstBlock": first,
        "firstBlockHint": first_hint,
        "secondBlock": second,
        "secondBlockHint": second_hint,
        "soundSignals": signals,
    }


def build_shortlists(
    records: list[dict[str, str]],
    mask_readings: dict[str, str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    ranked = sorted(
        records,
        key=lambda record: (
            -direct_features(record)[0],
            stable_hash_key(record["phoneNumber"]),
            record["phoneNumber"],
        ),
    )

    direct_records: dict[str, dict[str, str]] = {}

    def add_direct_candidates(
        candidates: Iterable[dict[str, str]],
        addition_limit: int | None = None,
    ) -> None:
        added = 0
        for candidate in candidates:
            if len(direct_records) >= MAX_DIRECT_CANDIDATES:
                break
            phone_number = candidate["phoneNumber"]
            if phone_number in direct_records:
                continue
            direct_records[phone_number] = candidate
            added += 1
            if addition_limit is not None and added >= addition_limit:
                break

    # Reserve representation before global scoring can fill the shortlist.
    # This makes a productive new source mask visible to the language curator.
    records_by_mask: dict[str, list[dict[str, str]]] = {}
    for record in ranked:
        records_by_mask.setdefault(record["sourceMask"], []).append(record)
    ordered_masks = list(mask_readings)
    ordered_masks.extend(sorted(set(records_by_mask) - set(ordered_masks)))
    for position in range(DIVERSE_CANDIDATES_PER_MASK):
        add_direct_candidates(
            records_by_mask[mask][position]
            for mask in ordered_masks
            if position < len(records_by_mask.get(mask, []))
        )

    add_direct_candidates(ranked[:BASE_DIRECT_CANDIDATES])
    add_direct_candidates(
        (
            record
            for record in ranked
            if any(
                is_strong_pattern(block)
                for block in (
                    raw_phone(record["phoneNumber"])[3:7],
                    raw_phone(record["phoneNumber"])[7:],
                )
            )
        ),
        MAX_STRONG_PATTERN_CANDIDATES,
    )
    add_direct_candidates(
        (record for record in ranked if is_balanced_echo(record)),
        MAX_BALANCED_ECHO_CANDIDATES,
    )

    add_direct_candidates(ranked)

    direct_ranked = sorted(
        direct_records.values(),
        key=lambda record: (
            -direct_features(record)[0],
            stable_hash_key(record["phoneNumber"]),
            record["phoneNumber"],
        ),
    )
    direct = [
        direct_candidate(record)
        for record in balanced_stable_interleave(direct_ranked)
    ]

    selected: dict[str, dict[str, str]] = {}
    per_mask: dict[str, int] = {}
    for record in sorted(
        records,
        key=lambda item: (
            not bool(
                mask_readings.get(raw_phone(item["phoneNumber"])[3:7], "")
            ),
            -direct_features(item)[0],
            stable_hash_key(item["phoneNumber"]),
            item["phoneNumber"],
        ),
    ):
        phone = raw_phone(record["phoneNumber"])
        first = phone[3:7]
        second = phone[7:]
        has_first_hint = bool(mask_readings.get(first, ""))
        has_second_hint = bool(mask_readings.get(second, ""))
        if not has_first_hint and not has_second_hint:
            continue
        if has_first_hint or per_mask.get(second, 0) < 2:
            selected[record["phoneNumber"]] = record
            per_mask[second] = per_mask.get(second, 0) + 1
        if len(selected) >= MAX_GOROAWASE_CANDIDATES:
            break

    for record in ranked:
        if len(selected) >= min(MAX_GOROAWASE_CANDIDATES, len(records)):
            break
        phone = raw_phone(record["phoneNumber"])
        if mask_readings.get(phone[3:7], "") or mask_readings.get(phone[7:], ""):
            selected.setdefault(record["phoneNumber"], record)

    goro_records = sorted(
        selected.values(),
        key=lambda record: (
            not bool(
                mask_readings.get(raw_phone(record["phoneNumber"])[3:7], "")
            ),
            -direct_features(record)[0],
            stable_hash_key(record["phoneNumber"]),
            record["phoneNumber"],
        ),
    )[:MAX_GOROAWASE_CANDIDATES]
    goroawase = [
        goroawase_candidate(record, mask_readings)
        for record in balanced_stable_interleave(goro_records)
    ]
    return direct, goroawase


def build_newly_found_shortlist(
    records: Sequence[dict[str, str]],
) -> list[dict[str, object]]:
    """Build a direct-sound shortlist from phones added since the baseline."""
    if len(records) < NEWLY_FOUND_RANKING_LIMIT:
        return []
    ranked = sorted(
        records,
        key=lambda record: (
            -direct_features(record)[0],
            stable_hash_key(record["phoneNumber"]),
            record["phoneNumber"],
        ),
    )[:MAX_NEWLY_FOUND_CANDIDATES]
    return [
        direct_candidate(record)
        for record in balanced_stable_interleave(
            ranked,
            band_size=NEWLY_FOUND_RANKING_LIMIT,
        )
    ]


def load_diff_summary(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataError(f"cannot read diff summary {path}: {exc}") from exc
    required = {
        "schemaVersion",
        "generatedAt",
        "comparisonAvailable",
        "scannedMaskCount",
        "currentPhoneCount",
        "previousPhoneCount",
        "added",
        "notObserved",
        "notScanned",
        "idChanged",
        "unchanged",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise DataError("diff summary has an unsupported schema")
    if payload["schemaVersion"] != 2:
        raise DataError("diff summary has an unsupported schema version")
    parse_timestamp(payload["generatedAt"], "diff summary generatedAt")
    if not isinstance(payload["comparisonAvailable"], bool):
        raise DataError("diff summary comparisonAvailable must be boolean")
    numeric_fields = required - {
        "schemaVersion",
        "generatedAt",
        "comparisonAvailable",
    }
    for key in numeric_fields:
        nonnegative_int(payload[key], f"diff summary {key}")
    if payload["scannedMaskCount"] < 1:
        raise DataError("diff summary scannedMaskCount must be at least 1")
    if payload["currentPhoneCount"] != (
        payload["added"] + payload["idChanged"] + payload["unchanged"]
    ):
        raise DataError("diff summary current counts are inconsistent")
    if payload["previousPhoneCount"] != (
        payload["notObserved"]
        + payload["notScanned"]
        + payload["idChanged"]
        + payload["unchanged"]
    ):
        raise DataError("diff summary previous counts are inconsistent")
    if not payload["comparisonAvailable"] and any(
        payload[key]
        for key in (
            "previousPhoneCount",
            "notObserved",
            "notScanned",
            "idChanged",
            "unchanged",
        )
    ):
        raise DataError("diff summary without a baseline has previous counts")
    return payload


def load_newly_found_records(
    records: Sequence[dict[str, str]],
    *,
    diff_path: Path | None,
    diff_summary_path: Path | None,
    scanned_masks: set[str],
) -> list[dict[str, str]]:
    """Resolve current phones added relative to a real comparable baseline."""
    if (diff_path is None) != (diff_summary_path is None):
        raise DataError("new ranking requires both diff and diff summary")
    if diff_path is None or diff_summary_path is None:
        return []

    summary = load_diff_summary(diff_summary_path)
    if summary["currentPhoneCount"] != len(records):
        raise DataError("diff summary current count contradicts current snapshot")
    if summary["scannedMaskCount"] != len(scanned_masks):
        raise DataError("diff summary scanned mask count contradicts ranking scope")
    try:
        source = io.StringIO(diff_path.read_text(encoding="utf-8"), newline="")
    except (OSError, UnicodeError) as exc:
        raise DataError(f"cannot read run diff {diff_path}: {exc}") from exc
    reader = csv.DictReader(source)
    if reader.fieldnames != list(DIFF_FIELDS):
        raise DataError(
            f"{diff_path}: expected CSV header {','.join(DIFF_FIELDS)}; "
            f"got {reader.fieldnames!r}"
        )

    allowed_change_types = {"added", "not_observed", "not_scanned", "id_changed"}
    current_by_raw = {raw_phone(record["phoneNumber"]): record for record in records}
    row_counts = {change_type: 0 for change_type in allowed_change_types}
    seen_phones: set[str] = set()
    added_records: list[dict[str, str]] = []
    for line_number, row in enumerate(reader, start=2):
        label = f"{diff_path}:{line_number}"
        if None in row or set(row) != set(DIFF_FIELDS):
            raise DataError(f"{label}: malformed run diff row")
        change_type = row["changeType"]
        phone = row["phoneNumber"]
        previous_id = row["previousId"]
        current_id = row["currentId"]
        source_mask = row["sourceMask"]
        if (
            change_type not in allowed_change_types
            or not RAW_PHONE_RE.fullmatch(phone)
            or not MASK_RE.fullmatch(source_mask)
            or not phone.endswith(source_mask)
        ):
            raise DataError(f"{label}: invalid run diff identity")
        if phone in seen_phones:
            raise DataError(f"{label}: duplicate run diff phone")
        seen_phones.add(phone)
        row_counts[change_type] += 1

        if change_type == "added":
            valid_ids = not previous_id and bool(ID_RE.fullmatch(current_id))
        elif change_type in {"not_observed", "not_scanned"}:
            valid_ids = bool(ID_RE.fullmatch(previous_id)) and not current_id
        else:
            valid_ids = (
                bool(ID_RE.fullmatch(previous_id))
                and bool(ID_RE.fullmatch(current_id))
                and previous_id != current_id
            )
        if not valid_ids:
            raise DataError(f"{label}: invalid IDs for {change_type}")

        in_scope = source_mask in scanned_masks
        if (change_type == "not_scanned") == in_scope:
            raise DataError(f"{label}: change type contradicts ranking scope")

        record = current_by_raw.get(phone)
        is_current = change_type in {"added", "id_changed"}
        if is_current:
            if record is None:
                raise DataError(f"{label}: current phone is absent from snapshot")
            if (
                record["offerId"] != current_id
                or record["sourceMask"] != source_mask
            ):
                raise DataError(f"{label}: identity contradicts current snapshot")
            if change_type == "added":
                added_records.append(record)
        elif record is not None:
            raise DataError(f"{label}: non-current phone is present in snapshot")

    summary_count_fields = {
        "added": "added",
        "notObserved": "not_observed",
        "notScanned": "not_scanned",
        "idChanged": "id_changed",
    }
    for summary_key, change_type in summary_count_fields.items():
        if summary[summary_key] != row_counts[change_type]:
            raise DataError(f"diff summary {summary_key} contradicts run diff")

    if not summary["comparisonAvailable"]:
        return []
    return added_records


def resolved_path(path: Path, label: str) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        raise DataError(f"cannot resolve {label} path {path}: {exc}") from exc


def validate_prepare_paths(
    current_path: Path,
    masks_file: Path,
    output: Path,
    diff_path: Path | None,
    diff_summary_path: Path | None,
) -> None:
    paths = {
        "current snapshot": resolved_path(current_path, "current snapshot"),
        "masks file": resolved_path(masks_file, "masks file"),
        "candidate output": resolved_path(output, "candidate output"),
    }
    if diff_path is not None:
        paths["run diff"] = resolved_path(diff_path, "run diff")
    if diff_summary_path is not None:
        paths["diff summary"] = resolved_path(diff_summary_path, "diff summary")
    if len(set(paths.values())) != len(paths):
        raise DataError("prepare input and output paths must be distinct")


def validate_compact_paths(
    *,
    context_path: Path,
    request_output: Path,
    direct_output: Path,
    goroawase_output: Path,
    newly_found_output: Path,
) -> None:
    paths = {
        "candidate context": resolved_path(context_path, "candidate context"),
        "AI request output": resolved_path(request_output, "AI request output"),
        "direct AI output": resolved_path(direct_output, "direct AI output"),
        "goroawase AI output": resolved_path(
            goroawase_output, "goroawase AI output"
        ),
        "newly-found AI output": resolved_path(
            newly_found_output, "newly-found AI output"
        ),
    }
    if len(set(paths.values())) != len(paths):
        raise DataError("compact input and output paths must all be distinct")
    output_parents = {
        paths["AI request output"].parent,
        paths["direct AI output"].parent,
        paths["goroawase AI output"].parent,
        paths["newly-found AI output"].parent,
    }
    if len(output_parents) != 1:
        raise DataError("compact AI outputs must share one directory")


def validate_render_paths(
    *,
    context_path: Path,
    selection_path: Path,
    current_path: Path,
    top_output: Path,
    goroawase_output: Path,
    release_output: Path,
    diff_summary_path: Path | None,
    catalog_summary_path: Path | None,
) -> None:
    inputs = [context_path, selection_path, current_path]
    if diff_summary_path is not None:
        inputs.append(diff_summary_path)
    if catalog_summary_path is not None:
        inputs.append(catalog_summary_path)
    outputs = [top_output, goroawase_output, release_output]
    resolved_inputs = [resolved_path(path, "render input") for path in inputs]
    resolved_outputs = [resolved_path(path, "render output") for path in outputs]
    if len(set(resolved_inputs + resolved_outputs)) != len(
        resolved_inputs + resolved_outputs
    ):
        raise DataError("render input and output paths must all be distinct")


def write_text_atomic(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(content)
        temporary.replace(path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise DataError(f"cannot write {path}: {exc}") from exc


def prepare_context(
    current_path: Path,
    masks_file: Path,
    output: Path,
    specialized_masks: Sequence[str] | None = None,
    diff_path: Path | None = None,
    diff_summary_path: Path | None = None,
) -> None:
    validate_prepare_paths(
        current_path,
        masks_file,
        output,
        diff_path,
        diff_summary_path,
    )
    mask_readings = load_mask_readings(masks_file)
    normalized_scope: list[str] = []
    if specialized_masks is not None:
        seen: set[str] = set()
        for mask in specialized_masks:
            if not MASK_RE.fullmatch(mask):
                raise DataError(f"invalid specialized mask: {mask!r}")
            if mask in seen:
                raise DataError(f"duplicate specialized mask: {mask}")
            seen.add(mask)
            normalized_scope.append(mask)
            mask_readings.setdefault(mask, "")
        if not normalized_scope:
            raise DataError("a specialized run must contain at least one mask")

    specialized = specialized_masks is not None
    records, snapshot_digest = read_current_snapshot(
        current_path,
        mask_readings,
        allowed_source_masks=set(normalized_scope) if specialized else None,
        minimum_records=0 if specialized else RANKING_LIMIT,
    )
    direct, goroawase = build_shortlists(records, mask_readings)
    scanned_masks = set(normalized_scope) if specialized else set(mask_readings)
    newly_found_records = load_newly_found_records(
        records,
        diff_path=diff_path,
        diff_summary_path=diff_summary_path,
        scanned_masks=scanned_masks,
    )
    newly_found = build_newly_found_shortlist(newly_found_records)
    if not specialized and len(direct) < RANKING_LIMIT:
        raise DataError(
            f"fewer than {RANKING_LIMIT} direct candidates are available; "
            f"found {len(direct)}"
        )
    if not specialized and len(goroawase) < RANKING_LIMIT:
        raise DataError(
            f"fewer than {RANKING_LIMIT} goroawase candidates are available; "
            f"found {len(goroawase)}"
        )
    selection_counts = {
        "top": min(RANKING_LIMIT, len(direct)),
        "goroawase": min(RANKING_LIMIT, len(goroawase)),
        "newlyFound": (
            NEWLY_FOUND_RANKING_LIMIT
            if len(newly_found) >= NEWLY_FOUND_RANKING_LIMIT
            else 0
        ),
    }
    payload = {
        "schemaVersion": CONTEXT_SCHEMA_VERSION,
        "sourceSnapshot": {
            "kind": CURRENT_SOURCE_KIND,
            "sha256": snapshot_digest,
            "recordCount": len(records),
        },
        "selectionCounts": selection_counts,
        "scope": {
            "specialized": specialized,
            "masks": normalized_scope,
        },
        "goroawaseCandidates": goroawase,
        "directCandidates": direct,
        "newlyFoundCandidates": newly_found,
    }
    write_text_atomic(
        output,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def compact_json_lines(records: Iterable[dict[str, object]]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )


def candidate_id(prefix: str, position: int) -> str:
    """Return the stable, compact identifier for one ordered candidate."""
    if prefix not in {"T", "G", "N"} or not 1 <= position <= 999:
        raise DataError("cannot assign candidate ID")
    return f"{prefix}{position:03d}"


def write_compact_ai_inputs(
    *,
    context_path: Path,
    request_output: Path,
    direct_output: Path,
    goroawase_output: Path,
    newly_found_output: Path,
) -> None:
    """Derive small, line-oriented Codex inputs from the strict context."""
    validate_compact_paths(
        context_path=context_path,
        request_output=request_output,
        direct_output=direct_output,
        goroawase_output=goroawase_output,
        newly_found_output=newly_found_output,
    )
    (
        direct,
        goroawase,
        newly_found,
        selection_counts,
        _scope_masks,
        source_snapshot,
    ) = load_context(context_path)

    direct_records = [
        {
            "candidateId": candidate_id("T", position),
            "phoneNumber": candidate["phoneNumber"],
            "flowReading": candidate["flowReading"],
            "firstMoraPattern": candidate["firstMoraPattern"],
            "secondMoraPattern": candidate["secondMoraPattern"],
            "soundSignals": candidate["soundSignals"],
        }
        for position, candidate in enumerate(direct.values(), start=1)
    ]
    goroawase_records = [
        {
            "candidateId": candidate_id("G", position),
            "phoneNumber": candidate["phoneNumber"],
            "firstBlockHint": candidate["firstBlockHint"],
            "secondBlockHint": candidate["secondBlockHint"],
            "suggestedReading": candidate["suggestedReading"],
        }
        for position, candidate in enumerate(goroawase.values(), start=1)
    ]
    newly_found_records = [
        {
            "candidateId": candidate_id("N", position),
            "phoneNumber": candidate["phoneNumber"],
            "flowReading": candidate["flowReading"],
            "firstMoraPattern": candidate["firstMoraPattern"],
            "secondMoraPattern": candidate["secondMoraPattern"],
            "soundSignals": candidate["soundSignals"],
        }
        for position, candidate in enumerate(newly_found.values(), start=1)
    ]
    request = {
        "schemaVersion": CONTEXT_SCHEMA_VERSION,
        "sourceSnapshot": source_snapshot,
        "selectionCounts": selection_counts,
        "candidateCounts": {
            "top": len(direct_records),
            "goroawase": len(goroawase_records),
            "newlyFound": len(newly_found_records),
        },
    }

    # Publish the small request last so it serves as the completion marker for
    # consumers downloading all four files from one artifact directory.
    write_text_atomic(direct_output, compact_json_lines(direct_records))
    write_text_atomic(goroawase_output, compact_json_lines(goroawase_records))
    write_text_atomic(newly_found_output, compact_json_lines(newly_found_records))
    write_text_atomic(
        request_output,
        json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n",
    )


def load_context(
    path: Path,
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, dict[str, object]],
    dict[str, dict[str, object]],
    dict[str, int],
    tuple[str, ...],
    dict[str, object],
]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataError(f"cannot read candidate context {path}: {exc}") from exc

    root_fields = {
        "schemaVersion",
        "sourceSnapshot",
        "selectionCounts",
        "scope",
        "goroawaseCandidates",
        "directCandidates",
        "newlyFoundCandidates",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != root_fields
        or payload["schemaVersion"] != CONTEXT_SCHEMA_VERSION
    ):
        raise DataError("candidate context has an unsupported schema")
    source_snapshot = payload["sourceSnapshot"]
    if (
        not isinstance(source_snapshot, dict)
        or set(source_snapshot) != {"kind", "sha256", "recordCount"}
        or source_snapshot["kind"] != CURRENT_SOURCE_KIND
        or not isinstance(source_snapshot["sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_snapshot["sha256"])
    ):
        raise DataError("candidate context has an invalid current snapshot binding")
    snapshot_record_count = nonnegative_int(
        source_snapshot["recordCount"],
        "candidate context sourceSnapshot recordCount",
    )
    selection_counts = payload.get("selectionCounts")
    if not isinstance(selection_counts, dict) or set(selection_counts) != {
        "top",
        "goroawase",
        "newlyFound",
    }:
        raise DataError("candidate context has invalid selection counts")
    for key, value in selection_counts.items():
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0
            <= value
            <= (NEWLY_FOUND_RANKING_LIMIT if key == "newlyFound" else RANKING_LIMIT)
        ):
            raise DataError(f"candidate context has invalid {key} selection count")

    scope = payload.get("scope")
    if not isinstance(scope, dict) or set(scope) != {"specialized", "masks"}:
        raise DataError("candidate context has an invalid scope")
    specialized = scope["specialized"]
    scope_masks = scope["masks"]
    if not isinstance(specialized, bool) or not isinstance(scope_masks, list):
        raise DataError("candidate context has an invalid scope")
    normalized_scope: list[str] = []
    for mask in scope_masks:
        if not isinstance(mask, str) or not MASK_RE.fullmatch(mask):
            raise DataError("candidate context contains an invalid scope mask")
        if mask in normalized_scope:
            raise DataError(f"candidate context contains duplicate scope mask {mask}")
        normalized_scope.append(mask)
    if specialized != bool(normalized_scope):
        raise DataError("candidate context scope mode and masks disagree")
    if not specialized and (
        selection_counts["top"] != RANKING_LIMIT
        or selection_counts["goroawase"] != RANKING_LIMIT
    ):
        raise DataError(
            f"full-scan context must request both TOP-{RANKING_LIMIT} rankings"
        )

    def make_index(
        key: str,
        minimum: int,
    ) -> dict[str, dict[str, object]]:
        records = payload.get(key)
        if not isinstance(records, list):
            raise DataError(f"candidate context {key} must be an array")
        maximum = {
            "directCandidates": MAX_DIRECT_CANDIDATES,
            "goroawaseCandidates": MAX_GOROAWASE_CANDIDATES,
            "newlyFoundCandidates": MAX_NEWLY_FOUND_CANDIDATES,
        }[key]
        if len(records) > maximum:
            raise DataError(f"candidate context {key} exceeds its size limit")
        index: dict[str, dict[str, object]] = {}
        for position, item in enumerate(records):
            if not isinstance(item, dict):
                raise DataError(f"{key} record {position} is not an object")
            direct_fields = {
                "phoneNumber",
                "standardReading",
                "flowReading",
                "firstMoraPattern",
                "secondMoraPattern",
                "soundSignals",
            }
            goroawase_fields = {
                "phoneNumber",
                "standardReading",
                "suggestedReading",
                "firstBlock",
                "firstBlockHint",
                "secondBlock",
                "secondBlockHint",
                "soundSignals",
            }
            expected_fields = (
                goroawase_fields if key == "goroawaseCandidates" else direct_fields
            )
            if set(item) != expected_fields:
                raise DataError(f"{key} record {position} has an invalid schema")
            phone = item["phoneNumber"]
            reading = item["standardReading"]
            if not isinstance(phone, str) or not FORMATTED_PHONE_RE.fullmatch(phone):
                raise DataError(f"{key} record {position} has an invalid phone")
            raw_number = raw_phone(phone)
            if not isinstance(reading, str) or reading != standard_reading(raw_number):
                raise DataError(f"{key} record {position} has an invalid reading")
            expected_signals = direct_features(
                {"phoneNumber": phone, "standardReading": reading}
            )[1]
            signals = item["soundSignals"]
            if (
                not isinstance(signals, list)
                or any(not isinstance(signal, str) for signal in signals)
                or signals != expected_signals
            ):
                raise DataError(f"{key} record {position} has invalid sound signals")
            if key != "goroawaseCandidates":
                if item["flowReading"] != flow_reading(raw_number):
                    raise DataError(
                        f"{key} record {position} has an invalid flow reading"
                    )
                if item["firstMoraPattern"] != list(mora_pattern(raw_number[3:7])):
                    raise DataError(
                        f"{key} record {position} has an invalid first mora pattern"
                    )
                if item["secondMoraPattern"] != list(mora_pattern(raw_number[7:])):
                    raise DataError(
                        f"{key} record {position} has an invalid second mora pattern"
                    )
            else:
                first = raw_number[3:7]
                second = raw_number[7:]
                first_hint = item["firstBlockHint"]
                second_hint = item["secondBlockHint"]
                if item["firstBlock"] != first or item["secondBlock"] != second:
                    raise DataError(f"{key} record {position} has invalid blocks")
                if not isinstance(first_hint, str) or not isinstance(
                    second_hint, str
                ):
                    raise DataError(f"{key} record {position} has invalid hints")
                for hint in (first_hint, second_hint):
                    if hint and (
                        not KANA_PREFIX_RE.match(hint)
                        or any(character in "[]<>`|\r\n" for character in hint)
                    ):
                        raise DataError(f"{key} record {position} has invalid hints")
                groups = reading.split("｜")
                expected_suggestion = "｜".join(
                    (groups[0], first_hint or groups[1], second_hint or groups[2])
                )
                if (
                    not first_hint
                    and not second_hint
                    or item["suggestedReading"] != expected_suggestion
                    or expected_suggestion == reading
                    or len(expected_suggestion) > 200
                ):
                    raise DataError(
                        f"{key} record {position} has an invalid suggested reading"
                    )
            if phone in index:
                raise DataError(f"duplicate {key} phone {phone}")
            index[phone] = item
        if len(index) < minimum:
            raise DataError(
                f"candidate context {key} contains fewer than {minimum} phones"
            )
        return index

    direct_index = make_index("directCandidates", selection_counts["top"])
    goroawase_index = make_index(
        "goroawaseCandidates", selection_counts["goroawase"]
    )
    newly_found_index = make_index(
        "newlyFoundCandidates", selection_counts["newlyFound"]
    )
    expected_new_selection_count = (
        NEWLY_FOUND_RANKING_LIMIT
        if len(newly_found_index) >= NEWLY_FOUND_RANKING_LIMIT
        else 0
    )
    if not expected_new_selection_count and newly_found_index:
        raise DataError(
            "candidate context must omit a newly-found shortlist smaller than 10"
        )
    expected_counts = {
        "top": min(RANKING_LIMIT, len(direct_index)),
        "goroawase": min(RANKING_LIMIT, len(goroawase_index)),
        "newlyFound": expected_new_selection_count,
    }
    if selection_counts != expected_counts:
        raise DataError("candidate context selection counts contradict candidates")
    if len(direct_index) != min(snapshot_record_count, MAX_DIRECT_CANDIDATES):
        raise DataError(
            "candidate context snapshot count contradicts direct candidates"
        )
    candidate_union = (
        set(direct_index) | set(goroawase_index) | set(newly_found_index)
    )
    if snapshot_record_count < len(candidate_union):
        raise DataError("candidate context snapshot count contradicts candidates")

    return (
        direct_index,
        goroawase_index,
        newly_found_index,
        selection_counts,
        tuple(normalized_scope),
        source_snapshot,
    )


def load_selection(
    path: Path,
    selection_counts: dict[str, int],
) -> dict[str, list[object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataError(f"cannot read Codex selection {path}: {exc}") from exc

    if not isinstance(payload, dict) or set(payload) != {
        "top",
        "goroawase",
        "newlyFound",
    }:
        raise DataError(
            "selection must contain exactly top, goroawase, and newlyFound"
        )
    top = payload["top"]
    goroawase = payload["goroawase"]
    newly_found = payload["newlyFound"]
    if not all(isinstance(value, list) for value in (top, goroawase, newly_found)):
        raise DataError("top, goroawase, and newlyFound must all be arrays")
    if len(top) != selection_counts["top"]:
        raise DataError(
            f"top must contain exactly {selection_counts['top']} entries"
        )
    if len(goroawase) != selection_counts["goroawase"]:
        raise DataError(
            "goroawase must contain exactly "
            f"{selection_counts['goroawase']} entries"
        )
    if len(newly_found) != selection_counts["newlyFound"]:
        raise DataError(
            "newlyFound must contain exactly "
            f"{selection_counts['newlyFound']} entries"
        )
    return {"top": top, "goroawase": goroawase, "newlyFound": newly_found}


def candidate_index_by_id(
    candidates: dict[str, dict[str, object]],
    prefix: str,
) -> dict[str, tuple[str, dict[str, object]]]:
    """Index validated, ordered context candidates by their compact IDs."""
    return {
        candidate_id(prefix, position): (phone, candidate)
        for position, (phone, candidate) in enumerate(candidates.items(), start=1)
    }


def selected_candidate(
    item: object,
    *,
    label: str,
    position: int,
    prefix: str,
    candidates_by_id: dict[str, tuple[str, dict[str, object]]],
) -> tuple[str, str, dict[str, object]]:
    if not isinstance(item, dict) or set(item) != {"candidateId"}:
        raise DataError(
            f"{label} entry {position} must contain only the candidateId key"
        )
    selected_id = item["candidateId"]
    if (
        not isinstance(selected_id, str)
        or not CANDIDATE_ID_RE.fullmatch(selected_id)
        or not selected_id.startswith(prefix)
    ):
        raise DataError(f"{label} entry {position} has an invalid candidateId")
    selected = candidates_by_id.get(selected_id)
    if selected is None:
        raise DataError(
            f"{label} entry {position} candidateId is outside the candidate context"
        )
    phone, candidate = selected
    return selected_id, phone, candidate


def validate_direct_entries(
    entries: Iterable[object],
    candidates: dict[str, dict[str, object]],
    *,
    label: str,
    prefix: str,
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    candidates_by_id = candidate_index_by_id(candidates, prefix)
    for position, item in enumerate(entries, start=1):
        selected_id, phone, candidate = selected_candidate(
            item,
            label=label,
            position=position,
            prefix=prefix,
            candidates_by_id=candidates_by_id,
        )
        if selected_id in seen:
            raise DataError(f"{label} contains duplicate candidateId {selected_id}")
        seen.add(selected_id)
        result.append((phone, str(candidate["standardReading"])))
    return result


def validate_top_entries(
    entries: Iterable[object],
    candidates: dict[str, dict[str, object]],
) -> list[tuple[str, str]]:
    return validate_direct_entries(entries, candidates, label="top", prefix="T")


def validate_goroawase_entries(
    entries: Iterable[object],
    candidates: dict[str, dict[str, object]],
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    forbidden = set("|[]<>\r\n") | {chr(96)}
    candidates_by_id = candidate_index_by_id(candidates, "G")

    for position, item in enumerate(entries, start=1):
        selected_id, phone, candidate = selected_candidate(
            item,
            label="goroawase",
            position=position,
            prefix="G",
            candidates_by_id=candidates_by_id,
        )
        if selected_id in seen:
            raise DataError(
                f"goroawase contains duplicate candidateId {selected_id}"
            )
        reading = candidate.get("suggestedReading")
        if (
            not isinstance(reading, str)
            or reading != reading.strip()
            or not reading
            or len(reading) > 200
            or any(character in forbidden for character in reading)
        ):
            raise DataError(f"goroawase entry {position} has an unsafe reading")

        standard = str(candidate["standardReading"])
        prefix = standard.split("｜", 1)[0] + "｜"
        if not reading.startswith(prefix) or reading.count("｜") != 2:
            raise DataError(
                f"goroawase entry {position} must keep all three phone groups"
            )
        if reading.split("｜")[1:] == standard.split("｜")[1:]:
            raise DataError(
                f"goroawase entry {position} must transform at least one "
                "four-digit block into wordplay"
            )

        seen.add(selected_id)
        result.append((phone, reading))
    return result


def validate_selection(
    context_path: Path,
    selection_path: Path,
    current_path: Path,
) -> tuple[
    list[tuple[str, str]],
    list[tuple[str, str]],
    list[tuple[str, str]],
    tuple[str, ...],
]:
    """Validate one AI selection and resolve its IDs to trusted context rows."""
    (
        direct_candidates,
        goroawase_candidates,
        newly_found_candidates,
        selection_counts,
        specialized_masks,
        source_snapshot,
    ) = load_context(context_path)
    current_records, current_digest = read_current_snapshot(
        current_path,
        {},
        allow_unlisted_masks=True,
        minimum_records=0,
    )
    if current_digest != source_snapshot["sha256"]:
        raise DataError("current snapshot does not match the candidate context digest")
    if len(current_records) != source_snapshot["recordCount"]:
        raise DataError("current snapshot does not match the candidate context count")
    current_phones = {record["phoneNumber"] for record in current_records}
    candidate_phones = (
        set(direct_candidates)
        | set(goroawase_candidates)
        | set(newly_found_candidates)
    )
    if not candidate_phones <= current_phones:
        raise DataError(
            "candidate context contains a phone absent from current snapshot"
        )
    selection = load_selection(selection_path, selection_counts)
    top_rows = validate_top_entries(selection["top"], direct_candidates)
    goroawase_rows = validate_goroawase_entries(
        selection["goroawase"], goroawase_candidates
    )
    newly_found_rows = validate_direct_entries(
        selection["newlyFound"],
        newly_found_candidates,
        label="newlyFound",
        prefix="N",
    )
    return top_rows, goroawase_rows, newly_found_rows, specialized_masks


def display_reading(phone: str, reading: str) -> str:
    prefix_digits = raw_phone(phone)[:3]
    expected_prefix = "・".join(DIGIT_JA[digit] for digit in prefix_digits)
    actual_prefix, separator, remaining_groups = reading.partition("｜")
    if (
        not separator
        or actual_prefix != expected_prefix
        or remaining_groups.count("｜") != 1
    ):
        raise DataError(f"{phone}: reading does not match all three phone groups")
    return remaining_groups


def markdown_table(rows: Sequence[tuple[str, str]]) -> str:
    lines = ["| 番号 | 読み |", "|---|---|"]
    lines.extend(
        f"| {phone} | {display_reading(phone, reading)} |"
        for phone, reading in rows
    )
    return "\n".join(lines)


def ranking_heading(count: int, label: str) -> str:
    return f"TOP {count} — {label}"


def specialized_notice(masks: Sequence[str], rounds: int) -> str:
    formatted_masks = ", ".join(f"`{mask}`" for mask in masks)
    return (
        "> [!IMPORTANT]\n"
        "> これは完全スキャンではありません。`workflow_dispatch` で指定された\n"
        "> マスクだけを対象にした特定マスクランです。\n"
        f"> 対象マスク: {formatted_masks}  \n"
        f"> 指定ラウンド数: `{rounds}`\n"
    )


def diff_summary_markdown(path: Path) -> str:
    payload = load_diff_summary(path)
    if not payload["comparisonAvailable"]:
        return (
            "## 前回の同一範囲ランとの差分\n\n"
            "比較できる前回の all_numbers.csv はありません。\n"
        )
    return (
        "## 前回の同一範囲ランとの差分\n\n"
        "| 新規観測 | 今回未観測 | 今回の範囲外 | ID変更 | 同一 |\n"
        "|---:|---:|---:|---:|---:|\n"
        f"| {payload['added']} | {payload['notObserved']} | "
        f"{payload['notScanned']} | {payload['idChanged']} | "
        f"{payload['unchanged']} |\n\n"
        "`今回未観測` は削除・利用不可を意味しません。ランダムな返却により、"
        "今回だけ現れなかった番号です。\n"
    )


def catalog_summary_markdown(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataError(f"cannot read catalog summary {path}: {exc}") from exc
    required = {
        "schemaVersion",
        "generatedAt",
        "historyMode",
        "evidenceDate",
        "runKey",
        "runKind",
        "evidenceModelVersion",
        "missThreshold",
        "retentionDays",
        "minimumNegativeLogMissLikelihood",
        "scannedMaskCount",
        "comparableMaskCount",
        "qualifiedMaskCount",
        "previousActiveCount",
        "currentObservedCount",
        "added",
        "reobserved",
        "resurrected",
        "qualifiedMisses",
        "possiblyUnavailable",
        "tombstoned",
        "activeCount",
        "lifecycleCount",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or payload["schemaVersion"] != 3
    ):
        raise DataError("catalog summary has an unsupported schema")
    parse_timestamp(payload["generatedAt"], "catalog summary generatedAt")
    history_mode = payload["historyMode"]
    if not isinstance(history_mode, str) or not history_mode.strip():
        raise DataError("catalog summary historyMode must be a non-empty string")
    if history_mode not in {"cache", "empty"}:
        raise DataError("catalog summary historyMode must be cache or empty")

    numeric_fields = required - {
        "schemaVersion",
        "generatedAt",
        "historyMode",
        "evidenceDate",
        "runKey",
        "runKind",
        "minimumNegativeLogMissLikelihood",
    }
    for key in numeric_fields:
        value = payload[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise DataError(f"catalog summary {key} must be a non-negative integer")
    likelihood = payload["minimumNegativeLogMissLikelihood"]
    if (
        not isinstance(likelihood, (int, float))
        or isinstance(likelihood, bool)
        or not math.isfinite(likelihood)
        or likelihood < 0
    ):
        raise DataError("catalog summary likelihood threshold is invalid")
    if (
        payload["evidenceModelVersion"] != 1
        or payload["missThreshold"] != 3
        or payload["retentionDays"] != 5
    ):
        raise DataError("catalog summary uses unsupported lifecycle thresholds")
    if payload["scannedMaskCount"] < 1:
        raise DataError("catalog summary scannedMaskCount must be at least 1")
    if not (
        payload["qualifiedMaskCount"]
        <= payload["comparableMaskCount"]
        <= payload["scannedMaskCount"]
    ):
        raise DataError("catalog summary mask counts are inconsistent")
    if history_mode == "empty" and payload["previousActiveCount"] != 0:
        raise DataError("catalog summary with empty history has previous records")
    if payload["currentObservedCount"] != (
        payload["added"] + payload["reobserved"]
    ):
        raise DataError("catalog summary observed counts are inconsistent")
    if payload["tombstoned"] > payload["qualifiedMisses"]:
        raise DataError("catalog summary tombstone count is inconsistent")
    if payload["possiblyUnavailable"] > payload["activeCount"]:
        raise DataError("catalog summary availability counts are inconsistent")
    if payload["activeCount"] > payload["lifecycleCount"]:
        raise DataError("catalog summary lifecycle counts are inconsistent")

    history_note = ""
    if history_mode == "empty":
        history_note = (
            "\nCache の履歴がないため、この実行は空のカタログから開始しました。\n"
        )

    table = (
        "| アクティブ | 今回観測 | 新規 | 適格未観測 | "
        "利用不可の可能性 | 今回 tombstone | 復帰 |\n"
        "|---:|---:|---:|---:|---:|---:|---:|\n"
        f"| {payload['activeCount']} | {payload['currentObservedCount']} | "
        f"{payload['added']} | {payload['qualifiedMisses']} | "
        f"{payload['possiblyUnavailable']} | {payload['tombstoned']} | "
        f"{payload['resurrected']} |\n"
    )

    threshold_note = (
        "カタログ上の状態は統計的な観測履歴であり、購入・予約の確定ではありません。\n"
    )
    return (
        "## 累積番号カタログ\n\n"
        + table
        + "\n"
        + threshold_note
        + history_note
    )


def render_outputs(
    context_path: Path,
    selection_path: Path,
    top_output: Path,
    goroawase_output: Path,
    release_output: Path,
    diff_summary_path: Path | None = None,
    catalog_summary_path: Path | None = None,
    rounds: int | None = None,
    current_path: Path | None = None,
) -> None:
    if current_path is None:
        raise DataError("render requires the bound current snapshot")
    validate_render_paths(
        context_path=context_path,
        selection_path=selection_path,
        current_path=current_path,
        top_output=top_output,
        goroawase_output=goroawase_output,
        release_output=release_output,
        diff_summary_path=diff_summary_path,
        catalog_summary_path=catalog_summary_path,
    )

    (
        top_rows,
        goroawase_rows,
        newly_found_rows,
        specialized_masks,
    ) = validate_selection(context_path, selection_path, current_path)

    notice = ""
    if specialized_masks:
        if rounds is None or rounds <= 0:
            raise DataError("specialized release requires a positive round count")
        notice = specialized_notice(specialized_masks, rounds) + "\n"

    top_heading = ranking_heading(len(top_rows), "音と読みやすさ")
    goroawase_heading = ranking_heading(len(goroawase_rows), "語呂合わせ")
    top_body = (
        f"# {top_heading}\n\n"
        + notice
        + markdown_table(top_rows)
        + "\n"
    )
    goroawase_body = (
        f"# {goroawase_heading}\n\n"
        + notice
        + markdown_table(goroawase_rows)
        + "\n"
    )
    release_body = (
        "# ラク・モビ・バンゴウ\n\n"
        + notice
        + "Codex が今回の実行で実際に観測された番号だけから"
        "選び、公開前に現在のスナップショットとの一致、番号、通常読みを"
        "自動検証しています。\n\n"
        f"## {top_heading}\n\n"
        + markdown_table(top_rows)
        + "\n\n"
        f"## {goroawase_heading}\n\n"
        + markdown_table(goroawase_rows)
        + "\n"
    )
    if newly_found_rows:
        new_heading = ranking_heading(
            len(newly_found_rows), "前回スナップショットからの追加（音と読みやすさ）"
        )
        release_body += (
            f"\n## {new_heading}\n\n"
            "比較可能な前回スナップショットになく、今回追加された番号だけを、"
            "通常読みの響きと読みやすさで順位付けしています。"
            "新規発行や現在の利用可能性を示すものではありません。\n\n"
            + markdown_table(newly_found_rows)
            + "\n"
        )
    if diff_summary_path is not None:
        release_body += "\n" + diff_summary_markdown(diff_summary_path)
    if catalog_summary_path is not None:
        release_body += "\n" + catalog_summary_markdown(catalog_summary_path)

    write_text_atomic(top_output, top_body)
    write_text_atomic(goroawase_output, goroawase_body)
    write_text_atomic(release_output, release_body)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or validate the release ranking data."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        help="create the strict candidate context used for rendering",
    )
    prepare.add_argument("--current", type=Path, required=True)
    prepare.add_argument("--masks-file", type=Path, required=True)
    prepare.add_argument(
        "--specialized-mask",
        dest="specialized_masks",
        action="append",
        metavar="MASK",
        help="limit a specialized prerelease context to this source mask",
    )
    prepare.add_argument("--diff", type=Path)
    prepare.add_argument("--diff-summary", type=Path)
    prepare.add_argument("--output", type=Path, required=True)

    compact = subparsers.add_parser(
        "compact",
        help="derive compact JSONL inputs for the Codex selection step",
    )
    compact.add_argument("--context", type=Path, required=True)
    compact.add_argument("--request-output", type=Path, required=True)
    compact.add_argument("--direct-output", type=Path, required=True)
    compact.add_argument("--goroawase-output", type=Path, required=True)
    compact.add_argument("--newly-found-output", type=Path, required=True)

    validate = subparsers.add_parser(
        "validate-selection",
        help="strictly validate a Codex selection without rendering",
    )
    validate.add_argument("--context", type=Path, required=True)
    validate.add_argument("--selection", type=Path, required=True)
    validate.add_argument("--current", type=Path, required=True)

    render = subparsers.add_parser(
        "render", help="validate Codex JSON and render release tables"
    )
    render.add_argument("--context", type=Path, required=True)
    render.add_argument("--selection", type=Path, required=True)
    render.add_argument("--current", type=Path, required=True)
    render.add_argument("--top-output", type=Path, required=True)
    render.add_argument("--goroawase-output", type=Path, required=True)
    render.add_argument("--release-output", type=Path, required=True)
    render.add_argument("--diff-summary", type=Path)
    render.add_argument("--catalog-summary", type=Path)
    render.add_argument("--rounds", type=positive_int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            prepare_context(
                args.current,
                args.masks_file,
                args.output,
                specialized_masks=args.specialized_masks,
                diff_path=args.diff,
                diff_summary_path=args.diff_summary,
            )
        elif args.command == "compact":
            write_compact_ai_inputs(
                context_path=args.context,
                request_output=args.request_output,
                direct_output=args.direct_output,
                goroawase_output=args.goroawase_output,
                newly_found_output=args.newly_found_output,
            )
        elif args.command == "validate-selection":
            validate_selection(args.context, args.selection, args.current)
        else:
            render_outputs(
                context_path=args.context,
                selection_path=args.selection,
                current_path=args.current,
                top_output=args.top_output,
                goroawase_output=args.goroawase_output,
                release_output=args.release_output,
                diff_summary_path=args.diff_summary,
                catalog_summary_path=args.catalog_summary,
                rounds=args.rounds,
            )
    except (DataError, OSError, UnicodeError, csv.Error) as exc:
        print(f"top table error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
