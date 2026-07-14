#!/usr/bin/env python3
"""verify_content.py — audit an already-parsed course's content against its
source PDF's markdown, one section type at a time.

This is NOT the same check as e2e_import.py (structural validity — right
shape, right counts, no empty required fields) or promptfoo (prompt
regression testing on fixtures). This checks CONTENT FIDELITY: is what's
sitting in a real user's saved course actually a faithful, accurate
extraction of the real source, or did something get paraphrased, dropped,
mismatched, or hallucinated despite passing structural validation? That
class of bug (e.g. a paraphrased zu_erledigen, or Hören Teil 2 dialogue
turns attributed to the wrong speaker) is invisible to shape-only checks —
catching it previously meant a human comparing screenshots by eye.

Runs one Gemini call per section type present in the course JSON, giving
it the FULL source markdown plus that section type's extracted JSON, and
asking it to act as an independent auditor and list concrete discrepancies
(or confirm none). Deliberately does not reuse the app's own parse prompt
or model choice — an auditor that shares the same prompt/model as the
original extraction would tend to repeat the same blind spots.

Usage:
    GEMINI_API_KEY=... python3 scripts/verify_content.py \\
        --markdown /path/to/source.md \\
        --course-json /path/to/course.json \\
        --section-type telefonnotiz \\
        [--out report.md]

Omit --section-type to audit every section type present in the course
(one call each) — costs roughly (markdown tokens + that section's JSON
tokens) x input rate, x12 for a full course. Printed per-call before each
request so cost isn't a surprise mid-run.

MULTI-RUN CONSENSUS (--runs, default 3): a single audit_section() call is
NOT reliable on its own — confirmed experimentally by running this script
twice, back to back, on the exact same (unchanged) telefonnotiz course:
the first clean run reported "NO DISCREPANCIES FOUND", the second run on
identical input found 4 discrepancies. Same temperature=0, same model,
same prompt, same input — the variance is real, not user error. Treating
one run's output as ground truth risks both false negatives (missed the
one run that would have caught it) and false positives (a single run's
misfire flagged as a real bug). With --runs > 1 this script instead runs
audit_section() N times per section type and aggregates findings by
cross-run agreement: a finding line seen in >=2 runs is reported as a
"(k/N runs)" consensus finding; a finding seen in only 1 run is still
shown, but demoted to a separate low-confidence section. Since findings
are free-text LLM output (not structured JSON), "the same finding" is
decided by normalized-prefix matching OR overall text similarity, not
exact string equality — see aggregate_findings().
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import generation_config  # noqa: E402

_GEMINI_URL_TMPL = (
    'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
)

# Independent of generation_config.model_for('discover') on purpose — see
# module docstring. Large context, needs to actually read the whole
# document reliably, so not the parse-side flash-lite either.
_AUDIT_MODEL = 'gemini-3.5-flash'

_AUDIT_PROMPT = """You are an independent fact-checker for a German B2 Beruf \
(telc) exam-prep app. Below is (1) the FULL source document the app was \
given, converted from PDF to Markdown, and (2) what the app's own \
extraction pipeline produced for ONE section type, as JSON.

Your job: verify the JSON is a faithful, accurate representation of what's \
actually printed in the source for this section type. Find every \
discrepancy — do not assume the extraction is correct.

Check specifically for:
- Paraphrased or reworded text where the source should have been copied \
verbatim (monologues, letter/passage text, answer-key fields).
- Wrong numbers: question counts, phone numbers, prices, dates, variant \
numbers.
- Content attributed to the wrong speaker, variant, or edition.
- Missing content: a variant/edition/field present in the source but \
absent from the JSON.
- Hallucinated content: anything in the JSON that isn't actually in the \
source at all.
- Wrong answers: a marked-correct option that doesn't match the source's \
own answer key.

A field containing the literal string "(nicht angegeben)" is NOT a bug — \
it's the app's own marker for a field that's genuinely blank in the \
source (e.g. an unfilled "Telefonnummer:" line). Only flag it as wrong if \
the source actually DOES contain a value there that was missed.

Report format: for each discrepancy found, one line: \
"<variant/item identifier>: <what's wrong, quoting source vs. extracted>". \
If you find nothing wrong after a careful check, respond with exactly: \
NO DISCREPANCIES FOUND

Do not comment on formatting, field ordering, or anything not listed \
above. Be concrete and quote exact text — "seems off" is not useful.

SECTION TYPE: {section_type}

SOURCE DOCUMENT (Markdown):
{markdown}

