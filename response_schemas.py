"""Gemini `responseSchema` definitions for the shared universal exercise
schema (lesen_teil1-4, hoeren_teil2-4, beschwerde, sprachbausteine_teil2).

Free-form JSON generation guided only by the prompt's textual
instructions was shown in production to drop required content under
real load: 'texts' coming back empty, question arrays short of the
official count (5/8 for hoeren_teil4, 1/2 for beschwerde) — inconsistent
across retries of the identical input, so this was model reliability
under sampling, not a wording problem the prompt text could fully fix.
responseSchema uses constrained decoding: the model CANNOT emit a
response that violates minItems/maxItems or omits a required field, so
this makes the failure mode structurally impossible rather than just
less likely.

This conflicts with the old <<SAME_AS_ORIGINAL>> dedup sentinel (a
string standing in for an array field) — a strictly-typed 'texts' field
can't be sometimes-array-sometimes-string. Prompts using this schema
drop that optimization and always emit full content per edition; see
the DEDUPLICATION rule in prompts.py's _UNIVERSAL template.

Also covers 'discover' (see DISCOVER_SCHEMA below) — a different shape
(no question counts involved), but the same underlying motivation: its
prompt asked for start_line as "a PLAIN integer with NO leading zeros"
because a zero-padded integer literal like 00042 isn't valid JSON, and
free-form generation leaked it anyway often enough that main.py carried
a regex workaround for it. A schema-typed INTEGER field can't be
serialized with leading zeros in the first place — constrained decoding
only ever produces valid JSON for the declared type.
"""

# Mirrors the 12 categories in prompts.py's DISCOVER prompt, plus the
# 'other' filler-block marker it also emits. Enforcing this as an enum
# means the model can't invent a 13th category that the client's
# groupChunksBySectionType silently doesn't know how to route.
DISCOVER_SECTION_TYPES = [
    'lesen_teil1', 'lesen_teil2', 'lesen_teil3', 'lesen_teil4',
    'beschwerde', 'sprachbausteine_teil1', 'sprachbausteine_teil2',
    'telefonnotiz', 'hoeren_teil1', 'hoeren_teil2', 'hoeren_teil3',
    'hoeren_teil4', 'other',
]

# variant_number/version_label are nullable, not required: the prompt's
# 'other' filler-block marker deliberately omits both ("no
# variant_number/version_label needed"). No minItems on the outer array
# — unlike a per-variant parse call (where discovery already proved
# content exists), a document that genuinely has nothing recognizable is
# a real, valid outcome here; import_screen.dart already handles a
# zero-item result gracefully.
DISCOVER_SCHEMA = {
    'type': 'ARRAY',
    'items': {
        'type': 'OBJECT',
        'properties': {
            'section_type': {'type': 'STRING', 'enum': DISCOVER_SECTION_TYPES},
            'variant_number': {'type': 'INTEGER', 'nullable': True},
            'version_label': {'type': 'STRING', 'nullable': True},
            'start_line': {'type': 'INTEGER'},
            # Self-healing anchor for start_line: transcribing a 5-digit line
            # number correctly ~170 times per document is exactly the kind of
            # narrow numeric-transcription task LLMs occasionally fumble
            # (confirmed live: "start_line": 515 instead of 9515 — one
            # dropped leading digit — even at temperature=0, since Gemini
            # doesn't guarantee bit-identical output across calls even
            # greedy). The client cross-checks this text against the actual
            # line at start_line and, on a mismatch, searches nearby lines
            # for it and corrects start_line itself — a wrong digit becomes
            # self-correcting instead of silently misplacing a chunk
            # boundary into an unrelated part of the document.
            'anchor': {'type': 'STRING'},
        },
        'required': ['section_type', 'start_line', 'anchor'],
    },
}

_VOICE_GENDER = {'type': 'STRING', 'enum': ['female', 'male', 'unknown']}

_SPEAKER_VOICE_GENDER = {
    'type': 'OBJECT',
    'properties': {
        'speaker': {'type': 'STRING'},
        'voice_gender': _VOICE_GENDER,
    },
    'required': ['speaker', 'voice_gender'],
}

_VOICE_METADATA = {
    'type': 'OBJECT',
    'properties': {
        'voice_gender': _VOICE_GENDER,
        'speaker_voice_genders': {
            'type': 'ARRAY',
            'items': _SPEAKER_VOICE_GENDER,
            'nullable': True,
        },
    },
}

