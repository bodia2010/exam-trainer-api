#!/usr/bin/env python3
"""check_verbatim_content.py — deterministic verbatim-content audit against
the course's own source.md, for the WHOLE course at once (all 12
section_types), not tied to any one document.

Two extraction bugs have been observed in the pipeline's Gemini output,
both on fields the prompts (prompts.py) explicitly require to be copied
"verbatim" from the source:

  1. HALLUCINATION: the model invents content with no counterpart
     anywhere in the source (confirmed precedent: hoeren_teil4 variant 8
     texts[].content, see the one-off scripts/fix_course.py).
  2. TRUNCATED_AT_SLASH: the source line reads "<X> / <Y>" (two
     alternative wordings across editions, joined with " / "), and only
     one half made it into the extracted field (confirmed precedent:
     telefonnotiz weitere_informationen bullets, same script).

Unlike fix_course.py (a one-off patch script hardcoded to one document's
specific bullets/variant), this script re-derives the set of verbatim
fields from response_schemas.py's shape and prompts.py's own "verbatim"
instructions, and scans every item of every section_type generically.

Method (both checks compare against a single normalized copy of
source.md — whitespace-collapsed, lowercased, NFKC-normalized, the same
_normalize() convention as check_answer_keys.py):
  a. HALLUCINATED: the first ~60 normalized chars of the field value are
     not found anywhere in normalized source.md.
  b. TRUNCATED_AT_SLASH: the field value IS found in normalized
     source.md, but as a substring of a LONGER run of text where a "/"
     sits within a few characters of either boundary — i.e. the source
     actually reads "<value> / <something else>" or "<something else> /
     <value>" and only "<value>" was kept.

Both are conservative on purpose (false negatives preferred over false
positives): short values are skipped, values that already contain a "/"
are skipped for (b), and the literal sentinel "(nicht angegeben)" (a
legitimate marker for genuinely blank source fields, see prompts.py) is
never flagged.

Run (no PyMuPDF/fitz dependency — plain stdlib, works in any venv):
    python3 scripts/check_verbatim_content.py \\
        --course-json /path/to/course.json \\
        --source-md /path/to/source.md \\
        [--section-type telefonnotiz]
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

SENTINEL = '(nicht angegeben)'
HALLUC_PROBE_LEN = 60
MIN_HALLUC_LEN = 8      # shorter values are too ambiguous to judge reliably
MIN_TRUNCATION_LEN = 10  # ditto for the truncation check
SLASH_WINDOW = 5         # "~5 characters" on either side, per the observed bug


# ---------------------------------------------------------------------------
# Normalization — adapted from check_answer_keys.py's _normalize(), copied
# rather than imported so this script has no PyMuPDF/fitz dependency (it
# never touches the PDF, only course.json + source.md).
# ---------------------------------------------------------------------------

def _normalize(text: str, strip_trailing: bool = True) -> str:
    """Whitespace/diacritics-stable comparison key. PDF->markdown export and
    Gemini output differ in hyphenation wraps, quote styles and trailing
    punctuation — strip all of it so comparisons aren't thrown off by them.

    strip_trailing=False keeps a trailing '.'/'。' — needed when normalizing
    source.md ONE LINE AT A TIME before joining lines into the search blob
    (see SourceIndex): stripping each line's own sentence-ending period
    individually, then joining with single spaces, silently deletes every
    internal sentence boundary in reconstructed multi-line dialogue/
    monologue content — confirmed live, this alone made a genuine,
    verbatim-present dialogue register as HALLUCINATED, because the value
    being checked (normalized whole, trailing-stripped only ONCE at its
    own end) still had its internal periods while the blob it was being
    searched in had lost every one of them.

    Also strips "**" — prompts.py's own HEADINGS rule deliberately wraps
    a verbatim sub-heading in double asterisks as a structural annotation
    ON TOP OF the real text (see buildContentSpan client-side), which
    source.md itself never contains. Left in, every text starting with a
    bolded heading probed as HALLUCINATED purely because of "**", not
    because the actual words weren't found."""
    text = unicodedata.normalize('NFKC', text)
    text = text.replace('-\n', '').replace('\n', ' ')
    text = text.replace('**', '')
    text = re.sub(r'[„“”"«»\'’]', '', text)
    text = re.sub(r'\s+', ' ', text).strip().lower()
    return text.rstrip('.。 ').strip() if strip_trailing else text


