"""Builds Gemini `generationConfig` and picks the model per section type —
pulled out of main.py so tooling (the promptfoo eval provider) can import
the exact same logic without dragging in Flask/Firebase/Upstash. main.py
imports this too, so there is exactly one place this ever gets decided —
an eval run can never silently diverge from what's actually deployed.
"""
from response_schemas import schema_for

DEFAULT_MODEL = 'gemini-3.1-flash-lite'

# Per-section model overrides. gemini-2.5-flash-lite was tried and
# rejected for discovery — it reliably dropped the "other" filler-block
# markers, reintroducing the runaway-chunk bug. gemini-3.1-flash-lite
# (the default) parses all 12 section types well, but proved structurally
# unable to segment DISCOVERY reliably: on a real 223K-token compilation
# it fragmented "Hören Teil 4 вариант 1" into 7 entries (spurious
# boundaries at single-question correction blocks AND at plain content
# lines, plus a fabricated version label), byte-identically across runs
# and across every prompt rewording tried; raising thinkingLevel to LOW
# made it strictly worse (6 fragments, four invented labels). The client
# slices chunks at every discovered boundary, so each spurious entry
# silently truncates a real exercise. gemini-3.5-flash on the identical
# input produced exactly one entry per variant with correct boundaries,
# stable across repeated runs (see DISCOVERY_BUG_ANALYSIS.md). Discovery
# is one call per unique document and its result is cached without TTL,
# so the price delta is a one-time cents-level cost per new PDF.
MODELS = {
    'discover': 'gemini-3.5-flash',
}


def model_for(section_type: str) -> str:
    return MODELS.get(section_type, DEFAULT_MODEL)


# Content-heavy section types (several full letters, or several separate
# audio transcripts + their own questions each) reliably drop content at
# MINIMAL thinking — confirmed on real imports: 'texts' coming back
# empty, question counts short (5/8 for hoeren_teil4, 1/2 for
# beschwerde), consistently across retries. Lighter section types
# (single letter/passage, fixed short question sets) stay at MINIMAL.
#
# telefonnotiz joined this set after a real premium import with several
# variants having 4 editions each (all editions of one variant_number are
# grouped into a single call per the SEGMENTATION rule in prompts.py, so
# a heavily-edited variant means 4 monologues + 4 five-field answer keys
# in one call): individual answer fields (telefonnummer,
# weitere_informationen) and even a whole edition's monologue came back
# empty, each in a different variant, consistently surviving the
# client's validation-triggered retry — the exact same "drops content
# under load, retry doesn't help" signature as the other four.
HEAVY_SECTION_TYPES = {
    'beschwerde', 'hoeren_teil2', 'hoeren_teil3', 'hoeren_teil4', 'telefonnotiz',
}


def build(model: str, section_type: str = '') -> dict:
    # Gemini 3.x renamed the thinking-budget knob: it's a coarse
    # thinkingLevel (MINIMAL/LOW/MEDIUM/HIGH), not a token budget.
    if model.startswith('gemini-3'):
        level = 'LOW' if section_type in HEAVY_SECTION_TYPES else 'MINIMAL'
        # This is extraction (pull out what's already in the document),
        # not creative writing. Low temperature favors the model
        # actually following "exactly N questions" instead of sampling
        # its way into skipping some — confirmed via production imports
        # where the identical input reproducibly came back with a
        # different (usually short) question count on every retry.
        #
        # Discovery goes all the way to 0 (greedy): its output is a list
        # of line numbers, and sampling noise corrupts them — observed
        # live: two back-to-back discover calls on the identical document
        # differed in exactly one digit ("start_line": 515 vs the correct
        # 9515, a dropped leading 9), and that single corrupted number
        # planted a ghost hoeren_teil4 boundary inside a Lesen section,
        # breaking BOTH sections' imports at once. Greedy decoding makes
        # the numeric path deterministic; the two clean temp=1 runs were
        # already byte-identical in every boundary, so we lose nothing.
        temperature = 0.2 if section_type != 'discover' else 0
        config = {
            'temperature': temperature,
            'thinkingConfig': {'thinkingLevel': level},
        }
    else:
        config = {
            'temperature': 1,  # required when thinkingBudget=0
            'thinkingConfig': {'thinkingBudget': 0},
        }
    schema = schema_for(section_type)
    if schema is not None:
        # Constrained decoding: the response literally cannot omit
        # 'texts' or have the wrong number of questions, instead of the
        # model merely being asked to include them. See
        # response_schemas.py for why this was worth doing.
        config['responseMimeType'] = 'application/json'
        config['responseSchema'] = schema
    return config
