"""Gate determinista que corre ANTES del LLM sobre el mensaje entrante crudo.

Dos casos que no se dejan a discrecion del modelo:
1. Pago (texto o imagen de comprobante): nunca se confirma por el LLM; se dispara
   notificar_pago_multiple y se responde algo generico.
2. Queja/garantia/devolucion: se escala siempre, aunque el modelo 'crea' saber.

Devuelve None si no aplica (deja pasar al agente), o un dict con la accion a tomar.
"""
from __future__ import annotations

import re

# Palabras que sugieren que el cliente reporta un pago.
_PAGO = re.compile(
    r"\b(pagu[eé]|deposit[eé]|transfer[ií]|ya\s+pagu|ya\s+deposit|ya\s+transfer|"
    r"comprobante|ya\s+qued[oó]|le\s+mand[eé]\s+mi\s+pago|listo\s+pagad)",
    re.IGNORECASE,
)
# Palabras de queja/garantia/devolucion.
# Los patrones nuevos salen de quejas REALES del bimestre (extracto 2026-07-25) que la
# version anterior dejaba pasar: "no me llegó el de disfraces" (el 'me' rompia \bno lleg),
# "llegó manchado", "faltó una pieza", "le hace falta una talla 4".
# Criterio: se prioriza PRECISION sobre recall. Un falso positivo corta la venta con una
# disculpa fuera de lugar, y el modelo ya tiene la regla 5 de escalar quejas como segunda
# linea. Por eso NO se agrega "falta" a secas: "me falta 1 para el envio gratis" es venta,
# no queja; solo dispara cuando falta una PIEZA/TALLA/MODELO.
_QUEJA = re.compile(
    r"\b(garant[ií]a|devoluci[oó]n|reembolso|reclamo|queja|"
    # no llegó / no me llega / todavia no llega / nunca me llegó / sigue sin llegar
    r"no\s+(me\s+)?lleg|(todav[ií]a|a[uú]n|nunca)\s+(no\s+)?(me\s+)?lleg|sigue\s+sin\s+lleg|"
    # estado del producto
    r"est[aá]\s+rot|lleg[oó]\s+rot|manchad|descos|desgarrad|defectuos|defecto|"
    r"da[nñ]ad|equivocad|incompleto|mal\s+estado|"
    # pedido incorrecto
    r"no\s+es\s+lo\s+que\s+ped|me\s+(mandaron|enviaron)\s+otr|no\s+coincide|"
    r"mal\s+(la\s+gu[ií]a|el\s+pedido|la\s+direcci[oó]n|la\s+orden)|"
    # paqueteria
    r"extraviad|paquete\s+perdid|"
    # faltantes: SOLO con la pieza de por medio, para no chocar con el envio gratis
    r"(me\s+)?falt[oó]\s+(una?\s+|\d+\s+)?(pz|pieza|prenda|modelo|talla|blusa|falda|vestido)|"
    r"hace\s+falta\s+(una?\s+|\d+\s+)?(pz|pieza|prenda|modelo|talla)"
    r")",
    re.IGNORECASE,
)


def evaluar(texto: str, tiene_imagen: bool) -> dict | None:
    """Evalua el mensaje entrante. Devuelve una accion determinista o None.

    Accion: {"tipo": "pago"|"queja", "mensaje_cliente": str}
    """
    t = texto or ""

    # Pago: texto con palabra clave, O imagen con contexto de pago/ambiguo.
    contexto_pago = bool(_PAGO.search(t))
    if contexto_pago or (tiene_imagen and _menciona_pago_debil(t)):
        return {
            "tipo": "pago",
            "mensaje_cliente": "Recibimos su comprobante, ¡muchas gracias! 🙌 En breve confirmamos su pago y le avisamos.",
        }

    if _QUEJA.search(t):
        return {
            "tipo": "queja",
            "mensaje_cliente": "Lamento mucho el inconveniente 🙏 Permítame revisar su caso para darle una solución; en un momento le confirmo.",
        }

    return None


def _menciona_pago_debil(texto: str) -> bool:
    """Senal debil de pago para acompañar una imagen (comprobante probable)."""
    return bool(re.search(r"\b(pago|dep[oó]sito|transferencia|oxxo|listo|ya)\b", texto or "", re.IGNORECASE))
