"""Servicio FastAPI: webhook que recibe mensajes de ManyChat y responde con el agente.

Flujo por mensaje (ASINCRONO, para respetar el limite de 10s de ManyChat):
1. Verifica el header x-itera-token (secreto compartido con ManyChat).
2. Responde 200 OK de inmediato (la Solicitud externa de ManyChat corta a los 10s;
   el agente + tools de Odoo tardan mas, asi que NO se procesa en linea).
3. En segundo plano: normaliza el payload (texto/imagen/audio), corre el gate
   determinista (pago/queja) y luego el agente, y ENTREGA la respuesta al cliente
   por la Sending API de ManyChat (sendContent), no por la respuesta del webhook.
Cada turno queda en la bitacora de decisiones para el loop de mejora.

Memoria: la transcripcion completa vive en SQLite (session_store.mensajes, append-only) y el
estado estructurado en la ficha (src/ficha.py). Cada turno se le manda al modelo una ventana de
los ultimos cfg.max_mensajes mas el bloque <estado_conversacion>. Antes solo se guardaban 20
entradas y se recortaba al ESCRIBIR, asi que el hilo viejo se borraba del disco y el agente
volvia a preguntar tallas y colores ya confirmados.

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

from . import (
    aprendizajes,
    decision_log,
    escalation_rules,
    ficha as ficha_mod,
    request_context,
    session_store,
)
from .agent import responder
from .config import cfg
from .manychat_api import enviar_mensaje, formato_whatsapp
from .telegram_api import enviar_telegram
from .tools.manychat_tools import escalar_impl, notificar_pago_impl
from .transcribe import transcribir

app = FastAPI(title="Agente Vendedor WhatsApp — ITERA")

_TIPOS_IMG = ("image/jpeg", "image/png", "image/gif", "image/webp")
_URL_SOLA = re.compile(r"^https?://\S+$")
# La URL puede venir en cualquier parte del texto, con el caption antes o despues.
_URL_EN_TEXTO = re.compile(r"https?://\S+")
# Comando con el que Benny le ENSENA algo al agente por WhatsApp.
_APRENDE_RE = re.compile(
    r"^\s*(?:aprende|aprender|aprendizaje|nota|recuerda)\s*[:\-]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
# Orden interna desde Telegram para actuar sobre la conversacion de UN cliente concreto:
#   CLIENTE 1491137321: mandale otra vez su cotizacion
# El '#' es un atajo equivalente. El subscriber_id de ManyChat es numerico.
_ORDEN_RE = re.compile(
    r"^\s*(?:cliente|#)\s*(\d{4,})\s*[:\-,]?\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def _es_url_sola(texto: str) -> bool:
    """True si el texto es UNA sola URL (sin nada mas)."""
    return bool(_URL_SOLA.match((texto or "").strip()))


def _separar_media(texto: str) -> tuple[str, str]:
    """Separa una URL de media del resto del texto. Devuelve (url, caption).

    Antes solo se detectaba la media cuando el texto era EXACTAMENTE una URL, asi que si
    ManyChat mandaba la foto y su caption juntos ('https://... me interesa en talla 6'),
    la foto no se detectaba y el modelo recibia la URL cruda como si fuera el mensaje.
    Ahora la URL se extrae venga donde venga y el caption se conserva.
    """
    m = _URL_EN_TEXTO.search(texto or "")
    if not m:
        return "", texto or ""
    # Colapsa el hueco que deja la URL al quitarla ("hola  <url>  cuanto?" -> "hola cuanto?").
    caption = re.sub(r"\s+", " ", texto[: m.start()] + " " + texto[m.end() :]).strip()
    return m.group(0), caption


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


@app.post("/telegram/inbound")
async def telegram_inbound(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Canal INTERNO: 'APRENDE:' para ensenar reglas y 'CLIENTE <id>: ...' para dar ordenes.

    Sigue sin ser un canal de conversacion: un mensaje suelto no corre el agente, para que un
    mensaje del equipo nunca se confunda con una venta (que es lo que pasaba en WhatsApp, donde
    un "CONTESTA AL CLIENTE" de Benny disparo notificar_pago_multiple). El agente solo se
    ejecuta con la forma explicita 'CLIENTE <subscriber_id>: <instruccion>', y siempre sobre la
    conversacion de ese cliente.
    """
    if cfg.telegram_webhook_secret:
        if request.headers.get("x-telegram-bot-api-secret-token") != cfg.telegram_webhook_secret:
            raise HTTPException(status_code=401, detail="secret invalido")

    update = await request.json()
    msg = update.get("message") or update.get("edited_message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    texto = (msg.get("text") or "").strip()
    if not chat_id:
        return {"status": "ignorado"}

    # /start y /id: para que Benny descubra su chat_id sin salir de Telegram.
    if texto.lower() in {"/start", "/id"}:
        enviar_telegram(
            f"Canal interno del agente vendedor ITERA 🤖\n\n"
            f"Tu chat_id es: {chat_id}\n\n"
            f"Ponlo en TELEGRAM_CHAT_ID (Benny) o en TELEGRAM_NOTIFY_CHAT_IDS (finanzas).\n\n"
            f'Aqui te llegan las escalaciones y los avisos de pago. Para ensenarme una '
            f'regla: "APRENDE: <la regla>".',
            chat_id=chat_id,
        )
        return {"status": "ok"}

    # Solo los chats internos pueden ensenar. Un desconocido no toca el conocimiento.
    from .telegram_api import destinatarios as _internos_tg

    if chat_id not in _internos_tg():
        enviar_telegram("Este canal es interno del equipo ITERA.", chat_id=chat_id)
        decision_log.registrar(
            f"tg:{chat_id}", texto, accion="error", error="chat de Telegram no autorizado"
        )
        return {"status": "no autorizado"}

    m = _APRENDE_RE.match(texto)
    if m:
        entrada = aprendizajes.agregar(m.group(1).strip(), autor="Benny")
        enviar_telegram(
            f'✅ Aprendido y guardado:\n"{entrada["texto"]}"\n\nLo aplicare de aqui en adelante.',
            chat_id=chat_id,
        )
        decision_log.registrar(
            f"tg:{chat_id}", texto, accion="aprendizaje", respuesta=entrada["texto"], entrega="ok"
        )
        return {"status": "ok"}

    # Orden sobre la conversacion de un cliente. En segundo plano: el agente + Odoo tardan mas
    # de lo que Telegram espera por el webhook.
    orden = _ORDEN_RE.match(texto)
    if orden:
        sid_destino, instruccion = orden.group(1), orden.group(2).strip()
        enviar_telegram(
            f"🤖 Entendido, trabajando sobre el cliente {sid_destino}...", chat_id=chat_id
        )
        background_tasks.add_task(
            _procesar_orden_interna, sid_destino, instruccion, chat_id
        )
        return {"status": "ok"}

    enviar_telegram(
        "Este canal recibe escalaciones y avisos de pago, y acepta dos comandos:\n\n"
        '• "APRENDE: <la regla>" — lo guardo como conocimiento permanente.\n'
        '• "CLIENTE <id>: <instruccion>" — actuo sobre la conversacion de ese cliente.\n'
        "   Ej: CLIENTE 1491137321: mandale otra vez su cotizacion\n"
        "   Ej: CLIENTE 1491137321: recuerdale que lleva y pregunta si avanzamos\n\n"
        "El id del cliente sale de las escalaciones que te mando.",
        chat_id=chat_id,
    )
    return {"status": "ok"}


def _entrega(res: dict) -> str:
    """Normaliza el resultado de enviar_mensaje para la bitacora: 'ok', 'sombra' o 'FALLO'."""
    if res.get("ok"):
        return "ok"
    detalle = res.get("detalle", "desconocido")
    if detalle.startswith("sombra"):
        return "sombra (no enviado al cliente)"
    return f"FALLO: {detalle}"


def _armar_messages(
    subscriber_id: str, texto: str, image_url: str = "", prefijo: str = ""
) -> list[dict]:
    """Arma los 'messages' del turno: historial + ficha + mensaje nuevo.

    La ficha (<estado_conversacion>) se antepone al mensaje NUEVO, no al system: el bloque system
    es el prefijo cacheado (~10,552 tokens) y es identico para todos los clientes. Meter datos
    por cliente ahi invalidaria el prompt caching en cada turno y triplicaria el costo.
    """
    # El mensaje entrante ya se registro en la transcripcion, asi que se omite de la ventana:
    # aqui va aparte como bloque nuevo, con su imagen si trae.
    previos = session_store.ventana_para_modelo(subscriber_id, omitir_ultimos=1)

    estado = ficha_mod.cargar_y_render(subscriber_id)

    contenido: list[dict] = []
    # Si la ventana ya termina en 'user' (pasa cuando el cliente manda dos mensajes seguidos y
    # los turnos se solapan), su texto se funde con este bloque en vez de agregar un segundo
    # mensaje de user: la Messages API espera roles alternados.
    if previos and previos[-1]["role"] == "user":
        contenido.append({"type": "text", "text": previos.pop()["content"]})

    encabezado = "\n\n".join(p for p in (prefijo, estado) if p)
    if encabezado:
        contenido.append({"type": "text", "text": encabezado})
    if texto:
        contenido.append({"type": "text", "text": texto})
    if image_url:
        contenido.append(_bloque_imagen(image_url))
    if not texto and not image_url:
        contenido.append({"type": "text", "text": "(el cliente envio un mensaje sin texto)"})
    return previos + [{"role": "user", "content": contenido}]


_PREFIJO_ORDEN = """\
<orden_interna>
Lo que sigue NO es un mensaje del cliente: es una instruccion del equipo de ITERA (Benny) sobre \
esta conversacion.
- Tu respuesta se le envia AL CLIENTE, no a Benny. Escribele al cliente, en tu voz normal de \
vendedor, como si retomaras la conversacion. NUNCA le hables a Benny ni menciones que recibiste \
una instruccion interna.
- Cumple la instruccion usando tus herramientas: reenviar_cotizacion para mandarle otra vez su \
cotizacion, consultar_cotizacion para recordarle exactamente que lleva (folio y total REALES, \
nunca de memoria), enviar_fotos_producto para mandarle fotos, buscar_catalogo para encontrar un \
modelo.
- Si la instruccion no se puede cumplir con tus herramientas, NO improvises ni inventes datos: \
escala con escalar_a_benny explicando que falta.
</orden_interna>
INSTRUCCION DEL EQUIPO: """


def _procesar_orden_interna(sid_destino: str, instruccion: str, chat_origen: str) -> None:
    """Ejecuta una orden del equipo sobre la conversacion de un cliente concreto.

    Resuelve el caso que fallaba en produccion: Benny pedia "al cliente que pregunto por
    guayabera hazle la lista de lo que lleva y enviasela" y el agente le contestaba A BENNY, con
    datos inventados, sin mandarle nada al cliente.
    """
    # El ContextVar apunta al CLIENTE: es lo que hace que las tools (fotos, cotizacion) le
    # entreguen a el y no a quien dio la orden.
    request_context.current_subscriber_id.set(sid_destino)

    # Ventana de 24h de WhatsApp: fuera de ella un envio libre no se entrega (Meta exige una
    # plantilla aprobada). Se avisa en vez de fallar en silencio, que es lo que pasaba antes.
    dentro, horas = session_store.dentro_de_ventana_wa(sid_destino)
    if not dentro:
        detalle = (
            f"hace {horas:.0f} h que no escribe" if horas is not None else "no hay mensajes suyos"
        )
        enviar_telegram(
            f"⛔ No pude escribirle al cliente {sid_destino}: {detalle}, esta FUERA de la ventana "
            "de 24 h de WhatsApp.\n\nMeta solo permite reabrir la conversacion con una plantilla "
            "aprobada. Escribele tu desde ManyChat y en cuanto conteste vuelvo a poder atenderlo.",
            chat_id=chat_origen,
        )
        decision_log.registrar(
            sid_destino,
            instruccion,
            accion="orden_interna",
            error=f"fuera de ventana de 24h ({detalle})",
        )
        return

    session_store.agregar_mensaje(sid_destino, "operador", f"[Orden del equipo] {instruccion}")
    messages = _armar_messages(sid_destino, instruccion, prefijo=_PREFIJO_ORDEN)

    try:
        texto_respuesta, tools_usadas, llamadas = responder(messages)
    except Exception as exc:  # noqa: BLE001
        enviar_telegram(
            f"⚠️ Falló la orden sobre el cliente {sid_destino}: {exc}", chat_id=chat_origen
        )
        decision_log.registrar(
            sid_destino, instruccion, accion="error", error=f"orden_interna: {exc}"
        )
        return

    session_store.agregar_mensaje(
        sid_destino, "assistant", texto_respuesta, tools=tools_usadas
    )
    res = enviar_mensaje(sid_destino, texto_respuesta)
    entrega = _entrega(res)

    enviar_telegram(
        f"{'✅' if res.get('ok') else '⚠️'} Orden ejecutada sobre el cliente {sid_destino}\n\n"
        f"Le envie:\n{formato_whatsapp(texto_respuesta)}\n\n"
        f"Tools: {', '.join(tools_usadas) or 'ninguna'}\nEntrega: {entrega}",
        chat_id=chat_origen,
    )
    decision_log.registrar(
        sid_destino,
        instruccion,
        accion="orden_interna",
        respuesta=formato_whatsapp(texto_respuesta),
        tools_invocadas=tools_usadas,
        motivo_escalacion=decision_log.motivo_de(llamadas),
        entrega=entrega,
    )


def _procesar(subscriber_id: str, texto: str, image_url: str, audio_url: str) -> None:
    """Procesa un mensaje y ENTREGA la respuesta al cliente por la Sending API de ManyChat.

    Corre en segundo plano (BackgroundTasks). Nunca lanza: todo fallo se registra en la
    bitacora y se degrada con un mensaje generico enviado al cliente.
    """
    # Deja el subscriber_id disponible a las tools que envian media (fotos) al cliente.
    request_context.current_subscriber_id.set(subscriber_id)

    # Cuando el cliente manda media por WhatsApp, ManyChat mete la URL del archivo dentro
    # de 'Ultima entrada de texto' (el campo 'text'), a veces sola y a veces junto con el
    # caption. Aqui se separa la URL del caption y cada uno se enruta a donde va: la URL
    # como imagen/audio, el caption como el mensaje del cliente. Conservar el caption
    # importa: es el 'por que' le manda la foto ("me interesa este en talla 6").
    # Con texto normal (sin http) el bloque ni se entra.
    if texto and not image_url and not audio_url and "http" in texto:
        url, caption = _separar_media(texto)
        tipo = _tipo_media(url) if url else ""
        print(
            f"MEDIA detectada tipo={tipo} url={url[:160]} caption={caption[:120]!r}",
            flush=True,
        )
        if tipo == "image":
            # El caption SI se conserva: es el 'por que' me manda la foto.
            image_url, texto = url, caption
        elif tipo == "audio":
            audio_url, texto = url, caption
        elif tipo:
            # video / archivo / desconocido: el modelo no los procesa. Antes se dejaba la
            # URL cruda como si fuera el mensaje del cliente y el agente intentaba
            # interpretar el link. Se reemplaza por un marcador para que pida una foto o
            # texto. NO es un caso raro: en el bimestre el 5.6% de los mensajes de
            # clientes son video (1,626), diez veces mas que los audios.
            etiqueta = "un video" if tipo == "video" else "un archivo o enlace"
            nota = (
                f"[El cliente envio {etiqueta}, que no puedo ver. Pedirle amablemente una "
                "FOTO del modelito o que escriba lo que necesita.]"
            )
            # Si venia con caption, se conserva: puede ser todo el pedido.
            texto = f"{caption} {nota}".strip() if caption else nota

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

    # El mensaje entrante se registra en la transcripcion ANTES de todo lo demas: aunque el turno
    # falle o lo atrape un gate, el mensaje del cliente no se pierde. Va antes de los gates a
    # proposito: un comprobante de pago o una queja tambien son parte del hilo, y ademas cuentan
    # para la ventana de 24h (si no, un cliente que solo mando su comprobante pareceria inactivo
    # y no se le podria recontactar).
    tipo_msg = "imagen" if image_url else ("audio" if audio_url else "texto")
    session_store.agregar_mensaje(
        subscriber_id,
        "user",
        texto,
        tipo=tipo_msg,
        media_url=image_url or audio_url or "",
    )

    # --- Gate determinista (antes del LLM) ---
    accion = escalation_rules.evaluar(texto, tiene_imagen)
    if accion and accion["tipo"] == "pago":
        notificar_pago_impl(
            mensaje=f"Cliente {subscriber_id} reporta un pago. Texto: {texto or '(solo imagen)'}",
            comprobante_url=image_url,
        )
        res = enviar_mensaje(subscriber_id, accion["mensaje_cliente"])
        session_store.agregar_mensaje(subscriber_id, "assistant", accion["mensaje_cliente"])
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
        session_store.agregar_mensaje(subscriber_id, "assistant", accion["mensaje_cliente"])
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
    messages = _armar_messages(subscriber_id, texto, image_url)

    try:
        texto_respuesta, tools_usadas, llamadas = responder(messages)
    except Exception as exc:  # noqa: BLE001 — cualquier fallo se registra y se degrada con gracia
        res = enviar_mensaje(
            subscriber_id, "Permítame confirmarle eso en un momento, por favor 🙏"
        )
        decision_log.registrar(
            subscriber_id, texto, accion="error", error=str(exc), entrega=_entrega(res)
        )
        return

    session_store.agregar_mensaje(
        subscriber_id, "assistant", texto_respuesta, tools=tools_usadas
    )

    # Entregar al cliente y registrar el turno CON el resultado de la entrega, para
    # detectar el caso 'respondio en el log pero el cliente no recibio nada'.
    res = enviar_mensaje(subscriber_id, texto_respuesta)
    accion_log = "escalo" if "escalar_a_benny" in tools_usadas else "respondio"
    decision_log.registrar(
        subscriber_id,
        texto,
        accion=accion_log,
        # Se loguea el texto YA formateado: es el que recibe el cliente. Antes se guardaba el
        # crudo y la bitacora mostraba **dobles** que formato_whatsapp ya habia convertido.
        respuesta=formato_whatsapp(texto_respuesta),
        tools_invocadas=tools_usadas,
        motivo_escalacion=decision_log.motivo_de(llamadas),
        entrega=_entrega(res),
    )
