"""Memoria de conversacion por subscriber (contexto de sesion) en SQLite.

Cada mensaje entrante de ManyChat es una llamada HTTP sin estado. Sin memoria, el
agente no recordaria la talla ya dicha ni la cotizacion en curso. Aqui se carga el
historial de turnos y los 'slots' (datos ya confirmados) por subscriber_id, y se
guarda tras cada respuesta. Vencimiento: conversaciones inactivas por SESSION_TTL_DIAS
no se cargan (evita arrastrar contexto viejo a una compra nueva).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .config import cfg

SESSION_TTL_DIAS = 7
_TTL_SEG = SESSION_TTL_DIAS * 24 * 3600
# Cuantos turnos recientes se reenvian al modelo (memoria conversacional acotada).
MAX_TURNOS = 20


def _conn() -> sqlite3.Connection:
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            subscriber_id TEXT PRIMARY KEY,
            turnos        TEXT NOT NULL DEFAULT '[]',
            slots         TEXT NOT NULL DEFAULT '{}',
            updated_at    REAL NOT NULL
        )
        """
    )
    return conn


def cargar(subscriber_id: str) -> tuple[list[dict], dict]:
    """Devuelve (turnos, slots) del subscriber. Si vencio o no existe, arranca limpio."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT turnos, slots, updated_at FROM conversations WHERE subscriber_id = ?",
            (subscriber_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return [], {}
    turnos_raw, slots_raw, updated_at = row
    if time.time() - updated_at > _TTL_SEG:
        return [], {}  # sesion vencida: no arrastrar contexto viejo
    try:
        turnos = json.loads(turnos_raw)
        slots = json.loads(slots_raw)
    except json.JSONDecodeError:
        return [], {}
    return turnos[-MAX_TURNOS:], slots


def guardar(subscriber_id: str, turnos: list[dict], slots: dict) -> None:
    """Persiste turnos (recortados) y slots del subscriber."""
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO conversations (subscriber_id, turnos, slots, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(subscriber_id) DO UPDATE SET
                turnos = excluded.turnos,
                slots = excluded.slots,
                updated_at = excluded.updated_at
            """,
            (
                subscriber_id,
                json.dumps(turnos[-MAX_TURNOS:], ensure_ascii=False),
                json.dumps(slots, ensure_ascii=False),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
