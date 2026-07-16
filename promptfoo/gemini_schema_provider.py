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


# Google AI Studio standard paid-tier rates in USD per token. Keep these
# deliberately limited to the two production models used by
# generation_config.py: an explicit A/B model with no reviewed rate still
# reports its token usage, but does not invent a cost.
_STANDARD_TIER_RATES = {
    # input, cached input, output (candidate + thinking), all per token.
    # Keep in sync with scripts/cost_report.py and Google's pricing page.
    'gemini-3.1-flash-lite': (
        0.25 / 1_000_000,
        0.025 / 1_000_000,
        1.50 / 1_000_000,
    ),
    'gemini-3.5-flash': (
        1.50 / 1_000_000,
        0.15 / 1_000_000,
        9.00 / 1_000_000,
    ),
}


def _non_negative_int(value):
    """Return a Gemini token count, rejecting malformed/negative values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return max(0, int(value))
    except (OverflowError, ValueError):
        return 0


def _usage_result(data, model):
    """Build Promptfoo usage/cost fields without retaining response content.

    Gemini bills both candidate and thinking tokens at the output rate. The
    Promptfoo-compatible ``completion`` field remains the visible candidate
    count, while thinking is exposed as reasoning detail and included in the
    estimated cost. If Gemini omitted usageMetadata, omit all accounting
    fields rather than report a misleading zero-cost request.
    """
    usage = data.get('usageMetadata')
    if not isinstance(usage, dict):
        return {}

    prompt_tokens = _non_negative_int(usage.get('promptTokenCount'))
    candidate_tokens = _non_negative_int(usage.get('candidatesTokenCount'))
    thoughts_tokens = _non_negative_int(usage.get('thoughtsTokenCount'))
    cached_tokens = min(
        prompt_tokens,
        _non_negative_int(usage.get('cachedContentTokenCount')),
    )
    reported_total = _non_negative_int(usage.get('totalTokenCount'))
    total_tokens = max(
        reported_total,
        prompt_tokens + candidate_tokens + thoughts_tokens,
    )

    token_usage = {
        'prompt': prompt_tokens,
        'completion': candidate_tokens,
        'total': total_tokens,
        'numRequests': 1,
    }
    if cached_tokens:
        token_usage['cached'] = cached_tokens
    if thoughts_tokens:
        token_usage['completionDetails'] = {
            'reasoning': thoughts_tokens,
            'acceptedPrediction': 0,
            'rejectedPrediction': 0,
        }

    result = {'tokenUsage': token_usage}
    rates = _STANDARD_TIER_RATES.get(model)
    if rates is not None:
        input_rate, cached_input_rate, output_rate = rates
        result['cost'] = (
            (prompt_tokens - cached_tokens) * input_rate
            + cached_tokens * cached_input_rate
            + (candidate_tokens + thoughts_tokens) * output_rate
        )
    return result


def call_api(prompt, options, context):
    section_type = context['vars'].get('section_type', 'discover')
    # No YAML `model:` config → the exact model production would use for
    # this section_type (generation_config.model_for), so the default
    # eval run can never drift from prod. An explicit YAML `model:` still
    # wins, for deliberate A/B comparison runs.
    model = (options.get('config', {}).get('model')
             or generation_config.model_for(section_type))
    api_key = os.environ.get('GOOGLE_API_KEY', '')
    if not api_key:
        return {'output': '', 'error': 'GOOGLE_API_KEY is not set'}

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
        output = data['candidates'][0]['content']['parts'][0]['text']
        return {
            'output': output,
            **_usage_result(data, model),
        }
    except requests.HTTPError as e:
        # Never let the raw response (may echo the request URL, which
        # includes ?key=<api_key>) leak into eval output/logs.
        status = getattr(getattr(e, 'response', None), 'status_code', 'unknown')
        return {'output': '', 'error': f'Gemini request failed: HTTP {status}'}
    except Exception as e:
        # Exception strings from HTTP clients may contain the request URL
        # (and therefore ?key=...), response bodies, or request content.
        return {'output': '', 'error': f'Gemini request failed: {type(e).__name__}'}
