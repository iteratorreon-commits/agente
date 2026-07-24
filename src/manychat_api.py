"""Cliente de la Sending API de ManyChat para enviar mensajes 1:1 salientes.

Se usa para: responder al cliente en el mismo flujo, escalar a Benny y notificar
pagos a varias personas. Meta impone una ventana de 24h para mensajes fuera de
plantilla; para notificaciones fuera de esa ventana puede requerirse un message tag
aprobado (checkpoint 2: confirmar en ManyChat).
"""
from __future__ import annotations

import httpx

from .config import cfg

_SEND_URL = "https://api.manychat.com/fb/sending/sendContent"


def _internos() -> set[str]:
    """Subscribers 'internos' (Benny + finanzas): a estos SI se les envia en modo sombra."""
    ids = set(cfg.notify_subscriber_ids)
    if cfg.benny_subscriber_id:
        ids.add(cfg.benny_subscriber_id)
    return ids


def enviar_mensaje(subscriber_id: str, texto: str, imagen_url: str | None = None) -> dict:
    """Envia un mensaje de texto (y opcionalmente una imagen) a un subscriber de ManyChat.

    Devuelve {"ok": bool, "detalle": str}. No lanza excepcion: los errores se reportan
    en el dict para que el caller (tool/webhook) los registre en la bitacora.
    """
    if not cfg.manychat_token:
        return {"ok": False, "detalle": "falta MANYCHAT_API_TOKEN"}
    if not subscriber_id:
        return {"ok": False, "detalle": "subscriber_id vacio"}

    # Modo sombra: no se entrega NADA al cliente (ni texto, ni fotos, ni cotizacion);
    # solo se registra en la bitacora para revision. Los internos (Benny/finanzas) SI
    # reciben, para no perder escalaciones ni avisos de pago mientras se valida.
    if cfg.shadow_mode and subscriber_id not in _internos():
        return {"ok": False, "detalle": "sombra: no enviado al cliente (AGENTE_SHADOW_MODE)"}

    # Solo-imagen valido (texto vacio): no mandamos un bloque de texto vacio.
    messages: list[dict] = []
    if texto:
        messages.append({"type": "text", "text": texto})
    if imagen_url:
        messages.append({"type": "image", "url": imagen_url})
    if not messages:
        return {"ok": False, "detalle": "mensaje vacio (sin texto ni imagen)"}

    # Envio libre: valido dentro de la ventana de 24h de WhatsApp (respuesta al cliente,
    # o a Benny/finanzas si tienen sesion activa). Fuera de 24h WhatsApp exige una
    # PLANTILLA aprobada configurada en ManyChat (pendiente para notificaciones frias).
    payload = {
        "subscriber_id": subscriber_id,
        "data": {"version": "v2", "content": {"type": "whatsapp", "messages": messages}},
    }
    headers = {
        "Authorization": f"Bearer {cfg.manychat_token}",
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(_SEND_URL, json=payload, headers=headers, timeout=20.0)
        resp.raise_for_status()
        return {"ok": True, "detalle": "enviado"}
    except httpx.HTTPError as exc:
        return {"ok": False, "detalle": f"error ManyChat: {exc}"}
