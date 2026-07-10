"""Builds Gemini `generationConfig` — pulled out of main.py so tooling
(the promptfoo eval provider) can import the exact same logic without
dragging in Flask/Firebase/Upstash. main.py imports this too, so there
is exactly one place this ever gets decided — an eval run can never
silently diverge from what's actually deployed.
"""
from response_schemas import schema_for

# Content-heavy section types (several full letters, or several separate
# audio transcripts + their own questions each) reliably drop content at
# MINIMAL thinking — confirmed on real imports: 'texts' coming back
# empty, question counts short (5/8 for hoeren_teil4, 1/2 for
# beschwerde), consistently across retries. Lighter section types
# (single letter/passage, fixed short question sets) stay at MINIMAL.
HEAVY_SECTION_TYPES = {'beschwerde', 'hoeren_teil2', 'hoeren_teil3', 'hoeren_teil4'}


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
        # Scoped to parse calls only, NOT discovery — discovery is a
        # broad classification task over the whole ~150K-token document
        # at once, a fundamentally different job, and there's no
        # evidence low temperature helps (or is even neutral) there.
        temperature = 0.2 if section_type != 'discover' else 1
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
