import unittest

import tts


class TtsVoiceSelectionTest(unittest.TestCase):
    def test_andrea_uses_a_female_voice(self):
        voice = tts.voice_for("Andrea Faber")

        self.assertIn(voice, tts.FEMALE_VOICES)
        self.assertNotIn(voice, tts.MALE_VOICES)


if __name__ == "__main__":
    unittest.main()