# ---------------------------------------------------------------------------
# Verbatim field map — derived from response_schemas.py's shapes and
# prompts.py's explicit "verbatim" instructions (grep -i verbatim prompts.py),
# not guessed:
#   - universal schema (prompts.py _UNIVERSAL / response_schemas.py
#     _universal_variant_schema, used by lesen_teil1-4, beschwerde,
#     sprachbausteine_teil2, hoeren_teil2-4): "texts[].content and every
#     options[].text must be the EXACT wording ... copied verbatim"
#     (prompts.py line ~33) — this covers option_pool[].text too, same
#     {letter, text} shape and same "never summarize/paraphrase an option"
#     wording, plus questions[].options[].text for choice-type questions.
#     Note question "text" (the stem) is explicitly NOT required verbatim
#     ("Where a text isn't printed verbatim ... keep it minimal and
#     neutral") — deliberately excluded.
#   - hoeren_teil1 (bespoke schema): "dialogue and every option text must
#     be the EXACT wording ... copied verbatim" (prompts.py line ~260).
#     richtig_falsch.statement / multiple_choice.stem are, like the
#     universal "text" stem, not claimed verbatim — excluded.
#   - telefonnotiz (bespoke schema, versions[]): "monologue must be the
#     EXACT wording ... copied verbatim" (line ~307), and "Read each
#     [answer] field from its own label, verbatim" (line ~308) — covers
#     the whole answer block: call_type, name, telefonnummer,
#     weitere_informationen[], zu_erledigen.
#   - sprachbausteine_teil1 is intentionally NOT included: its prompt
#     never uses the word "verbatim" for letter_text/all_options (checked
#     explicitly with grep, not assumed).
# ---------------------------------------------------------------------------

_UNIVERSAL_FIELDS = [
    'texts[].content',
    'option_pool[].text',
    'questions[].options[].text',
]

_UNIVERSAL_SECTION_TYPES = [
    'lesen_teil1', 'lesen_teil2', 'lesen_teil3', 'lesen_teil4',
    'beschwerde', 'sprachbausteine_teil2',
    'hoeren_teil2', 'hoeren_teil3', 'hoeren_teil4',
]

VERBATIM_FIELDS: dict[str, list[str]] = {st: _UNIVERSAL_FIELDS for st in _UNIVERSAL_SECTION_TYPES}
VERBATIM_FIELDS['hoeren_teil1'] = [
    'question_pairs[].dialogue',
    'question_pairs[].multiple_choice.options[].text',
]
VERBATIM_FIELDS['telefonnotiz'] = [
    'versions[].monologue',
    'versions[].answer.call_type',
    'versions[].answer.name',
    'versions[].answer.telefonnummer',
    'versions[].answer.weitere_informationen[]',
    'versions[].answer.zu_erledigen',
]


def _walk(node, parts: list[str], path: str):
    """Yield (path, str_value) for every string reachable from `node` by
    following `parts` — dotted path segments, each optionally ending in
    "[]" to mean 'iterate this list'. Purely structural, no section_type
    knowledge — the same walker drives every entry in VERBATIM_FIELDS."""
    if not parts:
        if isinstance(node, str):
            yield path, node
        return
    part = parts[0]
    rest = parts[1:]
    is_list = part.endswith('[]')
    key = part[:-2] if is_list else part
    if not isinstance(node, dict):
        return
    child = node.get(key)
    if is_list:
        if not isinstance(child, list):
            return
        for i, elem in enumerate(child):
            new_path = f'{path}.{key}[{i}]' if path else f'{key}[{i}]'
            if not rest:
                if isinstance(elem, str):
                    yield new_path, elem
            else:
                yield from _walk(elem, rest, new_path)
    else:
        new_path = f'{path}.{key}' if path else key
        if not rest:
            if isinstance(child, str):
                yield new_path, child
        else:
            yield from _walk(child, rest, new_path)


def iter_verbatim_values(item: dict, specs: list[str]):
    for spec in specs:
        yield from _walk(item, spec.split('.'), '')


def _version_label(item: dict, path: str) -> str:
    """Best-effort human label for a report line: 'variant N' plus, for
    telefonnotiz's nested versions[i], that edition's own label."""
    variant = item.get('variant_number', '?')
    m = re.match(r'versions\[(\d+)\]', path)
    if m and isinstance(item.get('versions'), list):
        vi = int(m.group(1))
        if 0 <= vi < len(item['versions']):
            label = item['versions'][vi].get('label')
            if label:
                return f'variant {variant} ed.{vi} ({label})'
    return f'variant {variant}'


# ---------------------------------------------------------------------------
# source.md index — one normalized blob for O(1) substring search per field,
# plus a line-offset table to map a match back to human-readable context.
# ---------------------------------------------------------------------------

