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
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))
from response_schemas import SPAN_TEXT_SECTION_TYPES, _UNIVERSAL_QUESTION_COUNTS  # noqa: E402
import line_extraction  # noqa: E402
import span_resolution  # noqa: E402
from fixture_loader import load_markdown  # noqa: E402


_VOICE_GENDERS = {'female', 'male', 'unknown'}


def _not_applicable(reason):
    return {'pass': True, 'score': 1, 'reason': reason}


def _voice_failure(reason):
    return {'pass': False, 'score': 0, 'reason': reason}


def _voice_success(reason):
    return {'pass': True, 'score': 1, 'reason': reason}


def _objects_or_failure(output):
    try:
        objects = _parse(output)
    except Exception as e:
        return None, _voice_failure(f'invalid JSON: {e}')
    if not isinstance(objects, list):
        return None, _voice_failure('top-level output is not a JSON array')
    return objects, None


def hoeren_teil4_voice_metadata_matches_fixture(output, context):
    """The existing real Hören Teil 4 eval must exercise all three hints.

    ``expected_text_voice_genders`` is deliberately fixture-owned rather
    than inferred from arbitrary names here. This assertion is a regression
    contract for that one known fixture, not a second production gender
    classifier which could repeat the bug the metadata feature removes.
    Tests without the variable remain no-ops, so the two unrelated Hören
    Teil 4 span/correction fixtures do not create additional paid calls.
    """
    if context['vars'].get('section_type') != 'hoeren_teil4':
        return _not_applicable('not Hören Teil 4')
    expected_raw = context['vars'].get('expected_text_voice_genders')
    if expected_raw is None:
        return _not_applicable('no fixture voice-gender expectation configured')
    try:
        expected = json.loads(expected_raw) if isinstance(expected_raw, str) else expected_raw
    except Exception as e:
        return _voice_failure(f'invalid expected_text_voice_genders: {e}')
    if not isinstance(expected, list) or not expected or any(
            gender not in _VOICE_GENDERS for gender in expected):
        return _voice_failure(
            'expected_text_voice_genders must be a non-empty female/male/unknown array')

    objects, failure = _objects_or_failure(output)
    if failure:
        return failure
    try:
        markdown = load_markdown(context)
        objects = span_resolution.apply_contextual_voice_metadata(
            objects,
            markdown,
            'hoeren_teil4',
        )
    except Exception as e:
        return _voice_failure(f'cannot apply contextual Hören Teil 4 metadata: {e}')
    actual = []
    for object_index, obj in enumerate(objects):
        if not isinstance(obj, dict):
            return _voice_failure(f'object[{object_index}] is not an object')
        for text_index, text in enumerate(obj.get('texts') or []):
            path = f'object[{object_index}].texts[{text_index}]'
            if not isinstance(text, dict):
                return _voice_failure(f'{path} is not an object')
            metadata = text.get('metadata')
            if not isinstance(metadata, dict):
                return _voice_failure(f'{path} is missing metadata')
            gender = metadata.get('voice_gender')
            if gender not in _VOICE_GENDERS:
                return _voice_failure(f'{path} has invalid/missing voice_gender: {gender!r}')
            if metadata.get('speaker_voice_genders'):
                return _voice_failure(
                    f'{path} is a monologue but contains speaker_voice_genders')
            actual.append(gender)

    if actual != expected:
        return _voice_failure(
            f'Hören Teil 4 voice genders {actual!r}, expected {expected!r}')
    return _voice_success(f'Hören Teil 4 voice genders match: {actual!r}')


_DIALOGUE_SPEAKER_RE = re.compile(
    r'(?:^|\n)\s*([A-ZÄÖÜ][\wÄÖÜäöüß]*(?:\s+[A-ZÄÖÜ0-9][\wÄÖÜ0-9äöüß]*)?)'
    r'\s*(?::|-)\s*',
    re.MULTILINE,
)
_FEMALE_ROLE_LABELS = {'frau', 'kundin', 'chefin', 'leiterin', 'verkäuferin'}
_MALE_ROLE_LABELS = {
    'herr', 'kunde', 'chef', 'leiter', 'teamleiter', 'verkäufer',
}


def _expected_role_gender(speaker):
    first = speaker.casefold().split()[0]
    if first in _FEMALE_ROLE_LABELS:
        return 'female'
    if first in _MALE_ROLE_LABELS:
        return 'male'
    return 'unknown'


