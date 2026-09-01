from __future__ import annotations

import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "state_cache", ROOT / "ci" / "state_cache.py"
)
assert SPEC is not None and SPEC.loader is not None
state_cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(state_cache)


class StateCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.state = Path(self.temporary_directory.name) / "state"
        csv_dir = self.state / "scopes" / "full" / "csv"
        csv_dir.mkdir(parents=True)
        (csv_dir / ".collector.lock").touch()
        self.write_csv(
            csv_dir / "1111.csv",
            state_cache.PER_MASK_FIELDS,
            [
                ("07080001111", "10"),
                ("07090001111", "20"),
                ("07090001111", "200"),
            ],
        )
        self.write_csv(
            self.state / "scopes" / "full" / "all_numbers.csv",
            state_cache.AGGREGATE_FIELDS,
            [("07080001111", "10", "1111")],
        )
        self.write_csv(
            self.state / "catalog.csv",
            state_cache.CATALOG_FIELDS,
            [
                (
                    "07080001111",
                    "10",
                    "1111",
                    "2026-08-20T10:00:00Z",
                    "2026-08-20T10:00:00Z",
                    "2026-08-24T10:00:00Z",
                    "1",
                    "0",
                    "active",
                )
            ],
        )
        self.write_csv(
            self.state / "lifecycle.csv",
            state_cache.LIFECYCLE_FIELDS,
            [
                (
                    "07080001111", "10", "1111",
                    "2026-08-20T10:00:00Z", "2026-08-20T10:00:00Z",
                    "2026-08-24T10:00:00Z", "run-0", "1", "1", "0",
                    "0", "", "0.000000000000", "retained",
                    "2026-08-20T10:00:00Z", "", "", "0", "", "0",
                    "native", "1",
                ),
                (
                    "07090001111", "20", "1111", "", "", "", "", "0",
                    "0", "0", "0", "", "0.000000000000",
                    "legacy_history_unknown", "", "", "", "0", "", "0",
                    "legacy_history", "1",
                ),
            ],
        )
        self.write_csv(
            self.state / "scopes" / "full" / "scan_history.csv",
            state_cache.SCAN_HISTORY_FIELDS,
            [],
        )
        self.write_csv(
            self.state / "mask_days.csv",
            state_cache.MASK_DAY_FIELDS,
            [],
        )
        self.write_csv(
            self.state / "lifecycle_events.csv",
            state_cache.EVENT_FIELDS,
            [],
        )
        self.write_manifest(3)

    @staticmethod
    def write_csv(path: Path, fields: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(fields)
            writer.writerows(rows)

    def write_manifest(self, version: int) -> None:
        (self.state / "manifest.json").write_text(
            json.dumps({"schemaVersion": version, "scopes": ["full"]}) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def read_dict(path: Path) -> list[dict[str, str]]:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_v3_state_is_valid(self) -> None:
        state_cache.validate_state(self.state)

    def test_v3_catalog_must_equal_lifecycle_projection(self) -> None:
        self.write_csv(self.state / "catalog.csv", state_cache.CATALOG_FIELDS, [])
        with self.assertRaisesRegex(state_cache.StateCacheError, "active projection"):
            state_cache.validate_state(self.state)

    def test_scan_history_rejects_downgraded_planning_headroom(self) -> None:
        self.write_csv(
            self.state / "scopes" / "full" / "scan_history.csv",
            state_cache.SCAN_HISTORY_FIELDS,
            [
                (
                    "2026-08-25T10:00:00Z", "1111", "1", "1", "5", "5", "0",
                    "5", "0", "1", "1", "0", "9000", "9000", "10000", "5",
                    "5", "300", "coverage_target", "true",
                )
            ],
        )
        with self.assertRaisesRegex(state_cache.StateCacheError, "invalid state row"):
            state_cache.validate_state(self.state)

    def test_scan_history_rejects_impossible_sampling_saturation(self) -> None:
        self.write_csv(
            self.state / "scopes" / "full" / "scan_history.csv",
            state_cache.SCAN_HISTORY_FIELDS,
            [
                (
                    "2026-08-25T10:00:00Z", "1111", "2", "2", "23", "23", "0",
                    "43", "0", "1", "1", "0", "9000", "9900", "5000", "100",
                    "100", "300", "sampling_saturated", "false",
                )
            ],
        )
        with self.assertRaisesRegex(state_cache.StateCacheError, "invalid state row"):
            state_cache.validate_state(self.state)

    def test_duplicate_qualified_mask_day_is_rejected(self) -> None:
        rows = []
        for run_key, recorded in (
            ("run-1", "2026-08-25T10:00:00Z"),
            ("run-2", "2026-08-25T11:00:00Z"),
        ):
            rows.append(
                (
                    "2026-08-25", "1111", run_key, "scheduled_full",
                    "2", "2", "5", "1", "1", "coverage_target",
                    "true", "true", "scheduled_full_comparable",
                    "0.094528654801", recorded, "1",
                )
            )
        self.write_csv(self.state / "mask_days.csv", state_cache.MASK_DAY_FIELDS, rows)
        with self.assertRaisesRegex(state_cache.StateCacheError, "duplicate qualified"):
            state_cache.validate_state(self.state)

    def test_lifecycle_tombstones_still_require_identity_history(self) -> None:
        rows = self.read_dict(self.state / "lifecycle.csv")
        rows.append(
            {
                "phoneNumber": "07070001111",
                "id": "70",
                "sourceMask": "1111",
                "firstSeenAt": "2026-08-01T00:00:00Z",
                "lastSeenAt": "2026-08-01T00:00:00Z",
                "lastCheckedAt": "2026-08-10T00:00:00Z",
                "lastObservedRunKey": "run-0",
                "seenRuns": "2",
                "seenQualifiedDays": "0",
                "resolvedSamplingMissDays": "0",
                "consecutiveQualifiedMissDays": "5",
                "lastQualifiedMissDate": "2026-08-10",
                "negativeLogMissLikelihood": "10.000000000000",
                "status": "statistically_stale",
                "statusChangedAt": "2026-08-10T00:00:00Z",
                "tombstonedAt": "2026-08-10T00:00:00Z",
                "tombstoneReason": "sampling",
                "resurrectionCount": "0",
                "lastResurrectedAt": "",
                "legacyComparableMisses": "0",
                "provenance": "native",
                "evidenceModelVersion": "1",
            }
        )
        self.write_csv(
            self.state / "lifecycle.csv",
            state_cache.LIFECYCLE_FIELDS,
            [tuple(row[field] for field in state_cache.LIFECYCLE_FIELDS) for row in rows],
        )
        with self.assertRaisesRegex(state_cache.StateCacheError, "absent"):
            state_cache.validate_state(self.state)

    def test_v3_lifecycle_must_cover_every_cached_history_phone(self) -> None:
        rows = [
            row
            for row in self.read_dict(self.state / "lifecycle.csv")
            if row["phoneNumber"] != "07090001111"
        ]
        self.write_csv(
            self.state / "lifecycle.csv",
            state_cache.LIFECYCLE_FIELDS,
            [tuple(row[field] for field in state_cache.LIFECYCLE_FIELDS) for row in rows],
        )
        with self.assertRaisesRegex(state_cache.StateCacheError, "phone sets disagree"):
            state_cache.validate_state(self.state)

    def test_v3_event_id_must_match_its_identity_fields(self) -> None:
        row = (
            "0" * 64,
            "2026-08-31T10:10:00Z",
            "2026-08-31",
            "run-1",
            "07080001111",
            "10",
            "1111",
            "added",
            "",
            "retained",
            "first_observation",
            "1",
        )
        self.write_csv(
            self.state / "lifecycle_events.csv",
            state_cache.EVENT_FIELDS,
            [row],
        )

        with self.assertRaisesRegex(state_cache.StateCacheError, "invalid state row"):
            state_cache.validate_state(self.state)

        valid_id = hashlib.sha256(
            "|".join((row[3], row[4], row[7], row[8], row[9])).encode("utf-8")
        ).hexdigest()
        valid_row = (valid_id,) + row[1:]
        self.write_csv(
            self.state / "lifecycle_events.csv",
            state_cache.EVENT_FIELDS,
            [valid_row],
        )
        state_cache.validate_state(self.state)

    def test_partial_v3_root_and_symlink_are_rejected(self) -> None:
        (self.state / "mask_days.csv").unlink()
        with self.assertRaisesRegex(state_cache.StateCacheError, "unexpected root"):
            state_cache.validate_state(self.state)

    def test_cli_reports_invalid_state(self) -> None:
        self.write_manifest(2)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = state_cache.main(["validate", "--state-dir", str(self.state)])
        self.assertEqual(result, 1)
        self.assertIn("state cache error:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
