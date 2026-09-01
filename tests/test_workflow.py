from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    source: str
    run: str | None
    uses: str | None


@dataclass(frozen=True)
class WorkflowJob:
    job_id: str
    source: str
    steps: tuple[WorkflowStep, ...]


def parse_step_lines(lines: list[str]) -> list[WorkflowStep]:
    starts = [
        index for index, line in enumerate(lines) if line.startswith("      - name: ")
    ]
    steps: list[WorkflowStep] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block_lines = lines[start:end]
        name = block_lines[0].split(": ", 1)[1]
        uses = next(
            (
                line.strip().split(": ", 1)[1].split(" #", 1)[0]
                for line in block_lines
                if line.startswith("        uses: ")
            ),
            None,
        )
        run: str | None = None
        for index, line in enumerate(block_lines):
            if line == "        run: |":
                script_lines: list[str] = []
                for script_line in block_lines[index + 1 :]:
                    if script_line and not script_line.startswith("          "):
                        break
                    script_lines.append(
                        script_line[10:] if script_line.startswith("          ") else ""
                    )
                run = "\n".join(script_lines) + "\n"
                break
            if line.startswith("        run: "):
                run = line.split(": ", 1)[1] + "\n"
                break
        steps.append(WorkflowStep(name, "\n".join(block_lines), run, uses))
    if not steps:
        raise AssertionError("no workflow steps parsed")
    return steps


def parse_workflow_jobs(path: Path) -> dict[str, WorkflowJob]:
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        jobs_line = lines.index("jobs:")
    except ValueError as error:
        raise AssertionError(f"no jobs block parsed from {path}") from error
    starts = [
        index
        for index, line in enumerate(lines[jobs_line + 1 :], jobs_line + 1)
        if line.startswith("  ")
        and not line.startswith("    ")
        and line.endswith(":")
    ]
    jobs: dict[str, WorkflowJob] = {}
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block_lines = lines[start:end]
        job_id = block_lines[0][2:-1]
        jobs[job_id] = WorkflowJob(
            job_id,
            "\n".join(block_lines),
            tuple(parse_step_lines(block_lines)),
        )
    if not jobs:
        raise AssertionError(f"no jobs block parsed from {path}")
    return jobs


def parse_workflow_steps(path: Path) -> list[WorkflowStep]:
    return [
        step for job in parse_workflow_jobs(path).values() for step in job.steps
    ]


