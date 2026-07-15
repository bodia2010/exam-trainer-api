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

## Running — standard pre-deploy gate

Before deploying any prompt/schema change (per the current rules in
`../PRODUCT_PLAN.md`: bump the affected cache version, run promptfoo, migrate
the curated cache, and rebuild the APK), run:

```bash
./run_all.sh
```

This is the one command that gives a single PASS/FAIL verdict across both
configs:

- `promptfooconfig.parse.yaml` runs once — any failure (any of the 18
  tests) fails the gate. Parse is not documented as flaky anywhere, so
  there's zero tolerance here.
- `promptfooconfig.discover.yaml` runs up to 3 times, because its own
  comments document the `single_question_correction_not_split` regression
  test as ~1-in-3 flaky at `temperature=1` (an accepted, monitored risk,
  not something to chase to zero — see the yaml). The script stops as
  soon as one run comes back fully green. If it never does, it still
  passes (with a loud warning) as long as every failure seen was that one
  documented-flaky test and nothing else; it fails hard if the *other*
  discover test (`other_markers_present` / `item_count_at_least`, not
  documented as flaky) ever fails, or on any unrecognized failure — so a
  real regression can't hide behind the known flakiness.

Exit code is non-zero only on a stable/real failure, so it's suitable as
a CI gate. Requires `GOOGLE_API_KEY` (see setup above); the script checks
for it up front and refuses to run instead of burning API calls on a
misconfigured environment.

## Running individual configs (for iterating on one prompt/fixture)

```bash
npx promptfoo@latest eval -c promptfooconfig.discover.yaml
npx promptfoo@latest eval -c promptfooconfig.parse.yaml
npx promptfoo@latest view   # opens a side-by-side web UI of the results
```

Useful while actively editing a single prompt, but `run_all.sh` is the
command to run before actually shipping — it's the only one that applies
the discover retry/tolerance logic instead of reporting a raw pass/fail
on a test known to blip ~1 in 3 runs.

## Adding a new candidate model

Add another entry under `providers:` in either config, same
`id: 'python:gemini_schema_provider.py'` with a different
`config: { model: <model-id> }`. generationConfig (temperature,
thinkingLevel/thinkingBudget, responseSchema) is derived automatically
from the model name and section_type by `../generation_config.py` — no
per-model config to hand-write. No other changes needed; the same
prompts and assertions run against it automatically.
