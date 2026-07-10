"""Custom promptfoo assertions targeting the exact regressions this
pipeline has actually hit (not generic "looks reasonable" checks):
- 2.5 Flash-Lite silently dropped the 'other' filler-block markers
  during discovery, reintroducing the runaway-chunk bug.
- Batched parse calls silently collapsed multiple distinct editions
  into fewer output objects than discovery found.
- The DEDUPLICATION sentinel leaking where it shouldn't (retired
  system-wide now that every type is schema-enforced — kept as a
  regression guard in case a future prompt edit reintroduces it).
- 'texts' coming back empty and question arrays short of the official
  count (5/8 for hoeren_teil4, 1/2 for beschwerde) — inconsistent across
  retries of the identical input under free-form generation; this is
  what response_schemas.py's minItems/maxItems now exists to prevent.
- A single already-numbered question getting a later "Варианты ответов
  от <date>"-style answer correction — discovery used to (mis)treat this
  as marking a whole new edition, splitting one variant's content across
  chunks and starving the real one of its own questions/texts.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from response_schemas import _UNIVERSAL_QUESTION_COUNTS  # noqa: E402


def _parse(output):
    text = output.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    return json.loads(text)


def other_markers_present(output, context):
    """Discovery must keep emitting 'other' filler-block markers —
    without them, a real exercise's boundary silently swallows the
    filler section that follows it."""
    try:
        items = _parse(output)
    except Exception as e:
        return {'pass': False, 'score': 0, 'reason': f'invalid JSON: {e}'}
    other = [i for i in items if i.get('section_type') == 'other']
    if not other:
        return {'pass': False, 'score': 0,
                'reason': 'no "other" filler markers found — regression risk '
                          'for the runaway-chunk bug'}
    return {'pass': True, 'score': 1,
            'reason': f'{len(other)} "other" markers found'}


def item_count_at_least(output, context):
    """Discovery must find at least as many real items as a known
    floor for this fixture (set via the test's `min_items` var) — a
    sharp drop signals the model is under-recognizing exercises."""
    try:
        items = _parse(output)
    except Exception as e:
        return {'pass': False, 'score': 0, 'reason': f'invalid JSON: {e}'}
    minimum = int(context['vars'].get('min_items', 1))
    real = [i for i in items if i.get('section_type') != 'other']
    ok = len(real) >= minimum
    return {'pass': ok, 'score': 1 if ok else 0,
            'reason': f'{len(real)} real items found (floor: {minimum})'}


def original_has_no_sentinel(output, context):
    """The original edition (version: null) must always be fully
    self-contained — if the sentinel leaks in there, expansion on the
    client has nothing to copy from and the placeholder reaches the UI."""
    try:
        objects = _parse(output)
    except Exception as e:
        return {'pass': False, 'score': 0, 'reason': f'invalid JSON: {e}'}
    for obj in objects:
        if obj.get('version') is not None:
            continue
        leaks = _find_sentinel(obj, '')
        if leaks:
            return {'pass': False, 'score': 0,
                    'reason': f'sentinel leaked into original at: {leaks}'}
    return {'pass': True, 'score': 1, 'reason': 'original variant(s) clean'}


def _find_sentinel(value, path):
    hits = []
    if value == '<<SAME_AS_ORIGINAL>>':
        hits.append(path or '<root>')
    elif isinstance(value, dict):
        for k, v in value.items():
            hits += _find_sentinel(v, f'{path}.{k}')
    elif isinstance(value, list):
        for i, v in enumerate(value):
            hits += _find_sentinel(v, f'{path}[{i}]')
    return hits


def question_pairs_exactly_three(output, context):
    """hoeren_teil1's own schema rule: every variant/edition object has
    exactly 3 question_pairs, no more, no less. A no-op pass for every
    other section type, which doesn't have this rule."""
    if context['vars'].get('section_type') != 'hoeren_teil1':
        return {'pass': True, 'score': 1, 'reason': 'not applicable to this section type'}
    try:
        objects = _parse(output)
    except Exception as e:
        return {'pass': False, 'score': 0, 'reason': f'invalid JSON: {e}'}
    bad = [
        (o.get('variant_number'), o.get('version'), len(o.get('question_pairs', [])))
        for o in objects
        if len(o.get('question_pairs', [])) != 3
    ]
    if bad:
        return {'pass': False, 'score': 0,
                'reason': f'objects with != 3 question_pairs: {bad}'}
    return {'pass': True, 'score': 1, 'reason': 'all objects have exactly 3 pairs'}


def editions_have_content(output, context):
    """Every reworked edition (version is not null) must still carry
    real quiz content — 'questions' for the universal schema, 'answers'
    for sprachbausteine_teil1, 'versions' for telefonnotiz. Catches the
    other failure direction: deduplication swallowing content it
    shouldn't have touched, leaving an edition structurally present but
    practically empty."""
    section_type = context['vars'].get('section_type')
    try:
        objects = _parse(output)
    except Exception as e:
        return {'pass': False, 'score': 0, 'reason': f'invalid JSON: {e}'}

    content_field = {
        'sprachbausteine_teil1': 'answers',
        'telefonnotiz': 'versions',
        'hoeren_teil1': 'question_pairs',
    }.get(section_type, 'questions')

    empty = []
    for o in objects:
        if section_type == 'telefonnotiz':
            # telefonnotiz nests editions under one object's "versions" —
            # check each entry there, not top-level "version"
            for v in o.get('versions', []):
                if not v.get('monologue') and not v.get('answer'):
                    empty.append((o.get('variant_number'), v.get('label')))
            continue
        if o.get('version') is None:
            continue
        if not o.get(content_field):
            empty.append((o.get('variant_number'), o.get('version')))

    if empty:
        return {'pass': False, 'score': 0,
                'reason': f'editions with no {content_field}: {empty}'}
    return {'pass': True, 'score': 1, 'reason': 'all editions have content'}


def expected_question_count(output, context):
    """Every object of a universal-schema section type must have EXACTLY
    the official telc question count (response_schemas.py's own
    _UNIVERSAL_QUESTION_COUNTS — same source of truth production reads,
    so this can't silently drift from what's actually enforced). No-op
    for section types outside that schema (hoeren_teil1, telefonnotiz,
    sprachbausteine_teil1 have their own shapes/checks)."""
    section_type = context['vars'].get('section_type')
    expected = _UNIVERSAL_QUESTION_COUNTS.get(section_type)
    if expected is None:
        return {'pass': True, 'score': 1, 'reason': 'not a universal-schema section type'}
    try:
        objects = _parse(output)
    except Exception as e:
        return {'pass': False, 'score': 0, 'reason': f'invalid JSON: {e}'}
    bad = [
        (o.get('variant_number'), o.get('version'), len(o.get('questions', [])))
        for o in objects
        if len(o.get('questions', [])) != expected
    ]
    if bad:
        return {'pass': False, 'score': 0,
                'reason': f'expected {expected} questions, got: {bad}'}
    return {'pass': True, 'score': 1, 'reason': f'all objects have exactly {expected} questions'}


def texts_not_empty(output, context):
    """Every object of a universal-schema section type must have a
    non-empty 'texts' array — this is the exact shape the 'reading
    passage/transcript is missing' production failures took (Beschwerde,
    Hören Teil 3), which schema enforcement's minItems:1 now exists to
    make structurally impossible."""
    section_type = context['vars'].get('section_type')
    if section_type not in _UNIVERSAL_QUESTION_COUNTS:
        return {'pass': True, 'score': 1, 'reason': 'not a universal-schema section type'}
    try:
        objects = _parse(output)
    except Exception as e:
        return {'pass': False, 'score': 0, 'reason': f'invalid JSON: {e}'}
    empty = [
        (o.get('variant_number'), o.get('version'))
        for o in objects
        if not o.get('texts')
    ]
    if empty:
        return {'pass': False, 'score': 0, 'reason': f'objects with empty texts: {empty}'}
    return {'pass': True, 'score': 1, 'reason': 'all objects have non-empty texts'}


def single_question_correction_not_split(output, context):
    """Discovery regression check for the exact bug this pipeline hit
    twice in production: a label like "Варианты ответов от <date>" right
    after ONE already-numbered question is a correction to that
    question's answer, not a new edition — it must NOT get its own
    discovered item. The fixture this runs against
    (regression_fixtures/discover_single_question_correction.txt) embeds
    exactly two real variants (one hoeren_teil4, one beschwerde), each
    containing one such correction — so discovery must report exactly 2
    real items, not 4."""
    try:
        items = _parse(output)
    except Exception as e:
        return {'pass': False, 'score': 0, 'reason': f'invalid JSON: {e}'}
    real = [i for i in items if i.get('section_type') != 'other']
    ok = len(real) == 2
    return {'pass': ok, 'score': 1 if ok else 0,
            'reason': f'{len(real)} real item(s) found (expected exactly 2 — a '
                      f'single-question correction must not split a variant): {real}'}
