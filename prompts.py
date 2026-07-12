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
- VERSIONS: the same variant often appears several times — the original plus reworked editions marked "Новая версия", "Новый вариант", a date, or "(тест №…)", where the rework covers MULTIPLE questions. Output EACH complete edition as its OWN object: same variant_number, but a distinct "version" label ("Neue Version 08.2024", "Test 150321", …; null for the original). Every edition must be self-contained — if it does not repeat the reading text or option pool, copy them from the original variant into it. Do NOT mix questions of different editions in one object.
- A label like "Новый вариант от <date>" or "Варианты ответов от <date>" placed right after ONE SINGLE already-numbered question, giving that ONE question new a)/b)/c) options, is NOT an edition — it's a later correction to that one question, and the whole variant stays ONE object. Between the two option blocks for that question number, use whichever one has a clearly marked "– 100%"/correct answer; if one block has an incomplete option (a blank option, or one ending in "?"), it is not usable — use the other block. Never output two objects, and never two competing answers for the same question number, over a single-question correction like this.
- SEGMENTATION: the input is pre-split into blocks separated by a line containing only <<<ITEM>>>. Each block was already identified as one distinct, complete variant or edition — output exactly one object per block, in the same order. Never merge two blocks into one object and never skip a block, even if two blocks look very similar to each other.
- Every edition (including reworked ones) must contain its OWN full "texts" and "option_pool" — repeat the identical content word-for-word if it doesn't change between editions. Never leave these empty or shortened, and never use a placeholder in place of the real content.
- "texts[].content" and every "options[].text" must be the EXACT wording from the source, copied verbatim (aside from the de-hyphenation rule below) — never summarize, shorten, or paraphrase a transcript/passage/option into your own words. A student preparing for the real exam needs to see precisely what would appear on it. Where a "text" (question stem) isn't printed verbatim in the source and must be phrased to introduce the options, keep it minimal and neutral rather than inventing exam-style phrasing not in the source.
- The response format requires EXACTLY the number of questions the section description above specifies, every time, for every edition — never fewer. Never invent facts, but every required question slot must still get its best-supported answer from context; if a marker is ambiguous, use the most clearly-marked or most complete option rather than omitting the question.
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

Also copy "anchor": the line's own text (everything after the ": " \
prefix, verbatim, no truncation) for EVERY item — this is a checked \
cross-reference for start_line, not optional decoration, so it must be \
the ACTUAL text at that exact line, copied character-for-character, \
never paraphrased, shortened, or copied from a nearby line instead.

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
- hoeren_teil4: five short phone announcements, each followed by its own multiple-choice question

VERSIONS: a labeled follow-up block ("Новая версия", "Neue Version", \
"Варианты ответов от <date>", "Другой вариант ответов", "Старый вариант \
вопросов", or similar — the label text alone is NOT the deciding \
signal, see below) can mean one of two completely different things, and \
you must count questions to tell them apart BEFORE deciding anything \
else:

1. It reworks MULTIPLE questions at once (most/all of the variant's \
questions get a new option set) → this IS a new edition. Output it as \
its own separate item, distinct version_label, marked at wherever ITS \
OWN questions begin, not the shared passage/dialogue above them. The \
original edition gets version_label: null.

2. It reworks exactly ONE already-numbered question and gives only THAT \
question new answer options → this is NOT an edition, it is a later \
correction to one question's answer. Do NOT emit a start_line for it, \
do NOT create a new item, and do NOT let it end the current item early \
— it is content belonging to the SAME item as the question it corrects, \
and everything after it (up through wherever the NEXT real question or \
exercise actually begins) still belongs to that same item too. This \
holds no matter how the label reads, even if it superficially resembles \
the multi-question case (mentions a date, says "Варианты"/"variants" \
plural, etc.) — the label text is decorative, only the question COUNT \
decides. A variant can contain several such single-question corrections \
scattered after different questions (e.g. one after question 36, a \
separate unrelated one after question 38) — evaluate each one on its \
own by the same one-question test; several single-question corrections \
in the same variant never add up to case 1, and none of them ever gets \
a start_line. A trivial reword of a single question with no new options \
at all is likewise not an edition — skip it, same as case 2.

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
- questions 32-35: type "choice" with a) b) c)
- The same conversation sometimes has TWO question sets back to back — a current one and an older one (labeled "Старый вариант вопросов" or similar), where one set has an incomplete/unanswerable option (e.g. "c) ?") and the other has all 4 answers clearly known. Do NOT merge or choose between them in one object — that always means an edition, exactly like VERSIONS above: output the fully-answered set as one object and the other as its own object with a distinct version label, both sharing variant_number, texts and audio_url via DEDUPLICATION. Every question in every edition must have a determinable answer — never output a question with no answer."""),

'hoeren_teil4': _u("""Section: Hören Teil 4 (short phone messages).
Variants start with "Hören Teil 4 (вариант №".
audio_url = the telegram link near the header.
Five messages "Nummer 36".."Nummer 40", each followed by a choice question with the same number.
- texts: one entry per message, title = "Nummer <N> <name>", content = the message transcript
- questions 36-40: type "choice" with a) b) c); the correct option is marked "– 100%"
- A question sometimes has a SECOND a) b) c) block right after it, introduced by "Варианты ответов от <date>" — this is a later correction to that ONE question's answer, not a new variant. Pick whichever block has a clearly marked "– 100%" answer; if a block has an incomplete option (a blank line after "b)", or "c) ?" with no text), it is not usable — use the other block instead. Output exactly ONE answer per question number, 5 questions total — never two objects and never two competing answers for the same number."""),