EXTRACTED JSON for this section type:
{section_json}
"""


def audit_section(
    markdown: str, section_type: str, section_json: list, api_key: str,
) -> str:
    prompt = _AUDIT_PROMPT.format(
        section_type=section_type,
        markdown=markdown,
        section_json=json.dumps(section_json, ensure_ascii=False, indent=2),
    )
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {
            'temperature': 0,
            'thinkingConfig': {'thinkingLevel': 'MEDIUM'},
        },
    }
    url = _GEMINI_URL_TMPL.format(model=_AUDIT_MODEL)

    # 503 (Gemini momentarily overloaded) is a real, observed problem —
    # yesterday's curation session hit a ~40-minute stretch of it. Retry a
    # few times with a short growing pause, same shape as main.py's own
    # _call_gemini. 429 (quota) is deliberately NOT retried here: Gemini's
    # own guidance for it is a much longer wait than makes sense to spend
    # inside one attempt of a multi-run audit — better to fail that run
    # fast and let --runs' aggregation, or a rerun of the whole script,
    # absorb it.
    last_status = None
    last_text = ''
    for attempt in range(3):
        try:
            resp = requests.post(url, params={'key': api_key}, json=payload, timeout=180)
        except requests.RequestException as e:
            return f'ERROR: could not reach Gemini: {type(e).__name__}: {e}'

        if resp.status_code == 200:
            data = resp.json()
            usage = data.get('usageMetadata') or {}
            print(
                f'    tokens: prompt={usage.get("promptTokenCount", 0)} '
                f'output={usage.get("candidatesTokenCount", 0)} '
                f'thoughts={usage.get("thoughtsTokenCount", 0)}',
                file=sys.stderr,
            )
            try:
                return data['candidates'][0]['content']['parts'][0]['text']
            except (KeyError, IndexError):
                return f'ERROR: unexpected response shape: {json.dumps(data)[:500]}'

        last_status, last_text = resp.status_code, resp.text
        if resp.status_code == 503 and attempt < 2:
            wait = 2 * (attempt + 1)
            print(f'    HTTP 503 (overloaded), retrying in {wait}s...', file=sys.stderr)
            time.sleep(wait)
            continue
        break

    return f'ERROR: Gemini request failed (HTTP {last_status}): {last_text[:500]}'


def _finding_key(line: str, key_len: int = 40) -> str:
    """Normalized comparison key for one finding line: lowercase, collapse
    whitespace, keep only the first ~40 chars. The audit prompt's own
    'Report format' instruction puts the variant/item identifier first on
    every finding line, so a shared prefix is a good proxy for "the same
    underlying finding" even when the LLM rephrases the rest of the
    sentence differently between runs — exact string equality is too
    strict for free-text output that isn't guaranteed byte-stable."""
    norm = re.sub(r'\s+', ' ', line.strip().lower())
    return norm[:key_len]


def parse_findings(run_text: str) -> list[str]:
    """One audit_section() response -> its individual finding lines (the
    audit prompt already asks for one discrepancy per line). Excludes the
    NO DISCREPANCIES FOUND sentinel, blank lines, and ERROR: lines — a
    failed run contributes zero findings, not a false "nothing wrong"."""
    findings = []
    for raw in run_text.splitlines():
        line = raw.strip()
        if not line or line.upper() == 'NO DISCREPANCIES FOUND' or line.startswith('ERROR:'):
            continue
        findings.append(line)
    return findings


_SIMILARITY_THRESHOLD = 0.6  # SequenceMatcher ratio; confirmed live against
# real audit output that two full rephrasings of the same finding land
# well above this and unrelated findings land well below it.


def _similar(a: str, b: str) -> bool:
    return difflib.SequenceMatcher(None, a, b).ratio() >= _SIMILARITY_THRESHOLD


def aggregate_findings(valid_run_texts: list[str]) -> list[dict]:
    """Cross-run consensus over free-text findings, matching "the same
    finding" by EITHER of the two criteria in the module docstring: a
    shared ~40-char normalized prefix key, OR near-identical full text
    (SequenceMatcher ratio). The prefix-only heuristic alone was tried
    first and confirmed live to under-merge: the audit prompt's own
    "Report format" line ("<variant/item identifier>: <what's wrong>")
    is a suggestion, not a hard template, and two runs describing the
    exact same bug started their sentences differently often enough
    ("variant 1, item 19: wrong answer marked correct, extracted 'a' ..."
    vs. "variant 1, question 19: wrong correct answer, extracted 'a' ...")
    that prefix-only matching split one real 2/2 consensus finding into
    two separate 1-run "low-confidence" entries. Falling back to overall
    similarity when the prefixes disagree catches that case without
    loosening the prefix check itself (still exact-enough for the common
    case where an identifier prefix DOES match).

    Within a single run, matches still collapse onto the same cluster
    before counting, so one chatty run can't inflate a finding's
    cross-run count past 1. Returns clusters sorted by count (number of
    DISTINCT runs that raised it) descending; each is {'count': int,
    'texts': [distinct raw phrasings seen, in first-seen order]}."""
    clusters: list[dict] = []  # each: {'key', 'norms': [...], 'texts': [...], 'runs': set}
    for run_idx, run_text in enumerate(valid_run_texts):
        matched_this_run: set[int] = set()  # cluster indices already hit by this run
        for line in parse_findings(run_text):
            key = _finding_key(line)
            norm = re.sub(r'\s+', ' ', line.strip().lower())
            hit = None
            for ci, cluster in enumerate(clusters):
                if ci in matched_this_run:
                    continue
                if key == cluster['key'] or _similar(norm, cluster['norms'][0]):
                    hit = ci
                    break
            if hit is None:
                clusters.append({'key': key, 'norms': [norm], 'texts': [line], 'runs': {run_idx}})
            else:
                cluster = clusters[hit]
                matched_this_run.add(hit)
                cluster['runs'].add(run_idx)
                cluster['norms'].append(norm)
                if line not in cluster['texts']:
                    cluster['texts'].append(line)
    groups = [{'count': len(c['runs']), 'texts': c['texts']} for c in clusters]
    return sorted(groups, key=lambda g: -g['count'])


