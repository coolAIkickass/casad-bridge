# transcribe.py — OpenAI Whisper API for voice note transcription
import io, os
from openai import OpenAI

client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))


def transcribe_audio(audio_bytes: bytes, language: str = 'hi') -> str:
    """
    Transcribe audio bytes using Whisper.
    language='hi' handles Hindi/Gujarati/English mixed audio well.
    """
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = 'note.ogg'   # WhatsApp sends OGG Opus

    transcript = client.audio.transcriptions.create(
        model='whisper-1',
        file=audio_file,
        language=language,
    )
    return transcript.text