_TEXT_ITEM = {
    'type': 'OBJECT',
    'properties': {
        'title': {'type': 'STRING'},
        'content': {'type': 'STRING'},
        'metadata': _VOICE_METADATA,
    },
    'required': ['title', 'content'],
}

_TEXT_SPAN_ITEM = {
    'type': 'OBJECT',
    'properties': {
        'title': {'type': 'STRING'},
        'start_line': {'type': 'INTEGER'},
        'end_line': {'type': 'INTEGER'},
        'heading_lines': {
            'type': 'ARRAY',
            'items': {'type': 'INTEGER'},
            'nullable': True,
        },
        'metadata': _VOICE_METADATA,
    },
    'required': ['title', 'start_line', 'end_line'],
}

_OPTION_ITEM = {
    'type': 'OBJECT',
    'properties': {
        'letter': {'type': 'STRING'},
        'text': {'type': 'STRING'},
    },
    'required': ['letter', 'text'],
}


SPAN_TEXT_SECTION_TYPES = {'lesen_teil2', 'hoeren_teil4'}


def _universal_variant_schema(question_count: int, span_texts: bool = False) -> dict:
    text_item = _TEXT_SPAN_ITEM if span_texts else _TEXT_ITEM
    return {
        'type': 'OBJECT',
        'properties': {
            'variant_number': {'type': 'INTEGER'},
            'version': {'type': 'STRING', 'nullable': True},
            'topic': {'type': 'STRING', 'nullable': True},
            'audio_url': {'type': 'STRING', 'nullable': True},
            'texts': {'type': 'ARRAY', 'items': text_item, 'minItems': 1},
            'option_pool': {'type': 'ARRAY', 'items': _OPTION_ITEM},
            'questions': {
                'type': 'ARRAY',
                'minItems': question_count,
                'maxItems': question_count,
                'items': {
                    'type': 'OBJECT',
                    'properties': {
                        'number': {'type': 'INTEGER'},
                        'type': {
                            'type': 'STRING',
                            'enum': ['true_false', 'choice', 'match'],
                        },
                        'text': {'type': 'STRING'},
                        'options': {'type': 'ARRAY', 'items': _OPTION_ITEM},
                        'answer': {'type': 'STRING'},
                    },
                    'required': ['number', 'type', 'text', 'answer'],
                },
            },
        },
        'required': ['variant_number', 'texts', 'questions'],
    }


# Official telc B2 Beruf question counts per section — fixed by the exam
# format itself (see prompts.py's DISCOVER category descriptions), not
# something that varies per document. Mirrors
# ParseService._expectedQuestionCount on the Flutter client, which keeps
# its own copy of this as a defense-in-depth check after parsing —
# belt-and-suspenders, since a schema bug here shouldn't be the only
# thing standing between a bad response and the user.
UNIVERSAL_QUESTION_COUNTS = {
    'lesen_teil1': 5,
    'lesen_teil2': 2,
    'lesen_teil3': 4,
    'lesen_teil4': 5,
    'beschwerde': 2,
    'sprachbausteine_teil2': 6,
    'hoeren_teil2': 4,
    'hoeren_teil3': 4,
    # Confirmed against telc's own official B2 Beruf test-format table:
    # Hören Teil 4 = 5 Multiple-Choice-Aufgaben, not 8. The old value of 8
    # (Nummer 36..43) was simply wrong from the start, not "usually 8, with
    # occasional shorter variants" — forcing exactly 8 made Gemini fabricate
    # placeholder "Not available" messages to pad a correctly-5-question
    # variant out to the wrong required length, which then failed
    # answer/option-consistency validation downstream.
    'hoeren_teil4': 5,
}

# Backward-compatible alias for promptfoo/assertions.py and any external
# tooling that imported the former private name before this constant became a
# shared cache-validation contract.
_UNIVERSAL_QUESTION_COUNTS = UNIVERSAL_QUESTION_COUNTS


