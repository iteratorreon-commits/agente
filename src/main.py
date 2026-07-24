"""Servicio FastAPI: webhook que recibe mensajes de ManyChat y responde con el agente.

Flujo por mensaje (ASINCRONO, para respetar el limite de 10s de ManyChat):
1. Verifica el header x-itera-token (secreto compartido con ManyChat).
2. Responde 200 OK de inmediato (la Solicitud externa de ManyChat corta a los 10s;
   el agente + tools de Odoo tardan mas, asi que NO se procesa en linea).
3. En segundo plano: normaliza el payload (texto/imagen/audio), corre el gate
   determinista (pago/queja) y luego el agente, y ENTREGA la respuesta al cliente
   por la Sending API de ManyChat (sendContent), no por la respuesta del webhook.
Cada turno queda en la bitacora de decisiones para el loop de mejora.

Bucle de conversacion: NO se arma con una flecha de regreso (ManyChat no lo permite).
El disparador 'Default Reply' re-dispara el flujo con CADA mensaje del cliente; el
flujo en ManyChat debe ser solo: Default Reply -> Solicitud externa (sin paso de
'Respuesta del contacto'/User Input, que se comeria el siguiente mensaje).
"""
from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from . import decision_log, escalation_rules, request_context, session_store
from .agent import responder
from .config import cfg
from .manychat_api import enviar_mensaje
from .tools.manychat_tools import escalar_impl, notificar_pago_impl
from .transcribe import transcribir

app = FastAPI(title="Agente Vendedor WhatsApp — ITERA")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _extraer(payload: dict) -> tuple[str, str, str, str]:
    """Devuelve (subscriber_id, texto, image_url, audio_url) de forma tolerante a nombres."""
    sid = str(
        payload.get("subscriber_id")
        or payload.get("id")
        or payload.get("contact_id")
        or ""
    )
    texto = (
        payload.get("raw_query")
        or payload.get("text")
        or payload.get("last_input_text")
        or payload.get("message")
        or ""
    )
    image_url = payload.get("image_url") or payload.get("image") or ""
    audio_url = payload.get("audio_url") or payload.get("audio") or ""
    return sid, str(texto), str(image_url), str(audio_url)


@app.post("/manychat/inbound")
async def inbound(
    request: Request,
    background_tasks: BackgroundTasks,
    x_itera_token: str = Header(default=""),
) -> dict:
    if not cfg.webhook_secret or x_itera_token != cfg.webhook_secret:
        raise HTTPException(status_code=401, detail="token invalido")

    payload = await request.json()
    subscriber_id, texto, image_url, audio_url = _extraer(payload)
    if not subscriber_id:
        raise HTTPException(status_code=400, detail="falta subscriber_id")

    # No se procesa en linea: la Solicitud externa de ManyChat corta a los 10s y el
    # agente/tools tardan mas. Se agenda en segundo plano y se responde YA con 200 OK;
    # la respuesta real se le entrega al cliente por la Sending API (ver _procesar).
    background_tasks.add_task(_procesar, subscriber_id, texto, image_url, audio_url)
    return {"status": "accepted"}


def _entrega(res: dict) -> str:
    """Normaliza el resultado de enviar_mensaje para la bitacora: 'ok', 'sombra' o 'FALLO'."""
    if res.get("ok"):
        return "ok"
    detalle = res.get("detalle", "desconocido")
    if detalle.startswith("sombra"):
        return "sombra (no enviado al cliente)"
    return f"FALLO: {detalle}"


