"""German dialogue text-to-speech via Microsoft Edge TTS (free, no API key).

Voice assignment is ported from the standalone scripts in
deutch-lernen/tts/generate_teil*.py, adapted to be stateless per request:
each speaker name deterministically hashes to a voice, so the same
speaker gets the same voice across separate /api/tts calls for one
dialogue (the client requests one line at a time).
"""
import hashlib
import edge_tts

MALE_VOICES = ["de-DE-FlorianMultilingualNeural", "de-DE-ConradNeural"]
FEMALE_VOICES = ["de-DE-KatjaNeural", "de-DE-AmalaNeural"]

_MALE_LABELS = {"chef", "leiter", "teamleiter", "verkäufer", "herr", "timbur"}
_FEMALE_LABELS = {"chefin", "leiterin", "verkäuferin", "kundin", "frau"}
_MALE_NAMES = {"karl", "zarif", "markus", "thomas", "mustafa", "ignacio",
               "tim", "joshua", "simmering"}
_FEMALE_NAMES = {"michaela", "melanie", "sandra", "amira", "anita",
                  "alimi", "barthum", "tn"}


def _gender(speaker: str) -> str:
    first = speaker.lower().split()[0] if speaker.strip() else ''
    if first in _MALE_LABELS or first in _MALE_NAMES:
        return 'male'
    if first in _FEMALE_LABELS or first in _FEMALE_NAMES:
        return 'female'
    if speaker.lower() in _MALE_NAMES:
        return 'male'
    if speaker.lower() in _FEMALE_NAMES:
        return 'female'
    return 'unknown'


def _stable_index(s: str, n: int) -> int:
    # Python's builtin hash() is randomized per-process — must use a
    # stable hash so the same speaker maps to the same voice across
    # independent /api/tts calls (and across cold starts).
    return int(hashlib.md5(s.encode('utf-8')).hexdigest(), 16) % n


def voice_for(speaker: str) -> str:
    speaker = speaker.strip()
    if not speaker:
        return MALE_VOICES[0]
    g = _gender(speaker)
    pool = {'male': MALE_VOICES, 'female': FEMALE_VOICES}.get(
        g, MALE_VOICES + FEMALE_VOICES)
    return pool[_stable_index(speaker, len(pool))]


async def synthesize(text: str, voice: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice)
    chunks = bytearray()
    async for chunk in communicate.stream():
        if chunk['type'] == 'audio':
            chunks.extend(chunk['data'])
    return bytes(chunks)
