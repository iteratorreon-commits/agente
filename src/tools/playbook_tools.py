"""Tool de consulta del playbook/politicas de venta (conocimiento local curado)."""
from __future__ import annotations

import json
from functools import lru_cache

from anthropic import beta_tool

from ..config import cfg


@lru_cache(maxsize=1)
def _cargar() -> tuple[dict, dict]:
    with open(cfg.knowledge_dir / "playbook.json", encoding="utf-8") as f:
        playbook = json.load(f)
    with open(cfg.knowledge_dir / "politicas.json", encoding="utf-8") as f:
        politicas = json.load(f)
    return playbook, politicas


@beta_tool
def consultar_playbook(situacion: str, momento: str = "") -> str:
    """Consulta guiones de venta y politicas del negocio para responder con el estilo de los mejores vendedores.

    Usala cuando dudes como responder a una situacion de venta (objecion de precio, algo
    agotado, cierre, seguimiento) o necesites un hecho del negocio (mayoreo, envio, pagos,
    tallas, facturacion). Devuelve guiones recomendados y politicas relevantes. Si aqui no
    hay evidencia para responder con certeza, ESCALA en vez de inventar.

    Args:
        situacion: Descripcion de la situacion (ej. 'cliente dice que esta caro', 'modelo agotado').
        momento: Filtro opcional del momento de venta:
                 apertura | precio | objecion | cierre | postventa.
    """
    playbook, politicas = _cargar()
    guiones = playbook.get("guiones", [])
    if momento:
        filtrados = [g for g in guiones if g.get("momento") == momento]
        guiones = filtrados or guiones

    # Ranking simple por solape de palabras con la situacion (sin embeddings, pocas docenas).
    palabras = {w for w in situacion.lower().split() if len(w) > 3}

    def score(g: dict) -> int:
        texto = f"{g.get('situacion','')} {g.get('respuesta_recomendada','')}".lower()
        return sum(1 for w in palabras if w in texto)

    top = sorted(guiones, key=score, reverse=True)[:4]

    return json.dumps(
        {
            "estilo": playbook.get("estilo_general", {}),
            "guiones_relevantes": [
                {
                    "situacion": g.get("situacion"),
                    "momento": g.get("momento"),
                    "respuesta_recomendada": g.get("respuesta_recomendada"),
                }
                for g in top
            ],
            "politicas": {
                "mayoreo": politicas.get("mayoreo"),
                "cotizacion": politicas.get("cotizacion"),
                "envio": politicas.get("envio"),
                "pagos": politicas.get("pagos"),
                "tallas": politicas.get("tallas"),
                "facturacion": politicas.get("facturacion"),
            },
        },
        ensure_ascii=False,
    )
