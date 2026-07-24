"""Contexto por-turno para que las tools sepan a quE cliente responder.

El Tool Runner ejecuta las @beta_tool sin pasarles el subscriber_id. Para que una tool
pueda ENVIAR media (fotos, etc.) al cliente correcto, _procesar setea aqui el subscriber_id
antes de correr el agente, y las tools lo leen. Se usa un ContextVar (no una global) para
que sea seguro con varios clientes procesados en paralelo en distintos hilos.
"""
from __future__ import annotations

import contextvars

current_subscriber_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_subscriber_id", default=""
)
