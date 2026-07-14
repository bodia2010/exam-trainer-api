"""Promptfoo prompt function for a parse pass (any section_type).

Same idea as prompt_discover.py — reads the live PROMPTS dict so the
eval never drifts from what's actually deployed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from prompts import PROMPTS  # noqa: E402
import line_extraction  # noqa: E402


def get_prompt(context: dict) -> str:
    section_type = context['vars']['section_type']
    markdown = context['vars']['markdown']
    # main.py numbers the markdown before substitution for telefonnotiz
    # (weitere_informationen is extracted as {start_line, end_line}
    # pointers, not retyped text — see line_extraction.py) — the prompt's
    # own instructions describe a numbered "00042: ..." format, so an
    # eval sending raw markdown here would test something main.py never
    # actually sends.
    if section_type == 'telefonnotiz':
        markdown = line_extraction.number_markdown(markdown)
    return PROMPTS[section_type].replace('{markdown}', markdown)