def hoeren_teil1_speaker_voice_metadata_exact(output, context):
    """Every labelled Hören Teil 1 turn needs one exact, non-invented hint."""
    if context['vars'].get('section_type') != 'hoeren_teil1':
        return _not_applicable('not Hören Teil 1')
    expected_raw = context['vars'].get('expected_dialogue_speaker_sets')
    if expected_raw is None:
        return _voice_failure('no expected_dialogue_speaker_sets configured')
    try:
        expected_lists = (
            json.loads(expected_raw) if isinstance(expected_raw, str) else expected_raw
        )
    except Exception as e:
        return _voice_failure(f'invalid expected_dialogue_speaker_sets: {e}')
    if (
            not isinstance(expected_lists, list)
            or not expected_lists
            or any(
                not isinstance(labels, list)
                or not labels
                or any(not isinstance(label, str) or not label for label in labels)
                or len(set(labels)) != len(labels)
                for labels in expected_lists)):
        return _voice_failure(
            'expected_dialogue_speaker_sets must be a non-empty array of '
            'non-empty unique string arrays')
    expected_sets = [frozenset(labels) for labels in expected_lists]
    if len(set(expected_sets)) != len(expected_sets):
        return _voice_failure('expected_dialogue_speaker_sets contains duplicates')
    optional_raw = context['vars'].get('optional_dialogue_speaker_sets', [])
    try:
        optional_lists = (
            json.loads(optional_raw) if isinstance(optional_raw, str) else optional_raw
        )
    except Exception as e:
        return _voice_failure(f'invalid optional_dialogue_speaker_sets: {e}')
    if not isinstance(optional_lists, list) or any(
            not isinstance(labels, list)
            or not labels
            or any(not isinstance(label, str) or not label for label in labels)
            for labels in optional_lists):
        return _voice_failure(
            'optional_dialogue_speaker_sets must be an array of non-empty string arrays')
    optional_sets = [frozenset(labels) for labels in optional_lists]
    allowed_sets = set(expected_sets) | set(optional_sets)

    objects, failure = _objects_or_failure(output)
    if failure:
        return failure
    try:
        markdown = load_markdown(context)
        objects = span_resolution.normalize_h1_variant_numbers(objects, markdown)
    except Exception as e:
        return _voice_failure(f'cannot normalize Hören Teil 1 variants: {e}')
    expected_object_count = context['vars'].get('expected_items')
    if expected_object_count is not None:
        try:
            expected_object_count = int(expected_object_count)
        except (TypeError, ValueError) as e:
            return _voice_failure(f'invalid expected_items: {e}')
        if len(objects) != expected_object_count:
            return _voice_failure(
                f'Hören Teil 1 output has {len(objects)} object(s), expected exactly '
                f'{expected_object_count} complete edition(s)')
    if _contains_placeholder(objects):
        return _voice_failure('Hören Teil 1 output contains fabricated placeholder content')
    expected_variant_raw = context['vars'].get('expected_variant_number')
    if expected_variant_raw is not None:
        try:
            expected_variant = int(expected_variant_raw)
        except (TypeError, ValueError) as e:
            return _voice_failure(f'invalid expected_variant_number: {e}')
        wrong_variants = [
            obj.get('variant_number') if isinstance(obj, dict) else None
            for obj in objects
            if not isinstance(obj, dict) or obj.get('variant_number') != expected_variant
        ]
        if wrong_variants:
            return _voice_failure(
                f'Hören Teil 1 contains variant numbers outside '
                f'{expected_variant}: {wrong_variants!r}')
    expected_variants_raw = context['vars'].get('expected_variant_numbers')
    if expected_variants_raw is not None:
        try:
            expected_variants = (
                json.loads(expected_variants_raw)
                if isinstance(expected_variants_raw, str)
                else expected_variants_raw
            )
        except Exception as e:
            return _voice_failure(f'invalid expected_variant_numbers: {e}')
        actual_variants = [
            obj.get('variant_number') if isinstance(obj, dict) else None
            for obj in objects
        ]
        if actual_variants != expected_variants:
            return _voice_failure(
                f'Hören Teil 1 variant numbers {actual_variants!r}, '
                f'expected {expected_variants!r}')
    checked_pairs = 0
    seen_sets = set()
    for object_index, obj in enumerate(objects):
        if not isinstance(obj, dict):
            return _voice_failure(f'object[{object_index}] is not an object')
        for pair_index, pair in enumerate(obj.get('question_pairs') or []):
            if not isinstance(pair, dict):
                return _voice_failure(
                    f'object[{object_index}].question_pairs[{pair_index}] is not an object')
            dialogue = pair.get('dialogue')
            path = f'object[{object_index}].question_pairs[{pair_index}]'
            if not isinstance(dialogue, str) or not dialogue.strip():
                return _voice_failure(f'{path} has missing/empty dialogue')
            speakers = list(dict.fromkeys(
                match.group(1).strip() for match in _DIALOGUE_SPEAKER_RE.finditer(dialogue)))
            if not speakers:
                return _voice_failure(f'{path} has no parseable speaker labels')
            speaker_set = frozenset(speakers)
            if speaker_set not in allowed_sets:
                return _voice_failure(
                    f'{path} speaker set {sorted(speaker_set)!r} is not one of the '
                    f'fixture-owned expected sets '
                    f'{[sorted(labels) for labels in allowed_sets]!r}')
            seen_sets.add(speaker_set)
            checked_pairs += 1
            metadata = pair.get('metadata')
            if not isinstance(metadata, dict):
                return _voice_failure(f'{path} is missing metadata')
            if metadata.get('voice_gender') is not None:
                return _voice_failure(
                    f'{path} is a labelled dialogue but contains recording-wide voice_gender')
            hints = metadata.get('speaker_voice_genders')
            if not isinstance(hints, list):
                return _voice_failure(f'{path} is missing speaker_voice_genders')

            actual = {}
            for hint_index, hint in enumerate(hints):
                if not isinstance(hint, dict):
                    return _voice_failure(f'{path}.speaker_voice_genders[{hint_index}] is invalid')
                speaker = hint.get('speaker')
                gender = hint.get('voice_gender')
                if not isinstance(speaker, str) or not speaker:
                    return _voice_failure(f'{path} contains an empty/non-string speaker')
                if speaker in actual:
                    return _voice_failure(f'{path} contains duplicate speaker {speaker!r}')
                if gender not in _VOICE_GENDERS:
                    return _voice_failure(
                        f'{path} speaker {speaker!r} has invalid voice_gender {gender!r}')
                actual[speaker] = gender

            expected = {speaker: _expected_role_gender(speaker) for speaker in speakers}
            if actual != expected:
                invented = sorted(set(actual) - set(expected))
                missing = sorted(set(expected) - set(actual))
                wrong = {
                    speaker: (actual.get(speaker), gender)
                    for speaker, gender in expected.items()
                    if speaker in actual and actual[speaker] != gender
                }
                return _voice_failure(
                    f'{path} speaker metadata mismatch; invented={invented!r}, '
                    f'missing={missing!r}, wrong={wrong!r}')

    if checked_pairs == 0:
        return _voice_failure('Hören Teil 1 output contained no labelled dialogue pairs')
    missing_sets = [
        sorted(labels) for labels in expected_sets if labels not in seen_sets
    ]
    if missing_sets:
        return _voice_failure(
            f'Hören Teil 1 output is missing expected speaker sets: {missing_sets!r}')
    return _voice_success(
        f'{checked_pairs} Hören Teil 1 dialogue pair(s) have exact speaker metadata '
        f'and cover all {len(expected_sets)} required fixture sets')


