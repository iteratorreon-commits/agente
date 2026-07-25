"""Tools de comunicacion interna: escalar a Benny y notificar pagos.

Van por TELEGRAM, no por WhatsApp. Razon principal: WhatsApp/Meta solo deja enviar
dentro de la ventana de 24h desde el ultimo mensaje del destinatario, asi que una
escalacion podia perderse en silencio justo cuando mas se necesitaba. Ver telegram_api.

Si Telegram no esta configurado (falta el token), se cae de vuelta a WhatsApp para no
quedarse sin canal durante el despliegue.

La logica vive en funciones planas (escalar_impl / notificar_pago_impl) para que el
gate determinista del webhook las llame directo, y en wrappers @beta_tool para el agente.
"""
from __future__ import annotations

import json

from anthropic import beta_tool

from ..config import cfg
from ..manychat_api import enviar_mensaje
from ..request_context import current_subscriber_id
from ..telegram_api import configurado as telegram_listo
from ..telegram_api import difundir, enviar_telegram

_TIP = '💡 Responde con "APRENDE: <la regla>" y lo guardo para no volver a preguntarte.'


def _quien_pregunta() -> str:
    """Identifica al cliente del turno en curso.

    El Tool Runner no le pasa el subscriber_id a las tools, asi que sale del ContextVar
    que _procesar setea. Sin esto la escalacion decia 'un cliente pregunta X' y no habia
    forma de saber a quien contestarle.
    """
    sid = current_subscriber_id.get()
    return f"Cliente {sid}" if sid else "Cliente (sin identificar)"


def escalar_impl(motivo: str, contexto: str, urgente: bool = False) -> dict:
    """Escala a Benny por Telegram. Devuelve {'escalado': bool, 'detalle': str}."""
    prefijo = "🚨 URGENTE" if urgente else "🔔 Consulta del agente vendedor"
    texto = f"{prefijo}\n\n{_quien_pregunta()}\n\nMotivo: {motivo}\n\nContexto: {contexto}\n\n{_TIP}"
    if telegram_listo():
        res = enviar_telegram(texto)
    else:
        res = enviar_mensaje(cfg.benny_subscriber_id, texto)
    return {"escalado": res["ok"], "detalle": res["detalle"]}


def notificar_pago_impl(mensaje: str, comprobante_url: str = "") -> dict:
    """Avisa a Benny + finanzas de un posible pago. Devuelve {'enviados','fallidos','total'}."""
    texto = f"💰 Posible pago por confirmar\n\n{mensaje}"
    if telegram_listo():
        return difundir(texto, imagen_url=comprobante_url)

    # Respaldo por WhatsApp mientras Telegram no este configurado.
    destinatarios = list(cfg.notify_subscriber_ids)
    if cfg.benny_subscriber_id and cfg.benny_subscriber_id not in destinatarios:
        destinatarios.append(cfg.benny_subscriber_id)
    if not destinatarios:
        return {"enviados": [], "fallidos": [], "total": 0, "detalle": "sin destinatarios"}
    enviados, fallidos = [], []
    for sid in destinatarios:
        res = enviar_mensaje(sid, texto, imagen_url=comprobante_url or None)
        (enviados if res["ok"] else fallidos).append(sid)
    return {"enviados": enviados, "fallidos": fallidos, "total": len(destinatarios)}


@beta_tool
def escalar_a_benny(motivo: str, contexto: str, urgente: bool = False) -> str:
    """Escala una duda o situacion a Benny (dueno) cuando el agente no sabe algo.

    Usala SIEMPRE que: no tengas evidencia en el playbook/politicas ni en una tool para
    responder con certeza; el cliente pida algo fuera de catalogo o una politica no confirmada;
    o haya una queja/garantia/devolucion. Es mejor escalar que inventar. Tras escalar, dile al
    cliente algo como 'permitame revisar su caso, en un momento le confirmo'.

    Args:
        motivo: Resumen corto de por que escalas (ej. 'cliente pide pedido personalizado').
        contexto: Contexto de la conversacion util para que Benny decida.
        urgente: True si requiere atencion inmediata.
    """
    return json.dumps(escalar_impl(motivo, contexto, urgente), ensure_ascii=False)


@beta_tool
def notificar_pago_multiple(mensaje: str, comprobante_url: str = "") -> str:
    """Notifica 1:1 a Benny y al equipo de finanzas que un cliente reporta un pago (para que lo validen).

    El agente NUNCA confirma un pago por su cuenta. Usa esta tool cuando el cliente dice que
    pago o manda un comprobante: envia el aviso (y el comprobante si hay) a cada persona por
    separado, simulando 'avisar al grupo'. Al cliente respondele algo generico como 'gracias,
    en breve confirmamos tu pago'.

    Args:
        mensaje: Detalle del pago reportado (cliente, monto, referencia si la hay).
        comprobante_url: URL de la imagen del comprobante recibido, si existe.
    """
    return json.dumps(notificar_pago_impl(mensaje, comprobante_url), ensure_ascii=False)
