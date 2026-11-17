"""Optional ElevenLabs text-to-speech endpoint."""
import os

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from interfaces.api.routes.security import require_uosa_scope

router = APIRouter()
DEFAULT_VOICE_ID = "R8ZkkN7zI7hdqzfZoX5O"
MAX_TEXT_LENGTH = 5000
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech"


class VoiceRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)


@router.post("/voice", dependencies=[Depends(require_uosa_scope("chat.use"))])
def generate_voice(request: VoiceRequest):
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Voice service is not configured.",
        )

    voice_id = os.getenv("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID)
    if not voice_id or len(voice_id) > 128:
        raise HTTPException(
            status_code=503,
            detail="Voice service is not configured correctly.",
        )

    try:
        result = requests.post(
            f"{ELEVENLABS_URL}/{voice_id}",
            params={"output_format": "mp3_44100_128"},
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": request.text,
                "model_id": "eleven_multilingual_v2",
            },
            timeout=(5, 45),
        )
        result.raise_for_status()
    except requests.RequestException as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status == 429:
            raise HTTPException(
                status_code=429,
                detail="Voice service is temporarily rate-limited.",
            ) from None
        raise HTTPException(
            status_code=502,
            detail="Voice generation failed. Text chat is still available.",
        ) from None

    if not result.content:
        raise HTTPException(
            status_code=502,
            detail="Voice service returned empty audio.",
        )

    return Response(content=result.content, media_type="audio/mpeg")