def telefonnotiz_nested_voice_metadata_matches_fixture(output, context):
    """Match every nested Telefonnotiz version to its fixture-owned hint."""
    if context['vars'].get('section_type') != 'telefonnotiz':
        return _not_applicable('not Telefonnotiz')
    expected_raw = context['vars'].get('expected_version_voice_genders')
    if expected_raw is None:
        return _not_applicable('no fixture voice-gender expectations configured')
    try:
        expected = (
            json.loads(expected_raw) if isinstance(expected_raw, str) else expected_raw
        )
    except Exception as e:
        return _voice_failure(f'invalid expected_version_voice_genders: {e}')
    if (
            not isinstance(expected, list)
            or not expected
            or any(gender not in _VOICE_GENDERS for gender in expected)):
        return _voice_failure(
            'expected_version_voice_genders must be a non-empty '
            'female/male/unknown array')

    objects, failure = _objects_or_failure(output)
    if failure:
        return failure
    expected_variant_raw = context['vars'].get('expected_variant_number')
    if expected_variant_raw is not None:
        try:
            expected_variant = int(expected_variant_raw)
        except (TypeError, ValueError) as e:
            return _voice_failure(f'invalid expected_variant_number: {e}')
        if len(objects) != 1:
            return _voice_failure(
                'Telefonnotiz fixture editions must be grouped into exactly one '
                f'variant {expected_variant} object, got {len(objects)} objects')
        actual_variant = (
            objects[0].get('variant_number')
            if isinstance(objects[0], dict)
            else None
        )
        if type(actual_variant) is not int or actual_variant != expected_variant:
            return _voice_failure(
                f'Telefonnotiz variant_number is {actual_variant!r}, expected '
                f'{expected_variant}; split-slash edition numbers must not be concatenated')
    actual = []
    for object_index, obj in enumerate(objects):
        if not isinstance(obj, dict):
            return _voice_failure(f'object[{object_index}] is not an object')
        for version_index, version in enumerate(obj.get('versions') or []):
            if not isinstance(version, dict):
                return _voice_failure(
                    f'object[{object_index}].versions[{version_index}] is not an object')
            path = f'object[{object_index}].versions[{version_index}]'
            metadata = version.get('metadata')
            if not isinstance(metadata, dict):
                return _voice_failure(f'{path} is missing metadata')
            gender = metadata.get('voice_gender')
            if gender not in _VOICE_GENDERS:
                return _voice_failure(
                    f'{path} has invalid/missing voice_gender: {gender!r}')
            if metadata.get('speaker_voice_genders'):
                return _voice_failure(
                    f'{path} is a monologue but contains speaker_voice_genders')
            actual.append(gender)

    if not actual:
        return _voice_failure('Telefonnotiz output contained no nested versions')
    if actual != expected:
        return _voice_failure(
            f'Telefonnotiz version voice genders {actual!r}, expected {expected!r}')
    return _voice_success(f'Telefonnotiz version voice genders match: {actual!r}')


