"""Tools del agente vendedor (decoradas con @beta_tool del SDK de Anthropic)."""
from .envia_tools import cotizar_envio
from .manychat_tools import escalar_a_benny, notificar_pago_multiple
from .odoo_tools import (
    buscar_catalogo,
    consultar_stock,
    crear_cotizacion,
    enviar_fotos_producto,
)
from .playbook_tools import consultar_playbook

TOOLS = [
    buscar_catalogo,
    consultar_stock,
    crear_cotizacion,
    enviar_fotos_producto,
    cotizar_envio,
    consultar_playbook,
    escalar_a_benny,
    notificar_pago_multiple,
]

__all__ = [
    "TOOLS",
    "buscar_catalogo",
    "consultar_stock",
    "crear_cotizacion",
    "enviar_fotos_producto",
    "cotizar_envio",
    "consultar_playbook",
    "escalar_a_benny",
    "notificar_pago_multiple",
]
