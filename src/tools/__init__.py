"""Tools del agente vendedor (decoradas con @beta_tool del SDK de Anthropic)."""
from .envia_tools import cotizar_envio
from .ficha_tools import anotar_pedido
from .manychat_tools import escalar_a_benny, notificar_pago_multiple
from .odoo_tools import (
    buscar_catalogo,
    consultar_cotizacion,
    consultar_stock,
    crear_cotizacion,
    enviar_fotos_producto,
    modificar_cotizacion,
    reenviar_cotizacion,
)
from .playbook_tools import consultar_playbook

TOOLS = [
    buscar_catalogo,
    consultar_stock,
    crear_cotizacion,
    consultar_cotizacion,
    modificar_cotizacion,
    reenviar_cotizacion,
    enviar_fotos_producto,
    anotar_pedido,
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
    "consultar_cotizacion",
    "modificar_cotizacion",
    "reenviar_cotizacion",
    "enviar_fotos_producto",
    "anotar_pedido",
    "cotizar_envio",
    "consultar_playbook",
    "escalar_a_benny",
    "notificar_pago_multiple",
]