class SourceIndex:
    def __init__(self, source_md_text: str):
        # str.splitlines() also breaks on \r, \v, \f, \x1c-\x1e, \x85,
        # U+2028/U+2029 — MarkItDown's PDF extraction embeds literal form
        # feeds (\f, page-break artifacts) in real documents, which
        # silently inflated the line count (12771 vs. the real 12566) and
        # made every reported line number drift further off with depth
        # into the document. split('\n') matches every other line-based
        # convention already established in this codebase (main.py,
        # parse_service.dart) for exactly this reason.
        self.source_lines = source_md_text.split('\n')
        self.norm_lines = [_normalize(line, strip_trailing=False) for line in self.source_lines]
        offsets = []
        pos = 0
        blob_parts = []
        # Exactly one space between any two non-empty pieces, however many
        # blank lines (paragraph breaks) separate them in the source — a
        # naive "one line, one trailing space" join put TWO spaces around
        # a blank-line paragraph break (one from the heading line's own
        # slot, one from the blank line's), while a value's own "\n\n"
        # collapses to a single space under _normalize()'s \s+ -> ' '
        # rule. A heading immediately followed by a blank line before its
        # body paragraph — extremely common — mismatched on this alone.
        pending_space = False
        for i, nl in enumerate(self.norm_lines):
            offsets.append(pos)
            is_last = i == len(self.norm_lines) - 1
            # A line ending in '-' is a hard-wrapped word split across the
            # PDF's print line break (e.g. "Fir-" / "menausweis") — the
            # pipeline's own de-hyphenation rule (prompts.py Common rules)
            # already joins these into one word in every extracted field,
            # so building the search blob with a literal space in between
            # ("fir- menausweis") made every value spanning one of these
            # wraps mismatch the source it's actually taken from.
            hyphen_wrap = nl.endswith('-') and not is_last
            piece = nl[:-1] if hyphen_wrap else nl
            if piece:
                if pending_space:
                    blob_parts.append(' ')
                    pos += 1
                    pending_space = False
                blob_parts.append(piece)
                pos += len(piece)
            if not hyphen_wrap:
                pending_space = True
        self.offsets = offsets
        self.blob = ''.join(blob_parts)

    def line_for_offset(self, offset: int) -> int:
        """0-based source line index containing normalized blob offset."""
        return max(0, bisect.bisect_right(self.offsets, offset) - 1)

    def context(self, start: int, end: int, pad: int = 0) -> tuple[int, int, str]:
        """Raw (non-normalized) source text spanning the blob region
        [start, end), for display. Returns (line_no_1based_start, line_no_1based_end, text)."""
        l0 = self.line_for_offset(start)
        l1 = self.line_for_offset(max(start, end - 1))
        lo = max(0, l0 - pad)
        hi = min(len(self.source_lines), l1 + 1 + pad)
        text = ' '.join(s.strip() for s in self.source_lines[lo:hi] if s.strip())
        return lo + 1, hi, text


def check_hallucinated(norm_value: str, idx: SourceIndex) -> bool:
    if len(norm_value) < MIN_HALLUC_LEN:
        return False
    probe = norm_value[:HALLUC_PROBE_LEN]
    return probe not in idx.blob


def check_truncated(norm_value: str, idx: SourceIndex):
    """Returns a (start_line, end_line, candidate_text, other_halves) tuple
    if `norm_value` is found as a substring of a longer source run with a
    '/' within SLASH_WINDOW chars of one boundary, else None.
    `other_halves` is the list of normalized text chunks on the OTHER
    side(s) of the slash(es) in that run — see the docstring note on
    edition-split false positives for why the caller needs these."""
    # A single common word (no internal whitespace — "Beschwerde", a
    # telefonnotiz call_type) isn't a phrase that can be "truncated", and
    # is exactly the kind of short, frequent token that turns up next to
    # an unrelated "/" somewhere else in the document purely by chance —
    # confirmed live: telefonnotiz call_type "Beschwerde" kept matching a
    # DIFFERENT variant's own "Beschwerde /..." answer-key line, nothing
    # to do with the item actually being checked. Requiring at least one
    # space still catches every real category of bug seen so far (phone
    # numbers are space-grouped, bullets/sentences are multi-word).
    if len(norm_value) < MIN_TRUNCATION_LEN or '/' in norm_value or ' ' not in norm_value:
        return None
    blob = idx.blob
    start = 0
    while True:
        pos = blob.find(norm_value, start)
        if pos == -1:
            return None
        end = pos + len(norm_value)
        left = blob[max(0, pos - SLASH_WINDOW):pos]
        right = blob[end:end + SLASH_WINDOW]
        if '/' in left or '/' in right:
            lo, hi, text = idx.context(pos, end, pad=0)
            # Strip a leading option-letter marker ("a) ", "b) ", ...)
            # from each half before comparing — the raw context text
            # still has it, but extracted option/field values never do,
            # so leaving it in place made every edition-split pair miss
            # its own cross-reference match. Re-strip trailing punctuation
            # per piece too: _normalize()'s rstrip only ran once, on the
            # END of the whole joined text, so an earlier piece (before
            # the last '/') kept its own trailing '.' and never matched
            # either — confirmed live on "a) gibt herrn klein
            # schutzkleidung. / übergibt ...", where the first half's
            # stray period alone was enough to break the set lookup.
            other_halves = [
                re.sub(r'^[a-h]\)\s*', '', p.strip()).rstrip('.。 ').strip()
                for p in _normalize(text).split('/')
            ]
            other_halves = [p for p in other_halves if p and p != norm_value]
            return lo, hi, text, other_halves
        start = pos + 1


