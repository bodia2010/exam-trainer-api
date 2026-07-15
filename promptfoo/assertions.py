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
from response_schemas import SPAN_TEXT_SECTION_TYPES, _UNIVERSAL_QUESTION_COUNTS  # noqa: E402
import line_extraction  # noqa: E402


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


def item_count_exactly_when_set(output, context):
    """Some fixtures have a semantic exact count that cannot be inferred
    from the number of ``<<<ITEM>>>`` chunks. Discovery may split one real
    edition around single-question correction blocks, while parse must merge
    those chunks back into exactly one object. Tests opt in with the
    ``expected_items`` variable; every other test is a no-op."""
    expected_raw = context['vars'].get('expected_items')
    if expected_raw is None:
        return {'pass': True, 'score': 1, 'reason': 'no exact item count configured'}
    try:
        items = _parse(output)
        expected = int(expected_raw)
    except Exception as e:
        return {'pass': False, 'score': 0, 'reason': f'invalid exact-count input/output: {e}'}
    real = [i for i in items if i.get('section_type') != 'other']
    ok = len(real) == expected
    return {'pass': ok, 'score': 1 if ok else 0,
            'reason': f'{len(real)} real items found (exactly expected: {expected})'}


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


def span_texts_resolve_cleanly(output, context):
    """Line-span text sections must return pointers that resolve through
    the real production helper, not retyped text. This catches the two
    practical bad shapes: spans that accidentally swallow the following
    question/options block, and hoeren_teil4 collapsing five separate
    phone messages into one shared transcript span."""
    section_type = context['vars'].get('section_type')
    if section_type not in SPAN_TEXT_SECTION_TYPES:
        return {'pass': True, 'score': 1, 'reason': 'not a span-text section type'}
    try:
        objects = _parse(output)
    except Exception as e:
        return {'pass': False, 'score': 0, 'reason': f'invalid JSON: {e}'}

    raw_lines = context['vars']['markdown'].split('\n')
    failures = []
    resolved_count = 0

    for obj in objects:
        texts = obj.get('texts')
        obj_label = (obj.get('variant_number'), obj.get('version'))
        if not isinstance(texts, list) or not texts:
            failures.append(f'object {obj_label} has no texts array')
            continue

        span_keys = []
        resolved_values = []
        for text_index, text in enumerate(texts):
            path = f'object {obj_label} texts[{text_index}]'
            if not isinstance(text, dict):
                failures.append(f'{path} is not an object: {text!r}')
                continue
            try:
                start = text['start_line']
                end = text['end_line']
            except KeyError:
                failures.append(f'{path} has invalid start_line/end_line: {text!r}')
                continue

            if not _is_plain_int(start) or not _is_plain_int(end):
                failures.append(f'{path} has non-integer start_line/end_line: {text!r}')
                continue

            heading_lines = _validate_heading_lines(
                text.get('heading_lines'), path, failures, start, end, len(raw_lines))
            if heading_lines is None:
                continue

            if start == -1 and end == -1:
                resolved = '(nicht angegeben)'
            elif start < 0 or end < 0:
                failures.append(f'{path} has a negative non-sentinel span: {text!r}')
                continue
            elif end < start:
                failures.append(f'{path} has an inverted span: {text!r}')
                continue
            elif start >= len(raw_lines) or end >= len(raw_lines):
                failures.append(
                    f'{path} span is out of range for {len(raw_lines)} source line(s): {text!r}')
                continue
            else:
                resolved = line_extraction.extract_block(
                    raw_lines, start, end, heading_lines=heading_lines)
                if not resolved:
                    failures.append(f'{path} resolves to empty content: {text!r}')
                bad_line = _first_question_or_option_line(resolved)
                if bad_line is not None:
                    failures.append(f'{path} includes question/option-looking line: {bad_line!r}')
                if _source_span_has_multiple_content_lines(raw_lines, start, end) and '\n' not in resolved:
                    failures.append(f'{path} source span is multi-line but resolved text has no newline')
                resolved_count += 1

            span_keys.append((start, end, tuple(heading_lines)))
            resolved_values.append(resolved)

        if section_type == 'hoeren_teil4':
            if len(texts) != 5:
                failures.append(f'object {obj_label} has {len(texts)} texts, expected 5')
            if len(set(span_keys)) != len(span_keys):
                failures.append(f'object {obj_label} reuses at least one text span: {span_keys}')
            if len(set(resolved_values)) != len(resolved_values):
                failures.append(f'object {obj_label} has duplicate resolved text entries')

    if failures:
        return {'pass': False, 'score': 0, 'reason': '; '.join(failures[:8])}
    return {'pass': True, 'score': 1,
            'reason': f'{resolved_count} span-backed text(s) resolved cleanly'}


def _is_plain_int(value):
    return type(value) is int


