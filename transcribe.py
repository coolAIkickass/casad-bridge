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
        # Prompt teaches the "X m plus Y m" decimal pattern with values that differ
        # from actual audio so Whisper doesn't treat it as a prior-transcript prefix.
        # temperature=0.2 reduces greedy shortcutting through repetitive measurement chains.
        prompt=(
            "Bridge inspection report. Decimal measurements: span 10.1 m, width 8.0 m. "
            "Both side 0.6 m crash barrier plus 8.0 m carriageway plus 1.2 m median "
            "plus 8.0 m carriageway plus 0.6 m crash barrier, no footpath provided."
        ),
        temperature=0.2,
        # No language forced — Whisper auto-detects Hindi/Gujarati/English
    )
    return transcript.text