def lesen_teil1_has_no_voice_metadata(output, context):
    """The shared universal prompt/schema must not annotate reading text."""
    if context['vars'].get('section_type') != 'lesen_teil1':
        return _not_applicable('not Lesen Teil 1')
    objects, failure = _objects_or_failure(output)
    if failure:
        return failure
    contaminated = []
    for object_index, obj in enumerate(objects):
        if not isinstance(obj, dict):
            return _voice_failure(f'object[{object_index}] is not an object')
        for text_index, text in enumerate(obj.get('texts') or []):
            if isinstance(text, dict) and text.get('metadata') is not None:
                contaminated.append(f'object[{object_index}].texts[{text_index}]')
    if contaminated:
        return _voice_failure(
            f'reading text unexpectedly contains TTS metadata: {contaminated!r}')
    return _voice_success('Lesen Teil 1 contains no TTS metadata')


def _parse(output):
    text = output.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    return json.loads(text)


def _contains_placeholder(value):
    if isinstance(value, str):
        return value.strip().casefold() == 'placeholder'
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_placeholder(item) for item in value.values())
    return False


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

    raw_lines = load_markdown(context).split('\n')
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
                text.get('heading_lines'), path, failures, len(raw_lines))
            if heading_lines is None:
                continue

            if start == -1 and end == -1:
                if heading_lines:
                    failures.append(
                        f'{path} missing-text sentinel must not contain heading_lines')
                    continue
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
                try:
                    start, end = line_extraction.normalize_span_for_adjacent_headings(
                        raw_lines,
                        start,
                        end,
                        heading_lines,
                    )
                    line_extraction.validate_heading_lines(
                        heading_lines,
                        start,
                        end,
                    )
                except (TypeError, ValueError) as e:
                    failures.append(f'{path} has invalid heading_lines: {e}')
                    continue
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


def _validate_heading_lines(value, path, failures, raw_line_count):
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
        if item < 0 or item >= raw_line_count:
            failures.append(
                f'{path} heading_lines entry is outside source lines: {item!r}')
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

    raw_lines = load_markdown(context).split('\n')
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
