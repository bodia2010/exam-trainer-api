# Model regression testing

Compares Gemini models on our actual prompts (read live from
`../prompts.py`, never a copy) against the specific failure modes we've
already hit in production:

- discovery dropping the `other` filler-block markers (found with 2.5
  Flash-Lite — reintroduces the runaway-chunk bug)
- parse calls silently collapsing multiple distinct editions into fewer
  output objects than discovery found (hoeren_teil1: 21 → 11 before the
  segmentation fix)
- the `<<SAME_AS_ORIGINAL>>` deduplication sentinel leaking into the
  original variant, which must always be fully self-contained

## One-time setup

```bash
export GOOGLE_API_KEY=...   # a Gemini API key (Google AI Studio)
export APP_SECRET=...       # same secret as the deployed backend
python3 make_fixtures.py /path/to/your/exam.pdf
```

`make_fixtures.py` calls the deployed backend to convert your PDF and run
discovery once, then saves the two files evals read from into
`fixtures/` — which is gitignored (the exam PDF content shouldn't sit in
a public repo).

## Running

```bash
npx promptfoo@latest eval -c promptfooconfig.discover.yaml
npx promptfoo@latest eval -c promptfooconfig.parse.yaml
npx promptfoo@latest view   # opens a side-by-side web UI of the results
```

## Adding a new candidate model

Add another entry under `providers:` in either config
(`google:<model-id>`, same `generationConfig` shape — note Gemini 3.x
models use `thinkingLevel` instead of `thinkingBudget`). No other changes
needed; the same prompts and assertions run against it automatically.
