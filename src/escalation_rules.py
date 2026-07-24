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
_QUEJA = re.compile(
    r"\b(garant[ií]a|devoluci[oó]n|reembolso|no\s+lleg[oó]|est[aá]\s+roto|"
    r"da[nñ]ad|equivocad|incompleto|me\s+falt[oó]|reclamo|queja|mal\s+estado)",
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
