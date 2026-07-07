# ─── Universal schema ─────────────────────────────────────────────────────────
# Sections without bespoke prompts share one JSON schema: reading texts /
# transcripts in `texts`, a shared letter pool for matching in `option_pool`,
# and typed questions (true_false / choice / match). One Flutter screen
# renders them all.

_UNIVERSAL = """Parse German B2 Beruf exam exercises from the Markdown below.

{hint}

For each variant return a JSON object:
{
  "variant_number": <integer>,
  "version": "<short version label or null>",
  "topic": "<short topic or null>",
  "audio_url": "<telegram url of the recording, or null>",
  "texts": [{"title": "<label>", "content": "<full text>"}],
  "option_pool": [{"letter": "a", "text": "<option text>"}],
  "questions": [
    {"number": <int>, "type": "true_false", "text": "<statement>", "answer": "richtig|falsch"},
    {"number": <int>, "type": "choice", "text": "<stem>", "options": [{"letter": "a", "text": "<text>"}], "answer": "<letter>"},
    {"number": <int>, "type": "match", "text": "<item text>", "answer": "<letter from option_pool>"}
  ]
}

Common rules:
- Use only the question types the section description above specifies; option_pool is [] unless it says otherwise
- Correct answers are marked with "– 100%", "(100%)", a letter written after the item, or similar markers
- VERSIONS: the same variant often appears several times — the original plus reworked editions marked "Новая версия", "Новый вариант", a date, or "(тест №…)". Output EACH complete edition as its OWN object: same variant_number, but a distinct "version" label ("Neue Version 08.2024", "Test 150321", …; null for the original). Every edition must be self-contained — if it does not repeat the reading text or option pool, copy them from the original variant into it. Do NOT mix questions of different editions in one object. A lone alternative wording of a single question is NOT an edition — ignore it and keep the answered one.
- Never invent content: skip a question if its options or correct answer cannot be determined
- De-hyphenate words the PDF split across a print line break (e.g. "Ausbildungs-\nkonzept" -> "Ausbildungskonzept") — texts must read as normal continuous prose, no stray hyphens or line breaks mid-word
- Ignore page numbers (lines with only digits) and Russian meta-commentary
- Return ONLY a valid JSON array of variant objects. No markdown wrapper, no explanation.

MARKDOWN:
{markdown}"""


def _u(hint):
    return _UNIVERSAL.replace('{hint}', hint)


# ─── Structure discovery ────────────────────────────────────────────────────
# Instead of the client guessing section boundaries with regex anchors tied
# to this one document's exact labeling ("Hören Teil 1" + the Russian word
# "вариант"), Gemini scans the WHOLE document once (comfortably within its
# 1M-token context) and finds every exercise instance by recognizing its
# STRUCTURE — works regardless of language, label wording, or whether a
# variant has a label at all. The client pre-numbers every line so Gemini
# reports an exact line number instead of quoting text verbatim (unreliable
# over a huge context) — trivial and exact to slice on the client.

