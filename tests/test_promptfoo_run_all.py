import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
RUN_ALL = PROJECT_DIR / "promptfoo" / "run_all.sh"


class PromptfooRunAllTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.log_path = root / "calls.log"
        self.fake_promptfoo = root / "fake_promptfoo.py"
        self.fake_promptfoo.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                from pathlib import Path
                import sys

                args = sys.argv[1:]
                config = args[args.index('-c') + 1]
                output = Path(args[args.index('-o') + 1])
                with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as log:
                    log.write(config + '\\n')

                scenario = os.environ.get('FAKE_SCENARIO', 'green')
                if config.endswith('parse.yaml'):
                    if scenario == 'parse_fail':
                        raise SystemExit(7)
                    results = [{'success': True} for _ in range(18)]
                    if scenario == 'parse_malformed':
                        output.write_text('{', encoding='utf-8')
                    elif scenario == 'parse_missing_results':
                        output.write_text('{}', encoding='utf-8')
                    elif scenario == 'parse_truncated':
                        output.write_text(
                            json.dumps({'results': {'results': results[:-1]}}),
                            encoding='utf-8',
                        )
                    elif scenario == 'parse_failed_result':
                        results[-1]['success'] = False
                        output.write_text(
                            json.dumps({'results': {'results': results}}),
                            encoding='utf-8',
                        )
                    else:
                        output.write_text(
                            json.dumps({'results': {'results': results}}),
                            encoding='utf-8',
                        )
                    raise SystemExit(0)

                fixtures = [
                    'fixtures/discover_input.txt',
                    'regression_fixtures/discover_single_question_correction.txt',
                    'regression_fixtures/discover_split_slash_variant_number.txt',
                ]
                results = [
                    {'success': True, 'vars': {'markdown_path': fixture}}
                    for fixture in fixtures
                ]
                if scenario == 'targeted_fail':
                    results[1] = {
                        'success': False,
                        'vars': {'markdown_path': fixtures[1]},
                        'testCase': {'description': 'small regression'},
                        'gradingResult': {'reason': 'assertion failed'},
                    }
                    status = 1
                elif scenario == 'real_fail':
                    results[0] = {
                        'success': False,
                        'vars': {'markdown_path': 'fixtures/discover_input.txt'},
                        'testCase': {'description': 'full document'},
                        'gradingResult': {'reason': 'missing boundaries'},
                    }
                    status = 1
                elif scenario == 'discover_truncated':
                    results.pop()
                    status = 0
                elif scenario == 'discover_duplicate':
                    results[-1] = results[0]
                    status = 0
                elif scenario == 'discover_malformed':
                    output.write_text('{', encoding='utf-8')
                    raise SystemExit(0)
                elif scenario == 'discover_missing_results':
                    output.write_text('{}', encoding='utf-8')
                    raise SystemExit(0)
                elif scenario == 'discover_failed_exit_zero':
                    results[0]['success'] = False
                    status = 0
                else:
                    status = 0
                output.write_text(
                    json.dumps({'results': {'results': results}}),
                    encoding='utf-8',
                )
                raise SystemExit(status)
                """
            ),
            encoding="utf-8",
        )
        self.fake_promptfoo.chmod(0o755)

    def _run(self, *args, key=True, scenario="green"):
        env = os.environ.copy()
        env.update(
            PROMPTFOO_COMMAND=str(self.fake_promptfoo),
            FAKE_LOG=str(self.log_path),
            FAKE_SCENARIO=scenario,
        )
        if key:
            env["GOOGLE_API_KEY"] = "test-key-not-real"
        else:
            env.pop("GOOGLE_API_KEY", None)
        return subprocess.run(
            ["bash", str(RUN_ALL), *args],
            cwd=PROJECT_DIR / "promptfoo",
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _calls(self):
        if not self.log_path.exists():
            return []
        return self.log_path.read_text(encoding="utf-8").splitlines()

    def test_no_mode_is_rejected_before_any_call(self):
        result = self._run()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(self._calls(), [])
        self.assertIn("choose a gate mode explicitly", result.stderr)

    def test_dry_run_needs_no_key_and_makes_no_calls(self):
        result = self._run("--parse-only", "--dry-run", key=False)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._calls(), [])
        self.assertIn("promptfooconfig.parse.yaml", result.stdout)
        self.assertNotIn("promptfooconfig.discover.yaml", result.stdout)
        self.assertIn("VERDICT: DRY-RUN (no API calls)", result.stdout)
        self.assertNotIn("VERDICT: PASS", result.stdout)
        self.assertIn("parse:    PLANNED", result.stdout)

    def test_full_release_dry_run_lists_each_config_once(self):
        result = self._run("--full-release", "--dry-run", key=False)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._calls(), [])
        self.assertEqual(result.stdout.count("promptfooconfig.parse.yaml"), 2)
        self.assertEqual(result.stdout.count("promptfooconfig.discover.yaml"), 2)
        self.assertIn("VERDICT: DRY-RUN (no API calls)", result.stdout)
        self.assertIn("parse:    PLANNED", result.stdout)
        self.assertIn("discover: PLANNED", result.stdout)

    def test_default_promptfoo_cli_version_is_pinned(self):
        env = os.environ.copy()
        env.pop("PROMPTFOO_COMMAND", None)
        env.pop("GOOGLE_API_KEY", None)
        result = subprocess.run(
            ["bash", str(RUN_ALL), "--parse-only", "--dry-run"],
            cwd=PROJECT_DIR / "promptfoo",
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("promptfoo@0.121.19", result.stdout)
        self.assertNotIn("promptfoo@latest", result.stdout)

    def test_parse_requires_exactly_18_successful_json_results(self):
        for scenario in (
            "parse_malformed",
            "parse_missing_results",
            "parse_truncated",
            "parse_failed_result",
        ):
            with self.subTest(scenario=scenario):
                result = self._run("--parse-only", scenario=scenario)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("parse:    FAIL", result.stdout)

    def test_parse_accepts_exactly_18_successful_results(self):
        result = self._run("--parse-only")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._calls(), ["promptfooconfig.parse.yaml"])
        self.assertIn("parse:    PASS", result.stdout)

    def test_full_release_stops_before_discovery_when_parse_fails(self):
        result = self._run("--full-release", scenario="parse_fail")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self._calls(), ["promptfooconfig.parse.yaml"])
        self.assertIn("stopping before paid discovery", result.stdout)

    def test_discover_only_runs_one_full_pass(self):
        result = self._run("--discover-only")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._calls(), ["promptfooconfig.discover.yaml"])
        self.assertIn("discovery classified as: GREEN", result.stdout)

    def test_targeted_failure_fails_without_whole_config_retry(self):
        result = self._run("--discover-only", scenario="targeted_fail")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self._calls(), ["promptfooconfig.discover.yaml"])
        self.assertIn("TARGETED_FAIL", result.stdout)
        self.assertIn("will NOT be retried", result.stdout)

    def test_real_failure_fails_without_retry(self):
        result = self._run("--discover-only", scenario="real_fail")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self._calls(), ["promptfooconfig.discover.yaml"])
        self.assertIn("REAL_FAIL", result.stdout)
        self.assertIn("release blocker", result.stdout)

    def test_discovery_malformed_incomplete_or_failed_json_is_real_failure(self):
        for scenario in (
            "discover_malformed",
            "discover_missing_results",
            "discover_truncated",
            "discover_duplicate",
            "discover_failed_exit_zero",
        ):
            with self.subTest(scenario=scenario):
                result = self._run("--discover-only", scenario=scenario)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("REAL_FAIL", result.stdout)

    def test_live_mode_without_key_refuses_before_any_call(self):
        result = self._run("--discover-only", key=False)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(self._calls(), [])
        self.assertIn("GOOGLE_API_KEY is not set", result.stderr)

    def test_conflicting_modes_are_rejected(self):
        result = self._run("--parse-only", "--full-release")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(self._calls(), [])
        self.assertIn("choose only one gate mode", result.stderr)


if __name__ == "__main__":
    unittest.main()
