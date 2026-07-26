"""Memoria de conversacion por subscriber (contexto de sesion) en SQLite.

Cada mensaje entrante de ManyChat es una llamada HTTP sin estado, asi que toda la memoria
del agente vive aqui. Dos piezas distintas, a proposito:

1. `mensajes` — transcripcion APPEND-ONLY, una fila por mensaje. La app NUNCA borra de esta
   tabla. Es la fuente de verdad del hilo: de aqui sale la ventana que se le manda al modelo,
   el calculo de la ventana de 24h de WhatsApp y la auditoria.
2. `conversations.ficha` — estado ESTRUCTURADO de la conversacion (pedido en curso, cotizacion
   vigente, modelos ya mostrados...). Sobrevive al recorte de la ventana, que es justo lo que
   antes se perdia. Ver src/ficha.py.

Por que se rehizo (bug real de produccion): la version anterior guardaba
`json.dumps(turnos[-MAX_TURNOS:])`, o sea recortaba al ESCRIBIR. El historial viejo se borraba
del disco para siempre y no existia el hilo completo en ninguna parte. Con 20 entradas (= 10
idas y vueltas) una conversacion normal de mayoreo ya se pasaba, y el agente volvia a preguntar
tallas y colores que el cliente le habia confirmado 40 minutos antes.

La columna vieja `turnos` se conserva sin uso para que un rollback del deploy siga funcionando.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .config import cfg

# Antiguedad maxima de un mensaje para que entre en el contexto del modelo. Evita arrastrar
# una compra vieja a una nueva. Las filas NO se borran: siguen disponibles para auditoria.
SESSION_TTL_DIAS = 7
_TTL_SEG = SESSION_TTL_DIAS * 24 * 3600

# Ventana de 24h de WhatsApp/Meta: fuera de ella no se puede enviar sin plantilla aprobada.
VENTANA_WA_SEG = 24 * 3600

# Un candado por subscriber para el lee-modifica-escribe de la ficha. `_procesar` corre en el
# threadpool de BackgroundTasks, asi que dos mensajes seguidos del mismo cliente pueden
# solaparse; sin candado uno sobrescribia la ficha del otro.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(subscriber_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(subscriber_id, threading.Lock())


def _conn() -> sqlite3.Connection:
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: manejamos las transacciones a mano (BEGIN IMMEDIATE) para que dos
    # escritores concurrentes se serialicen en vez de pisarse.
    conn = sqlite3.connect(cfg.db_path, timeout=30.0, isolation_level=None)
    # WAL: deja leer mientras otro escribe (varios turnos en paralelo).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mensajes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            subscriber_id TEXT NOT NULL,
            ts            REAL NOT NULL,
            role          TEXT NOT NULL,
            texto         TEXT NOT NULL DEFAULT '',
            tipo          TEXT NOT NULL DEFAULT 'texto',
            media_url     TEXT NOT NULL DEFAULT '',
            tools         TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_mensajes_sub_ts ON mensajes(subscriber_id, ts)"
    )
    # Enlace subscriber -> cotizacion. Sin esto no hay forma de saber que sale.order es de que
    # cliente de WhatsApp: el partner de Odoo se busca por telefono y el agente casi nunca lo
    # tiene, asi que consultar_cotizacion() sin argumentos no podria resolver nada.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cotizaciones (
            subscriber_id TEXT NOT NULL,
            order_id      INTEGER NOT NULL,
            folio         TEXT NOT NULL DEFAULT '',
            creada_at     REAL NOT NULL,
            PRIMARY KEY (subscriber_id, order_id)
        )
        """
    )
    # Migracion aditiva de la columna `ficha` (la tabla puede venir de un deploy anterior).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
    if "ficha" not in cols:
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN ficha TEXT NOT NULL DEFAULT '{}'"
        )
    return conn


# --------------------------------------------------------------------------------------
# Transcripcion (append-only)
# --------------------------------------------------------------------------------------