DISCOVER = """You are analyzing a German language exam practice document \
(telc-style: Lesen, Hören, Schreiben, Sprachbausteine). It compiles many \
practice variants of different exercise types, possibly labeled \
inconsistently — different languages (German, Russian, English, or no \
label at all), different numbering conventions, or no explicit "Teil N" \
marker at all.

Every line below is prefixed with a 5-digit, zero-padded line number and \
": ", e.g. "00042: Hören Teil 1 (вариант №3)" (that line's number is 42). \
Use ONLY these prefixes to report a position — never invent or estimate a \
line number. In your JSON output, write start_line as a PLAIN integer with \
NO leading zeros (42, not "00042" or 00042) — leading zeros make the \
number invalid JSON.

Find EVERY distinct practice-exercise instance (one variant of one \
exercise type) in the document, and classify each into ONE of these \
categories by RECOGNIZING ITS STRUCTURE, not just a literal label:

- lesen_teil1: 5 people/statements matched to 8 short texts a-h (a matching exercise)
- lesen_teil2: a longer workplace text, then a true/false question and a 3-option multiple-choice question
- lesen_teil3: 4 person situations matched to 6 forum-reply texts a-f (or "no text fits")
- lesen_teil4: a meeting-minutes/Protokoll text followed by 5 multiple-choice questions
- beschwerde: an internal memo + a customer complaint letter, 2 multiple-choice questions, and a model reply letter
- sprachbausteine_teil1: a formal letter with 6 numbered gaps filled from ONE shared word list (~10 words, same pool for every gap)
- sprachbausteine_teil2: a formal letter with 6 numbered gaps, each gap having its OWN 3 options (a/b/c)
- telefonnotiz: a phone-message monologue (a voicemail) with an answer key (caller name, phone number, reason for call)
- hoeren_teil1: a short two-person dialogue, then a true/false question and a 3-option multiple-choice question — repeats 3 times per variant
- hoeren_teil2: four short monologues/dialogues (numbered), each matched to one of six statements a-f
- hoeren_teil3: one longer workplace conversation followed by 4 multiple-choice questions
- hoeren_teil4: eight short phone announcements, each followed by its own multiple-choice question

VERSIONS: the same variant sometimes reappears as a reworked edition \
(marked "Новая версия", "Neue Version", a later date, "Другой вариант \
ответов", or similar) — output each edition as its own separate item with \
a distinct version_label; the original edition gets version_label: null. \
A trivial reword of a single question (not the whole variant) is NOT a \
separate edition — skip it.

Between two exercises there is sometimes a non-exercise block — a table of \
contents/summary page, a links-only reference section (Forumsbeitrag, \
Sprechen/Mündliche Prüfung materials, "Antwortbögen"/"Struktur" link \
lists), or Russian meta-commentary. The client uses each item's start_line \
to also mark the PREVIOUS item's end, so an unmarked filler block would \
silently get glued onto whichever exercise precedes it. To prevent that, \
also emit a marker for the START of every such filler block: \
{"section_type": "other", "start_line": <int>} (no variant_number/version_label needed).

For each real exercise item found, return:
{"section_type": "<one of the 12 keys above>", "variant_number": <integer — the variant's own printed number if there is one, else your best sequential guess>, "version_label": "<short label or null>", "start_line": <integer — the line-number prefix where this item's content begins>}

Return ONLY a valid JSON array, ordered by start_line ascending. No \
markdown wrapper, no explanation. If nothing genuinely matches a \
category, don't include it — do not force a match.

MARKDOWN:
{markdown}"""


