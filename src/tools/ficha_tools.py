"""Tool para que el agente anote el pedido que el cliente va dictando.

Los datos duros (folio, total, tallas, colores, costo de envio) los escriben las tools de Odoo
y envia.com de forma determinista. Pero lo que el cliente pide a lo largo de varios mensajes
sueltos solo lo sabe el modelo, y es justo lo que se perdia: en el hilo real del 25-jul el
cliente confirmo "Blusa Campesina Tri blanco, 2 pz en tallas 6, 8, 10 y 12" a las 22:09 y a las
22:47 el agente le volvio a preguntar el color, porque el historial ya se habia recortado.
"""
from __future__ import annotations

import json

from anthropic import beta_tool

from .. import ficha


@beta_tool
def anotar_pedido(lineas: list[dict], ya_preguntado: list[str] | None = None) -> str:
    """Anota el pedido que el cliente ha confirmado hasta ahora, para no volver a preguntarlo.

    Usala durante la RECEPCION del pedido, cada vez que el cliente confirme un modelo con sus
    tallas y cantidades, y cada vez que el pedido cambie. Manda SIEMPRE el pedido COMPLETO
    acumulado (reemplaza lo anterior), no solo lo nuevo del ultimo mensaje.

    Esto se te devuelve en <estado_conversacion> en cada turno, asi que sobrevive aunque la
    conversacion se haga larga. No es una cotizacion: no aparta piezas ni calcula precios.

    Args:
        lineas: El pedido acumulado, una entrada por variante:
            {"modelo": str, "template_id": int, "talla": str, "color": str, "cantidad": int}.
            'color' puede ir vacio si el cliente aun no lo define o pidio surtido.
        ya_preguntado: Datos que YA le preguntaste y no debes volver a pedir
            (ej. ['color de las blusas', 'codigo postal']). Opcional.
    """
    resultado = ficha.set_pedido(lineas=lineas or [], ya_preguntado=ya_preguntado or [])
    if not resultado:
        return "NO_ANOTADO: sin cliente en contexto."
    return json.dumps(
        {
            "anotado": True,
            "pedido_en_curso": resultado.get("pedido_en_curso") or [],
            "_instruccion": (
                "Queda anotado y lo veras en <estado_conversacion> los proximos turnos. NO se lo "
                "vuelvas a preguntar al cliente. Esto NO es una cotizacion: cuando el cliente "
                "diga que ya termino, valida stock y usa crear_cotizacion."
            ),
        },
        ensure_ascii=False,
    )
