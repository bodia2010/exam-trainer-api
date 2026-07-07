"""Promptfoo prompt function for a parse pass (any section_type).

Same idea as prompt_discover.py — reads the live PROMPTS dict so the
eval never drifts from what's actually deployed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from prompts import PROMPTS  # noqa: E402


def get_prompt(context: dict) -> str:
    section_type = context['vars']['section_type']
    markdown = context['vars']['markdown']
    return PROMPTS[section_type].replace('{markdown}', markdown)