def format_consensus_report(run_results: list[str], total_runs: int) -> str:
    """Turn N raw audit_section() outputs for one section type into a
    single readable report: consensus findings first (seen in >=2 runs,
    most-frequent first), then a low-confidence section for findings seen
    in exactly 1 run. Runs that errored out are reported separately and
    excluded from the run count used for the "(k/N)" denominators, so a
    transient failure doesn't silently dilute the consensus."""
    valid = [r for r in run_results if not r.strip().startswith('ERROR:')]
    errors = [r for r in run_results if r.strip().startswith('ERROR:')]

    lines = []
    if errors:
        lines.append(f'{len(errors)}/{total_runs} run(s) FAILED (excluded from consensus below):')
        for e in errors:
            lines.append(f'  - {e.strip()}')
        lines.append('')

    n_valid = len(valid)
    if n_valid == 0:
        lines.append('All runs failed — no consensus possible.')
        return '\n'.join(lines)

    if all(r.strip().upper() == 'NO DISCREPANCIES FOUND' for r in valid):
        lines.append(f'NO DISCREPANCIES FOUND ({n_valid}/{n_valid} clean runs)')
        return '\n'.join(lines)

    groups = aggregate_findings(valid)
    high = [g for g in groups if g['count'] >= 2]
    low = [g for g in groups if g['count'] == 1]

    if high:
        lines.append(f'CONSENSUS FINDINGS (seen in >=2 of {n_valid} runs):')
        for g in high:
            lines.append(f"- ({g['count']}/{n_valid} runs) {g['texts'][0]}")
            for alt in g['texts'][1:]:
                lines.append(f'    also phrased as: {alt}')
        lines.append('')
    if low:
        lines.append(f'LOW-CONFIDENCE (1 run only, out of {n_valid}):')
        for g in low:
            lines.append(f"- {g['texts'][0]}")
        lines.append('')
    if not high and not low:
        lines.append('No findings extracted (unexpected report shape).')
    return '\n'.join(lines).rstrip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--markdown', required=True, type=Path)
    parser.add_argument('--course-json', required=True, type=Path)
    parser.add_argument('--section-type', default=None,
                         help='Audit only this section type; omit to audit all present.')
    parser.add_argument('--out', type=Path, default=None,
                         help='Write the report here instead of stdout.')
    parser.add_argument('--runs', type=int, default=3,
                         help='Audit each section type this many times and '
                              'aggregate findings by cross-run agreement '
                              '(default 3; see module docstring for why a '
                              'single run is not reliable). --runs 1 falls '
                              'back to the old single-pass behavior.')
    args = parser.parse_args()
    if args.runs < 1:
        parser.error('--runs must be >= 1')

    api_key = os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        print('ERROR: set GEMINI_API_KEY in the environment.', file=sys.stderr)
        sys.exit(2)

    markdown = args.markdown.read_text(encoding='utf-8')
    course = json.loads(args.course_json.read_text(encoding='utf-8'))
    # Accept either the full course.json shape ({"sections": {...}, "title":
    # ...}) or the bare {section_type: [items]} dict pulled straight from
    # Redis — same convention already used by check_answer_keys.py /
    # check_verbatim_content.py / update_curated_content.py.
    sections = course.get('sections') if isinstance(course.get('sections'), dict) else course

    types_to_check = [args.section_type] if args.section_type else list(sections.keys())

    report_lines = [f'# Content verification — {course.get("title", "?")}', '']
    for section_type in types_to_check:
        if section_type not in sections:
            report_lines.append(f'## {section_type}\n\nSKIPPED — not present in course JSON.\n')
            continue
        section_json = sections[section_type]
        print(f'auditing {section_type} ({len(section_json)} items) '
              f'— {args.runs} run(s)...', file=sys.stderr)

        run_results = []
        for run_idx in range(args.runs):
            if args.runs > 1:
                print(f'  run {run_idx + 1}/{args.runs}...', file=sys.stderr)
            t0 = time.time()
            result = audit_section(markdown, section_type, section_json, api_key)
            print(f'    done in {time.time() - t0:.1f}s', file=sys.stderr)
            run_results.append(result)

        if args.runs == 1:
            report_lines.append(f'## {section_type}\n\n{run_results[0]}\n')
        else:
            consensus = format_consensus_report(run_results, args.runs)
            report_lines.append(f'## {section_type}\n\n{consensus}\n')

    report = '\n'.join(report_lines)
    if args.out:
        args.out.write_text(report, encoding='utf-8')
        print(f'Report written to {args.out}', file=sys.stderr)
    else:
        print(report)


if __name__ == '__main__':
    main()