def _validate_heading_lines(value, path, failures, start, end, raw_line_count):
    if value is None:
        return []
    if not isinstance(value, list):
        failures.append(f'{path} heading_lines is not a list/null: {value!r}')
        return None
    headings = []
    for item in value:
        if not _is_plain_int(item):
            failures.append(f'{path} heading_lines contains non-integer value: {item!r}')
            continue
        if item < 0 or item >= raw_line_count or item < start or item > end:
            failures.append(
                f'{path} heading_lines entry is outside the text span/source lines: {item!r}')
            continue
        headings.append(item)
    if len(headings) != len(value):
        return None
    return headings


def _first_question_or_option_line(resolved):
    for line in resolved.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        if _looks_like_question_or_option(stripped):
            return stripped
    return None


def _looks_like_question_or_option(line):
    return (
        (len(line) > 3 and line[0].isdigit() and '. ' in line[:5])
        or line.startswith(('a) ', 'b) ', 'c) ', 'd) ', 'e) ', 'f) '))
    )


def _source_span_has_multiple_content_lines(raw_lines, start_line, end_line):
    if not raw_lines or start_line < 0 or end_line < 0:
        return False
    start = max(0, min(start_line, len(raw_lines) - 1))
    end = max(0, min(end_line, len(raw_lines) - 1))
    if end < start:
        start, end = end, start
    return sum(1 for line in raw_lines[start:end + 1] if line.strip()) > 1


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


def split_slash_variant_number_not_concatenated(output, context):
    """Discovery regression check for the exact bug this pipeline hit in
    production: a printed variant number of the form "N/M" (e.g.
    "вариант №3/1", meaning the M-th edition of variant N) got its digits
    concatenated into a brand-new integer variant_number (31) instead of
    being recognized as variant N's own second edition. The fixture this
    runs against (regression_fixtures/discover_split_slash_variant_number.txt)
    embeds a "Telefonnotiz (вариант №5)" original and a
    "Telefonnotiz (вариант №5/1)" edition with different content — so
    discovery must report exactly 2 real telefonnotiz items, BOTH with
    variant_number == 5 (one version_label null, one not), and must NOT
    report any item with variant_number == 51 or any other concatenated/
    non-5 number."""
    try:
        items = _parse(output)
    except Exception as e:
        return {'pass': False, 'score': 0, 'reason': f'invalid JSON: {e}'}
    real = [i for i in items if i.get('section_type') != 'other']
    telefonnotiz = [i for i in real if i.get('section_type') == 'telefonnotiz']
    bad_numbers = [i for i in telefonnotiz if i.get('variant_number') != 5]
    if bad_numbers:
        return {'pass': False, 'score': 0,
                'reason': f'expected every telefonnotiz item to have '
                          f'variant_number == 5, got: {bad_numbers}'}
    if len(telefonnotiz) != 2:
        return {'pass': False, 'score': 0,
                'reason': f'expected exactly 2 telefonnotiz items (original '
                          f'+ "5/1" edition), got {len(telefonnotiz)}: {telefonnotiz}'}
    originals = [i for i in telefonnotiz if i.get('version_label') is None]
    editions = [i for i in telefonnotiz if i.get('version_label') is not None]
    if len(originals) != 1 or len(editions) != 1:
        return {'pass': False, 'score': 0,
                'reason': f'expected 1 original (version_label null) and 1 '
                          f'edition (version_label set), got originals='
                          f'{originals}, editions={editions}'}
    return {'pass': True, 'score': 1,
            'reason': f'both editions correctly reported under variant_number 5: {telefonnotiz}'}


