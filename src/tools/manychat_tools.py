"""Tools de comunicacion 1:1 via ManyChat: escalar a Benny y notificar pagos.

WhatsApp/ManyChat NO permite que un bot escriba en grupos, por eso el 'grupo de
pagos' se simula notificando 1:1 a varias personas (Benny + finanzas) por separado.

La logica vive en funciones planas (escalar_impl / notificar_pago_impl) para que el
gate determinista del webhook las llame directo, y en wrappers @beta_tool para el agente.
"""
from __future__ import annotations

import json

from anthropic import beta_tool

from ..config import cfg
from ..manychat_api import enviar_mensaje


def escalar_impl(motivo: str, contexto: str, urgente: bool = False) -> dict:
    """Envia una escalacion 1:1 a Benny. Devuelve {'escalado': bool, 'detalle': str}."""
    prefijo = "🚨 URGENTE" if urgente else "🔔 Consulta del agente vendedor"
    texto = f"{prefijo}\n\nMotivo: {motivo}\n\nContexto: {contexto}"
    res = enviar_mensaje(cfg.benny_subscriber_id, texto)
    return {"escalado": res["ok"], "detalle": res["detalle"]}


def notificar_pago_impl(mensaje: str, comprobante_url: str = "") -> dict:
    """Notifica 1:1 a Benny + finanzas un posible pago. Devuelve {'enviados','fallidos','total'}."""
    destinatarios = list(cfg.notify_subscriber_ids)
    if cfg.benny_subscriber_id and cfg.benny_subscriber_id not in destinatarios:
        destinatarios.append(cfg.benny_subscriber_id)
    if not destinatarios:
        return {"enviados": [], "fallidos": [], "total": 0, "detalle": "sin destinatarios"}

    texto = f"💰 Posible pago por confirmar\n\n{mensaje}"
    enviados, fallidos = [], []
    for sid in destinatarios:
        res = enviar_mensaje(sid, texto, imagen_url=comprobante_url or None)
        (enviados if res["ok"] else fallidos).append(sid)
    return {"enviados": enviados, "fallidos": fallidos, "total": len(destinatarios)}


@beta_tool
def escalar_a_benny(motivo: str, contexto: str, urgente: bool = False) -> str:
    """Escala una duda o situacion a Benny (dueno) por WhatsApp 1:1 cuando el agente no sabe algo.

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
