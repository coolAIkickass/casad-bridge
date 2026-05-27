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
        # Prompt teaches Whisper decimal formatting. Values differ from real inspection
        # figures so Whisper doesn't treat this as a prior-transcript prefix.
        prompt=(
            "Measurements use decimals like 0.45 m, 7.5 m, 10.1 m, 0.5 m. "
            "Span lengths: 10.1 meter, 23.5 meter. "
            "Carriageway: 0.45 m, barrier 7.5 m + 0.3 m median."
        ),
        # No language forced — Whisper auto-detects Hindi/Gujarati/English
    )
    return transcript.text
