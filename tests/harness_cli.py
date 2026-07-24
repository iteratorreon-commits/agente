"""Harness CLI para probar el agente sin WhatsApp (Fase 2).

Simula una conversacion por consola manteniendo memoria de sesion. Util para probar
guiones, escalaciones y flujo de cotizacion antes de conectar ManyChat.

Uso:
    python -m tests.harness_cli
    (requiere .env con ANTHROPIC_API_KEY y, para tools reales, credenciales de Odoo)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import decision_log, escalation_rules, session_store  # noqa: E402
from src.agent import responder  # noqa: E402

SID = "test-cli-001"


def main() -> None:
    print("Agente vendedor ITERA — harness CLI. Escribe 'salir' para terminar.\n")
    while True:
        try:
            texto = input("Cliente> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if texto.lower() in {"salir", "exit", "quit"}:
            break
        if not texto:
            continue

        gate = escalation_rules.evaluar(texto, tiene_imagen=False)
        if gate:
            print(f"[GATE {gate['tipo']}] Agente> {gate['mensaje_cliente']}\n")
            decision_log.registrar(SID, texto, accion=f"gate_{gate['tipo']}")
            continue

        turnos, slots = session_store.cargar(SID)
        messages = list(turnos) + [{"role": "user", "content": texto}]
        respuesta, tools = responder(messages)
        if tools:
            print(f"  (tools: {', '.join(tools)})")
        print(f"Agente> {respuesta}\n")

        nuevos = list(turnos) + [
            {"role": "user", "content": texto},
            {"role": "assistant", "content": respuesta},
        ]
        session_store.guardar(SID, nuevos, slots)


if __name__ == "__main__":
    main()
