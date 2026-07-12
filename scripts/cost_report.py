#!/usr/bin/env python3
"""
cost_report.py — aggregate GEMINI_USAGE log lines into a cost report.

Usage:
    vercel logs <deployment-url> --since 1d | python3 scripts/cost_report.py
    python3 scripts/cost_report.py < logs.txt
    python3 scripts/cost_report.py --input logs.txt

Reads arbitrary log text on stdin (or from --input FILE), picks out lines
containing a `GEMINI_USAGE ...` marker emitted by main.py::_call_gemini,
e.g.:

    GEMINI_USAGE section_type=hoeren_teil1 tariff=free prompt_tokens=1234 candidates_tokens=567 thoughts_tokens=12

Any other log lines (including CACHE_LOOKUP lines from main.py's /api/cache
handler, Vercel's own timestamp/request-id prefixes, etc.) are ignored —
matching is done by locating the `GEMINI_USAGE` marker anywhere in the
line, not by requiring the whole line to match, since Vercel prefixes each
line with its own metadata before the text passed to print().

Prints one row per (section_type, tariff) with call count, average prompt
tokens (input), average output tokens (candidates + thoughts — both are
billed as output tokens under Gemini's pricing), and a $ cost estimate.
Pricing is looked up per section_type via generation_config.model_for, the
same function main.py uses to pick the model for each call — this matters
because 'discover' runs on gemini-3.5-flash ($1.50 / $9 per 1M in/out)
while every other section type runs on gemini-3.1-flash-lite ($0.25 /
$1.50 per 1M in/out), a 6x per-token difference. A single flat rate across
every row would badly understate discover's share, which is also the
largest call by token count (a whole document vs. one small chunk for
everything else) — in practice usually the dominant cost driver. A TOTAL
row follows.

Self-test (fabricated sample, no real logs needed — this is what was run
to confirm the script doesn't crash and the math is sane):

    cat <<'EOF' | python3 scripts/cost_report.py
    GEMINI_USAGE section_type=hoeren_teil1 tariff=free prompt_tokens=1000 candidates_tokens=200 thoughts_tokens=50
    GEMINI_USAGE section_type=hoeren_teil1 tariff=free prompt_tokens=1100 candidates_tokens=210 thoughts_tokens=40
    GEMINI_USAGE section_type=discover tariff=premium prompt_tokens=150000 candidates_tokens=3000 thoughts_tokens=500
    not a usage line, should be skipped
    2026-07-09T12:00:00.000Z  [GET] /api/parse  GEMINI_USAGE section_type=lesen_teil1 tariff=premium prompt_tokens=5000 candidates_tokens=800 thoughts_tokens=0
    EOF
"""
import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import generation_config  # noqa: E402

_USAGE_RE = re.compile(
    r'GEMINI_USAGE\s+section_type=(?P<section_type>\S+)\s+'
    r'tariff=(?P<tariff>\S+)\s+'
    r'prompt_tokens=(?P<prompt_tokens>\d+)\s+'
    r'candidates_tokens=(?P<candidates_tokens>\d+)\s+'
    r'thoughts_tokens=(?P<thoughts_tokens>\d+)'
)

# $ per token, (input, output). Update alongside generation_config.MODELS —
# a model missing here falls back to gemini-3.1-flash-lite's rate, which
# would silently understate cost for anything actually running pricier, so
# keep this in sync rather than relying on the fallback.
_MODEL_PRICING = {
    'gemini-3.1-flash-lite': (0.25 / 1_000_000, 1.50 / 1_000_000),
    'gemini-3.5-flash': (1.50 / 1_000_000, 9.00 / 1_000_000),
}
_DEFAULT_PRICING = _MODEL_PRICING['gemini-3.1-flash-lite']


def _rates_for(section_type):
    model = generation_config.model_for(section_type)
    return _MODEL_PRICING.get(model, _DEFAULT_PRICING)


def parse_lines(lines):
    """Yield (section_type, tariff, prompt_tokens, output_tokens) for every
    GEMINI_USAGE line found; silently skips anything that doesn't match
    (other log noise, blank lines, Vercel banners, etc.)."""
    for line in lines:
        m = _USAGE_RE.search(line)
        if not m:
            continue
        prompt_tokens = int(m.group('prompt_tokens'))
        output_tokens = int(m.group('candidates_tokens')) + int(m.group('thoughts_tokens'))
        yield m.group('section_type'), m.group('tariff'), prompt_tokens, output_tokens


def aggregate(records):
    groups = defaultdict(lambda: {'calls': 0, 'prompt_tokens': 0, 'output_tokens': 0})
    for section_type, tariff, prompt_tokens, output_tokens in records:
        g = groups[(section_type, tariff)]
        g['calls'] += 1
        g['prompt_tokens'] += prompt_tokens
        g['output_tokens'] += output_tokens
    return groups


def format_report(groups):
    if not groups:
        return 'No GEMINI_USAGE lines found in input.'

    rows = []
    total_calls = total_prompt = total_output = 0
    total_cost = 0.0
    for (section_type, tariff), g in sorted(groups.items()):
        calls = g['calls']
        avg_in = g['prompt_tokens'] / calls
        avg_out = g['output_tokens'] / calls
        input_rate, output_rate = _rates_for(section_type)
        cost = g['prompt_tokens'] * input_rate + g['output_tokens'] * output_rate
        rows.append((section_type, tariff, calls, avg_in, avg_out, cost))
        total_calls += calls
        total_prompt += g['prompt_tokens']
        total_output += g['output_tokens']
        total_cost += cost

    header = (
        f'{"section_type":<20} {"tariff":<8} {"calls":>6} '
        f'{"avg_in":>10} {"avg_out":>10} {"cost_$":>10}'
    )
    lines = [header, '-' * len(header)]
    for section_type, tariff, calls, avg_in, avg_out, cost in rows:
        lines.append(
            f'{section_type:<20} {tariff:<8} {calls:>6} '
            f'{avg_in:>10.1f} {avg_out:>10.1f} {cost:>10.4f}'
        )

    lines.append('-' * len(header))
    lines.append(
        f'{"TOTAL":<20} {"":<8} {total_calls:>6} '
        f'{(total_prompt / total_calls if total_calls else 0):>10.1f} '
        f'{(total_output / total_calls if total_calls else 0):>10.1f} '
        f'{total_cost:>10.4f}'
    )
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description=(
            'Aggregate GEMINI_USAGE log lines (from `vercel logs` or main.py '
            'directly) into a per-import cost report.'
        ),
    )
    parser.add_argument(
        '--input', '-i', type=argparse.FileType('r'), default=sys.stdin,
        help='Log file to read (default: stdin). Pipe `vercel logs ...` straight in.',
    )
    args = parser.parse_args()

    records = list(parse_lines(args.input))
    groups = aggregate(records)
    print(format_report(groups))


if __name__ == '__main__':
    main()
