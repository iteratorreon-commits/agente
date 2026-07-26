"""Bitacora append-only de decisiones del agente (JSONL) para el loop de mejora.

Cada turno registra que hizo el agente: respondio / escalo / error, con las tools
invocadas, su motivo y la respuesta. La skill 'revisar-agente-vendedor' lee esto para
proponer ajustes al playbook que Benny aprueba. NO se registran datos de pago crudos.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .config import cfg

# Tools cuyo argumento 'motivo' explica una escalacion.
_TOOLS_ESCALACION = ("escalar_a_benny", "notificar_pago_multiple")


def motivo_de(llamadas: list[dict] | None) -> str:
    """Extrae el motivo de escalacion de las llamadas a tools del turno.

    Antes el campo quedaba SIEMPRE vacio en las escalaciones del agente (solo se guardaban los
    nombres de las tools), y la skill 'revisar-agente-vendedor' agrupa justo por este campo, asi
    que no podia analizar ninguna. Ahora sale del argumento real que mando el modelo.
    """
    for c in llamadas or []:
        if c.get("tool") in _TOOLS_ESCALACION:
            entrada = c.get("input") or {}
            motivo = entrada.get("motivo") or entrada.get("mensaje") or ""
            if motivo:
                return str(motivo)[:300]
    return ""


def registrar(
    subscriber_id: str,
    mensaje_cliente: str,
    accion: str,
    respuesta: str = "",
    tools_invocadas: list[str] | None = None,
    motivo_escalacion: str = "",
    error: str = "",
    entrega: str = "",
) -> None:
    """Agrega una linea a logs/decisiones.jsonl.

    accion: 'respondio' | 'escalo' | 'gate_pago' | 'error'.
    entrega: resultado de la Sending API de ManyChat ('ok' | 'FALLO: <detalle>' | '').
        Clave para diagnosticar el caso 'el bot respondio en el log pero el cliente
        no recibio nada' (fallo silencioso de sendContent en el flujo async).
    """
    path = Path(cfg.decision_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entrada = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "subscriber_id": subscriber_id,
        "mensaje_cliente": mensaje_cliente[:500],
        "accion": accion,
        "tools_invocadas": tools_invocadas or [],
        "motivo_escalacion": motivo_escalacion,
        "error": error,
        "entrega": entrega,
        "respuesta": respuesta[:800],
    }
    linea = json.dumps(entrada, ensure_ascii=False)

    # Espejo a stdout: en produccion (Render) la pestana Logs captura stdout, asi se
    # pueden revisar las decisiones (incl. respuestas en modo sombra) sin abrir el disco.
    print("DECISION " + linea, flush=True, file=sys.stdout)

    # Archivo append-only (fuente para la skill 'revisar-agente-vendedor'). No debe
    # tumbar el turno si el disco falla: se degrada al espejo de stdout.
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except OSError as exc:  # noqa: BLE001
        print(f"DECISION_LOG_ERROR no se pudo escribir {path}: {exc}", flush=True)
