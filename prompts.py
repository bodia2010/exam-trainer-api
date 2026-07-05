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
- Ignore page numbers (lines with only digits) and Russian meta-commentary
- Return ONLY a valid JSON array of variant objects. No markdown wrapper, no explanation.

MARKDOWN:
{markdown}"""


def _u(hint):
    return _UNIVERSAL.replace('{hint}', hint)


PROMPTS = {

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
The letter shows answers inline like "52 (b - eine Bestellung)" — replace each with a [52]-style marker.
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
- Inline blanks appear as "46 (e- sicher)" — convert to [46] markers in letter_text
- Question numbers are 46–51 (or 42–51 depending on variant)
- VERSIONS: headers like "Sprachbausteine Teil 1 (вариант №3)(новая версия от …)" are reworked editions — output each as a SEPARATE object: same variant_number, distinct "version" label (null for the original), with its own complete letter_text, answers and all_options.
- Ignore page numbers and Russian meta-text
- Return ONLY a valid JSON array. No markdown wrapper, no explanation.

MARKDOWN:
{markdown}""",

}