PROMPTS = {

'discover': DISCOVER,

'lesen_teil1': _u("""Section: Lesen Teil 1 (matching headlines).
Variants start with "Lesen Teil 1 (вариант №".
Each variant has 5 numbered items (1-5): a person and a statement, with the correct letter written at the end of the line.
Then 8 short texts a)-h), each with a headline in the first line.
- questions: type "match" — number, text = the statement, answer = the letter
- option_pool: the 8 letters, text = the headline only
- texts: one entry per text, title = "a) <headline>", content = the paragraph"""),

'lesen_teil2': _u("""Section: Lesen Teil 2 (reading a workplace text).
Variants start with "Text 1 (вариант №" or "Text 2 (вариант №".
topic = "Text 1" or "Text 2" plus the subject of the passage.
- texts: the reading passage, title = its heading
- questions: one true_false (a statement followed by "Richtig / falsch") and one choice (stem with a) b) c) options). Question numbers are as printed (6, 7, 8, 9...). Determine the correct answer from markers or, if unmarked, from the passage content."""),

'lesen_teil3': _u("""Section: Lesen Teil 3 (matching forum answers).
Variants start with "Lesen Teil 3 (вариант №".
Items 10-13: a name and a situation text; the correct letter is written after the name ("X" means no text matches).
Then reply texts a)-f) from other forum users.
- questions: type "match" — text = name + situation text, answer = the letter (or "x")
- option_pool: letters a-f, text = the replier's name and first few words, PLUS {"letter": "x", "text": "Kein Text passt"}
- texts: one entry per reply, title = "a) <name>", content = the reply"""),

'lesen_teil4': _u("""Section: Lesen Teil 4 (Protokoll / Sitzungsprotokoll).
Variants start with "Lesen Teil 4 (вариант №".
- texts: the full Protokoll text, title = its subject in parentheses (e.g. "Zulieferer, Fahrtenbuch")
- questions 14-18: type "choice" with a) b) c) options"""),

'beschwerde': _u("""Section: Lesen und Schreiben — Beschwerde (complaint letters).
Variants start with "Lesen und Schreiben Teil Beschwerde (вариант №" or "Lesen und Schreiben Beschwerde".
topic = the subject line (e.g. Putzdienst, Tischlampe).
- questions 19-20 (numbers as printed): type "choice" with a) b) c)
- texts: every letter/e-mail of the variant in order, title = its role ("Interne Mail", "Beschwerdebrief", "Musterantwort"), content = the full letter. The reply (Musterantwort) is the model answer for the writing task — always include it."""),

'sprachbausteine_teil2': _u("""Section: Sprachbausteine Teil 2 (letter with gaps 52-57).
Variants start with "Sprachbausteine Teil 2 (вариант №".
The letter shows answers inline like "52 (b - eine Bestellung)" — replace the WHOLE thing with a [52]-style marker, including the answer text itself. Never leave the answer's own words also present as plain text right after the marker.
- texts: one entry, title = the letter subject, content = the letter with [52]...[57] markers
- questions 52-57: type "choice", text = "[<number>]", options = the printed a) b) c) lists, answer = the letter shown inline in the letter"""),

'hoeren_teil3': _u("""Section: Hören Teil 3 (a longer workplace conversation).
Variants start with "Hören Teil 3 (вариант №".
audio_url = the "Ссылка на запись" telegram link.
- texts: the conversation transcript, title = the topic after "Nummer 32-35"
- questions 32-35: type "choice" with a) b) c). Prefer the question set whose answers are known; skip questions with incomplete options (e.g. "c) ?")."""),

'hoeren_teil4': _u("""Section: Hören Teil 4 (short phone messages).
Variants start with "Hören Teil 4 (вариант №".
audio_url = the telegram link near the header.
Eight messages "Nummer 36".."Nummer 43", each followed by a choice question with the same number.
- texts: one entry per message, title = "Nummer <N> <name>", content = the message transcript
- questions 36-43: type "choice" with a) b) c); the correct option is marked "– 100%\""""),

'hoeren_teil1': """Parse German B2 Beruf exam Hören Teil 1 exercises from the Markdown below.

Find all sections starting with "Hören Teil 1 (вариант №".
For each variant return a JSON object:
{
  "variant_number": <integer>,
  "version": "<short version label or null>",
  "audio_url": "<url or null>",
  "question_pairs": [
    {
      "pair_audio_url": "<url or null>",
      "dialogue": "<full dialogue text>",
      "richtig_falsch": {
        "number": <even question number>,
        "statement": "<statement to judge>",
        "answer": <true or false>
      },
      "multiple_choice": {
        "number": <odd question number>,
        "stem": "<question stem>",
        "options": [
          {"letter": "a", "text": "<text>"},
          {"letter": "b", "text": "<text>"},
          {"letter": "c", "text": "<text>"}
        ],
        "correct_letter": "<a|b|c>"
      }
    }
  ]
}

Rules:
- Each variant has exactly 3 question pairs (e.g. 22+23, 24+25, 26+27)
- audio_url: single URL at top of variant; null if absent
- pair_audio_url: fill only if separate URL appears before each "Nummer N und N"
- VERSIONS: if the variant appears as a reworked edition ("Новая версия", "Новый вариант от <дата>", "(тест №…)") with its own full set of question pairs, output it as a SEPARATE object: same variant_number, distinct "version" label (null for the original), self-contained dialogues and questions. A lone alternative wording of a single question is NOT an edition — keep the answered one.
- Ignore lines of only digits (page numbers) and Russian meta-text
- Return ONLY a valid JSON array. No markdown wrapper, no explanation.

MARKDOWN:
{markdown}""",

'hoeren_teil2': _u("""Section: Hören Teil 2 (matching statements to dialogues).
Variants start with "Hören Teil 2 (вариант №".
audio_url = the telegram link near the header.
An answer key like "28 … (a)" maps question numbers 28-31 to statement letters; statements a)-f) follow.
- option_pool: the six statements a)-f)
- questions 28-31: type "match", text = "Nummer <N>", answer = the letter from the key
- texts: one entry per dialogue, title = "Nummer <N>", content = the dialogue transcript"""),

'telefonnotiz': """Parse German B2 Beruf Telefonnotiz exercises from the Markdown below.

Find all sections starting with "Telefonnotiz (вариант №".
Each variant may have multiple versions (Старый вариант / Новый вариант / dates).

For each variant return:
{
  "variant_number": <integer>,
  "topic": "<topic from header e.g. Büromaterialien>",
  "versions": [
    {
      "label": "<Старый вариант | Новый вариант | date | empty string>",
      "audio_url": "<url or null>",
      "monologue": "<full spoken text>",
      "answer": {
        "call_type": "<Beschwerde | Angebot | Buchung | Anfrage>",
        "name": "<caller name>",
        "telefonnummer": "<phone number>",
        "weitere_informationen": ["<bullet 1>", "<bullet 2>"],
        "zu_erledigen": "<action>"
      }
    }
  ]
}

Rules:
- Ignore page numbers and Russian meta-text
- "– 100%" in header = high confidence, ignore suffix
- Return ONLY a valid JSON array. No markdown wrapper, no explanation.

MARKDOWN:
{markdown}""",

'sprachbausteine_teil1': """Parse German B2 Beruf Sprachbausteine Teil 1 from the Markdown below.

Find all sections starting with "Sprachbausteine Teil 1 (вариант №".
For each variant return:
{
  "variant_number": <integer>,
  "version": "<short version label or null>",
  "topic": "<letter topic>",
  "letter_text": "<letter text with blanks as [46], [47], ... [51]>",
  "answers": [
    {"question_number": 46, "letter": "e", "word": "SICHER"},
    {"question_number": 47, "letter": "d", "word": "FÜR"},
    {"question_number": 48, "letter": "b", "word": "DA"},
    {"question_number": 49, "letter": "f", "word": "SONDERN"},
    {"question_number": 50, "letter": "g", "word": "UM"},
    {"question_number": 51, "letter": "a", "word": "BESTIMMT"}
  ],
  "all_options": [
    {"letter": "a", "text": "BESTIMMT"},
    {"letter": "b", "text": "DA"},
    {"letter": "c", "text": "DAMIT"},
    {"letter": "d", "text": "FÜR"},
    {"letter": "e", "text": "SICHER"},
    {"letter": "f", "text": "SONDERN"},
    {"letter": "g", "text": "UM"},
    {"letter": "h", "text": "ÜBER"},
    {"letter": "i", "text": "WEGEN"},
    {"letter": "j", "text": "WIE"}
  ]
}

Rules:
- Inline blanks appear as "46 (e- sicher)" — convert to [46] markers in letter_text. The marker fully REPLACES that whole inline chunk, including the answer word itself — never leave the word ALSO present as plain text right after the marker (e.g. "...mit Menschen, [49] sondern auch..." is WRONG if 49's answer is "SONDERN"; it must read "...mit Menschen, [49] auch...").
- De-hyphenate words the PDF split across a print line break (e.g. "Ausbildungs-\nkonzept" -> "Ausbildungskonzept") — letter_text must read as normal continuous prose, no stray hyphens or line breaks mid-word.
- Question numbers are 46–51 (or 42–51 depending on variant)
- VERSIONS: headers like "Sprachbausteine Teil 1 (вариант №3)(новая версия от …)" are reworked editions — output each as a SEPARATE object: same variant_number, distinct "version" label (null for the original), with its own complete letter_text, answers and all_options.
- Ignore page numbers and Russian meta-text
- Return ONLY a valid JSON array. No markdown wrapper, no explanation.

MARKDOWN:
{markdown}""",

}
