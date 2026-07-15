#!/usr/bin/env python3
"""Apply reviewed curation patches to a freshly generated course JSON.

Each override is deliberately optimistic-locking: ``old`` must match the
current value exactly before it is replaced with ``new``.  A reparse that
changes item ordering or content therefore fails closed instead of applying a
manual correction to the wrong exercise.

Override shape::

    {
      "section": "hoeren_teil4",
      "variant": 8,
      "path": "questions[number=36].answer",
      "old": "a",
      "new": "c",
      "reason": "PDF highlight marks option c"
    }

Paths are relative to the uniquely selected section item. They support dict
keys, numeric list indexes (``texts[0].content``), and stable list selectors
(``questions[number=36]`` or ``versions[label=Neue Version]``). ``<item>``
removes a whole item and requires ``new: null``.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any


_SEGMENT_RE = re.compile(r'(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<selectors>(?:\[[^\]]+\])*)')
_SELECTOR_RE = re.compile(r'\[([^\]]+)\]')


def _sections(course: object) -> dict:
    if not isinstance(course, dict):
        raise ValueError('course JSON must be an object')
    wrapped = course.get('sections')
    return wrapped if isinstance(wrapped, dict) else course


def _selector_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _path_operations(path: str) -> list[tuple[str, Any]]:
    if not isinstance(path, str) or not path:
        raise ValueError('override path must be a non-empty string')
    operations: list[tuple[str, Any]] = []
    for segment in path.split('.'):
        match = _SEGMENT_RE.fullmatch(segment)
        if not match:
            raise ValueError(f'invalid override path segment: {segment!r}')
        operations.append(('key', match.group('key')))
        for selector in _SELECTOR_RE.findall(match.group('selectors')):
            if re.fullmatch(r'0|[1-9][0-9]*', selector):
                operations.append(('index', int(selector)))
                continue
            if '=' not in selector:
                raise ValueError(f'invalid list selector [{selector}] in {path!r}')
            field, raw_value = selector.split('=', 1)
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', field) or not raw_value:
                raise ValueError(f'invalid list selector [{selector}] in {path!r}')
            operations.append(('match', (field, _selector_value(raw_value))))
    return operations


def _step(value: Any, operation: tuple[str, Any], path: str) -> Any:
    kind, argument = operation
    if kind == 'key':
        if not isinstance(value, dict) or argument not in value:
            raise ValueError(f'{path}: object key {argument!r} not found')
        return value[argument]
    if kind == 'index':
        if not isinstance(value, list) or argument >= len(value):
            raise ValueError(f'{path}: list index {argument} not found')
        return value[argument]
    if not isinstance(value, list):
        raise ValueError(f'{path}: selector requires a list')
    field, expected = argument
    matches = [entry for entry in value
               if isinstance(entry, dict) and entry.get(field) == expected]
    if len(matches) != 1:
        raise ValueError(
            f'{path}: selector [{field}={expected!r}] matched {len(matches)} entries')
    return matches[0]


def _replace_path(item: dict, path: str, old: Any, new: Any) -> None:
    operations = _path_operations(path)
    parent: Any = item
    for operation in operations[:-1]:
        parent = _step(parent, operation, path)

    kind, argument = operations[-1]
    if kind == 'key':
        if not isinstance(parent, dict) or argument not in parent:
            raise ValueError(f'{path}: object key {argument!r} not found')
        current = parent[argument]
        if current != old:
            raise ValueError(f'{path}: old value mismatch; expected {old!r}, found {current!r}')
        parent[argument] = copy.deepcopy(new)
        return
    if kind == 'index':
        if not isinstance(parent, list) or argument >= len(parent):
            raise ValueError(f'{path}: list index {argument} not found')
        current = parent[argument]
        if current != old:
            raise ValueError(f'{path}: old value mismatch; expected {old!r}, found {current!r}')
        parent[argument] = copy.deepcopy(new)
        return
    raise ValueError(f'{path}: a path cannot end with a list selector')


def _target_item(items: list, override: dict, label: str) -> tuple[int, dict]:
    variant = override.get('variant')
    candidates = [(index, item) for index, item in enumerate(items)
                  if isinstance(item, dict) and item.get('variant_number') == variant]
    item_index = override.get('item_index')
    if item_index is not None:
        if not isinstance(item_index, int) or isinstance(item_index, bool):
            raise ValueError(f'{label}: item_index must be an integer')
        candidates = [(index, item) for index, item in candidates if index == item_index]
    if len(candidates) != 1:
        raise ValueError(
            f'{label}: section item variant={variant!r} matched {len(candidates)} entries')
    return candidates[0]


def apply_overrides(course: object, overrides: object) -> dict:
    if not isinstance(overrides, list):
        raise ValueError('overrides JSON must be a list')
    result = copy.deepcopy(course)
    sections = _sections(result)
    seen: set[tuple] = set()

    for index, override in enumerate(overrides):
        label = f'override[{index}]'
        if not isinstance(override, dict):
            raise ValueError(f'{label} must be an object')
        missing = [key for key in ('section', 'variant', 'path', 'old', 'new', 'reason')
                   if key not in override]
        if missing:
            raise ValueError(f'{label} missing required keys: {missing}')
        section = override['section']
        variant = override['variant']
        reason = override['reason']
        path = override['path']
        if not isinstance(section, str) or not section:
            raise ValueError(f'{label}: section must be a non-empty string')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f'{label}: reason must be a non-empty string')
        if not isinstance(variant, int) or isinstance(variant, bool):
            raise ValueError(f'{label}: variant must be an integer')
        items = sections.get(section)
        if not isinstance(items, list):
            raise ValueError(f'{label}: section {section!r} is missing or not a list')

        identity = (section, variant, override.get('item_index'), path)
        if identity in seen:
            raise ValueError(f'{label}: duplicate override target {identity!r}')
        seen.add(identity)

        item_index, item = _target_item(items, override, label)
        if path == '<item>':
            if override['new'] is not None:
                raise ValueError(f'{label}: <item> removal requires new: null')
            if item != override['old']:
                raise ValueError(f'{label}: <item> old value does not match selected item')
            del items[item_index]
        else:
            _replace_path(item, path, override['old'], override['new'])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course', required=True, type=Path)
    parser.add_argument('--overrides', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)

    course = json.loads(args.course.read_text(encoding='utf-8'))
    overrides = json.loads(args.overrides.read_text(encoding='utf-8'))
    result = apply_overrides(course, overrides)
    args.out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Applied {len(overrides)} override(s); wrote {args.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
