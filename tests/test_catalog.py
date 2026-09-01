from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("catalog", ROOT / "ci" / "catalog.py")
assert SPEC is not None and SPEC.loader is not None
catalog = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = catalog
SPEC.loader.exec_module(catalog)


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.sequence = 0

    @staticmethod
    def write_csv(path: Path, fields: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(fields)
            writer.writerows(rows)

    @staticmethod
    def read_dict(path: Path) -> list[dict[str, str]]:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def make_summary(
        self,
        path: Path,
        lifecycle_path: Path | None,
        current_rows: list[tuple[str, str, str]],
        *,
        comparable: bool = True,
        target_bps: int | None = None,
        empty_probe: bool = False,
    ) -> None:
        lifecycle = (
            catalog.load_lifecycle(lifecycle_path) if lifecycle_path is not None else {}
        )
        historical = len(lifecycle)
        pool = sum(
            record.status in catalog.COVERAGE_POOL_STATUSES
            for record in lifecycle.values()
        )
        current_phones = {row[0] for row in current_rows}
        known = sum(
            phone in current_phones and record.status in catalog.COVERAGE_POOL_STATUSES
            for phone, record in lifecycle.items()
        )
        observed = len(current_rows)
        new = observed - known
        if empty_probe:
            if pool == 0 or current_rows:
                raise AssertionError("empty probe fixture requires a warm empty pool")
            comparable = True
            achieved = "0"
            target = 9000
            stop = "empty_probe_limit"
        elif pool == 0:
            comparable = False
            achieved = ""
            stop = "cold_start_saturated"
            target = 8000
        else:
            achieved = str(10_000 * known // pool)
            target = (
                target_bps
                if target_bps is not None
                else min(9000, 10_000 * known // pool)
            )
            stop = "coverage_target" if comparable else "round_limit"
        row = (
            "1111",
            str(historical),
            str(pool),
            "5",
            "5",
            "0",
            "0" if empty_probe else str(max(observed, 5 if comparable else observed)),
            "5" if empty_probe else "0",
            str(observed),
            str(known),
            str(new),
            str(target),
            "9900",
            achieved,
            "5",
            "5",
            "5",
            stop,
            "true" if comparable else "false",
        )
        self.write_csv(path, catalog.MASK_SUMMARY_FIELDS, [row])

    def run_update(
        self,
        *,
        previous: dict[str, Path] | None,
        current_rows: list[tuple[str, str, str]],
        when: datetime,
        evidence_day: date,
        run_kind: str = "scheduled_full",
        run_key: str | None = None,
        comparable: bool = True,
        empty_probe: bool = False,
        coverage_override: list[tuple[str, str]] | None = None,
    ) -> tuple[dict[str, Path], dict[str, object]]:
        self.sequence += 1
        name = f"run-{self.sequence}"
        run_key = run_key or name
        current = self.root / f"{name}-current.csv"
        coverage_pool = self.root / f"{name}-coverage-pool.csv"
        summary_input = self.root / f"{name}-mask.csv"
        self.write_csv(current, ("phoneNumber", "id", "sourceMask"), current_rows)
        lifecycle = (
            catalog.load_lifecycle(previous["lifecycle"]) if previous else {}
        )
        coverage_rows = [
            (phone, record.source_mask)
            for phone, record in sorted(lifecycle.items())
            if record.status in catalog.COVERAGE_POOL_STATUSES
        ]
        self.write_csv(
            coverage_pool,
            catalog.COVERAGE_POOL_FIELDS,
            coverage_rows if coverage_override is None else coverage_override,
        )
        self.make_summary(
            summary_input,
            previous["lifecycle"] if previous else None,
            current_rows,
            comparable=comparable,
            empty_probe=empty_probe,
        )
        outputs = {
            key: self.root / f"{name}-{filename}"
            for key, filename in {
                "catalog": "catalog.csv",
                "lifecycle": "lifecycle.csv",
                "days": "mask-days.csv",
                "events": "events.csv",
                "summary": "summary.json",
            }.items()
        }
        result = catalog.update_catalog(
            current_path=current,
            coverage_pool_path=coverage_pool,
            previous_path=previous["catalog"] if previous else None,
            previous_lifecycle_path=previous["lifecycle"] if previous else None,
            previous_mask_days_path=previous["days"] if previous else None,
            previous_events_path=previous["events"] if previous else None,
            mask_summary_path=summary_input,
            observed_at=when,
            evidence_date=evidence_day,
            run_key=run_key,
            run_kind=run_kind,
            catalog_output=outputs["catalog"],
            lifecycle_output=outputs["lifecycle"],
            mask_days_output=outputs["days"],
            events_output=outputs["events"],
            summary_output=outputs["summary"],
        )
        outputs["current_input"] = current
        outputs["coverage_input"] = coverage_pool
        outputs["mask_input"] = summary_input
        return outputs, result

    def seed_two(self) -> tuple[dict[str, Path], datetime]:
        started = datetime(2026, 8, 20, 10, tzinfo=timezone.utc)
        outputs, _summary = self.run_update(
            previous=None,
            current_rows=[
                ("07080001111", "1", "1111"),
                ("07090001111", "2", "1111"),
            ],
            when=started,
            evidence_day=date(2026, 8, 20),
        )
        return outputs, started

    def test_same_jst_day_can_add_only_one_qualified_miss(self) -> None:
        previous, started = self.seed_two()
        keeper = [("07090001111", "2", "1111")]
        previous, _ = self.run_update(
            previous=previous,
            current_rows=keeper,
            when=started + timedelta(days=1),
            evidence_day=date(2026, 8, 21),
        )
        previous, summary = self.run_update(
            previous=previous,
            current_rows=keeper,
            when=started + timedelta(days=1, hours=2),
            evidence_day=date(2026, 8, 21),
        )

        lifecycle = catalog.load_lifecycle(previous["lifecycle"])
        self.assertEqual(
            lifecycle["07080001111"].consecutive_qualified_miss_days, 1
        )
        self.assertEqual(summary["qualifiedMaskCount"], 0)
        days = self.read_dict(previous["days"])
        self.assertEqual([row["qualified"] for row in days[-2:]], ["true", "false"])

    def test_manual_and_non_comparable_runs_are_positive_only(self) -> None:
        previous, started = self.seed_two()
        keeper = [("07090001111", "2", "1111")]
        previous, _ = self.run_update(
            previous=previous,
            current_rows=keeper,
            when=started + timedelta(days=1),
            evidence_day=date(2026, 8, 21),
            run_kind="manual_full",
        )
        previous, _ = self.run_update(
            previous=previous,
            current_rows=keeper,
            when=started + timedelta(days=2),
            evidence_day=date(2026, 8, 22),
            comparable=False,
        )

        lifecycle = catalog.load_lifecycle(previous["lifecycle"])
        self.assertEqual(
            lifecycle["07080001111"].consecutive_qualified_miss_days, 0
        )

    def test_five_low_coverage_days_do_not_tombstone(self) -> None:
        previous, started = self.seed_two()
        # Establish the minimum two qualified positive days.
        both = [
            ("07080001111", "1", "1111"),
            ("07090001111", "2", "1111"),
        ]
        for offset in (1, 2):
            previous, _ = self.run_update(
                previous=previous,
                current_rows=both,
                when=started + timedelta(days=offset),
                evidence_day=date(2026, 8, 20) + timedelta(days=offset),
            )
        keeper = [("07090001111", "2", "1111")]
        for offset in range(3, 8):
            previous, _ = self.run_update(
                previous=previous,
                current_rows=keeper,
                when=started + timedelta(days=offset),
                evidence_day=date(2026, 8, 20) + timedelta(days=offset),
            )

        record = catalog.load_lifecycle(previous["lifecycle"])["07080001111"]
        self.assertEqual(record.status, "possibly_unavailable")
        self.assertLess(
            record.negative_log_miss_likelihood,
            catalog.MIN_NEGATIVE_LOG_LIKELIHOOD,
        )
        self.assertIn("07080001111", catalog.load_catalog(previous["catalog"]))

    def test_high_evidence_creates_persistent_tombstone_and_manual_resurrection(self) -> None:
        started = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
        phones = [
            (f"070{index:04d}1111", str(index + 1), "1111")
            for index in range(100)
        ]
        previous, _ = self.run_update(
            previous=None,
            current_rows=phones,
            when=started,
            evidence_day=date(2026, 8, 1),
        )
        target = phones[0]
        observed = phones[1:]
        for offset in range(1, 6):
            previous, summary = self.run_update(
                previous=previous,
                current_rows=observed,
                when=started + timedelta(days=offset),
                evidence_day=date(2026, 8, 1) + timedelta(days=offset),
            )

        lifecycle = catalog.load_lifecycle(previous["lifecycle"])
        tombstone = lifecycle[target[0]]
        self.assertEqual(tombstone.status, "statistically_stale")
        self.assertEqual(tombstone.seen_qualified_days, 0)
        self.assertNotIn(target[0], catalog.load_catalog(previous["catalog"]))
        self.assertEqual(len(lifecycle), 100)
        self.assertEqual(summary["tombstoned"], 1)

        original_first_seen = tombstone.first_seen_at
        previous, summary = self.run_update(
            previous=previous,
            current_rows=phones,
            when=started + timedelta(days=6),
            evidence_day=date(2026, 8, 7),
            run_kind="manual_specialized",
        )
        resurrected = catalog.load_lifecycle(previous["lifecycle"])[target[0]]
        self.assertEqual(resurrected.status, "retained")
        self.assertEqual(resurrected.first_seen_at, original_first_seen)
        self.assertEqual(resurrected.resurrection_count, 1)
        self.assertEqual(summary["resurrected"], 1)
        self.assertIn(target[0], catalog.load_catalog(previous["catalog"]))

    def test_warm_empty_mask_tombstones_one_hit_only_after_five_days(self) -> None:
        started = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
        phone = ("07080001111", "1", "1111")
        previous, _ = self.run_update(
            previous=None,
            current_rows=[phone],
            when=started,
            evidence_day=date(2026, 8, 1),
        )

        for offset in range(1, 6):
            previous, summary = self.run_update(
                previous=previous,
                current_rows=[],
                when=started + timedelta(days=offset),
                evidence_day=date(2026, 8, 1) + timedelta(days=offset),
                empty_probe=True,
            )
            record = catalog.load_lifecycle(previous["lifecycle"])[phone[0]]
            if offset < 5:
                self.assertNotEqual(record.status, "statistically_stale")

        record = catalog.load_lifecycle(previous["lifecycle"])[phone[0]]
        self.assertEqual(record.seen_qualified_days, 0)
        self.assertEqual(record.status, "statistically_stale")
        self.assertGreaterEqual(
            record.negative_log_miss_likelihood,
            catalog.MIN_NEGATIVE_LOG_LIKELIHOOD,
        )
        self.assertEqual(summary["tombstoned"], 1)

    def test_catalog_rejects_a_coverage_pool_missing_global_active_phone(self) -> None:
        previous, started = self.seed_two()
        with self.assertRaisesRegex(catalog.CatalogError, "exact lifecycle"):
            self.run_update(
                previous=previous,
                current_rows=[("07090001111", "2", "1111")],
                when=started + timedelta(days=1),
                evidence_day=date(2026, 8, 21),
                coverage_override=[("07090001111", "1111")],
            )

    def test_legacy_history_unknown_is_activated_without_resurrection(self) -> None:
        phone = "07080001111"
        previous = {
            key: self.root / filename
            for key, filename in {
                "catalog": "legacy-catalog.csv",
                "lifecycle": "legacy-lifecycle.csv",
                "days": "legacy-days.csv",
                "events": "legacy-events.csv",
            }.items()
        }
        self.write_csv(previous["catalog"], catalog.CATALOG_FIELDS, [])
        self.write_csv(
            previous["lifecycle"],
            catalog.LIFECYCLE_FIELDS,
            [
                (
                    phone, "1", "1111", "", "", "", "", "0", "0", "0",
                    "0", "", "0.000000000000", "legacy_history_unknown", "",
                    "", "", "0", "", "0", "legacy_history", "1",
                )
            ],
        )
        self.write_csv(previous["days"], catalog.MASK_DAY_FIELDS, [])
        self.write_csv(previous["events"], catalog.EVENT_FIELDS, [])

        updated, summary = self.run_update(
            previous=previous,
            current_rows=[(phone, "1", "1111")],
            when=datetime(2026, 8, 20, 10, tzinfo=timezone.utc),
            evidence_day=date(2026, 8, 20),
            run_kind="manual_specialized",
        )
        record = catalog.load_lifecycle(updated["lifecycle"])[phone]
        self.assertEqual(record.status, "retained")
        self.assertEqual(record.resurrection_count, 0)
        self.assertEqual(summary["resurrected"], 0)
        events = self.read_dict(updated["events"])
        self.assertEqual(events[-1]["eventType"], "status_changed")
        self.assertEqual(events[-1]["reason"], "legacy_history_observed")

    def test_duplicate_run_key_is_rejected_fail_closed(self) -> None:
        previous, started = self.seed_two()
        keeper = [("07090001111", "2", "1111")]
        processed, _ = self.run_update(
            previous=previous,
            current_rows=keeper,
            when=started + timedelta(days=1),
            evidence_day=date(2026, 8, 21),
            run_key="stable-run-key",
        )
        with self.assertRaisesRegex(catalog.CatalogError, "already been processed"):
            self.run_update(
                previous=processed,
                current_rows=keeper,
                when=started + timedelta(days=1),
                evidence_day=date(2026, 8, 21),
                run_key="stable-run-key",
            )

    def test_duplicate_cold_run_key_cannot_change_identity_or_outputs(self) -> None:
        started = datetime(2026, 8, 20, 10, tzinfo=timezone.utc)
        current = [
            ("07080001111", "1", "1111"),
            ("07090001111", "2", "1111"),
        ]
        processed, _ = self.run_update(
            previous=None,
            current_rows=current,
            when=started,
            evidence_day=date(2026, 8, 20),
            run_key="cold-run-key",
        )
        replay = {
            key: self.root / f"cold-replay-{filename}"
            for key, filename in {
                "catalog": "catalog.csv",
                "lifecycle": "lifecycle.csv",
                "days": "mask-days.csv",
                "events": "events.csv",
                "summary": "summary.json",
            }.items()
        }
        changed_current = self.root / "cold-replay-changed-current.csv"
        self.write_csv(
            changed_current,
            ("phoneNumber", "id", "sourceMask"),
            [
                ("07080001111", "998", "1111"),
                ("07090001111", "999", "1111"),
            ],
        )
        with self.assertRaisesRegex(catalog.CatalogError, "already been processed"):
            catalog.update_catalog(
                current_path=changed_current,
                coverage_pool_path=processed["coverage_input"],
                previous_path=processed["catalog"],
                previous_lifecycle_path=processed["lifecycle"],
                previous_mask_days_path=processed["days"],
                previous_events_path=processed["events"],
                mask_summary_path=processed["mask_input"],
                observed_at=started,
                evidence_date=date(2026, 8, 20),
                run_key="cold-run-key",
                run_kind="scheduled_full",
                catalog_output=replay["catalog"],
                lifecycle_output=replay["lifecycle"],
                mask_days_output=replay["days"],
                events_output=replay["events"],
                summary_output=replay["summary"],
            )
        self.assertFalse(any(path.exists() for path in replay.values()))

    def test_strict_comparable_arithmetic_is_not_trusted_blindly(self) -> None:
        path = self.root / "mask.csv"
        row = (
            "1111", "2", "2", "5", "5", "0", "5", "0", "1", "1", "0",
            "5000", "9900", "5000", "5", "5", "5", "round_limit", "true",
        )
        self.write_csv(path, catalog.MASK_SUMMARY_FIELDS, [row])
        with self.assertRaisesRegex(catalog.CatalogError, "comparable flag"):
            catalog.load_mask_summary(path)

    def test_planning_headroom_cannot_be_downgraded_by_input(self) -> None:
        path = self.root / "mask-without-headroom.csv"
        row = (
            "1111", "1", "1", "5", "5", "0", "5", "0", "1", "1", "0",
            "9000", "9000", "10000", "5", "5", "300", "coverage_target", "true",
        )
        self.write_csv(path, catalog.MASK_SUMMARY_FIELDS, [row])
        with self.assertRaisesRegex(catalog.CatalogError, "inconsistent mask summary"):
            catalog.load_mask_summary(path)

    def test_sampling_saturation_requires_enough_samples(self) -> None:
        path = self.root / "invalid-sampling-saturation.csv"
        row = (
            "1111", "2", "2", "23", "23", "0", "43", "0", "1", "1", "0",
            "9000", "9900", "5000", "100", "100", "300",
            "sampling_saturated", "false",
        )
        self.write_csv(path, catalog.MASK_SUMMARY_FIELDS, [row])
        with self.assertRaisesRegex(catalog.CatalogError, "inconsistent mask summary"):
            catalog.load_mask_summary(path)

    def test_lifecycle_event_id_must_match_its_identity_fields(self) -> None:
        path = self.root / "events.csv"
        row = (
            "0" * 64,
            "2026-08-31T10:10:00Z",
            "2026-08-31",
            "run-1",
            "07080001111",
            "1",
            "1111",
            "added",
            "",
            "retained",
            "first_observation",
            "1",
        )
        self.write_csv(path, catalog.EVENT_FIELDS, [row])

        with self.assertRaisesRegex(
            catalog.CatalogError, "event id does not match its fields"
        ):
            catalog.load_events(path)

if __name__ == "__main__":
    unittest.main()