# ---------------------------------------------------------------------------

def iter_items(sections: dict, section_filter: str | None):
    for section_type, items in sections.items():
        if section_filter and section_type != section_filter:
            continue
        if section_type not in VERBATIM_FIELDS:
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                yield section_type, item


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--course-json', required=True, type=Path)
    parser.add_argument('--source-md', required=True, type=Path)
    parser.add_argument('--section-type', default=None,
                         help='limit the audit to one section_type')
    args = parser.parse_args()

    course = json.loads(args.course_json.read_text(encoding='utf-8'))
    # Real course.json wraps sections under "sections"; the curated
    # section-dict fixtures used for self-testing are the bare
    # {section_type: [items]} dict itself — accept either.
    sections = course.get('sections') if isinstance(course.get('sections'), dict) else course

    source_md_text = args.source_md.read_text(encoding='utf-8')
    idx = SourceIndex(source_md_text)
    print(f'source.md: {len(idx.source_lines)} lines indexed\n', file=sys.stderr)

    # First pass: every normalized verbatim value anywhere in the course.
    # Needed to tell apart the two things that look identical at the
    # surface (our value found next to a '/' in the source) but mean
    # opposite things: (a) one bullet/field genuinely sliced in half —
    # the real, confirmed bug this script targets — vs. (b) the source
    # printing TWO EDITIONS' alternate wording of the same slot side by
    # side ("a) wird eine neue Cateringfirma gesucht. / sollen sofort
    # Einladungen verschickt werden.", each half belonging to a
    # DIFFERENT edition of the same question) — confirmed live on
    # hoeren_teil1: this is expected, documented VERSIONS behavior, not
    # truncation, and each edition already correctly holds only its own
    # half elsewhere in the course. If the "other half" of a slash-split
    # match is itself present as some OTHER field's own value in this
    # same course, (b) is far more likely than (a) — don't flag it.
    all_values: set[str] = set()
    for section_type, item in iter_items(sections, args.section_type):
        for _, value in iter_verbatim_values(item, VERBATIM_FIELDS[section_type]):
            nv = _normalize(value)
            if nv:
                all_values.add(nv)

    counts: Counter[str] = Counter()
    for section_type, item in iter_items(sections, args.section_type):
        specs = VERBATIM_FIELDS[section_type]
        for path, value in iter_verbatim_values(item, specs):
            if not value or value == SENTINEL:
                counts['SKIPPED_SENTINEL'] += 1
                continue
            norm_value = _normalize(value)
            if not norm_value:
                continue
            label = _version_label(item, path)
            qname = f'{section_type} / {label} / {path}'

            trunc = check_truncated(norm_value, idx)
            if trunc is not None:
                lo, hi, candidate, other_halves = trunc
                if any(oh in all_values for oh in other_halves):
                    counts['SPLIT_ACROSS_EDITIONS'] += 1
                    continue  # the other half is some OTHER field's own
                              # verbatim value elsewhere in this course —
                              # a correctly-split multi-edition pair, not
                              # a truncation bug; not worth printing
                counts['TRUNCATED_AT_SLASH'] += 1
                print(f'TRUNCATED_AT_SLASH  {qname}\n'
                      f'    current: {value!r}\n'
                      f'    found near source.md line {lo}'
                      + (f'-{hi}' if hi != lo else '') + f': {candidate!r}')
                continue  # don't also run the hallucination check on a
                          # value we've already positively located in the
                          # source — it's not hallucinated, just truncated

            if check_hallucinated(norm_value, idx):
                counts['HALLUCINATED'] += 1
                print(f'HALLUCINATED  {qname}\n'
                      f'    value: {value[:120]!r}{"..." if len(value) > 120 else ""}')
                continue

            counts['OK'] += 1

    print('\nSummary:', dict(counts))
    sys.exit(1 if (counts['HALLUCINATED'] or counts['TRUNCATED_AT_SLASH']) else 0)


if __name__ == '__main__':
    main()
