# Model regression testing

Runs Gemini against our actual prompts AND generationConfig (both read
live from `../prompts.py` and `../generation_config.py` via the custom
`gemini_schema_provider.py`, never a copy) against the specific failure
modes we've already hit in production:

- discovery dropping the `other` filler-block markers (found with 2.5
  Flash-Lite — reintroduces the runaway-chunk bug)
- parse calls silently collapsing multiple distinct editions into fewer
  output objects than discovery found (hoeren_teil1: 21 → 11 before the
  segmentation fix)
- the `<<SAME_AS_ORIGINAL>>` deduplication sentinel leaking into the
  original variant (retired system-wide now that every type is
  schema-enforced — kept as a regression guard)
- `texts` coming back empty, or a question array short of the official
  telc count (5/8 for hoeren_teil4, 1/2 for beschwerde) — inconsistent
  across retries of the identical input under free-form generation; this
  is what `responseSchema`'s `minItems`/`maxItems` now exists to prevent
- a single already-numbered question's later answer correction
  ("Варианты ответов от `<date>`") getting mistaken by discovery for the
  start of a whole new edition, splitting one variant's content across
  chunks and starving the real object of its own questions/texts
  (`regression_fixtures/` — synthetic, safe to commit, unlike
  `fixtures/`)

## One-time setup

```bash
export GOOGLE_API_KEY=...      # a Gemini API key (Google AI Studio) — evals call Gemini directly
export FIREBASE_ID_TOKEN=...   # see make_fixtures.py's docstring for how to get one
python3 make_fixtures.py /path/to/your/exam.pdf
```

`make_fixtures.py` calls the deployed backend to convert your PDF and run
discovery once, then saves the two files evals read from into
`fixtures/` — which is gitignored (the exam PDF content shouldn't sit in
a public repo).

The YAML configs intentionally pass fixture names as `markdown_path`, not
`markdown: file://...`. Promptfoo applies JavaScript `trim()` to text loaded
through a `file://` variable, which removes a leading PDF form-feed (`\x0c`)
and makes the eval input differ from production. `fixture_loader.py` reads the
UTF-8 bytes without trimming; prompt functions and source-based assertions use
that same raw value.

## Running — cost-aware pre-deploy gate

Before deploying any prompt/schema change (per the current rules in
`../PRODUCT_PLAN.md`: bump the affected cache version, run promptfoo, migrate
the curated cache, and rebuild the APK), run:

Use the smallest gate that covers the changed effective payload:

```bash
./run_all.sh --parse-only            # 18 parse cases
./run_all.sh --discover-only         # one discovery pass
./run_all.sh --full-release          # parse, then one discovery pass
./run_all.sh --full-release --dry-run
```

- A mode is mandatory: a bare `./run_all.sh` fails before making calls. This
  prevents a future discovery change from silently receiving a parse-only
  release verdict.
- Parse runs its 18 tests once with zero tolerance. The TTS rollout is
  checked inside those existing calls: Hören Teil 4, Hören Teil 1 and
  Telefonnotiz must return the expected voice metadata, while Lesen Teil 1
  must not receive TTS metadata.
- Discovery is explicit because the full-document fixture dominates cost.
  Use it when `DISCOVER`, `DISCOVER_SCHEMA`, its model/generation config or
  discovery processing changed. A full release can still require both stages.
- `--full-release` stops after a parse failure, before spending on discovery.
  Discovery runs exactly once. Any failed test fails closed; the script never
  resubmits the full document just because a small synthetic regression failed.
- `--dry-run` needs no API key and makes no network calls.

The runner is pinned to the locally validated Promptfoo CLI version; update
that pin deliberately together with config validation and offline shell tests.

The custom provider returns Gemini token usage and a Standard-tier cost
estimate to Promptfoo. Thinking tokens are billed as output; cached input is
reported and charged at the model's cached-input rate. Unknown A/B models keep
their token counts but deliberately omit a cost instead of using a guessed
price. Update the reviewed rates in `gemini_schema_provider.py` and
`scripts/cost_report.py` together when production models or Google pricing
change.

The first live TTS-metadata parse gate on 2026-07-16 made exactly 18 calls,
used 97,573 total tokens and reported an estimated `$0.080047` cost. It failed
closed at 14/18 and exposed four concrete issues; the deterministic repairs and
captured-response replay are recorded in `../PRODUCT_PLAN.md`. For a new or
changed fixture, prompt, model or assertion contract, a live run must not be
treated as green until all 18 strict results pass. For this unchanged captured
fixture, release evidence is the recorded response set plus a direct,
no-network replay through the current runtime/assertion helpers; that replay
passes all 18 cases after the documented deterministic remediation.

Do not use Promptfoo `--model-outputs` as an assumed offline replay mechanism
with this custom Python provider: version 0.121.19 still invoked the live
provider and auto-loaded the local `.env` in the verified run. Offline replay
must call the Python assertions/runtime helpers directly with an explicitly
sanitized environment, or use a dedicated no-network provider config.

Exit code is non-zero for any selected assertion, provider, malformed-result
or environment failure, so the runner fails closed and is suitable as a CI
gate. Requires `GOOGLE_API_KEY` (see setup above); the script checks for it up
front and refuses to run instead of burning API calls on a misconfigured
environment.

## Running individual configs (for iterating on one prompt/fixture)

```bash
npx promptfoo@0.121.19 eval -c promptfooconfig.discover.yaml
npx promptfoo@0.121.19 eval -c promptfooconfig.parse.yaml
npx promptfoo@0.121.19 view   # opens a side-by-side web UI of the results
```

Useful while actively editing a single prompt. Before shipping, use
`run_all.sh` with the mode matching the changed production payload; use
`--full-release` whenever both parse and discovery require validation.

## Adding a new candidate model

Add another entry under `providers:` in either config, same
`id: 'python:gemini_schema_provider.py'` with a different
`config: { model: <model-id> }`. generationConfig (temperature,
thinkingLevel/thinkingBudget, responseSchema) is derived automatically
from the model name and section_type by `../generation_config.py` — no
per-model config to hand-write. No other changes needed; the same
prompts and assertions run against it automatically.
