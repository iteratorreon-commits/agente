"""Canal interno por Telegram: escalaciones y avisos de pago para Benny y finanzas.

Por que Telegram y no WhatsApp para lo interno:
- WhatsApp/Meta solo deja enviar dentro de una ventana de 24h desde el ultimo mensaje
  del destinatario. Una escalacion de madrugada, con Benny sin escribirle al bot en
  todo el dia, se PERDIA en silencio mientras al cliente ya se le dijo "en un momento
  le confirmo". Telegram no tiene esa ventana.
- Separa el canal interno del canal de clientes: los mensajes de Benny ya no entran al
  agente vendedor como si fuera una conversacion de venta.
- No consume cuota de ManyChat y finanzas no necesita ser un subscriber de WhatsApp.

Contrato identico al de manychat_api.enviar_mensaje: devuelve {"ok": bool, "detalle": str}
y NUNCA lanza excepcion, para que el caller la registre en la bitacora.
"""
from __future__ import annotations

import httpx

from .config import cfg

_API = "https://api.telegram.org/bot{token}/{metodo}"
# Telegram corta los mensajes en 4096 caracteres.
_MAX = 4000


def configurado() -> bool:
    """True si hay token y al menos un destinatario. Si es False, el caller cae a WhatsApp."""
    return bool(cfg.telegram_bot_token and destinatarios())


def destinatarios() -> list[str]:
    """Chats internos: Benny + finanzas, sin repetir y conservando el orden."""
    ids: list[str] = []
    for cid in [cfg.telegram_chat_id, *cfg.telegram_notify_chat_ids]:
        if cid and cid not in ids:
            ids.append(cid)
    return ids


def enviar_telegram(texto: str, chat_id: str = "", imagen_url: str = "") -> dict:
    """Envia a UN chat de Telegram. Si no se pasa chat_id, usa el de Benny."""
    if not cfg.telegram_bot_token:
        return {"ok": False, "detalle": "falta TELEGRAM_BOT_TOKEN"}
    destino = chat_id or cfg.telegram_chat_id
    if not destino:
        return {"ok": False, "detalle": "falta TELEGRAM_CHAT_ID"}

    texto = (texto or "")[:_MAX]
    if imagen_url:
        metodo, payload = "sendPhoto", {"chat_id": destino, "photo": imagen_url, "caption": texto[:1000]}
    else:
        metodo, payload = "sendMessage", {"chat_id": destino, "text": texto}

    try:
        resp = httpx.post(
            _API.format(token=cfg.telegram_bot_token, metodo=metodo), json=payload, timeout=20.0
        )
        resp.raise_for_status()
        return {"ok": True, "detalle": "enviado"}
    except httpx.HTTPError as exc:
        return {"ok": False, "detalle": f"error Telegram: {exc}"}


def difundir(texto: str, imagen_url: str = "") -> dict:
    """Envia a TODOS los chats internos. Devuelve {'enviados','fallidos','total'}."""
    enviados, fallidos = [], []
    for cid in destinatarios():
        res = enviar_telegram(texto, chat_id=cid, imagen_url=imagen_url)
        (enviados if res["ok"] else fallidos).append(cid)
    return {"enviados": enviados, "fallidos": fallidos, "total": len(destinatarios())}
