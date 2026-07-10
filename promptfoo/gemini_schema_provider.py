"""Custom promptfoo provider that calls Gemini with the EXACT
generationConfig our backend actually sends — imported from
../generation_config.py, never a copy.

promptfoo's built-in `google:<model>` provider takes one static config
per provider entry in the YAML, but our real generationConfig (notably
responseSchema) varies PER section_type. Without this, an eval using the
built-in provider tests free-form generation that isn't what's deployed
— exactly the gap that let the schema work ship without eval coverage
catching whether it actually helps.
"""
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import generation_config  # noqa: E402


def call_api(prompt, options, context):
    section_type = context['vars'].get('section_type', 'discover')
    model = options.get('config', {}).get('model', 'gemini-3.1-flash-lite')
    api_key = os.environ.get('GOOGLE_API_KEY', '')
    if not api_key:
        return {'error': 'GOOGLE_API_KEY is not set'}

    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': generation_config.build(model, section_type),
    }
    try:
        resp = requests.post(
            f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
            params={'key': api_key},
            json=payload,
            timeout=100,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data['candidates'][0]['content']['parts'][0]['text']
        return {'output': text}
    except requests.HTTPError as e:
        # Never let the raw response (may echo the request URL, which
        # includes ?key=<api_key>) leak into eval output/logs.
        return {'error': f'Gemini request failed: HTTP {e.response.status_code}'}
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}
