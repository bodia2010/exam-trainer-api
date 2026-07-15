"""Load Promptfoo markdown fixtures without changing their contents.

Promptfoo resolves text ``file://`` variables with JavaScript ``trim()``.
That removes a leading form-feed (``\x0c``), even though production passes
the PDF converter's markdown to Gemini unchanged.  Configs therefore pass a
plain ``markdown_path`` and this module performs the UTF-8 read itself.
"""
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def load_markdown(context: dict) -> str:
    """Return raw fixture text, or an inline ``markdown`` test value.

    Paths are relative to the promptfoo directory and intentionally confined
    to it.  ``read_bytes().decode()`` avoids both whitespace trimming and
    universal-newline conversion.
    """
    variables = context['vars']
    path_value = variables.get('markdown_path')
    if path_value is None:
        return variables['markdown']
    if 'markdown' in variables:
        raise ValueError('set either markdown_path or markdown, not both')
    if not isinstance(path_value, str) or not path_value:
        raise ValueError('markdown_path must be a non-empty string')

    path = (BASE_DIR / path_value).resolve()
    try:
        path.relative_to(BASE_DIR)
    except ValueError as exc:
        raise ValueError('markdown_path must stay inside the promptfoo directory') from exc
    return path.read_bytes().decode('utf-8')