# ─── hoeren_teil1 (bespoke schema — question_pairs, not texts/questions) ──────
# Same fixed-count reasoning as the universal schema's 'questions': a
# variant always has exactly 3 pairs (Nummer 22/23, 24/25, 26/27). Also
# drops the <<SAME_AS_ORIGINAL>> sentinel that question_pairs entries
# used to be allowed to be — a strictly-typed OBJECT array can't have a
# string standing in for an entry, so every edition now repeats its own
# full pair objects instead (see the matching prompts.py rewrite).
_HOEREN_TEIL1_PAIR = {
    'type': 'OBJECT',
    'properties': {
        'pair_audio_url': {'type': 'STRING', 'nullable': True},
        'dialogue': {'type': 'STRING'},
        'metadata': _VOICE_METADATA,
        'richtig_falsch': {
            'type': 'OBJECT',
            'properties': {
                'number': {'type': 'INTEGER'},
                'statement': {'type': 'STRING'},
                'answer': {'type': 'BOOLEAN'},
            },
            'required': ['number', 'statement', 'answer'],
        },
        'multiple_choice': {
            'type': 'OBJECT',
            'properties': {
                'number': {'type': 'INTEGER'},
                'stem': {'type': 'STRING'},
                'options': {
                    'type': 'ARRAY',
                    'items': _OPTION_ITEM,
                    'minItems': 3,
                    'maxItems': 3,
                },
                'correct_letter': {'type': 'STRING', 'enum': ['a', 'b', 'c']},
            },
            'required': ['number', 'stem', 'options', 'correct_letter'],
        },
    },
    'required': ['dialogue', 'richtig_falsch', 'multiple_choice'],
}

HOEREN_TEIL1_SCHEMA = {
    'type': 'ARRAY',
    'minItems': 1,
    'items': {
        'type': 'OBJECT',
        'properties': {
            'variant_number': {'type': 'INTEGER'},
            'version': {'type': 'STRING', 'nullable': True},
            'audio_url': {'type': 'STRING', 'nullable': True},
            'question_pairs': {
                'type': 'ARRAY',
                'minItems': 3,
                'maxItems': 3,
                'items': _HOEREN_TEIL1_PAIR,
            },
        },
        'required': ['variant_number', 'question_pairs'],
    },
}


# ─── telefonnotiz ───────────────────────────────────────────────────────────
# No fixed count here — 'versions' legitimately varies (1 for a variant
# with no reworked edition, more otherwise) — so only minItems:1 (must
# have at least the original), no maxItems. No dedup sentinel was ever
# used for this type, so nothing to remove from the prompt.

# Line-span pointer instead of retyped text — main.py slices the actual
# words out of the numbered chunk after the fact (see line_extraction.py).
# Confirmed live yesterday: asking Gemini to retype a "<X> / <Y>" bullet
# verbatim sometimes kept only one half. A pointer can't be truncated the
# same way — there's no text generation step left to drop half of.
# start_line == end_line == -1 is the sentinel for "this edition
# genuinely has no bullets printed" (see prompts.py) — main.py maps that
# back to the pre-existing "(nicht angegeben)" convention rather than
# attempting a slice, so weitere_informationen's OWN final shape (list of
# strings) and every downstream consumer (client validation, UI) are
# completely unchanged.
#
# slash_index: confirmed live testing on a real chunk that "/" in this
# source means two DIFFERENT things a line pointer alone can't tell
# apart: (a) one edition's own bullet with two alternate readings of the
# SAME fact ("Weiße und grüne Farbe mitbringen / Farbe: weiß und rot") —
# keep the whole thing, both alternates belong to this edition; or (b) a
# single answer-key block PRINTED ONCE but covering several editions,
# each field holding each edition's own value joined by "/" ("Name:
# Mayer/ Meyer / Azrael") — this edition needs only ITS OWN slice.
# Omitted/-1 means case (a) (keep the full line text, unsplit — the
# default, and what most bullets need). A non-negative integer means
# case (b): split the extracted text on "/" and take that 0-based
# segment. The model already made this exact judgment call under the
# old retyping-based prompt (it correctly wrote "Mayer" for one edition
# and "Meyer" for another from this same shared line) — this only
# changes HOW it expresses that choice, from retyping a pre-decided
# substring to pointing at which one, so a later mechanical step can't
# introduce a NEW truncation bug on top of an already-correct decision.
_LINE_SPAN = {
    'type': 'OBJECT',
    'properties': {
        'start_line': {'type': 'INTEGER'},
        'end_line': {'type': 'INTEGER'},
        'slash_index': {'type': 'INTEGER', 'nullable': True},
    },
    'required': ['start_line', 'end_line'],
}

