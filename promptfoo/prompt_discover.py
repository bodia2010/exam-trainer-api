"""Promptfoo prompt function for the 'discover' pass.

Imports PROMPTS directly from ../prompts.py instead of keeping a copy —
the eval always tests whatever is actually deployed, no risk of the
fixture drifting out of sync with the real prompt.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from prompts import PROMPTS  # noqa: E402


def get_prompt(context: dict) -> str:
    markdown = context['vars']['markdown']
    return PROMPTS['discover'].replace('{markdown}', markdown)
