PROMPTS = {

'hoeren_teil1': """Parse German B2 Beruf exam Hören Teil 1 exercises from the Markdown below.

Find all sections starting with "Hören Teil 1 (вариант №".
For each variant return a JSON object:
{
  "variant_number": <integer>,
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
- Ignore lines of only digits (page numbers) and Russian meta-text
- Return ONLY a valid JSON array. No markdown wrapper, no explanation.

MARKDOWN:
{markdown}""",

'hoeren_teil2': """Parse German B2 Beruf exam Hören Teil 2 exercises from the Markdown below.

Find all sections starting with "Hören Teil 2 (вариант №".
For each variant return:
{
  "variant_number": <integer>,
  "audio_url": "<url or null>",
  "options": [
    {"letter": "a", "text": "<statement>"},
    {"letter": "b", "text": "<statement>"},
    {"letter": "c", "text": "<statement>"},
    {"letter": "d", "text": "<statement>"},
    {"letter": "e", "text": "<statement>"},
    {"letter": "f", "text": "<statement>"}
  ],
  "questions": [
    {"number": 28, "monologue": "<full monologue>", "correct_letter": "<a-f>"},
    {"number": 29, "monologue": "<full monologue>", "correct_letter": "<a-f>"},
    {"number": 30, "monologue": "<full monologue>", "correct_letter": "<a-f>"},
    {"number": 31, "monologue": "<full monologue>", "correct_letter": "<a-f>"}
  ]
}

Rules:
- Answer key (28→a, 29→d, etc.) appears before dialogues — use for correct_letter
- If "Новый вариант ответов" block present, add "alternate_answers" field
- Ignore page numbers and Russian meta-text
- Return ONLY a valid JSON array. No markdown wrapper, no explanation.

MARKDOWN:
{markdown}""",

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
- Ignore page numbers and Russian meta-text
- Return ONLY a valid JSON array. No markdown wrapper, no explanation.

MARKDOWN:
{markdown}""",

}
