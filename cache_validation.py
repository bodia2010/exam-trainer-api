"""Fail-closed eligibility checks for values published to shared Redis.

Gemini responseSchema constrains syntax, but shared-cache publication is a
stronger boundary: one bad nondeterministic response must not become the
immutable result for every later user. These checks mirror the Flutter
ParseService structural validator and additionally validate discovery anchors.
"""

from typing import Any

from response_schemas import DISCOVER_SECTION_TYPES, UNIVERSAL_QUESTION_COUNTS


ITEM_DELIMITER = '<<<ITEM>>>'
SAME_SENTINEL = '<<SAME_AS_ORIGINAL>>'
NO_ANSWER_SENTINEL = '(nicht angegeben)'

EXERCISE_TYPES = set(UNIVERSAL_QUESTION_COUNTS) | {
    'hoeren_teil1',
    'telefonnotiz',
    'sprachbausteine_teil1',
}


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _contains(value: Any, needle: str) -> bool:
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_contains(child, needle) for child in value.values())
    if isinstance(value, list):
        return any(_contains(child, needle) for child in value)
    return False


def _letters(value: Any) -> set[Any]:
    if not isinstance(value, list):
        return set()
    return {
        option.get('letter')
        for option in value
        if isinstance(option, dict) and isinstance(option.get('letter'), str)
    }


def _valid_universal(item: dict, section_type: str) -> bool:
    texts = item.get('texts')
    questions = item.get('questions')
    if not isinstance(texts, list) or not texts:
        return False
    if (
        not isinstance(questions, list) or
        len(questions) != UNIVERSAL_QUESTION_COUNTS[section_type]
    ):
        return False
    pool_letters = _letters(item.get('option_pool'))
    for question in questions:
        if not isinstance(question, dict):
            return False
        answer = question.get('answer')
        if answer == NO_ANSWER_SENTINEL:
            continue
        question_type = question.get('type')
        if question_type == 'match':
            if answer not in pool_letters:
                return False
        elif question_type == 'choice':
            if answer not in _letters(question.get('options')):
                return False
        elif question_type == 'true_false':
            if answer not in {'richtig', 'falsch'}:
                return False
        else:
            return False
    return True


def _valid_hoeren_teil1(item: dict) -> bool:
    pairs = item.get('question_pairs')
    if not isinstance(pairs, list) or len(pairs) != 3:
        return False
    for pair in pairs:
        if not isinstance(pair, dict) or not _nonempty_string(pair.get('dialogue')):
            return False
        richtig_falsch = pair.get('richtig_falsch')
        multiple_choice = pair.get('multiple_choice')
        if (
            not isinstance(richtig_falsch, dict) or
            not isinstance(richtig_falsch.get('answer'), bool) or
            not isinstance(multiple_choice, dict)
        ):
            return False
        options = multiple_choice.get('options')
        if not isinstance(options, list) or not options:
            return False
        correct_letter = multiple_choice.get('correct_letter')
        if (
            not isinstance(correct_letter, str) or
            correct_letter not in _letters(options)
        ):
            return False
    return True


def _valid_telefonnotiz(item: dict) -> bool:
    versions = item.get('versions')
    if not isinstance(versions, list) or not versions:
        return False
    for version in versions:
        if not isinstance(version, dict) or not _nonempty_string(version.get('monologue')):
            return False
        answer = version.get('answer')
        if not isinstance(answer, dict):
            return False
        for field in ('call_type', 'name', 'telefonnummer', 'zu_erledigen'):
            if not _nonempty_string(answer.get(field)):
                return False
        info = answer.get('weitere_informationen')
        if not isinstance(info, list) or not info:
            return False
    return True


def _valid_sprachbausteine_teil1(item: dict) -> bool:
    letter_text = item.get('letter_text')
    answers = item.get('answers')
    if not _nonempty_string(letter_text) or not isinstance(answers, list) or not answers:
        return False
    option_letters = _letters(item.get('all_options'))
    for answer in answers:
        if not isinstance(answer, dict):
            return False
        number = answer.get('question_number')
        if answer.get('letter') not in option_letters or f'[{number}]' not in letter_text:
            return False
    return True


def valid_group(value: Any, section_type: str) -> bool:
    if section_type not in EXERCISE_TYPES or not isinstance(value, list) or not value:
        return False
    for item in value:
        variant_number = item.get('variant_number') if isinstance(item, dict) else None
        if (
            not isinstance(item, dict) or
            not isinstance(variant_number, int) or
            isinstance(variant_number, bool)
        ):
            return False
        if _contains(item, SAME_SENTINEL) or _contains(item, ITEM_DELIMITER):
            return False
        if section_type in UNIVERSAL_QUESTION_COUNTS:
            valid = _valid_universal(item, section_type)
        elif section_type == 'hoeren_teil1':
            valid = _valid_hoeren_teil1(item)
        elif section_type == 'telefonnotiz':
            valid = _valid_telefonnotiz(item)
        else:
            valid = _valid_sprachbausteine_teil1(item)
        if not valid:
            return False
    return True


def _normalized(text: str) -> str:
    return ' '.join(text.split())


def valid_discovery(value: Any, raw_markdown: str) -> bool:
    if not isinstance(value, list) or not value:
        return False
    raw_lines = raw_markdown.split('\n')
    corrected_lines: set[int] = set()
    exercise_count = 0
    for item in value:
        if not isinstance(item, dict):
            return False
        section_type = item.get('section_type')
        start_line = item.get('start_line')
        anchor = item.get('anchor')
        if section_type not in set(DISCOVER_SECTION_TYPES):
            return False
        if not isinstance(start_line, int) or isinstance(start_line, bool):
            return False
        if not _nonempty_string(anchor) or len(anchor.strip()) < 8:
            return False
        needle = _normalized(anchor)
        matches = [
            index for index, line in enumerate(raw_lines)
            if needle in _normalized(line)
        ]
        if not matches:
            return False
        corrected = min(matches, key=lambda index: abs(index - start_line))
        if corrected in corrected_lines:
            return False
        corrected_lines.add(corrected)
        if section_type == 'other':
            continue
        variant_number = item.get('variant_number')
        if not isinstance(variant_number, int) or isinstance(variant_number, bool):
            return False
        exercise_count += 1
    return exercise_count > 0


def eligible(value: Any, section_type: str, raw_markdown: str | None = None) -> bool:
    if section_type == 'discover':
        return raw_markdown is not None and valid_discovery(value, raw_markdown)
    return valid_group(value, section_type)
