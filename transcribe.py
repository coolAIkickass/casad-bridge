# transcribe.py — Groq Whisper API for voice note transcription (free tier)
import io, os
from groq import Groq

client = Groq(api_key=os.getenv('GROQ_API_KEY'))


def transcribe_audio(audio_bytes: bytes) -> str:
    """Transcribe OGG Opus audio bytes using Groq's free Whisper endpoint."""
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = 'note.ogg'   # WhatsApp sends OGG Opus

    transcript = client.audio.transcriptions.create(
        model='whisper-large-v3',
        file=audio_file,
        # Prompt primes Whisper to retain decimal values (e.g. 0.45 m, 7.5 m)
        # which it otherwise drops in repetitive measurement chains near segment boundaries.
        prompt=(
            "Bridge inspection report. Measurements use decimals: 0.45 m, 7.5 m, "
            "10.1 m, 22.4 m, 0.9 m. Span lengths: 10.1 meter, 23.5 meter. "
            "Carriageway: 0.45 m crash barrier plus 7.5 m carriageway plus 0.9 m median."
        ),
        # No language forced — Whisper auto-detects Hindi/Gujarati/English
    )
    return transcript.text