_TELEFONNOTIZ_ANSWER = {
    'type': 'OBJECT',
    'properties': {
        'call_type': {'type': 'STRING'},
        'name': {'type': 'STRING'},
        'telefonnummer': {'type': 'STRING'},
        'weitere_informationen': {'type': 'ARRAY', 'items': _LINE_SPAN},
        'zu_erledigen': {'type': 'STRING'},
    },
    'required': ['call_type', 'name', 'telefonnummer', 'zu_erledigen'],
}

TELEFONNOTIZ_SCHEMA = {
    'type': 'ARRAY',
    'minItems': 1,
    'items': {
        'type': 'OBJECT',
        'properties': {
            'variant_number': {'type': 'INTEGER'},
            'topic': {'type': 'STRING', 'nullable': True},
            'versions': {
                'type': 'ARRAY',
                'minItems': 1,
                'items': {
                    'type': 'OBJECT',
                    'properties': {
                        'label': {'type': 'STRING', 'nullable': True},
                        'audio_url': {'type': 'STRING', 'nullable': True},
                        'monologue': {'type': 'STRING'},
                        'metadata': _VOICE_METADATA,
                        'answer': _TELEFONNOTIZ_ANSWER,
                    },
                    'required': ['monologue', 'answer'],
                },
            },
        },
        'required': ['variant_number', 'versions'],
    },
}


# ─── sprachbausteine_teil1 ──────────────────────────────────────────────────
# Question count genuinely varies here (46-51 = 6 blanks, or 42-51 = 10
# blanks depending on the variant) — unlike the universal types, there's
# no single fixed official count to pin minItems==maxItems to, so this
# only floors at the smaller of the two known shapes rather than forcing
# an exact match. Also drops the <<SAME_AS_ORIGINAL>> sentinel for
# letter_text/all_options, same reasoning as everywhere else.
_SPRACHBAUSTEINE1_ANSWER = {
    'type': 'OBJECT',
    'properties': {
        'question_number': {'type': 'INTEGER'},
        'letter': {'type': 'STRING'},
        'word': {'type': 'STRING'},
    },
    'required': ['question_number', 'letter', 'word'],
}

SPRACHBAUSTEINE_TEIL1_SCHEMA = {
    'type': 'ARRAY',
    'minItems': 1,
    'items': {
        'type': 'OBJECT',
        'properties': {
            'variant_number': {'type': 'INTEGER'},
            'version': {'type': 'STRING', 'nullable': True},
            'topic': {'type': 'STRING', 'nullable': True},
            'letter_text': {'type': 'STRING'},
            'answers': {
                'type': 'ARRAY',
                'minItems': 6,
                'items': _SPRACHBAUSTEINE1_ANSWER,
            },
            'all_options': {
                'type': 'ARRAY',
                'minItems': 6,
                'items': _OPTION_ITEM,
            },
        },
        'required': ['variant_number', 'letter_text', 'answers', 'all_options'],
    },
}


def schema_for(section_type: str):
    """Returns a Gemini responseSchema for section_type, or None if this
    type isn't schema-enforced (yet) — callers fall back to free-form
    JSON generation guided only by the prompt's textual instructions."""
    if section_type == 'discover':
        return DISCOVER_SCHEMA
    if section_type == 'hoeren_teil1':
        return HOEREN_TEIL1_SCHEMA
    if section_type == 'telefonnotiz':
        return TELEFONNOTIZ_SCHEMA
    if section_type == 'sprachbausteine_teil1':
        return SPRACHBAUSTEINE_TEIL1_SCHEMA
    count = UNIVERSAL_QUESTION_COUNTS.get(section_type)
    if count is None:
        return None
    # minItems here matters as much as the per-object constraints above —
    # without it, an empty array `[]` is fully schema-valid, so a call
    # that "gives up" can return zero variants with no error at all. That
    # silently drops the whole section from the course instead of
    # surfacing as a retryable/reportable failure like every other
    # malformed-content case.
    return {
        'type': 'ARRAY',
        'minItems': 1,
        'items': _universal_variant_schema(
            count,
            span_texts=section_type in SPAN_TEXT_SECTION_TYPES,
        ),
    }
