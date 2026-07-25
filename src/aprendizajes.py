"""Conocimiento aprendido en vivo: indicaciones que Benny le da al agente por WhatsApp.

Objetivo: que Benny no tenga que repetir lo mismo. Cuando el agente escala una duda y
Benny responde con 'APRENDE: <indicacion>', esa indicacion se guarda aqui y se inyecta en
el system prompt de TODAS las conversaciones siguientes, con prioridad sobre suposiciones.

Se guarda en un JSON (cfg.aprendizajes_path). En produccion ese path apunta al disco
persistente (/data) para que sobreviva a los redeploys.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import cfg


def _path() -> Path:
    return Path(cfg.aprendizajes_path)


def listar() -> list[dict]:
    """Devuelve la lista de aprendizajes; [] si no hay archivo o esta corrupto."""
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("aprendizajes", []) if isinstance(data, dict) else []
        return [it for it in items if isinstance(it, dict) and it.get("texto")]
    except (OSError, json.JSONDecodeError):
        return []


def agregar(texto: str, autor: str = "Benny") -> dict:
    """Agrega una indicacion aprendida y la persiste. Devuelve la entrada creada."""
    entrada = {
        "fecha": time.strftime("%Y-%m-%d"),
        "autor": autor,
        "texto": (texto or "").strip(),
    }
    items = listar()
    items.append(entrada)
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"aprendizajes": items}, f, ensure_ascii=False, indent=2)
    return entrada


def render_para_prompt() -> str:
    """Bloque de texto para inyectar en el system prompt (vacio si no hay aprendizajes)."""
    items = listar()
    if not items:
        return ""
    lineas = "\n".join(f"- {it['texto']}" for it in items)
    return (
        "\n\n## Conocimiento aprendido (indicaciones de Benny — RESPETALAS SIEMPRE)\n"
        "Estas reglas las dio el dueno del negocio; tienen prioridad sobre cualquier "
        "suposicion. Aplicalas sin volver a preguntar ni escalar lo que ya este aqui:\n"
        f"{lineas}"
    )
