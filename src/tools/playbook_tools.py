"""Tool de consulta del playbook/politicas/FAQ de venta (conocimiento local curado)."""
from __future__ import annotations

import json
import unicodedata
from functools import lru_cache

from anthropic import beta_tool

from ..config import cfg

# Palabras que no discriminan nada al rankear (se dejan las interrogativas: cuanto,
# donde, cuando, cual, como, porque).
_STOP = {
    "para", "pero", "esta", "este", "esto", "esos", "esas", "estos", "estas",
    "usted", "ustedes", "tambien", "ademas", "entonces", "bueno", "sobre",
    "desde", "hasta", "entre", "muy", "mas", "que", "los", "las", "del",
    "una", "uno", "con", "por", "sin", "son", "sus", "les", "nos", "hay",
    "cliente", "señorita", "senorita", "senor", "joven", "hola", "gracias",
    "favor", "quiere", "quieren", "pide", "piden", "dice", "pregunta",
    # Verbos de relleno: aparecen en casi toda pregunta y empataban el ranking
    # ("hacen factura" caia en el guion de "hacen cambios").
    "hacen", "hacer", "hace", "tienen", "tiene", "tengo", "tener",
    "puedo", "puede", "pueden", "manejan", "maneja", "seria", "serian",
}


def _norm(texto: str) -> str:
    """Minusculas y sin acentos, para que 'garantía' case con 'garantia'."""
    d = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in d if unicodedata.category(c) != "Mn")


def _tokens(texto: str) -> set[str]:
    """Palabras de contenido, con plural recortado ('facturas' -> 'factura')."""
    out = set()
    for w in _norm(texto).replace("/", " ").replace("-", " ").split():
        w = "".join(c for c in w if c.isalnum())
        if len(w) <= 3 or w in _STOP:
            continue
        if len(w) > 5 and w.endswith("es"):
            w = w[:-2]
        elif len(w) > 4 and w.endswith("s"):
            w = w[:-1]
        out.add(w)
    return out


@lru_cache(maxsize=1)
def _cargar() -> tuple[dict, dict, dict]:
    with open(cfg.knowledge_dir / "playbook.json", encoding="utf-8") as f:
        playbook = json.load(f)
    with open(cfg.knowledge_dir / "politicas.json", encoding="utf-8") as f:
        politicas = json.load(f)
    try:
        with open(cfg.knowledge_dir / "faq.json", encoding="utf-8") as f:
            faq = json.load(f)
    except FileNotFoundError:
        faq = {"faq": []}
    return playbook, politicas, faq


def _rankear(items: list[dict], campos: tuple[str, ...], consulta: set[str], top: int) -> list[dict]:
    """Ordena por solape de tokens con la consulta. Sin embeddings ni indices.

    El PRIMER campo (la pregunta / la situacion) pesa doble: un match ahi es mucho mas
    informativo que uno en el cuerpo de la respuesta, donde casi cualquier palabra puede
    aparecer de pasada. Sin ese peso, "hacen factura" empataba con el guion de garantia.
    """
    def score(it: dict) -> int:
        total = 0
        for peso, campo in zip((2, 1, 1, 1, 1), campos):
            total += peso * len(consulta & _tokens(str(it.get(campo, ""))))
        return total

    con_puntaje = [(score(it), i, it) for i, it in enumerate(items)]
    # i desempata para que el orden sea estable y no dependa del dict.
    con_puntaje.sort(key=lambda t: (-t[0], t[1]))
    return [it for s, _, it in con_puntaje[:top] if s > 0] or [it for _, _, it in con_puntaje[:top]]


def render_politicas_para_prompt() -> str:
    """Politicas del negocio, para inyectarlas en el system prompt CACHEADO.

    No se devuelven por la tool a proposito: un resultado de tool se queda en el historial
    y se reenvia a precio completo en CADA turno siguiente (~5,900 tokens = $0.012/turno,
    mas caro que todo lo demas junto). En el bloque cacheado del system cuestan 0.1x y
    ademas estan siempre disponibles sin gastar una llamada.
    """
    _, politicas, _ = _cargar()
    return (
        "\n\n## Politicas del negocio ITERA (hechos duros — NO los contradigas ni inventes otros)\n"
        "Ya los tienes aqui: no llames a consultar_playbook solo para leer una politica.\n"
        + json.dumps(politicas, ensure_ascii=False, indent=1)
    )


@beta_tool
def consultar_playbook(situacion: str, momento: str = "") -> str:
    """Consulta guiones de venta y preguntas frecuentes con la redaccion de los mejores vendedores.

    Usala cuando dudes COMO redactar una respuesta de venta: apertura, objecion de precio,
    algo agotado, cierre, seguimiento, o una pregunta frecuente del cliente (ubicacion,
    horarios, envios, pagos, facturacion, garantia, tallas, mayoreo, catalogos).
    Devuelve guiones y FAQ con el texto listo para mandar por WhatsApp.

    NOTA: las politicas del negocio (los hechos duros) ya estan en tu contexto; no llames a
    esta tool solo para consultarlas. Llamala para obtener la REDACCION probada.
    Si no hay evidencia para responder con certeza, ESCALA en vez de inventar.

    Args:
        situacion: Descripcion de la situacion o la pregunta del cliente
                   (ej. 'donde estan ubicados', 'hacen factura', 'cliente dice que esta caro').
        momento: Filtro opcional del momento de venta:
                 apertura | informacion | precio | objecion | cierre | postventa.
    """
    playbook, politicas, faq = _cargar()
    consulta = _tokens(situacion)

    guiones = playbook.get("guiones", [])
    if momento:
        filtrados = [g for g in guiones if g.get("momento") == momento]
        guiones = filtrados or guiones
    top_guiones = _rankear(guiones, ("situacion", "respuesta_recomendada"), consulta, 4)
    top_faq = _rankear(faq.get("faq", []), ("pregunta", "respuesta", "tema"), consulta, 3)

    return json.dumps(
        {
            "estilo": playbook.get("estilo_general", {}),
            "guiones_relevantes": [
                {
                    "situacion": g.get("situacion"),
                    "momento": g.get("momento"),
                    "respuesta_recomendada": g.get("respuesta_recomendada"),
                }
                for g in top_guiones
            ],
            "faq_relevante": [
                {
                    "pregunta": f.get("pregunta"),
                    "respuesta": f.get("respuesta"),
                    "nota_interna": f.get("nota_interna"),
                }
                for f in top_faq
            ],
            "politicas": "Ya estan en tu contexto (system prompt). No se repiten aqui.",
        },
        ensure_ascii=False,
    )