def _procesar(subscriber_id: str, texto: str, image_url: str, audio_url: str) -> None:
    """Procesa un mensaje y ENTREGA la respuesta al cliente por la Sending API de ManyChat.

    Corre en segundo plano (BackgroundTasks). Nunca lanza: todo fallo se registra en la
    bitacora y se degrada con un mensaje generico enviado al cliente.
    """
    # Deja el subscriber_id disponible a las tools que envian media (fotos) al cliente.
    request_context.current_subscriber_id.set(subscriber_id)

    # Audio -> transcripcion (se trata como texto del cliente).
    if audio_url:
        try:
            texto = (texto + " " + transcribir(audio_url)).strip()
        except Exception as exc:  # noqa: BLE001
            decision_log.registrar(subscriber_id, texto, accion="error", error=f"audio: {exc}")

    # Guarda contra merge fields de ManyChat SIN resolver (ej. "{{last_input_text}}"
    # literal): pasa cuando el body de la Solicitud externa usa un campo mal escrito.
    # No es un mensaje real del cliente -> se ignora el texto para no correr el agente
    # sobre basura, y se registra para que se corrija en ManyChat.
    if texto and "{{" in texto and "}}" in texto:
        decision_log.registrar(
            subscriber_id,
            texto,
            accion="error",
            error="merge_field_sin_resolver: el body de la Solicitud externa en ManyChat "
            "manda el placeholder literal en vez del mensaje del cliente",
        )
        texto = ""

    tiene_imagen = bool(image_url)

    # --- Gate determinista (antes del LLM) ---
    accion = escalation_rules.evaluar(texto, tiene_imagen)
    if accion and accion["tipo"] == "pago":
        notificar_pago_impl(
            mensaje=f"Cliente {subscriber_id} reporta un pago. Texto: {texto or '(solo imagen)'}",
            comprobante_url=image_url,
        )
        res = enviar_mensaje(subscriber_id, accion["mensaje_cliente"])
        decision_log.registrar(
            subscriber_id,
            texto,
            accion="gate_pago",
            respuesta=accion["mensaje_cliente"],
            entrega=_entrega(res),
        )
        return

    if accion and accion["tipo"] == "queja":
        escalar_impl(
            motivo="Queja/garantia detectada por gate",
            contexto=f"Cliente {subscriber_id}: {texto}",
            urgente=True,
        )
        res = enviar_mensaje(subscriber_id, accion["mensaje_cliente"])
        decision_log.registrar(
            subscriber_id,
            texto,
            accion="escalo",
            motivo_escalacion="queja/garantia (gate)",
            respuesta=accion["mensaje_cliente"],
            entrega=_entrega(res),
        )
        return

    # --- Camino normal: agente ---
    turnos, slots = session_store.cargar(subscriber_id)

    contenido: list[dict] = []
    if texto:
        contenido.append({"type": "text", "text": texto})
    if image_url:
        contenido.append(
            {"type": "image", "source": {"type": "url", "url": image_url}}
        )
    if not contenido:
        contenido.append({"type": "text", "text": "(el cliente envio un mensaje sin texto)"})

    messages = list(turnos) + [{"role": "user", "content": contenido}]

    try:
        texto_respuesta, tools_usadas = responder(messages)
    except Exception as exc:  # noqa: BLE001 — cualquier fallo se registra y se degrada con gracia
        res = enviar_mensaje(
            subscriber_id, "Permítame confirmarle eso en un momento, por favor 🙏"
        )
        decision_log.registrar(
            subscriber_id, texto, accion="error", error=str(exc), entrega=_entrega(res)
        )
        return

    # Guardar historial (solo texto; las imagenes no se re-persisten).
    nuevos_turnos = list(turnos) + [
        {"role": "user", "content": texto or "(imagen)"},
        {"role": "assistant", "content": texto_respuesta},
    ]
    session_store.guardar(subscriber_id, nuevos_turnos, slots)

    # Entregar al cliente y registrar el turno CON el resultado de la entrega, para
    # detectar el caso 'respondio en el log pero el cliente no recibio nada'.
    res = enviar_mensaje(subscriber_id, texto_respuesta)
    accion_log = "escalo" if "escalar_a_benny" in tools_usadas else "respondio"
    decision_log.registrar(
        subscriber_id,
        texto,
        accion=accion_log,
        respuesta=texto_respuesta,
        tools_invocadas=tools_usadas,
        entrega=_entrega(res),
    )
