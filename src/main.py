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

import base64
import re

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from . import aprendizajes, decision_log, escalation_rules, request_context, session_store
from .agent import responder
from .config import cfg
from .manychat_api import enviar_mensaje
from .tools.manychat_tools import escalar_impl, notificar_pago_impl
from .transcribe import transcribir

app = FastAPI(title="Agente Vendedor WhatsApp — ITERA")

_TIPOS_IMG = ("image/jpeg", "image/png", "image/gif", "image/webp")
_URL_SOLA = re.compile(r"^https?://\S+$")
# Comando con el que Benny le ENSENA algo al agente por WhatsApp.
_APRENDE_RE = re.compile(
    r"^\s*(?:aprende|aprender|aprendizaje|nota|recuerda)\s*[:\-]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def _es_url_sola(texto: str) -> bool:
    """True si el texto es UNA sola URL (sin nada mas)."""
    return bool(_URL_SOLA.match((texto or "").strip()))


def _tipo_media(url: str) -> str:
    """Clasifica una URL por su content-type: 'image' | 'audio' | 'video' | 'other'.

    ManyChat mete la URL del archivo en 'Ultima entrada de texto' cuando el cliente manda
    media en WhatsApp (imagen/audio/video), incluso en Default Reply. Aqui la clasificamos
    para enrutarla bien. Usa HEAD (barato); si el CDN no responde HEAD, cae a un GET.
    """
    try:
        r = httpx.head(url, timeout=15, follow_redirects=True)
        ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if not ct:
            r = httpx.get(url, timeout=20, follow_redirects=True)
            ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
    except httpx.HTTPError:
        return "other"
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("audio/"):
        return "audio"
    if ct.startswith("video/"):
        return "video"
    return "other"


def _bloque_imagen(url: str) -> dict:
    """Convierte la URL de una imagen entrante (ManyChat/WhatsApp) en un bloque para el modelo.

    Baja los bytes del lado del server y los manda como base64: evita que la API de Anthropic
    tenga que ir por la URL (que puede estar bloqueada por robots.txt, requerir token o expirar).
    Si la descarga falla, cae a pasar la URL directo (mejor esfuerzo).
    """
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        media = r.headers.get("content-type", "image/jpeg").split(";")[0].strip().lower()
        if media not in _TIPOS_IMG:
            media = "image/jpeg"
        b64 = base64.standard_b64encode(r.content).decode()
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}}
    except (httpx.HTTPError, ValueError):
        return {"type": "image", "source": {"type": "url", "url": url}}


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

    # ManyChat NO expone la imagen como campo aparte: cuando el cliente manda media en
    # WhatsApp, mete la URL del archivo dentro de 'Ultima entrada de texto' (el campo
    # 'text'). Si el "texto" es en realidad una sola URL de media, la reenrutamos como
    # imagen o audio. Es seguro: con texto normal, _es_url_sola es False y no hace nada.
    if texto and not image_url and not audio_url and _es_url_sola(texto):
        tipo = _tipo_media(texto)
        print(f"MEDIA detectada tipo={tipo} url={texto.strip()[:200]}", flush=True)
        if tipo == "image":
            image_url, texto = texto.strip(), ""
        elif tipo == "audio":
            audio_url, texto = texto.strip(), ""
        else:
            # video / archivo / desconocido: el modelo no los procesa. Antes se dejaba la
            # URL cruda como si fuera el mensaje del cliente y el agente intentaba
            # interpretar el link. Se reemplaza por un marcador para que pida una foto o
            # texto. NO es un caso raro: en el bimestre el 5.6% de los mensajes de
            # clientes son video (1,626), diez veces mas que los audios.
            etiqueta = "un video" if tipo == "video" else "un archivo"
            texto = (
                f"[El cliente envio {etiqueta}, que no puedo ver. Pedirle amablemente una "
                "FOTO del modelito o que escriba lo que necesita.]"
            )

    # --- ENSENANZA de Benny (solo el dueno): 'APRENDE: <indicacion>' ---
    # Guarda la indicacion como conocimiento permanente (se inyecta en el prompt de todas
    # las conversaciones) para que el agente no vuelva a preguntar/escalar lo mismo. Este
    # mensaje NO corre por el agente.
    if subscriber_id == cfg.benny_subscriber_id and texto:
        m = _APRENDE_RE.match(texto)
        if m:
            entrada = aprendizajes.agregar(m.group(1).strip(), autor="Benny")
            enviar_mensaje(
                subscriber_id,
                f'✅ Aprendido y guardado:\n"{entrada["texto"]}"\n\n'
                "Lo aplicare de aqui en adelante 🙌",
            )
            decision_log.registrar(
                subscriber_id, texto, accion="aprendizaje", respuesta=entrada["texto"]
            )
            return

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
        contenido.append(_bloque_imagen(image_url))
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
