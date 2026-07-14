"""Promptfoo prompt function for a parse pass (any section_type).

Same idea as prompt_discover.py — reads the live PROMPTS dict so the
eval never drifts from what's actually deployed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from prompts import PROMPTS  # noqa: E402
from response_schemas import SPAN_TEXT_SECTION_TYPES  # noqa: E402
import line_extraction  # noqa: E402


def get_prompt(context: dict) -> str:
    section_type = context['vars']['section_type']
    markdown = context['vars']['markdown']
    # main.py numbers the markdown before substitution for span-backed
    # sections (telefonnotiz's weitere_informationen and line-span
    # texts for lesen_teil2 / hoeren_teil4). Their prompts ask for
    # {start_line, end_line} pointers into a numbered "00042: ..." copy,
    # so an eval sending raw markdown here would test something main.py
    # never actually sends.
    if section_type == 'telefonnotiz' or section_type in SPAN_TEXT_SECTION_TYPES:
        markdown = line_extraction.number_markdown(markdown)
    return PROMPTS[section_type].replace('{markdown}', markdown)
