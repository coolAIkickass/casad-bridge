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
        # No language forced — Whisper auto-detects Hindi/Gujarati/English
    )
    return transcript.text
