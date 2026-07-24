"""Transcripcion de notas de voz (checkpoint 6).

Descarga el audio que entrega ManyChat y lo transcribe con OpenAI (gpt-4o-transcribe).
Credencial propia de este proyecto (OPENAI_API_KEY), NO la del proyecto n8n.
Si no hay credencial, devuelve un marcador para que el agente pida el mensaje por texto.
"""
from __future__ import annotations

import httpx

from .config import cfg


def transcribir(audio_url: str) -> str:
    """Devuelve el texto transcrito del audio, o un marcador si no es posible."""
    if not cfg.openai_api_key:
        return "[audio recibido — transcripcion no configurada; pedir al cliente que escriba su mensaje]"
    try:
        audio = httpx.get(audio_url, timeout=30.0)
        audio.raise_for_status()
        resp = httpx.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
            files={"file": ("nota.ogg", audio.content, "audio/ogg")},
            data={"model": "gpt-4o-transcribe", "language": "es"},
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json().get("text", "").strip() or "[audio sin texto reconocible]"
    except httpx.HTTPError:
        return "[no se pudo transcribir el audio; pedir al cliente que escriba su mensaje]"
