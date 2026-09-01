from __future__ import annotations

import contextlib
import csv
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "raku-mobi-bangou.py"
SPEC = importlib.util.spec_from_file_location("raku_mobi_bangou", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
collector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


class CollectorTests(unittest.TestCase):
    def test_load_masks_accepts_explicit_goroawase_column(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "masks.txt"
            path.write_text(
                "# ordinary\n1235\n# goroawase\n1122 | いい夫婦\n",
                encoding="utf-8",
            )

            self.assertEqual(collector.load_masks(path), ["1235", "1122"])

            path.write_text("1235\n1235 | 読み\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate mask 1235"):
                collector.load_masks(path)

    def test_parse_offers_validates_mask_and_fields(self) -> None:
        offers = collector.parse_offers(
            {
                "randomPhoneNumbers": [
                    {"phoneNumber": "07012341111", "id": "1234567890123456789"}
                ]
            },
            "1111",
        )
        self.assertEqual(
            offers,
            [collector.PhoneOffer("07012341111", "1234567890123456789")],
        )

        with self.assertRaises(collector.ResponseError):
            collector.parse_offers(
                {
                    "randomPhoneNumbers": [
                        {"phoneNumber": "07012349012", "id": "1"}
                    ]
                },
                "1111",
            )

    def test_five_empty_responses_deactivate_mask_for_the_run(self) -> None:
        calls: list[str] = []
        sleeps: list[float] = []

        def fake_fetch(_client, _url, mask, _timeout):
            calls.append(mask)
            if mask == "1111":
                return []
            return [collector.PhoneOffer("07000002222", "2222000000000000001")]

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["1111", "2222"],
                    output_dir=output_dir,
                    rounds=5,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    deep_scan=True,
                    mask_cooldown=0,
                    request_attempts=3,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=sleeps.append,
                    random_delay=lambda _minimum, _maximum: 1.5,
                )

            self.assertEqual(stats.failed_requests, 0)
            self.assertEqual(calls.count("1111"), 5)
            self.assertEqual(calls.count("2222"), 5)
            self.assertEqual(calls, ["1111", "2222"] * 5)
            self.assertEqual(sleeps, [1.5] * 9)
            self.assertFalse(stats.mask_stats["1111"].comparable)
            self.assertEqual(
                stats.mask_stats["1111"].stop_reason,
                "empty_probe_limit",
            )
            self.assertEqual(stats.mask_stats["1111"].successful_responses, 5)
            self.assertEqual(stats.mask_stats["2222"].stop_reason, "round_limit")
            self.assertEqual(
                (output_dir / "1111.csv").read_text(encoding="utf-8"),
                "phoneNumber,id\n",
            )
            self.assertEqual(
                (output_dir / "2222.csv").read_text(encoding="utf-8"),
                "phoneNumber,id\n07000002222,2222000000000000001\n",
            )

    def test_five_empty_warm_probes_are_comparable_whole_pool_evidence(self) -> None:
        phone = "07000001111"
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            (output_dir / "1111.csv").write_text(
                f"phoneNumber,id\n{phone},1\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["1111"],
                    output_dir=output_dir,
                    rounds=300,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    opener=object(),
                    fetch=lambda *_args: [],
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        mask_stats = stats.mask_stats["1111"]
        self.assertEqual(mask_stats.successful_responses, 5)
        self.assertEqual(mask_stats.empty_responses, 5)
        self.assertEqual(mask_stats.stop_reason, "empty_probe_limit")
        self.assertTrue(mask_stats.comparable)

    def test_nonempty_response_resets_empty_streak(self) -> None:
        responses = iter(
            [
                [],
                [],
                [collector.PhoneOffer("07000003333", "3333000000000000001")],
                [],
                [],
                [],
            ]
        )
        calls = 0

        def fake_fetch(_client, _url, _mask, _timeout):
            nonlocal calls
            calls += 1
            return next(responses)

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["3333"],
                    output_dir=Path(temporary),
                    rounds=6,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    request_attempts=3,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        self.assertEqual(stats.failed_requests, 0)
        self.assertEqual(calls, 6)

    def test_five_empty_responses_after_a_result_stop_cold_mask(self) -> None:
        offer = collector.PhoneOffer("07000003333", "3333000000000000001")
        responses = iter([[offer], [], [], [], [], []])

        def fake_fetch(_client, _url, _mask, _timeout):
            return next(responses)

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["3333"],
                    output_dir=Path(temporary),
                    rounds=300,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    request_attempts=3,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        self.assertEqual(stats.mask_stats["3333"].successful_responses, 6)
        self.assertEqual(stats.mask_stats["3333"].stop_reason, "empty_probe_limit")
        self.assertFalse(stats.mask_stats["3333"].comparable)

    def test_transient_request_failure_is_retried(self) -> None:
        attempts = 0

        def fake_fetch(_client, _url, _mask, _timeout):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise URLError("temporary")
            return [collector.PhoneOffer("07000004444", "4444000000000000001")]

        with tempfile.TemporaryDirectory() as temporary:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                stats = collector.collect(
                    masks=["4444"],
                    output_dir=Path(temporary),
                    rounds=1,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    request_attempts=3,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        self.assertEqual(stats.failed_requests, 0)
        self.assertEqual(stats.retries, 2)
        self.assertEqual(attempts, 3)

    def test_transient_json_decode_failure_is_retried(self) -> None:
        attempts = 0

        def fake_fetch(_client, _url, _mask, _timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise json.JSONDecodeError("temporary truncation", "{", 1)
            return [collector.PhoneOffer("07000004444", "4444000000000000001")]

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["4444"],
                    output_dir=Path(temporary),
                    rounds=1,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        self.assertEqual(attempts, 2)
        self.assertEqual(stats.retries, 1)

    def test_exhausted_request_fails_fast_after_initial_plus_three_retries(self) -> None:
        calls: list[str] = []
        stats = collector.CollectionStats()

        def fake_fetch(_client, _url, mask, _timeout):
            calls.append(mask)
            raise URLError("still unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaisesRegex(
                    collector.FatalCollectionError,
                    "mask 5555: request exhausted after 4 attempts",
                ),
            ):
                collector.collect(
                    masks=["5555", "6666"],
                    output_dir=Path(temporary),
                    rounds=1,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    request_attempts=collector.DEFAULT_REQUEST_ATTEMPTS,
                    opener=object(),
                    fetch=fake_fetch,
                    stats=stats,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

            with collector.OfferStore(Path(temporary), ["5555"]) as reopened:
                self.assertEqual(reopened.phone_count, 0)

        self.assertEqual(stats.failed_requests, 1)
        self.assertEqual(stats.retries, 3)
        self.assertEqual(calls, ["5555"] * 4)

    def test_default_retry_budget_is_three_retries_after_initial_attempt(self) -> None:
        args = collector.build_parser().parse_args([])
        self.assertEqual(collector.DEFAULT_REQUEST_ATTEMPTS, 4)
        self.assertEqual(args.target_coverage, 9_000)
        self.assertEqual(args.request_limit, 5_000)
        self.assertEqual(args.mask_cooldown, 30.0)
        self.assertFalse(hasattr(args, "request_attempts"))
        self.assertFalse(hasattr(args, "failure_limit"))

        args = collector.build_parser().parse_args(
            ["--target-coverage", "0.80"]
        )
        self.assertEqual(args.target_coverage, 8_000)

    def test_non_retryable_response_stops_before_next_mask(self) -> None:
        calls: list[str] = []

        def fake_fetch(_client, _url, mask, _timeout):
            calls.append(mask)
            raise collector.ResponseError("phone does not match requested mask")

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                collector.FatalCollectionError,
                "non-retryable endpoint response",
            ):
                collector.collect(
                    masks=["1111", "2222"],
                    output_dir=Path(temporary),
                    rounds=1,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        self.assertEqual(calls, ["1111"])

    def test_api_url_requires_clean_https_endpoint(self) -> None:
        self.assertEqual(
            collector.validate_api_url("https://example.test/path"),
            "https://example.test/path",
        )
        for invalid in (
            "http://example.test/path",
            "https://example.test/path?",
            "https://example.test/path?mask=1",
            "https://example.test/path#",
            "https://example.test/path#fragment",
            "https://user:secret@example.test/path",
            "not-a-url",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    collector.validate_api_url(invalid)

    def test_warm_mask_reobserves_history_until_coverage_target(self) -> None:
        calls = 0
        known = collector.PhoneOffer("07000006666", "6666000000000000001")

        def fake_fetch(_client, _url, _mask, _timeout):
            nonlocal calls
            calls += 1
            return [known]

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            (output_dir / "6666.csv").write_text(
                "phoneNumber,id\n07000006666,6666000000000000001\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["6666"],
                    output_dir=output_dir,
                    rounds=20,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    deep_scan=True,
                    mask_cooldown=0,
                    request_attempts=3,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        self.assertEqual(calls, collector.MIN_PROBES)
        self.assertEqual(stats.added_phones, 0)
        self.assertEqual(stats.deactivated_coverage, 1)
        self.assertEqual(stats.active_masks, 0)
        self.assertTrue(stats.mask_stats["6666"].comparable)
        self.assertEqual(
            stats.mask_stats["6666"].stop_reason,
            "coverage_target",
        )
        self.assertEqual(stats.mask_stats["6666"].historical_distinct_at_start, 1)
        self.assertEqual(stats.mask_stats["6666"].coverage_pool_at_start, 1)
        self.assertEqual(stats.mask_stats["6666"].achieved_coverage_bps, 10_000)

    def test_lifecycle_exclusions_define_the_coverage_pool(self) -> None:
        phones = [
            "07000001111",
            "07000011111",
            "07000021111",
            "07000031111",
        ]
        lifecycle = {
            phones[0]: ("1111", "retained"),
            phones[1]: ("1111", "statistically_stale"),
            phones[2]: ("1111", "confirmed_unavailable"),
            phones[3]: ("1111", "legacy_history_unknown"),
        }
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            (output_dir / "1111.csv").write_text(
                "phoneNumber,id\n"
                + "".join(
                    f"{phone},{index + 1}\n"
                    for index, phone in enumerate(phones)
                ),
                encoding="utf-8",
            )
            with collector.OfferStore(output_dir, ["1111"]) as store:
                pools = collector.coverage_pools(["1111"], store, lifecycle)

        self.assertEqual(pools["1111"], frozenset((phones[0],)))

    def test_global_lifecycle_phone_enters_another_scope_coverage_pool(self) -> None:
        local = "07000001111"
        specialized_only = "07000011111"
        lifecycle = {
            local: ("1111", "retained"),
            specialized_only: ("1111", "possibly_unavailable"),
        }
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            (output_dir / "1111.csv").write_text(
                f"phoneNumber,id\n{local},1\n",
                encoding="utf-8",
            )
            with collector.OfferStore(output_dir, ["1111"]) as store:
                historical = collector.historical_pools(
                    ["1111"], store, lifecycle
                )
                coverage = collector.coverage_pools(["1111"], store, lifecycle)

        expected = frozenset((local, specialized_only))
        self.assertEqual(historical["1111"], expected)
        self.assertEqual(coverage["1111"], expected)

    def test_lifecycle_only_phone_is_observed_as_known_in_another_scope(self) -> None:
        local = "07000001111"
        specialized_only = "07000011111"
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            (output_dir / "1111.csv").write_text(
                f"phoneNumber,id\n{local},1\n",
                encoding="utf-8",
            )
            with collector.OfferStore(output_dir, ["1111"]) as store:
                historical = {"1111": {local, specialized_only}}
                coverage = {"1111": {local, specialized_only}}
                with contextlib.redirect_stdout(io.StringIO()):
                    stats = collector.collect(
                        masks=["1111"],
                        output_dir=output_dir,
                        rounds=5,
                        delay_min=1.1,
                        delay_max=2.0,
                        timeout=1.0,
                        api_url="https://example.test/api",
                        mask_cooldown=0,
                        historical_pool_by_mask=historical,
                        coverage_pool_by_mask=coverage,
                        opener=object(),
                        fetch=lambda *_args: [
                            collector.PhoneOffer(specialized_only, "2")
                        ],
                        store=store,
                        sleep=lambda _delay: None,
                        random_delay=lambda _minimum, _maximum: 1.1,
                    )

        mask_stats = stats.mask_stats["1111"]
        self.assertEqual(mask_stats.historical_distinct_at_start, 2)
        self.assertEqual(mask_stats.coverage_pool_at_start, 2)
        self.assertEqual(mask_stats.observed_known_phones, {specialized_only})
        self.assertEqual(mask_stats.observed_new_phones, set())

    def test_lifecycle_loader_requires_exact_schema_and_valid_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lifecycle.csv"
            row = {field: "" for field in collector.LIFECYCLE_FIELDS}
            row.update(
                phoneNumber="07000001111",
                id="1",
                sourceMask="1111",
                status="possibly_unavailable",
            )
            with path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=collector.LIFECYCLE_FIELDS,
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerow(row)

            self.assertEqual(
                collector.load_lifecycle_statuses(path),
                {"07000001111": ("1111", "possibly_unavailable")},
            )
            path.write_text("phoneNumber,status\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported lifecycle CSV"):
                collector.load_lifecycle_statuses(path)

    def test_resurrected_excluded_phone_is_new_to_the_coverage_pool(self) -> None:
        excluded = collector.PhoneOffer("07000001111", "1")
        retained_phone = "07000011111"

        def fake_fetch(_client, _url, _mask, _timeout):
            return [excluded]

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            (output_dir / "1111.csv").write_text(
                "phoneNumber,id\n"
                "07000001111,1\n"
                f"{retained_phone},2\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["1111"],
                    output_dir=output_dir,
                    rounds=20,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    coverage_pool_by_mask={"1111": {retained_phone}},
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        mask_stats = stats.mask_stats["1111"]
        self.assertEqual(mask_stats.stop_reason, "request_cap")
        self.assertTrue(mask_stats.comparable)
        self.assertEqual(mask_stats.historical_distinct_at_start, 2)
        self.assertEqual(mask_stats.coverage_pool_at_start, 1)
        self.assertEqual(mask_stats.observed_known_phones, set())
        self.assertEqual(mask_stats.observed_new_phones, {excluded.phone_number})
        self.assertEqual(mask_stats.achieved_coverage_bps, 0)

    def test_historical_low_hazard_increases_the_request_budget(self) -> None:
        known = {f"known-{index}" for index in range(20)}
        historical = collector.MaskStats(
            historical_distinct_at_start=100,
            coverage_pool_at_start=100,
            successful_responses=100,
            http_requests=100,
            response_phone_samples=250,
            observed_phones=set(known),
            observed_known_phones=set(known),
            target_coverage_bps=9_000,
            estimated_request_budget=91,
            request_cap=91,
            round_limit=300,
            stop_reason="round_limit",
        )
        record = collector.ScanHistoryRecord(
            "2026-08-20T00:00:00Z",
            historical.row("1111"),
        )
        budget = collector.estimate_request_budget(
            pool_size=100,
            target_coverage_bps=9_000,
            round_limit=300,
            history=[record],
        )
        empirical_hazard = -collector.math.log1p(-0.2) / 100
        expected = collector.math.ceil(-collector.math.log1p(-0.9) / empirical_hazard)

        self.assertEqual(budget, expected)
        self.assertGreater(budget, 300)

    def test_collection_budget_has_sampling_headroom_above_stop_target(self) -> None:
        pool = {f"070{index:04d}1111" for index in range(100)}
        returned = [
            collector.PhoneOffer(phone, str(index + 1))
            for index, phone in enumerate(sorted(pool)[:3])
        ]
        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["1111"],
                    output_dir=Path(temporary),
                    rounds=1,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    historical_pool_by_mask={"1111": pool},
                    coverage_pool_by_mask={"1111": pool},
                    opener=object(),
                    fetch=lambda *_args: returned,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        expected = collector.estimate_request_budget(
            pool_size=100,
            target_coverage_bps=collector.DEFAULT_PLANNING_COVERAGE_BPS,
            round_limit=1,
            history=(),
        )
        target_only = collector.estimate_request_budget(
            pool_size=100,
            target_coverage_bps=collector.DEFAULT_TARGET_COVERAGE_BPS,
            round_limit=1,
            history=(),
        )
        self.assertEqual(stats.planning_coverage_bps, 9_900)
        self.assertEqual(stats.mask_stats["1111"].estimated_request_budget, expected)
        self.assertGreater(expected, target_only)

    def test_online_headroom_above_hard_cap_is_not_comparable(self) -> None:
        phones = [f"070{index:04d}1111" for index in range(100)]
        pool = set(phones)
        calls = 0

        def fake_fetch(_client, _url, _mask, _timeout):
            nonlocal calls
            start = calls * 2
            calls += 1
            return [
                collector.PhoneOffer(phone, str(index + 1))
                for index, phone in enumerate(
                    phones[start : start + 2],
                    start=start,
                )
            ]

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["1111"],
                    output_dir=Path(temporary),
                    rounds=10,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    historical_pool_by_mask={"1111": pool},
                    coverage_pool_by_mask={"1111": pool},
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        online_hazard = -collector.math.log1p(-0.2) / 10
        expected = collector.math.ceil(
            -collector.math.log1p(-0.99) / online_hazard
        )
        mask_stats = stats.mask_stats["1111"]
        self.assertEqual(mask_stats.estimated_request_budget, expected)
        self.assertGreater(expected, mask_stats.round_limit)
        self.assertEqual(mask_stats.stop_reason, "round_limit")
        self.assertFalse(mask_stats.comparable)

    def test_scan_history_round_trips_and_retains_thirty_rows_per_mask(self) -> None:
        phone = "07000001111"
        mask_stats = collector.MaskStats(
            successful_responses=1,
            http_requests=1,
            response_phone_samples=1,
            observed_phones={phone},
            observed_new_phones={phone},
            estimated_request_budget=1,
            request_cap=1,
            round_limit=1,
            stop_reason="round_limit",
        )
        previous = [
            collector.ScanHistoryRecord(
                f"2026-01-{day:02d}T00:00:00Z",
                mask_stats.row("1111"),
            )
            for day in range(1, 32)
        ]
        stats = collector.CollectionStats(mask_stats={"1111": mask_stats})

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scan_history.csv"
            collector.write_scan_history(
                path,
                previous,
                ["1111"],
                stats,
                "2026-02-01T00:00:00Z",
            )
            loaded = collector.load_scan_history(path)

        self.assertEqual(len(loaded), 30)
        self.assertEqual(loaded[0].observed_at, "2026-01-03T00:00:00Z")
        self.assertEqual(loaded[-1].observed_at, "2026-02-01T00:00:00Z")

    def test_global_request_limit_stops_every_remaining_mask(self) -> None:
        calls: list[str] = []

        def fake_fetch(_client, _url, mask, _timeout):
            calls.append(mask)
            return [
                collector.PhoneOffer(
                    f"0700000{mask}",
                    f"{mask}000000000000001",
                )
            ]

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["1111", "2222"],
                    output_dir=Path(temporary),
                    rounds=20,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    request_limit=3,
                    mask_cooldown=0,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        self.assertEqual(calls, ["1111", "2222", "1111"])
        self.assertEqual(stats.requests, 3)
        self.assertEqual(stats.responses, 3)
        self.assertEqual(stats.deactivated_global_limit, 2)
        self.assertEqual(stats.active_masks, 0)
        self.assertTrue(
            all(
                mask_stats.stop_reason == "global_request_limit"
                and not mask_stats.comparable
                for mask_stats in stats.mask_stats.values()
            )
        )

    def test_mask_cooldown_is_measured_after_the_previous_response(self) -> None:
        clock = [0.0]
        sleeps: list[float] = []

        def fake_sleep(delay: float) -> None:
            sleeps.append(delay)
            clock[0] += delay

        def fake_fetch(_client, _url, _mask, _timeout):
            return [collector.PhoneOffer("07000001111", "1")]

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                collector.collect(
                    masks=["1111"],
                    output_dir=Path(temporary),
                    rounds=2,
                    delay_min=1.0,
                    delay_max=1.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=30.0,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=fake_sleep,
                    random_delay=lambda _minimum, _maximum: 1.0,
                    monotonic=lambda: clock[0],
                )

        self.assertEqual(sleeps, [30.0])

    def test_priority_starts_only_after_five_fair_probes_per_mask(self) -> None:
        calls: list[str] = []
        per_mask_calls = {"1111": 0, "2222": 0}
        pools = {
            mask: {
                f"070{index:04d}{mask}"
                for index in range(10 if mask == "1111" else 100)
            }
            for mask in per_mask_calls
        }

        def fake_fetch(_client, _url, mask, _timeout):
            calls.append(mask)
            per_mask_calls[mask] += 1
            phone = sorted(pools[mask])[0]
            return [collector.PhoneOffer(phone, phone[3:])]

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                collector.collect(
                    masks=["1111", "2222"],
                    output_dir=Path(temporary),
                    rounds=100,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    request_limit=11,
                    mask_cooldown=0,
                    historical_pool_by_mask=pools,
                    coverage_pool_by_mask=pools,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        self.assertEqual(calls[: 2 * collector.MIN_PROBES], ["1111", "2222"] * 5)
        # Equal sampling progress makes the mask with the larger relative
        # coverage deficit the first post-bootstrap choice.
        self.assertEqual(calls[-1], "2222")

    def test_priority_prefers_observed_new_phone_yield(self) -> None:
        calls: list[str] = []
        per_mask_calls = {"1111": 0, "2222": 0}
        pools = {
            mask: {f"070{index:04d}{mask}" for index in range(100)}
            for mask in per_mask_calls
        }

        def offers(mask: str, indexes: list[int]) -> list[collector.PhoneOffer]:
            return [
                collector.PhoneOffer(
                    f"070{index:04d}{mask}",
                    f"{mask}{index:04d}",
                )
                for index in indexes
            ]

        def fake_fetch(_client, _url, mask, _timeout):
            calls.append(mask)
            index = per_mask_calls[mask]
            per_mask_calls[mask] += 1
            if mask == "1111":
                return offers(mask, [0, 100 + index])
            return offers(mask, [0, 1])

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                collector.collect(
                    masks=["1111", "2222"],
                    output_dir=Path(temporary),
                    rounds=100,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    request_limit=11,
                    mask_cooldown=0,
                    historical_pool_by_mask=pools,
                    coverage_pool_by_mask=pools,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        self.assertEqual(calls[:10], ["1111", "2222"] * 5)
        self.assertEqual(calls[-1], "1111")

    def test_priority_starvation_guard_and_round_cap_are_deterministic(self) -> None:
        calls: list[str] = []
        per_mask_calls = {"1111": 0, "2222": 0}
        pools = {
            mask: {f"070{index:04d}{mask}" for index in range(100)}
            for mask in per_mask_calls
        }

        def fake_fetch(_client, _url, mask, _timeout):
            calls.append(mask)
            index = per_mask_calls[mask]
            per_mask_calls[mask] += 1
            indexes = [0, 100 + index] if mask == "1111" else [0, 1]
            return [
                collector.PhoneOffer(
                    f"070{phone_index:04d}{mask}",
                    f"{mask}{phone_index:04d}",
                )
                for phone_index in indexes
            ]

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["1111", "2222"],
                    output_dir=Path(temporary),
                    rounds=100,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    request_limit=14,
                    mask_cooldown=0,
                    historical_pool_by_mask=pools,
                    coverage_pool_by_mask=pools,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        self.assertEqual(calls[10:], ["1111", "1111", "1111", "2222"])
        self.assertTrue(
            all(
                item.successful_responses <= item.round_limit
                for item in stats.mask_stats.values()
            )
        )
        self.assertEqual(
            stats.rounds,
            max(item.successful_responses for item in stats.mask_stats.values()),
        )

    def test_priority_uses_a_cooldown_ready_mask_before_a_higher_score(self) -> None:
        calls: list[str] = []
        clock = [0.0]
        per_mask_calls = {"1111": 0, "2222": 0, "3333": 0}
        pools = {
            mask: {f"070{index:04d}{mask}" for index in range(100)}
            for mask in per_mask_calls
        }

        def fake_sleep(delay: float) -> None:
            clock[0] += delay

        def fake_fetch(_client, _url, mask, _timeout):
            calls.append(mask)
            index = per_mask_calls[mask]
            per_mask_calls[mask] += 1
            indexes = [0, 100 + index] if mask == "3333" else [0]
            return [
                collector.PhoneOffer(
                    f"070{phone_index:04d}{mask}",
                    f"{mask}{phone_index:04d}",
                )
                for phone_index in indexes
            ]

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                collector.collect(
                    masks=["1111", "2222", "3333"],
                    output_dir=Path(temporary),
                    rounds=100,
                    delay_min=1.0,
                    delay_max=1.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    request_limit=16,
                    mask_cooldown=2.0,
                    historical_pool_by_mask=pools,
                    coverage_pool_by_mask=pools,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=fake_sleep,
                    random_delay=lambda _minimum, _maximum: 1.0,
                    monotonic=lambda: clock[0],
                )

        self.assertEqual(calls[:15], ["1111", "2222", "3333"] * 5)
        self.assertEqual(calls[-1], "1111")

    def test_warm_mask_stops_after_sampling_plateau(self) -> None:
        calls = 0
        repeated = [
            collector.PhoneOffer("07000001111", "1"),
            collector.PhoneOffer("07000011111", "2"),
        ]
        pool = {f"070{index:04d}1111" for index in range(100)}

        def fake_fetch(_client, _url, _mask, _timeout):
            nonlocal calls
            calls += 1
            return repeated

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["1111"],
                    output_dir=Path(temporary),
                    rounds=100,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    historical_pool_by_mask={"1111": pool},
                    coverage_pool_by_mask={"1111": pool},
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        mask_stats = stats.mask_stats["1111"]
        self.assertEqual(calls, 23)
        self.assertEqual(mask_stats.successful_responses, 23)
        self.assertEqual(mask_stats.stop_reason, "sampling_saturated")
        self.assertFalse(mask_stats.comparable)
        self.assertEqual(stats.deactivated_sampling_saturated, 1)
        self.assertEqual(stats.as_dict()["deactivatedSamplingSaturated"], 1)

    def test_deep_scan_continues_past_warm_sampling_plateau(self) -> None:
        calls = 0
        repeated = [
            collector.PhoneOffer("07000001111", "1"),
            collector.PhoneOffer("07000011111", "2"),
        ]
        pool = {f"070{index:04d}1111" for index in range(100)}

        def fake_fetch(_client, _url, _mask, _timeout):
            nonlocal calls
            calls += 1
            return repeated

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["1111"],
                    output_dir=Path(temporary),
                    rounds=25,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    deep_scan=True,
                    mask_cooldown=0,
                    historical_pool_by_mask={"1111": pool},
                    coverage_pool_by_mask={"1111": pool},
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        self.assertEqual(calls, 25)
        self.assertEqual(stats.mask_stats["1111"].stop_reason, "round_limit")
        self.assertEqual(stats.deactivated_sampling_saturated, 0)
        self.assertTrue(stats.deep_scan)
        self.assertTrue(stats.as_dict()["deepScan"])

    def test_warm_sampling_plateau_resets_on_first_seen_phone(self) -> None:
        calls = 0
        pool = {f"070{index:04d}1111" for index in range(100)}

        def make_offers(indexes: list[int]) -> list[collector.PhoneOffer]:
            return [
                collector.PhoneOffer(f"070{index:04d}1111", str(index + 1))
                for index in indexes
            ]

        def fake_fetch(_client, _url, _mask, _timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                return make_offers([0, 1])
            if calls == 17:
                return make_offers([0, 2])
            return make_offers([0, 1])

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["1111"],
                    output_dir=Path(temporary),
                    rounds=100,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    historical_pool_by_mask={"1111": pool},
                    coverage_pool_by_mask={"1111": pool},
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        self.assertEqual(calls, 39)
        self.assertEqual(
            stats.mask_stats["1111"].stop_reason,
            "sampling_saturated",
        )

    def test_sampling_saturation_history_is_validated_and_non_comparable(self) -> None:
        mask_stats = collector.MaskStats(
            historical_distinct_at_start=100,
            coverage_pool_at_start=100,
            successful_responses=23,
            http_requests=23,
            response_phone_samples=44,
            observed_phones={"phone"},
            observed_known_phones={"phone"},
            estimated_request_budget=100,
            request_cap=100,
            round_limit=100,
            stop_reason="sampling_saturated",
        )
        stats = collector.CollectionStats(mask_stats={"1111": mask_stats})

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scan_history.csv"
            collector.write_scan_history(
                path,
                [],
                ["1111"],
                stats,
                "2026-09-01T00:00:00Z",
            )
            loaded = collector.load_scan_history(path)
            self.assertEqual(len(loaded), 1)
            indexes = {
                field: index
                for index, field in enumerate(collector.MASK_SUMMARY_FIELDS)
            }
            self.assertEqual(
                loaded[0].values[indexes["stopReason"]],
                "sampling_saturated",
            )
            self.assertEqual(loaded[0].values[indexes["comparable"]], "false")

            mask_stats.response_phone_samples = 43
            collector.write_scan_history(
                path,
                [],
                ["1111"],
                stats,
                "2026-09-01T00:00:00Z",
            )
            with self.assertRaisesRegex(ValueError, "sampling saturation"):
                collector.load_scan_history(path)

    def test_cold_mask_saturates_only_after_fifteen_duplicate_samples(self) -> None:
        offer = collector.PhoneOffer("07000006666", "6666000000000000001")
        calls = 0

        def fake_fetch(_client, _url, _mask, _timeout):
            nonlocal calls
            calls += 1
            return [offer]

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["6666"],
                    output_dir=Path(temporary),
                    rounds=30,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=lambda _delay: None,
                    random_delay=lambda _minimum, _maximum: 1.1,
                )

        self.assertEqual(calls, 16)
        self.assertEqual(stats.added_phones, 1)
        self.assertEqual(stats.deactivated_empty_probe, 0)
        self.assertEqual(stats.deactivated_cold_saturated, 1)
        self.assertFalse(stats.mask_stats["6666"].comparable)
        self.assertEqual(
            stats.mask_stats["6666"].stop_reason,
            "cold_start_saturated",
        )

    def test_offer_store_appends_without_rereading_and_exports_observed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_dir = root / "csv"
            csv_dir.mkdir()
            (csv_dir / "1111.csv").write_text(
                "phoneNumber,id\n07000001111,1001\n",
                encoding="utf-8",
            )
            with collector.OfferStore(csv_dir, ["1111"]) as store:
                with mock.patch.object(
                    collector,
                    "read_existing",
                    side_effect=AssertionError(
                        "CSV must not be reread while appending"
                    ),
                ):
                    result = store.append(
                        "1111",
                        [
                            collector.PhoneOffer("07000001111", "1001"),
                            collector.PhoneOffer("07000001111", "1002"),
                            collector.PhoneOffer("07000011111", "1003"),
                        ],
                    )
                output = root / "run" / "all_numbers.csv"
                exported = store.export_observed(output)

            self.assertEqual(result, collector.AppendResult(new_rows=2, new_phones=1))
            self.assertEqual(exported, 2)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "phoneNumber,id,sourceMask\n"
                "07000001111,1001,1111\n"
                "07000011111,1003,1111\n",
            )
            self.assertIn(
                "07000001111,1002\n",
                (csv_dir / "1111.csv").read_text(encoding="utf-8"),
            )

    def test_observed_export_excludes_unseen_stored_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_dir = root / "csv"
            csv_dir.mkdir()
            (csv_dir / "1111.csv").write_text(
                "phoneNumber,id\n07000001111,1001\n",
                encoding="utf-8",
            )
            with collector.OfferStore(csv_dir, ["1111"]) as store:
                observed = root / "observed.csv"

                self.assertEqual(store.export_observed(observed), 0)
                self.assertEqual(
                    observed.read_text(encoding="utf-8"),
                    "phoneNumber,id,sourceMask\n",
                )

                store.append(
                    "1111",
                    [collector.PhoneOffer("07000001111", "1001")],
                )
                self.assertEqual(store.export_observed(observed), 1)
                self.assertIn(
                    "07000001111,1001,1111",
                    observed.read_text(encoding="utf-8"),
                )

    def test_offer_store_lock_is_exclusive_and_released_on_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "csv"
            first = collector.OfferStore(output_dir, ["1111"])
            try:
                with self.assertRaisesRegex(ValueError, "already locked"):
                    collector.OfferStore(output_dir, ["1111"])
            finally:
                first.close()

            with collector.OfferStore(output_dir, ["1111"]) as second:
                second.append(
                    "1111",
                    [collector.PhoneOffer("07000001111", "1001")],
                )
            self.assertEqual(
                sorted(path.name for path in output_dir.iterdir()),
                ["1111.csv"],
            )

    def test_offer_store_rewrite_is_atomic_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "csv"
            with collector.OfferStore(output_dir, ["1111"]) as store:
                path = output_dir / "1111.csv"
                original = path.read_bytes()
                with mock.patch.object(
                    collector.os,
                    "replace",
                    side_effect=OSError("simulated replace failure"),
                ):
                    with self.assertRaisesRegex(
                        ValueError, "cannot transactionally write"
                    ):
                        store.append(
                            "1111",
                            [collector.PhoneOffer("07000001111", "1001")],
                        )

                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(store.phone_count, 0)
                self.assertEqual(
                    list(output_dir.glob(".1111.csv.*.tmp")),
                    [],
                )

                result = store.append(
                    "1111",
                    [collector.PhoneOffer("07000001111", "1001")],
                )
                self.assertEqual(result, collector.AppendResult(1, 1))

    def test_run_summary_translates_parent_directory_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            blocker = Path(temporary) / "not-a-directory"
            blocker.write_text("blocked", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "cannot write run summary"):
                collector.write_summary(
                    blocker / "summary.json",
                    status="fatal",
                    exit_code=1,
                    mask_count=0,
                    stats=None,
                    store=None,
                )

    def test_cross_run_diff_distinguishes_random_non_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.csv"
            current = root / "current.csv"
            baseline.write_text(
                "phoneNumber,id,sourceMask\n"
                "07000001111,1001,1111\n"
                "07000022222,2001,2222\n"
                "07000044444,4001,4444\n",
                encoding="utf-8",
            )
            current.write_text(
                "phoneNumber,id,sourceMask\n"
                "07000001111,1002,1111\n"
                "07000033333,3001,3333\n",
                encoding="utf-8",
            )
            diff = root / "diff.csv"
            summary = root / "diff_summary.json"
            counts = collector.write_run_diff(
                current,
                collector.read_deduplicated(baseline),
                ["1111", "3333", "4444"],
                diff,
                summary,
            )

            self.assertEqual(counts["added"], 1)
            self.assertEqual(counts["notObserved"], 1)
            self.assertEqual(counts["notScanned"], 1)
            self.assertEqual(counts["idChanged"], 1)
            self.assertEqual(
                set(counts),
                {
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
                },
            )
            self.assertRegex(str(counts["generatedAt"]), r"Z$")
            self.assertEqual(
                counts["currentPhoneCount"],
                counts["added"] + counts["idChanged"] + counts["unchanged"],
            )
            self.assertEqual(
                counts["previousPhoneCount"],
                counts["notObserved"]
                + counts["notScanned"]
                + counts["idChanged"]
                + counts["unchanged"],
            )
            contents = diff.read_text(encoding="utf-8")
            self.assertIn("added,07000033333,,3001,3333", contents)
            self.assertIn("not_scanned,07000022222,2001,,2222", contents)
            self.assertIn("not_observed,07000044444,4001,,4444", contents)
            self.assertIn("id_changed,07000001111,1001,1002,1111", contents)

    def test_deduplicated_csv_rejects_offer_id_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "baseline.csv"
            path.write_text(
                "phoneNumber,id,sourceMask\n"
                "07000001111,1001,1111\n"
                "07000002222,1001,2222\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "id belongs to another phone"):
                collector.read_deduplicated(path)

    def test_diff_without_baseline_accounts_for_every_current_phone_as_added(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "current.csv"
            current.write_text(
                "phoneNumber,id,sourceMask\n"
                "07000001111,1001,1111\n",
                encoding="utf-8",
            )
            counts = collector.write_run_diff(
                current,
                None,
                ["1111"],
                root / "diff.csv",
                root / "summary.json",
            )

            self.assertFalse(counts["comparisonAvailable"])
            self.assertEqual(counts["currentPhoneCount"], 1)
            self.assertEqual(counts["added"], 1)
            for key in (
                "previousPhoneCount",
                "notObserved",
                "notScanned",
                "idChanged",
                "unchanged",
            ):
                self.assertEqual(counts[key], 0)

    def test_rate_limit_honors_retry_after(self) -> None:
        attempts = 0
        sleeps: list[float] = []
        headers = Message()
        headers["Retry-After"] = "7"

        def fake_fetch(_client, _url, _mask, _timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise HTTPError(
                    "https://example.test/api",
                    429,
                    "Too Many Requests",
                    headers,
                    None,
                )
            return [collector.PhoneOffer("07000007777", "7777000000000000001")]

        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                stats = collector.collect(
                    masks=["7777"],
                    output_dir=Path(temporary),
                    rounds=1,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=sleeps.append,
                    random_delay=lambda minimum, _maximum: minimum,
                )

        self.assertEqual(stats.retries, 1)
        self.assertEqual(attempts, 2)
        self.assertEqual(sleeps, [7.0])

    def test_final_retry_after_is_logged_closed_and_never_delays_next_mask(self) -> None:
        calls: list[str] = []
        sleeps: list[float] = []
        errors: list[HTTPError] = []
        events: list[tuple[str, dict[str, object]]] = []

        class TrackedHTTPError(HTTPError):
            was_closed = False

            def close(self) -> None:
                self.was_closed = True
                super().close()

        def fake_fetch(_client, _url, mask, _timeout):
            calls.append(mask)
            headers = Message()
            headers["Retry-After"] = "7"
            error = TrackedHTTPError(
                "https://example.test/api",
                503,
                "Service Unavailable",
                headers,
                None,
            )
            errors.append(error)
            raise error

        def capture_event(_level, event, **fields):
            events.append((event, fields))

        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(collector, "log_event", side_effect=capture_event),
                self.assertRaises(collector.FatalCollectionError),
            ):
                collector.collect(
                    masks=["7777", "8888"],
                    output_dir=Path(temporary),
                    rounds=1,
                    delay_min=1.1,
                    delay_max=2.0,
                    timeout=1.0,
                    api_url="https://example.test/api",
                    mask_cooldown=0,
                    opener=object(),
                    fetch=fake_fetch,
                    sleep=sleeps.append,
                    random_delay=lambda minimum, _maximum: minimum,
                )

        self.assertEqual(calls, ["7777"] * 4)
        self.assertEqual(sleeps, [7.0, 7.0, 7.0])
        self.assertTrue(all(error.was_closed for error in errors))
        exhausted = [fields for event, fields in events if event == "request_exhausted"]
        self.assertEqual(len(exhausted), 1)
        self.assertEqual(exhausted[0]["retryAfterSeconds"], 7.0)

    def test_service_unavailable_honors_retry_after(self) -> None:
        headers = Message()
        headers["Retry-After"] = "7"
        error = HTTPError(
            "https://example.test/api",
            503,
            "Service Unavailable",
            headers,
            None,
        )

        try:
            delay = collector.backoff_seconds(
                error,
                attempt=1,
                random_delay=lambda _minimum, _maximum: 999.0,
            )
        finally:
            error.close()

        self.assertEqual(delay, 7.0)

    def test_permanent_http_responses_stop_without_retrying(self) -> None:
        for status in (400, 403, 422):
            with self.subTest(status=status):
                attempts = 0

                def fake_fetch(_client, _url, _mask, _timeout):
                    nonlocal attempts
                    attempts += 1
                    raise HTTPError(
                        "https://example.test/api",
                        status,
                        "Permanent error",
                        Message(),
                        None,
                    )

                with tempfile.TemporaryDirectory() as temporary:
                    with self.assertRaisesRegex(
                        collector.FatalCollectionError,
                        "non-retryable endpoint response",
                    ):
                        collector.collect(
                            masks=["8888"],
                            output_dir=Path(temporary),
                            rounds=10,
                            delay_min=1.1,
                            delay_max=2.0,
                            timeout=1.0,
                            api_url="https://example.test/api",
                            mask_cooldown=0,
                            opener=object(),
                            fetch=fake_fetch,
                            sleep=lambda _delay: None,
                        )

                self.assertEqual(attempts, 1)

    def test_redirects_are_not_followed(self) -> None:
        handler = collector.NoRedirectHandler()
        self.assertIsNone(handler.redirect_request())

    def test_malformed_baseline_fails_before_store_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.csv"
            baseline.write_text("wrong,header\nvalue,value\n", encoding="utf-8")
            stdout = io.StringIO()
            with (
                mock.patch.object(collector, "collect") as collect_mock,
                mock.patch.object(collector, "OfferStore") as store_mock,
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = collector.main(
                    [
                        "1111",
                        "--api-url",
                        "https://example.test/api",
                        "--baseline-csv",
                        str(baseline),
                        "--run-dir",
                        str(root / "run"),
                        "--output-dir",
                        str(root / "csv"),
                    ]
                )

            self.assertEqual(exit_code, 1)
            collect_mock.assert_not_called()
            store_mock.assert_not_called()
            self.assertIn("expected CSV header", stdout.getvalue())
            self.assertFalse((root / "csv").exists())
            self.assertIn(
                '"status": "fatal"',
                (root / "run" / "summary.json").read_text(encoding="utf-8"),
            )

    def test_validated_baseline_is_not_reread_after_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.csv"
            baseline.write_text(
                "phoneNumber,id,sourceMask\n"
                "07000002222,2001,2222\n",
                encoding="utf-8",
            )

            def fake_collect(*, stats, **_kwargs):
                baseline.write_text("corrupted after validation\n", encoding="utf-8")
                stats.rounds = 1
                stats.active_masks = 1
                return stats

            with (
                mock.patch.object(collector, "collect", side_effect=fake_collect),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = collector.main(
                    [
                        "1111",
                        "--api-url",
                        "https://example.test/api",
                        "--baseline-csv",
                        str(baseline),
                        "--run-dir",
                        str(root / "run"),
                        "--output-dir",
                        str(root / "csv"),
                    ]
                )

            self.assertEqual(exit_code, 0)
            summary = json.loads(
                (root / "run" / "diff_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["previousPhoneCount"], 1)
            self.assertEqual(summary["notScanned"], 1)

    def test_baseline_cannot_alias_output_or_existing_run_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "csv"
            output_dir.mkdir()
            output_csv = output_dir / "1111.csv"
            output_csv.write_text("phoneNumber,id\n", encoding="utf-8")
            output_alias = root / "output-alias.csv"
            output_alias.symlink_to(output_csv)
            with self.assertRaisesRegex(ValueError, "inside --output-dir"):
                collector.validate_runtime_paths(
                    output_dir.resolve(),
                    (root / "run").resolve(),
                    output_alias.resolve(),
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.csv"
            baseline.write_text(
                "phoneNumber,id,sourceMask\n",
                encoding="utf-8",
            )
            run_dir = root / "run"
            artifact = run_dir / "arbitrary" / "nested-output.dat"
            artifact.parent.mkdir(parents=True)
            artifact.symlink_to(baseline)

            with self.assertRaisesRegex(ValueError, "alias run artifact"):
                collector.validate_runtime_paths(
                    (root / "csv").resolve(),
                    run_dir.resolve(),
                    baseline.resolve(),
                )

    def test_fatal_configuration_still_writes_logs_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.dict("os.environ", {}, clear=True),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = collector.main(
                    [
                        "1111",
                        "--run-dir",
                        str(run_dir),
                        "--output-dir",
                        str(Path(temporary) / "csv"),
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("fatal:", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")
            self.assertTrue((run_dir / "logs" / "collector.log").is_file())
            self.assertIn(
                "run_fatal",
                (run_dir / "logs" / "errors.log").read_text(encoding="utf-8"),
            )
            summary = (run_dir / "summary.json").read_text(encoding="utf-8")
            self.assertIn('"status": "fatal"', summary)
            self.assertNotIn("example.test", summary)

    def test_successful_main_writes_artifacts_with_quiet_console(self) -> None:
        def fake_collect(*, store, stats, **_kwargs):
            store.append(
                "1111",
                [collector.PhoneOffer("07000001111", "1111000000000000001")],
            )
            stats.rounds = 1
            stats.requests = 1
            stats.responses = 1
            stats.received = 1
            stats.added_rows = 1
            stats.added_phones = 1
            stats.active_masks = 1
            mask_stats = stats.mask_stats["1111"]
            mask_stats.successful_responses = 1
            mask_stats.http_requests = 1
            mask_stats.response_phone_samples = 1
            mask_stats.observed_phones.add("07000001111")
            mask_stats.observed_new_phones.add("07000001111")
            mask_stats.estimated_request_budget = 1
            mask_stats.request_cap = 1
            mask_stats.round_limit = 1
            return stats

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(collector, "collect", side_effect=fake_collect),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = collector.main(
                    [
                        "1111",
                        "--api-url",
                        "https://example.test/api",
                        "--run-dir",
                        str(run_dir),
                        "--output-dir",
                        str(root / "csv"),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(len(stdout.getvalue().splitlines()), 2)
            self.assertIn("start: masks 1", stdout.getvalue())
            self.assertIn("finished: rounds 1", stdout.getvalue())
            self.assertIn(
                "07000001111,1111000000000000001,1111",
                (run_dir / "all_numbers.csv").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (run_dir / "coverage_pool.csv").read_text(encoding="utf-8"),
                "phoneNumber,sourceMask\n",
            )
            self.assertTrue((run_dir / "diff.csv").is_file())
            self.assertEqual(
                (run_dir / "mask_summary.csv").read_text(encoding="utf-8"),
                ",".join(collector.MASK_SUMMARY_FIELDS)
                + "\n"
                + "1111,0,0,1,1,0,1,0,1,0,1,9000,9900,,1,1,1,"
                "round_limit,false\n",
            )
            scan_history = (run_dir / "scan_history.csv").read_text(
                encoding="utf-8"
            )
            self.assertTrue(scan_history.startswith(
                ",".join(collector.SCAN_HISTORY_FIELDS) + "\n"
            ))
            self.assertIn(
                '"comparisonAvailable": false',
                (run_dir / "diff_summary.json").read_text(encoding="utf-8"),
            )
            self.assertIn(
                '"status": "success"',
                (run_dir / "summary.json").read_text(encoding="utf-8"),
            )
            self.assertIn(
                '"observedPhoneCount": 1',
                (run_dir / "summary.json").read_text(encoding="utf-8"),
            )
            summary = json.loads(
                (run_dir / "summary.json").read_text(encoding="utf-8")
            )
            self.assertRegex(
                summary["finishedAt"],
                r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$",
            )
            diagnostics = (run_dir / "logs" / "errors.log").read_text(
                encoding="utf-8"
            )
            self.assertEqual(diagnostics, "")

    def test_success_is_not_printed_when_artifact_finalization_fails(self) -> None:
        def fake_collect(*, stats, **_kwargs):
            stats.rounds = 1
            stats.requests = 1
            stats.responses = 1
            stats.active_masks = 1
            return stats

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(collector, "collect", side_effect=fake_collect),
                mock.patch.object(
                    collector,
                    "write_run_diff",
                    side_effect=ValueError("diff finalization failed"),
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = collector.main(
                    [
                        "1111",
                        "--api-url",
                        "https://example.test/api",
                        "--run-dir",
                        str(root / "run"),
                        "--output-dir",
                        str(root / "csv"),
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertNotIn("finished:", stdout.getvalue())
            self.assertIn("fatal: diff finalization failed", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn(
                '"status": "fatal"',
                (root / "run" / "summary.json").read_text(encoding="utf-8"),
            )

    def test_unexpected_failure_is_logged_with_sanitized_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    collector,
                    "collect",
                    side_effect=RuntimeError(
                        "unexpected at https://secret.example/private"
                    ),
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = collector.main(
                    [
                        "1111",
                        "--api-url",
                        "https://example.test/api",
                        "--run-dir",
                        str(root / "run"),
                        "--output-dir",
                        str(root / "csv"),
                    ]
                )

            log = (root / "run" / "logs" / "collector.log").read_text(
                encoding="utf-8"
            )
            self.assertEqual(exit_code, 1)
            self.assertIn("fatal: unexpected RuntimeError", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("unexpected_fatal", log)
            self.assertIn("[redacted-url]", log)
            self.assertIn('"traceback"', log)
            self.assertNotIn("secret.example", log)


if __name__ == "__main__":
    unittest.main()