def steps_by_name(path: Path) -> dict[str, WorkflowStep]:
    steps = parse_workflow_steps(path)
    names = [step.name for step in steps]
    if len(names) != len(set(names)):
        raise AssertionError(f"workflow step names must be unique: {path}")
    return {step.name: step for step in steps}


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")
        self.steps = steps_by_name(WORKFLOW)
        self.jobs = parse_workflow_jobs(WORKFLOW)

    @staticmethod
    def run_script(
        script: str,
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        process_environment = os.environ.copy()
        process_environment.update(environment)
        return subprocess.run(
            ["bash", "-c", script],
            cwd=cwd,
            env=process_environment,
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def write_csv(path: Path, fields: list[str], rows: list[list[str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(fields)
            writer.writerows(rows)

    def test_dispatch_metadata_sorts_and_isolates_mask_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            work = Path(temporary_directory)
            output = work / "outputs"
            result = self.run_script(
                self.steps["Initialize run metadata and diagnostics"].run or "",
                cwd=work,
                environment={
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_OUTPUT": str(output),
                    "GITHUB_RUN_ID": "42",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "INPUT_DEEP_SCAN": "true",
                    "INPUT_ROUNDS": "17",
                    "INPUT_REQUEST_LIMIT": "7000",
                    "MASK_OVERRIDE": "3322, 1111,2222",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            metadata = dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
            selected = "1111\n2222\n3322\n"
            digest = hashlib.sha256(selected.encode()).hexdigest()
            self.assertEqual(metadata["rounds"], "17")
            self.assertEqual(metadata["request-limit"], "7000")
            self.assertEqual(metadata["deep-scan"], "true")
            self.assertEqual(metadata["run-artifact"], "raku-mobi-bangou-run-42-1")
            self.assertEqual(metadata["specialized"], "true")
            self.assertEqual(metadata["cache_scope"], f"specialized-{digest}")
            self.assertEqual(
                (work / "run" / "selected_masks.txt").read_text(encoding="utf-8"),
                selected,
            )

    def test_dispatch_metadata_rejects_duplicate_masks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            work = Path(temporary_directory)
            result = self.run_script(
                self.steps["Initialize run metadata and diagnostics"].run or "",
                cwd=work,
                environment={
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_OUTPUT": str(work / "outputs"),
                    "GITHUB_RUN_ID": "42",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "INPUT_ROUNDS": "100",
                    "INPUT_REQUEST_LIMIT": "5000",
                    "MASK_OVERRIDE": "1111,1111",
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Duplicate mask override: 1111", result.stderr)

    def test_dispatch_metadata_rejects_invalid_request_limit(self) -> None:
        metadata = self.steps["Initialize run metadata and diagnostics"].run or ""
        with tempfile.TemporaryDirectory() as temporary_directory:
            work = Path(temporary_directory)
            for request_limit in ("0", "9001", "1.5", "9223372036854775808"):
                result = self.run_script(
                    metadata,
                    cwd=work,
                    environment={
                        "GITHUB_EVENT_NAME": "workflow_dispatch",
                        "GITHUB_OUTPUT": str(work / "outputs"),
                        "GITHUB_RUN_ID": "42",
                        "GITHUB_RUN_ATTEMPT": "1",
                        "INPUT_ROUNDS": "300",
                        "INPUT_REQUEST_LIMIT": request_limit,
                        "MASK_OVERRIDE": "",
                    },
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "Request limit must be an integer from 1 to 9000.",
                    result.stderr,
                )

    def test_request_limit_accepts_boundaries_and_schedule_uses_default(self) -> None:
        metadata = self.steps["Initialize run metadata and diagnostics"].run or ""
        with tempfile.TemporaryDirectory() as temporary_directory:
            work = Path(temporary_directory)
            for request_limit in ("1", "9000"):
                output = work / f"dispatch-{request_limit}"
                result = self.run_script(
                    metadata,
                    cwd=work,
                    environment={
                        "GITHUB_EVENT_NAME": "workflow_dispatch",
                        "GITHUB_OUTPUT": str(output),
                        "GITHUB_RUN_ID": "42",
                        "GITHUB_RUN_ATTEMPT": "1",
                        "INPUT_ROUNDS": "300",
                        "INPUT_REQUEST_LIMIT": request_limit,
                        "MASK_OVERRIDE": "",
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                values = dict(
                    line.split("=", 1)
                    for line in output.read_text(encoding="utf-8").splitlines()
                )
                self.assertEqual(
                    values["request-limit"], request_limit
                )

            schedule_output = work / "schedule"
            result = self.run_script(
                metadata,
                cwd=work,
                environment={
                    "GITHUB_EVENT_NAME": "schedule",
                    "GITHUB_OUTPUT": str(schedule_output),
                    "GITHUB_RUN_ID": "43",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "MASK_OVERRIDE": "",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(
                line.split("=", 1)
                for line in schedule_output.read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(
                values["request-limit"], "5000"
            )
            self.assertEqual(values["deep-scan"], "false")

    def test_dispatch_can_reuse_an_exact_collection_artifact(self) -> None:
        metadata = self.steps["Initialize run metadata and diagnostics"].run or ""
        with tempfile.TemporaryDirectory() as temporary_directory:
            work = Path(temporary_directory)
            output = work / "outputs"
            environment = {
                "COLLECTION_ARTIFACT": "raku-mobi-bangou-run-33414505042-1",
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_OUTPUT": str(output),
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_RUN_ID": "99",
                "INPUT_ROUNDS": "300",
                "INPUT_REQUEST_LIMIT": "5000",
                "MASK_OVERRIDE": "",
                "SKIP_COLLECTION": "true",
            }
            result = self.run_script(
                metadata, cwd=work, environment=environment
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(outputs["reuse"], "true")
            self.assertEqual(outputs["source-run-id"], "33414505042")
            self.assertEqual(
                outputs["source-artifact"],
                "raku-mobi-bangou-run-33414505042-1",
            )

            environment["COLLECTION_ARTIFACT"] = ""
            failed = self.run_script(
                metadata, cwd=work, environment=environment
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("Collection artifact must look like", failed.stderr)

    def test_reused_collection_is_validated_and_normal_collection_is_skipped(self) -> None:
        download = self.steps["Download reused collection artifact"]
        validate = self.steps["Validate reused collection artifact"]
        self.assertIn("steps.metadata.outputs.reuse == 'true'", download.source)
        self.assertIn("github-token: ${{ github.token }}", download.source)
        self.assertIn("run-id: ${{ steps.metadata.outputs.source-run-id }}", download.source)
        for normal_step in (
            "Restore collection state cache",
            "Normalize collection state",
            "Collect phone-number candidates",
            "Package CSV run",
            "Stage collection state cache",
        ):
            self.assertIn(
                "steps.metadata.outputs.reuse != 'true'",
                self.steps[normal_step].source,
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            work = Path(temporary_directory)
            reused = work / "reused"
            (reused / "logs").mkdir(parents=True)
            (work / "run").mkdir()
            shutil.copy2(ROOT / "masks.txt", work / "masks.txt")
            for relative_path in (
                "all_numbers.csv",
                "catalog.csv",
                "coverage_pool.csv",
                "diff.csv",
                "lifecycle.csv",
                "lifecycle_events.csv",
                "mask_days.csv",
                "scan_history.csv",
                "logs/collector.log",
                "logs/errors.log",
            ):
                path = reused / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("data\n", encoding="utf-8")
            self.write_csv(
                reused / "mask_summary.csv",
                ["mask", "roundLimit"],
                [["1111", "300"]],
            )
            (reused / "summary.json").write_text(
                json.dumps({"status": "success", "exitCode": 0}),
                encoding="utf-8",
            )
            (reused / "catalog_summary.json").write_text("{}\n", encoding="utf-8")
            (reused / "diff_summary.json").write_text("{}\n", encoding="utf-8")
            output = work / "outputs"
            result = self.run_script(
                validate.run or "",
                cwd=work,
                environment={
                    "GITHUB_OUTPUT": str(output),
                    "SPECIALIZED": "false",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "rounds=300\n")
            self.assertTrue((work / "run" / "catalog_summary.json").is_file())

    def test_invalid_cache_starts_from_an_empty_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            work = Path(temporary_directory)
            (work / "ci").mkdir()
            shutil.copy2(ROOT / "ci" / "state_cache.py", work / "ci" / "state_cache.py")
            (work / "state").mkdir()
            (work / "state" / "manifest.json").write_text("{}\n", encoding="utf-8")

            result = self.run_script(
                self.steps["Normalize collection state"].run or "",
                cwd=work,
                environment={
                    "CSV_DIR": "state/scopes/full/csv",
                    "MATCHED_KEY": "raku-mobi-bangou-state-v3-old",
                    "RESTORE_OUTCOME": "success",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((work / "state" / "manifest.json").exists())
            self.assertTrue((work / "state" / "scopes" / "full" / "csv").is_dir())
            self.assertIn("starting from an empty state", result.stderr)

    def test_jobs_are_separate_and_rerunnable(self) -> None:
        self.assertEqual(tuple(self.jobs), ("collect", "rank", "publish-release"))
        collect = self.jobs["collect"]
        rank = self.jobs["rank"]
        publish = self.jobs["publish-release"]

        default_branch_gate = (
            "    if: ${{ github.ref == format('refs/heads/{0}', "
            "github.event.repository.default_branch) }}"
        )
        self.assertIn(default_branch_gate, collect.source)
        self.assertNotIn(default_branch_gate, rank.source)
        self.assertNotIn(default_branch_gate, publish.source)
        self.assertIn("    needs: collect", rank.source)
        self.assertIn("    runs-on: [self-hosted, Linux, ARM64, codex]", rank.source)
        self.assertIn("    needs:\n      - collect\n      - rank", publish.source)
        self.assertNotIn("copilot-requests:", self.workflow)

        collect_outputs = collect.source.split("    steps:", 1)[0]
        self.assertIn("tag: ${{ steps.metadata.outputs.tag }}", collect_outputs)
        self.assertIn("run-artifact:", collect_outputs)
        self.assertIn("specialized:", collect_outputs)
        self.assertIn("rounds:", collect_outputs)
        for dead_output in ("csv_dir:", "run-kind:", "run-key:", "evidence-date:"):
            self.assertNotIn(dead_output, collect_outputs)

        self.assertIn("path: run", self.steps["Download collected run artifact"].source)
        self.assertIn("path: release/run", self.steps["Download run artifact"].source)
        self.assertIn("path: release/run", self.steps["Download TOP artifacts"].source)

    def test_cache_uses_only_current_validated_schema(self) -> None:
        normalize = self.steps["Normalize collection state"].run or ""
        stage = self.steps["Stage collection state cache"].run or ""
        upload = self.steps["Upload run artifact"].source

        self.assertIn("raku-mobi-bangou-state-v3-", self.workflow)
        self.assertNotIn("raku-mobi-bangou-state-v2-", self.workflow)
        self.assertIn("state_cache.py validate", normalize)
        self.assertNotIn("state_cache.py migrate", normalize)
        self.assertIn("state_cache.py write-manifest", stage)
        self.assertIn("state_cache.py validate", stage)
        self.assertIn("path: run/", upload)
        self.assertNotIn("csv_dir", upload.lower())
        self.assertNotIn("/*.csv", upload)
        self.assertNotIn("Clear cache restore target", self.steps)
        self.assertNotIn("Run tests", self.steps)

    def test_codex_is_installed_once_and_run_quietly_in_one_retry_loop(self) -> None:
        rank = self.jobs["rank"]
        rank_uses = [step.uses for step in rank.steps if step.uses]
        self.assertFalse(
            any(use.startswith("actions/setup-python@") for use in rank_uses)
        )
        python_check = self.steps["Verify ranking Python"].run or ""
        self.assertIn("python3 -c", python_check)
        self.assertNotIn("python3.13", python_check)
        for step in rank.steps:
            if step.run and "ci/top_tables.py" in step.run:
                self.assertIn("python3 ci/top_tables.py", step.run)
                self.assertNotIn("python3.13", step.run)

        action_steps = [
            step
            for step in rank.steps
            if step.uses and step.uses.startswith("openai/codex-action@")
        ]
        self.assertEqual(len(action_steps), 1)
        installer = action_steps[0]
        self.assertEqual(installer.name, "Install pinned Codex with official Action")
        self.assertEqual(
            installer.uses,
            "openai/codex-action@86365089eb2b84e0a8fb0717b304f8bdcb13b20e",
        )
        self.assertIn('codex-version: "0.151.0"', installer.source)
        self.assertNotIn("prompt-file:", installer.source)

        ranking = self.steps["Select and validate rankings with Codex"].run or ""
        self.assertIn("for attempt in 1 2 3", ranking)
        self.assertIn("timeout --signal=TERM --kill-after=30s 15m", ranking)
        self.assertIn('"${codex_bin}" exec', ranking)
        self.assertIn("env -i", ranking)
        self.assertIn("--ephemeral", ranking)
        self.assertIn("--sandbox read-only", ranking)
        self.assertIn("--output-schema", ranking)
        self.assertIn("--model gpt-5.6-sol", ranking)
        self.assertIn('model_reasoning_effort="xhigh"', ranking)
        self.assertIn('service_tier="fast"', ranking)
        self.assertIn("top_tables.py validate-selection", ranking)
        self.assertIn(">/dev/null 2>&1", ranking)
        self.assertNotIn("tee ", ranking)

        prompt = self.steps["Prepare Codex ranking prompt"].run or ""
        for block in (
            "<ranking-request-json>",
            "<top-candidates-jsonl>",
            "<goroawase-candidates-jsonl>",
            "<newly-found-candidates-jsonl>",
        ):
            self.assertIn(block, prompt)
        self.assertNotIn("codex-prompt-attempt", self.workflow)

    def test_codex_runtime_is_temporary_and_refresh_is_atomic(self) -> None:
        prepare = self.steps["Prepare temporary Codex runtime"].run or ""
        persist = self.steps["Persist refreshed Codex authentication"]
        cleanup = self.steps["Remove local ranking traces"].run or ""

        self.assertIn("mktemp -d", prepare)
        self.assertIn('persistence = "none"', prepare)
        self.assertIn('inherit = "none"', prepare)
        self.assertIn("NPM_CONFIG_PREFIX=", prepare)
        self.assertNotIn("proxy-marker", prepare)
        self.assertNotIn("codex_secure_wrapper", self.workflow)
        self.assertNotIn("secret_leak_guard", self.workflow)
        self.assertNotIn("stale Codex runtime", prepare)

        self.assertIn("if: ${{ always()", persist.source)
        self.assertIn("mktemp", persist.run or "")
        self.assertIn("mv -fT", persist.run or "")
        self.assertIn("CODEX_TEMP_HOME", persist.run or "")

        self.assertIn('if: ${{ always() }}', self.steps["Remove local ranking traces"].source)
        self.assertIn("CODEX_RUNTIME_ROOT", cleanup)
        self.assertNotIn('find "${RUNNER_TEMP}"', cleanup)

        with tempfile.TemporaryDirectory() as temporary_directory:
            work = Path(temporary_directory)
            runner_temp = work / "runner-temp"
            runtime = runner_temp / "raku-mobi-bangou-codex.ABC123"
            runtime.mkdir(parents=True)
            (runtime / "trace").write_text("temporary\n", encoding="utf-8")
            (work / "run").mkdir()
            (work / "run" / "prompt").write_text("temporary\n", encoding="utf-8")
            auth = work / ".codex" / "auth.json"
            auth.parent.mkdir()
            auth.write_text("persistent\n", encoding="utf-8")

            result = self.run_script(
                cleanup,
                cwd=work,
                environment={
                    "CODEX_RUNTIME_ROOT": str(runtime),
                    "GITHUB_WORKSPACE": str(work),
                    "RUNNER_TEMP": str(runner_temp),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(runtime.exists())
            self.assertFalse((work / "run").exists())
            self.assertEqual(auth.read_text(encoding="utf-8"), "persistent\n")

    def test_top_transfer_artifact_contains_only_publisher_inputs(self) -> None:
        upload = self.steps["Upload TOP artifacts"].source
        expected = (
            "            run/TOP.md\n"
            "            run/GOROAWASE.md\n"
            "            run/RELEASE_NOTES.md"
        )
        self.assertIn(expected, upload)
        for internal in (
            "top-candidates.json",
            "top-selection-request.json",
            "top-direct-candidates.jsonl",
            "top-goroawase-candidates.jsonl",
            "top-newly-found-candidates.jsonl",
            "top-selection.json",
            "codex-prompt.md",
        ):
            self.assertNotIn(internal, upload)
        self.assertIn("if-no-files-found: error", upload)

    def test_package_writes_only_the_csv_zip_under_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            work = Path(temporary_directory)
            csv_dir = work / "state" / "scopes" / "full" / "csv"
            self.write_csv(
                csv_dir / "1111.csv",
                ["phoneNumber", "id"],
                [["07080001111", "10"]],
            )
            (work / "run" / "logs").mkdir(parents=True)
            for relative_path in (
                "all_numbers.csv",
                "coverage_pool.csv",
                "diff.csv",
                "diff_summary.json",
                "summary.json",
                "mask_summary.csv",
                "scan_history.csv",
                "catalog.csv",
                "lifecycle.csv",
                "mask_days.csv",
                "lifecycle_events.csv",
                "catalog_summary.json",
            ):
                (work / "run" / relative_path).write_text("data\n", encoding="utf-8")
            (work / "run" / "logs" / "collector.log").write_text(
                "collector initialized\n", encoding="utf-8"
            )
            (work / "run" / "logs" / "errors.log").touch()
            archive = "raku-mobi-bangou_2026-08-24_19-00_csv.zip"

            result = self.run_script(
                self.steps["Package CSV run"].run or "",
                cwd=work,
                environment={
                    "ARCHIVE": archive,
                    "CATALOG_OUTCOME": "success",
                    "COLLECTION_OUTCOME": "success",
                    "CSV_DIR": "state/scopes/full/csv",
                    "GITHUB_WORKSPACE": str(work),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            archive_path = work / "run" / archive
            self.assertTrue(archive_path.is_file())
            entries = subprocess.run(
                ["zipinfo", "-1", str(archive_path)],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.splitlines()
            self.assertEqual(entries, ["csv/1111.csv"])
            self.assertNotIn("expired_numbers.csv", self.workflow)

    def test_schedule_and_release_shape(self) -> None:
        self.assertEqual(self.workflow.count("    - cron:"), 1)
        self.assertIn('cron: "10 10 * * *"', self.workflow)
        self.assertIn('timezone: "Asia/Tokyo"', self.workflow)
        collect = self.steps["Collect phone-number candidates"].run or ""
        self.assertIn('--request-limit "${REQUEST_LIMIT}"', collect)
        self.assertIn('deep_scan_args=(--deep-scan)', collect)
        self.assertIn('"${deep_scan_args[@]}"', collect)
        self.assertIn("REQUEST_LIMIT: ${{ steps.metadata.outputs.request-limit }}", self.workflow)

        publish = self.steps["Publish release"].run or ""
        expected_assets = """\
assets=(
  "release/run/TOP.md"
  "release/run/GOROAWASE.md"
  "release/run/all_numbers.csv"
)"""
        self.assertIn(expected_assets, publish)
        self.assertIn('--title "${TAG}"', publish)
        self.assertIn('--notes-file release/run/RELEASE_NOTES.md', publish)
        self.assertIn('gh release view "${TAG}"', publish)
        self.assertIn('gh release create "${TAG}"', publish)
        self.assertIn('gh release upload "${TAG}" "${assets[@]}"', publish)
        self.assertIn("--clobber", publish)
        self.assertIn('gh release edit "${TAG}"', publish)
        self.assertIn("gh release delete-asset", publish)
        self.assertNotIn("--paginate", publish)
        self.assertNotIn("resolve_tag_commit", publish)
        self.assertNotIn("inspect_remote_state", publish)
        self.assertNotIn("gh api", publish)

    def test_all_actions_are_pinned_to_full_commit_shas(self) -> None:
        self.assertIn('python-version: "3.10"', CI_WORKFLOW.read_text(encoding="utf-8"))
        for workflow_path in (WORKFLOW, CI_WORKFLOW):
            for step in parse_workflow_steps(workflow_path):
                if step.uses is not None:
                    self.assertRegex(
                        step.uses,
                        r"^[^@]+@[0-9a-f]{40}$",
                        f"unpinned action in {workflow_path.name}: {step.uses}",
                    )

    def test_codex_output_schema_allows_only_typed_candidate_ids(self) -> None:
        schema = json.loads(
            (ROOT / "ci" / "top-selection.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]), {"top", "goroawase", "newlyFound"}
        )
        expected = {
            "top": "^T[0-9]{3}$",
            "goroawase": "^G[0-9]{3}$",
            "newlyFound": "^N[0-9]{3}$",
        }
        for ranking, pattern in expected.items():
            reference = schema["properties"][ranking]["items"]["$ref"]
            definition = schema["$defs"][reference.rsplit("/", 1)[1]]
            self.assertFalse(definition["additionalProperties"])
            self.assertEqual(definition["properties"]["candidateId"]["pattern"], pattern)


if __name__ == "__main__":
    unittest.main()