def agregar_mensaje(
    subscriber_id: str,
    role: str,
    texto: str,
    tipo: str = "texto",
    media_url: str = "",
    tools: list[str] | None = None,
) -> None:
    """Agrega un mensaje a la transcripcion. Nunca sobrescribe ni borra nada.

    role: 'user' (cliente), 'assistant' (agente) u 'operador' (orden interna del equipo).
    tipo: 'texto' | 'imagen' | 'audio' | 'video' | 'archivo' — para poder reconstruir despues
    que el cliente mando una FOTO y no un texto (son el 44% de los mensajes).
    """
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO mensajes (subscriber_id, ts, role, texto, tipo, media_url, tools) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                subscriber_id,
                time.time(),
                role,
                texto or "",
                tipo,
                media_url or "",
                json.dumps(tools or [], ensure_ascii=False),
            ),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def historial(subscriber_id: str, limite: int = 0, solo_vigentes: bool = True) -> list[dict]:
    """Mensajes del subscriber en orden cronologico.

    limite=0 trae todo. solo_vigentes filtra por el TTL (lo que se le puede mandar al modelo);
    con False trae el hilo completo, que es lo que sirve para auditoria y recontacto.
    """
    corte = time.time() - _TTL_SEG if solo_vigentes else 0
    conn = _conn()
    try:
        # Se piden los ULTIMOS N (ts desc) y luego se invierte: si se pidieran los primeros N
        # con limite, se le mandaria al modelo el arranque de la conversacion y no lo reciente.
        filas = conn.execute(
            "SELECT role, texto, tipo, media_url, tools, ts FROM mensajes "
            "WHERE subscriber_id = ? AND ts >= ? ORDER BY ts DESC, id DESC LIMIT ?",
            (subscriber_id, corte, limite if limite > 0 else -1),
        ).fetchall()
    finally:
        conn.close()
    out = [
        {"role": r, "texto": t, "tipo": tp, "media_url": m, "tools": json.loads(tl or "[]"), "ts": ts}
        for r, t, tp, m, tl, ts in filas
    ]
    out.reverse()
    return out


def _render(msg: dict) -> str:
    """Texto con el que un mensaje viejo entra al contexto del modelo.

    Antes las fotos se guardaban como el string literal "(imagen)", asi que el 44% de los
    mensajes de clientes quedaba como un placeholder de 8 caracteres y "esta blusa" en un
    mensaje posterior no tenia referente. Ahora se conserva que era una foto y su URL.
    """
    texto = (msg.get("texto") or "").strip()
    tipo = msg.get("tipo") or "texto"
    if tipo == "texto":
        return texto or "(mensaje sin texto)"
    etiqueta = {
        "imagen": "el cliente envio una FOTO",
        "audio": "el cliente envio una nota de voz",
        "video": "el cliente envio un video",
        "archivo": "el cliente envio un archivo",
    }.get(tipo, f"el cliente envio {tipo}")
    marca = f"[{etiqueta}: {msg.get('media_url') or 'sin url'}]"
    return f"{marca} {texto}".strip() if texto else marca


def ventana_para_modelo(subscriber_id: str, omitir_ultimos: int = 0) -> list[dict]:
    """Historial reciente en formato Messages API (roles user/assistant alternados).

    Trae los ultimos cfg.max_mensajes dentro del TTL. La API exige que el primer mensaje sea
    de 'user' y que los roles alternen, asi que se descartan los 'assistant' iniciales y se
    funden los consecutivos del mismo rol (el cliente manda su pedido en varios mensajes
    sueltos, es lo normal aqui).

    omitir_ultimos descarta las N filas mas recientes. El caller registra el mensaje entrante
    en la transcripcion ANTES de correr el agente (para no perderlo si el turno falla) y luego
    lo manda aparte como bloque nuevo —con su imagen si trae—, asi que necesita excluirlo de
    aqui para no duplicarlo.
    """
    msgs = historial(subscriber_id, limite=max(2, cfg.max_mensajes) + omitir_ultimos)
    if omitir_ultimos > 0:
        msgs = msgs[:-omitir_ultimos] or []

    salida: list[dict] = []
    for m in msgs:
        # 'operador' (orden interna del equipo) entra como user: es una instruccion, no una
        # respuesta del agente.
        rol = "assistant" if m["role"] == "assistant" else "user"
        texto = _render(m)
        if not salida and rol == "assistant":
            continue  # la conversacion no puede abrir con el agente
        if salida and salida[-1]["role"] == rol:
            salida[-1]["content"] += "\n" + texto
        else:
            salida.append({"role": rol, "content": texto})
    # La API rechaza un ultimo mensaje de assistant cuando vamos a agregar el turno nuevo del
    # cliente; _procesar siempre agrega uno de user despues, asi que esto queda consistente.
    return salida