'hoeren_teil1': """Parse German B2 Beruf exam Hören Teil 1 exercises from the Markdown below.

Find all sections starting with "Hören Teil 1 (вариант №".
For each variant return a JSON object:
{
  "variant_number": <integer>,
  "version": "<short version label or null>",
  "audio_url": "<url or null>",
  "question_pairs": [<pair 1>, <pair 2>, <pair 3>]
}
question_pairs ALWAYS has exactly 3 entries, in that order, and EVERY
entry is always a full pair object — never omitted, never a partial
object, never a placeholder string, even when a reworked edition's block
only restates one pair's content (see DEDUPLICATION below for what to do
with the other two).

A full pair object:
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

Rules:
- audio_url: single URL at top of variant; null if absent
- pair_audio_url: fill only if separate URL appears before each "Nummer N und N"
- Determine richtig_falsch.answer and multiple_choice.correct_letter from whatever marks the correct one in the source: "– 100%", "(100%)", a letter written after the item, or similar markers; if genuinely unmarked, decide from the dialogue content instead of leaving it undetermined.
- "dialogue" and every option "text" must be the EXACT wording from the source, copied verbatim — never summarize, shorten, or paraphrase into your own words. A student preparing for the real exam needs to see precisely what would appear on it.
- VERSIONS: if the variant appears as a reworked edition ("Новая версия", "Новый вариант от <дата>", "(тест №…)"), output it as a SEPARATE object: same variant_number, distinct "version" label (null for the original). A lone alternative wording of a single question is NOT an edition — keep the answered one.
- SEGMENTATION: the input is pre-split into blocks separated by a line containing only <<<ITEM>>>. Each block was already identified as one distinct edition — output exactly one object per block, in the same order, never merging or skipping a block. A block for a reworked edition often restates only the ONE pair that actually changed (e.g. just "Nummer 24 und 25") — that is normal, not an error; see DEDUPLICATION for how to fill the other two entries.
- DEDUPLICATION: for a reworked edition (version is not null), any question_pairs entry the block doesn't restate, or restates word-for-word identically to the original variant's (version: null) same-position entry, must be COPIED from that original entry word-for-word — repeat its full pair object, do not invent different content and do not leave it out. Give a pair its own (possibly changed) content only when the edition's block actually contains it. The original variant itself must always contain full objects for all 3 pairs.
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
- SEGMENTATION: the input is pre-split into blocks separated by a line containing only <<<ITEM>>>. Each block is one distinct edition of a variant, already identified as separate. Group blocks that share the same variant_number under one object, but include EVERY block as its own entry in that object's "versions" list — never merge two blocks into one versions entry and never skip a block, even if two blocks look very similar to each other.
- "monologue" must be the EXACT wording from the source, copied verbatim — never summarize or shorten it. A student preparing for the real exam needs to see precisely what would appear on it.
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
- SEGMENTATION: the input is pre-split into blocks separated by a line containing only <<<ITEM>>>. Each block was already identified as one distinct, complete variant or edition — output exactly one object per block, in the same order. Never merge two blocks into one object and never skip a block, even if two blocks look very similar to each other.
- DEDUPLICATION: for a reworked edition (version is not null), if its "letter_text" or "all_options" would be word-for-word IDENTICAL to the original variant's (version: null), COPY them over word-for-word — every object needs its own real, complete "letter_text" and "all_options", never left empty or shortened just because another edition already has the same content.
- Ignore page numbers and Russian meta-text
- Return ONLY a valid JSON array. No markdown wrapper, no explanation.

MARKDOWN:
{markdown}""",

}