def telefonnotiz_shared_answer_block_splits_correctly(output, context):
    """Parse regression check for the shared-answer-block discovery from
    2026-07-14: this source sometimes prints ONE answer-key block shared
    by several editions, each field's value joined by "/" in printed
    order — different from a single edition's own bullet that legitimately
    contains "/" as two alternate readings of the SAME fact (which must
    stay joined). The fixture this runs against
    (regression_fixtures/telefonnotiz_shared_answer_block_slash_index.txt)
    embeds exactly that: 2 editions ("Alte Version" / "Neue Version"),
    ONE shared "Weitere Informationen:"/"Name:"/"Telefonnummer:" block
    printed once, each field's value a "/"-joined pair.

    Resolves weitere_informationen's {start_line, end_line, slash_index}
    pointers with the REAL line_extraction.extract_span (not a
    reimplementation) against the fixture's own raw lines, then checks:
    both editions must end up with DIFFERENT, non-"/"-containing text for
    the shared bullets (proving slash_index actually split the block,
    matching each edition's own monologue) — a regression that stops
    resolving slash_index would instead leave both editions with the
    same full "X / Y" text, or a raw "/" leaking through."""
    if context['vars'].get('section_type') != 'telefonnotiz':
        return {'pass': True, 'score': 1, 'reason': 'not applicable to this section type'}
    try:
        objects = _parse(output)
    except Exception as e:
        return {'pass': False, 'score': 0, 'reason': f'invalid JSON: {e}'}

    raw_lines = context['vars']['markdown'].split('\n')
    variant = next((o for o in objects if o.get('variant_number') == 12), None)
    if variant is None:
        return {'pass': False, 'score': 0,
                'reason': f'no variant_number == 12 object found: {objects}'}
    versions = variant.get('versions') or []
    if len(versions) != 2:
        return {'pass': False, 'score': 0,
                'reason': f'expected exactly 2 versions (one per edition), got '
                          f'{len(versions)}: {versions}'}

    resolved = []
    for v in versions:
        answer = v.get('answer') or {}
        spans = answer.get('weitere_informationen')
        if not isinstance(spans, list) or not spans:
            return {'pass': False, 'score': 0,
                    'reason': f'version {v.get("label")!r} has no weitere_informationen spans: {answer}'}
        texts = []
        for span in spans:
            if not isinstance(span, dict) or 'start_line' not in span or 'end_line' not in span:
                return {'pass': False, 'score': 0,
                         'reason': f'weitere_informationen entry is not a {{start_line, end_line}} '
                                   f'span (retyped text instead?): {span!r}'}
            try:
                start, end = int(span['start_line']), int(span['end_line'])
            except (TypeError, ValueError):
                return {'pass': False, 'score': 0, 'reason': f'non-integer span: {span!r}'}
            slash_index = span.get('slash_index')
            texts.append(line_extraction.extract_span(
                raw_lines, start, end, strip_bullet=True,
                slash_index=int(slash_index) if slash_index is not None else None))
        resolved.append((v.get('label'), texts, answer.get('name')))

    (label_a, texts_a, name_a), (label_b, texts_b, name_b) = resolved

    leaked_slash = [t for t in texts_a + texts_b if '/' in t]
    if leaked_slash:
        return {'pass': False, 'score': 0,
                'reason': f'resolved bullet text still contains a raw "/" — slash_index was '
                          f'not applied: {leaked_slash}'}

    if texts_a == texts_b:
        return {'pass': False, 'score': 0,
                'reason': f'both editions resolved to IDENTICAL weitere_informationen '
                          f'({texts_a}) — the shared block was not split per-edition '
                          f'(slash_index ignored or both set the same)'}

    if not name_a or not name_b or name_a == name_b or '/' in (name_a or '') or '/' in (name_b or ''):
        return {'pass': False, 'score': 0,
                'reason': f'plain-text "name" field not correctly resolved per edition: '
                          f'{label_a}={name_a!r}, {label_b}={name_b!r}'}

    return {'pass': True, 'score': 1,
            'reason': f'shared answer block correctly split per edition: '
                      f'{label_a}={texts_a}/{name_a!r}, {label_b}={texts_b}/{name_b!r}'}


def telefonnotiz_no_bullets_sentinel(output, context):
    """Parse regression check for the [{"start_line": -1, "end_line": -1}]
    sentinel prompts.py defines for "this edition genuinely prints no
    Weitere Informationen: bullets at all" — must not be confused with
    the ordinary case (real bullets present) or degrade into an empty
    list / omitted field, which main.py's _resolve_telefonnotiz_spans
    only recognizes as the (nicht angegeben) case via this EXACT
    sentinel position. The fixture this runs against
    (regression_fixtures/telefonnotiz_no_bullets_sentinel.txt) has a
    single edition whose answer key goes straight from "Name:"/
    "Telefonnummer:" to "Zu erledigen:" with no "Weitere Informationen:"
    label printed at all — a real, if less common, shape this source
    uses."""
    if context['vars'].get('section_type') != 'telefonnotiz':
        return {'pass': True, 'score': 1, 'reason': 'not applicable to this section type'}
    try:
        objects = _parse(output)
    except Exception as e:
        return {'pass': False, 'score': 0, 'reason': f'invalid JSON: {e}'}

    variant = next((o for o in objects if o.get('variant_number') == 27), None)
    if variant is None:
        return {'pass': False, 'score': 0,
                'reason': f'no variant_number == 27 object found: {objects}'}
    versions = variant.get('versions') or []
    if len(versions) != 1:
        return {'pass': False, 'score': 0,
                'reason': f'expected exactly 1 version, got {len(versions)}: {versions}'}

    answer = versions[0].get('answer') or {}
    spans = answer.get('weitere_informationen')
    if spans != [{'start_line': -1, 'end_line': -1}]:
        return {'pass': False, 'score': 0,
                'reason': f'expected the sentinel [{{"start_line": -1, "end_line": -1}}] for a '
                          f'genuinely bullet-less edition, got: {spans!r}'}
    return {'pass': True, 'score': 1, 'reason': 'no-bullets sentinel correctly reported'}