def ultimo_mensaje_cliente(subscriber_id: str) -> float | None:
    """Timestamp del ultimo mensaje ENTRANTE del cliente, o None si no hay.

    Base del calculo de la ventana de 24h de WhatsApp: fuera de ella un envio libre no se
    entrega y Meta exige una plantilla aprobada.
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT ts FROM mensajes WHERE subscriber_id = ? AND role = 'user' "
            "ORDER BY ts DESC LIMIT 1",
            (subscriber_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def dentro_de_ventana_wa(subscriber_id: str) -> tuple[bool, float | None]:
    """(puede_enviarse, horas_desde_el_ultimo_mensaje). Sin mensajes -> (False, None)."""
    ts = ultimo_mensaje_cliente(subscriber_id)
    if ts is None:
        return False, None
    transcurrido = time.time() - ts
    return transcurrido <= VENTANA_WA_SEG, transcurrido / 3600.0


# --------------------------------------------------------------------------------------
# Ficha (estado estructurado)
# --------------------------------------------------------------------------------------


def cargar_ficha(subscriber_id: str) -> dict:
    """Ficha del subscriber. Vencida por TTL o inexistente -> {} (arranca limpio)."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT ficha, updated_at FROM conversations WHERE subscriber_id = ?",
            (subscriber_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    ficha_raw, updated_at = row
    if time.time() - (updated_at or 0) > _TTL_SEG:
        return {}
    try:
        return json.loads(ficha_raw or "{}")
    except json.JSONDecodeError:
        return {}


def guardar_ficha(subscriber_id: str, ficha: dict) -> None:
    """Persiste la ficha del subscriber."""
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO conversations (subscriber_id, ficha, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(subscriber_id) DO UPDATE SET
                ficha = excluded.ficha,
                updated_at = excluded.updated_at
            """,
            (subscriber_id, json.dumps(ficha, ensure_ascii=False), time.time()),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def actualizar_ficha(subscriber_id: str, cambio) -> dict:
    """Aplica `cambio(ficha)` bajo candado y guarda. Devuelve la ficha resultante.

    El candado es lo que evita que dos turnos en paralelo del mismo cliente se pisen la ficha
    (lee-modifica-escribe). `cambio` debe mutar el dict que recibe o devolver uno nuevo.
    """
    with _lock(subscriber_id):
        ficha = cargar_ficha(subscriber_id)
        resultado = cambio(ficha)
        if isinstance(resultado, dict):
            ficha = resultado
        guardar_ficha(subscriber_id, ficha)
        return ficha


# --------------------------------------------------------------------------------------
# Enlace subscriber <-> cotizacion de Odoo
# --------------------------------------------------------------------------------------


def registrar_cotizacion(subscriber_id: str, order_id: int, folio: str) -> None:
    """Deja constancia de que esta cotizacion de Odoo es de este cliente de WhatsApp."""
    if not subscriber_id or not order_id:
        return
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO cotizaciones (subscriber_id, order_id, folio, creada_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(subscriber_id, order_id) DO UPDATE SET "
            "folio = excluded.folio",
            (subscriber_id, int(order_id), folio or "", time.time()),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def cotizaciones_de(subscriber_id: str, limite: int = 5) -> list[dict]:
    """Cotizaciones registradas para este subscriber, la mas reciente primero."""
    conn = _conn()
    try:
        filas = conn.execute(
            "SELECT order_id, folio, creada_at FROM cotizaciones WHERE subscriber_id = ? "
            "ORDER BY creada_at DESC LIMIT ?",
            (subscriber_id, limite),
        ).fetchall()
    finally:
        conn.close()
    return [{"order_id": o, "folio": f, "creada_at": c} for o, f, c in filas]
