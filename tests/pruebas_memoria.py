# -*- coding: utf-8 -*-
"""Pruebas de MEMORIA y COTIZACIONES: cubren las fallas reales del 25-jul-2026.

Que se verifica y por que (todas salen de produccion, no son hipoteticas):
1. La transcripcion NO se recorta al escribir. Antes `guardar()` persistia solo las ultimas 20
   entradas (= 10 idas y vueltas), asi que el hilo viejo se borraba del disco para siempre. En
   el hilo real de 1491137321 el cliente confirmo "Blusa Campesina Tri blanco, 2 pz en tallas
   6, 8, 10 y 12" a las 22:09 y a las 22:47 el agente le volvio a preguntar el color.
2. Las fotos ya no quedan como el string "(imagen)". Son el 44% de los mensajes de clientes.
3. La ficha conserva pedido, folio, modelos mostrados y CP.
4. La ventana de 24h de WhatsApp: fuera de ella no se intenta enviar, se avisa por Telegram.
5. Las cotizaciones se LEEN de Odoo. El agente afirmo "cotizacion S04541 por $10,300" con
   tools_invocadas: []; ese folio no existe (la orden real era S04552 por $2,800) y encima le
   mezclo un producto de la orden de OTRO cliente.
6. modificar_cotizacion se niega sobre una orden ya confirmada.

SEGURIDAD: intercepta TODO envio (WhatsApp y Telegram) y usa una DB temporal. Solo LEE de Odoo.

Uso:  .venv\\Scripts\\python -m tests.pruebas_memoria
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# DB temporal ANTES de importar config, para no tocar agente.db.
_TMP = tempfile.mkdtemp(prefix="itera-pruebas-")
os.environ["DB_PATH"] = str(Path(_TMP) / "prueba.db")
os.environ["DECISION_LOG_PATH"] = str(Path(_TMP) / "decisiones.jsonl")

# ---- Interceptores (NADA real sale) ---------------------------------------
_ENVIOS: list[dict] = []
_TELEGRAMS: list[str] = []


def _stub_enviar_mensaje(subscriber_id: str, texto: str, imagen_url: str | None = None) -> dict:
    _ENVIOS.append({"a": subscriber_id, "texto": texto, "imagen_url": imagen_url})
    return {"ok": True, "detalle": "STUB"}


def _stub_enviar_telegram(texto: str, chat_id: str = "", imagen_url: str = "") -> dict:
    _TELEGRAMS.append(texto)
    return {"ok": True, "detalle": "STUB"}


import src.main as _main  # noqa: E402
import src.manychat_api as _mc_api  # noqa: E402
import src.telegram_api as _tg_api  # noqa: E402
import src.tools.manychat_tools as _mc_tools  # noqa: E402
import src.tools.odoo_tools as _odoo_tools  # noqa: E402

for _m in (_mc_api, _odoo_tools, _mc_tools, _main):
    if hasattr(_m, "enviar_mensaje"):
        _m.enviar_mensaje = _stub_enviar_mensaje
for _m in (_tg_api, _odoo_tools, _mc_tools, _main):
    if hasattr(_m, "enviar_telegram"):
        _m.enviar_telegram = _stub_enviar_telegram

from src import decision_log, ficha, request_context, session_store  # noqa: E402
from src.config import cfg  # noqa: E402

# Cotizacion REAL en Odoo usada como referencia (verificada 2026-07-25).
FOLIO_REAL = "S04552"
TOTAL_REAL = 2800.0
LINEAS_REALES = 18
FOLIO_INVENTADO = "S04541"  # el que el agente se saco de la manga
FOLIO_CONFIRMADO = "S04549"  # state='sale', no debe poder modificarse

_FALLOS: list[str] = []


def check(nombre: str, cond: bool, detalle: str = "") -> None:
    print(("  OK   " if cond else "  FALLA") + f" {nombre}" + (f" — {detalle}" if detalle else ""))
    if not cond:
        _FALLOS.append(nombre)


# ======================================================================================
def prueba_1_historial_integro() -> None:
    print("\n[1] La transcripcion no se recorta al escribir")
    sid = "p1-historial"
    for i in range(30):
        session_store.agregar_mensaje(sid, "user", f"mensaje cliente {i}")
        session_store.agregar_mensaje(sid, "assistant", f"respuesta agente {i}")

    completo = session_store.historial(sid, limite=0)
    check("60 filas en disco", len(completo) == 60, f"{len(completo)}")
    check("el primer mensaje sobrevive", completo[0]["texto"] == "mensaje cliente 0")

    ventana = session_store.ventana_para_modelo(sid)
    check(f"ventana acotada a {cfg.max_mensajes}", len(ventana) <= cfg.max_mensajes, f"{len(ventana)}")
    check("arranca con user (lo exige la API)", ventana[0]["role"] == "user")
    check(
        "roles alternan (lo exige la API)",
        all(ventana[i]["role"] != ventana[i + 1]["role"] for i in range(len(ventana) - 1)),
    )
    check(
        "omitir_ultimos no duplica el mensaje entrante",
        len(session_store.ventana_para_modelo(sid, omitir_ultimos=1)) <= len(ventana),
    )

    # Dos mensajes del cliente seguidos (turnos solapados): _armar_messages tiene que fundirlos,
    # no dejar dos 'user' consecutivos, que es lo que espera la Messages API.
    sid_b = "p1-seguidos"
    session_store.agregar_mensaje(sid_b, "user", "quiero blusas")
    session_store.agregar_mensaje(sid_b, "user", "en talla 6")
    armado = _main._armar_messages(sid_b, "en talla 6")
    check("roles alternan con 2 mensajes seguidos del cliente",
          all(armado[i]["role"] != armado[i + 1]["role"] for i in range(len(armado) - 1)),
          str([m["role"] for m in armado]))
    check("no se pierde el primer mensaje al fundir",
          any("quiero blusas" in str(b) for b in armado[-1]["content"]))


def prueba_2_fotos() -> None:
    print("\n[2] Las fotos conservan su referencia (44% de los mensajes)")
    sid = "p2-foto"
    session_store.agregar_mensaje(
        sid, "user", "", tipo="imagen", media_url="https://cdn.manychat.com/x.jpg"
    )
    v = session_store.ventana_para_modelo(sid)
    check("se marca como FOTO, no '(imagen)'", "FOTO" in v[0]["content"], v[0]["content"][:60])
    check("conserva la URL", "x.jpg" in v[0]["content"])

    sid2 = "p2-video"
    session_store.agregar_mensaje(sid2, "user", "mira este", tipo="video", media_url="http://v/1.mp4")
    v2 = session_store.ventana_para_modelo(sid2)
    check("el caption se conserva junto al video", "mira este" in v2[0]["content"])


def prueba_3_ficha() -> None:
    print("\n[3] La ficha conserva lo que la ventana ya no alcanza")
    sid = "p3-ficha"
    request_context.current_subscriber_id.set(sid)
    ficha.set_pedido(
        lineas=[
            {"modelo": "BLUSA CAMPESINA TRI", "template_id": 111, "talla": "6", "color": "BLANCO", "cantidad": 2},
            {"modelo": "BLUSA CAMPESINA TRI", "template_id": 111, "talla": "8", "color": "BLANCO", "cantidad": 2},
        ],
        ya_preguntado=["color de las blusas"],
    )
    ficha.agregar_modelos([{
        "template_id": 111, "nombre": "BLUSA CAMPESINA TRI",
        "tallas_disponibles": ["6", "8", "10", "12"], "colores_disponibles": ["BLANCO", "NEGRO"],
    }])
    ficha.set_envio(cp="30640", ciudad="Huixtla", costo=270.0)
    ficha.set_cotizacion(folio=FOLIO_REAL, order_id=12374, total=TOTAL_REAL, estado="draft")

    r = ficha.cargar_y_render(sid)
    for etiqueta, aguja in (
        ("el pedido dictado", "BLUSA CAMPESINA TRI"),
        ("el folio real", FOLIO_REAL),
        ("el template_id para reusarlo", "111"),
        ("el CP (no se vuelve a pedir)", "30640"),
        ("lo ya preguntado", "color de las blusas"),
    ):
        check(f"la ficha trae {etiqueta}", aguja in r)
    check("cabe en pocos tokens", len(r) < 4000, f"{len(r)} chars ~ {len(r)//4} tokens")
    check("ficha vacia no gasta tokens", ficha.cargar_y_render("p3-nuevo") == "")

    # consultar_stock solo aporta el nombre: no debe borrar tallas/colores ya guardados.
    ficha.agregar_modelos([{"template_id": 111, "nombre": "BLUSA CAMPESINA TRI"}])
    m = next(x for x in session_store.cargar_ficha(sid)["modelos_mostrados"] if x["template_id"] == 111)
    check("agregar_modelos fusiona, no reemplaza", m["tallas"] == ["6", "8", "10", "12"], str(m["tallas"]))


def prueba_4_ventana_24h() -> None:
    print("\n[4] Ventana de 24h de WhatsApp")
    sid = "p4-ventana"
    session_store.agregar_mensaje(sid, "user", "hola")
    dentro, _ = session_store.dentro_de_ventana_wa(sid)
    check("recien escribio: dentro", dentro is True)

    conn = sqlite3.connect(cfg.db_path)
    conn.execute("UPDATE mensajes SET ts = ? WHERE subscriber_id = ?", (time.time() - 30 * 3600, sid))
    conn.commit()
    conn.close()
    fuera, horas = session_store.dentro_de_ventana_wa(sid)
    check("hace 30h: fuera", fuera is False, f"{horas:.0f}h")
    check("sin mensajes: fuera", session_store.dentro_de_ventana_wa("p4-nadie")[0] is False)

    _ENVIOS.clear()
    _TELEGRAMS.clear()
    _main._procesar_orden_interna(sid, "mandale su cotizacion", "chat-interno")
    check("fuera de ventana NO le escribe al cliente", not [e for e in _ENVIOS if e["a"] == sid])
    check("fuera de ventana avisa al equipo", any("ventana" in t for t in _TELEGRAMS))


def prueba_4b_gates_no_dejan_hueco() -> None:
    print("\n[4b] Los mensajes que atrapa el gate igual entran al hilo")
    # Si un comprobante de pago o una queja no se registraran, el hilo quedaria con un hueco y
    # el cliente pareceria inactivo para la ventana de 24h (no se le podria recontactar).
    sid = "p4b-gate"
    _ENVIOS.clear()
    _main._procesar(sid, "Ya deposité, aquí está mi comprobante", "", "")
    hist = session_store.historial(sid, limite=0)
    check("el mensaje de pago queda en el hilo", any(h["role"] == "user" for h in hist), f"{len(hist)} filas")
    check("la respuesta del gate queda en el hilo", any(h["role"] == "assistant" for h in hist))
    check("cuenta para la ventana de 24h", session_store.dentro_de_ventana_wa(sid)[0] is True)

    sid2 = "p4b-queja"
    _main._procesar(sid2, "Me llegó manchado el vestido", "", "")
    h2 = session_store.historial(sid2, limite=0)
    check("la queja queda en el hilo", len(h2) >= 2, f"{len(h2)} filas")


def prueba_5_concurrencia() -> None:
    print("\n[5] Turnos en paralelo no pierden mensajes")
    sid = "p5-concurrencia"

    def escribir(n: int) -> None:
        for i in range(20):
            session_store.agregar_mensaje(sid, "user", f"h{n}-m{i}")

    hilos = [threading.Thread(target=escribir, args=(n,)) for n in range(4)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    check("80 mensajes, ninguno perdido", len(session_store.historial(sid, limite=0)) == 80)

    sid2 = "p5-ficha"

    def anotar(n: int) -> None:
        for i in range(10):
            ficha.set_pedido(ya_preguntado=[f"d-{n}-{i}"], subscriber_id=sid2)

    hilos = [threading.Thread(target=anotar, args=(n,)) for n in range(4)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    f = session_store.cargar_ficha(sid2)
    check("la ficha no se corrompe bajo candado", isinstance(f.get("ya_preguntado"), list))


def prueba_6_bitacora() -> None:
    print("\n[6] motivo_escalacion deja de salir vacio")
    m = decision_log.motivo_de([
        {"tool": "buscar_catalogo", "input": {"query": "monos"}},
        {"tool": "escalar_a_benny", "input": {"motivo": "no encuentro monos en catalogo"}},
    ])
    check("saca el motivo real", "monos" in m, m)
    check("sin escalacion, vacio", decision_log.motivo_de([{"tool": "consultar_stock", "input": {}}]) == "")
    check("tolera llamadas vacias", decision_log.motivo_de(None) == "")


def prueba_7_cotizacion_real() -> None:
    print("\n[7] Las cotizaciones se LEEN de Odoo (caso S04541)")
    sid = "p7-cotizacion"
    request_context.current_subscriber_id.set(sid)

    r = json.loads(_odoo_tools.consultar_cotizacion.func(folio=FOLIO_REAL))
    check(f"folio {FOLIO_REAL}", r.get("folio") == FOLIO_REAL, str(r.get("folio")))
    check(f"total real ${TOTAL_REAL:,.0f} (el agente dijo $10,300)", r.get("total") == TOTAL_REAL, str(r.get("total")))
    check(f"{LINEAS_REALES} lineas", len(r.get("lineas") or []) == LINEAS_REALES, str(len(r.get("lineas") or [])))
    check("borrador -> modificable", r.get("modificable") is True)
    guayabera = [l for l in r["lineas"] if "GUAYABERA" in (l["descripcion"] or "").upper()]
    check("2 pz por talla (el agente dijo 4)", all(l["cantidad"] == 2.0 for l in guayabera))
    check("a $100 (el agente dijo $150/$200)", all(l["precio_unitario"] == 100.0 for l in guayabera))
    check("sin Adelita (era de otro cliente)",
          not any("ADELITA" in (l["descripcion"] or "").upper() for l in r["lineas"]))
    check("refresca la ficha con el folio real", FOLIO_REAL in ficha.cargar_y_render(sid))

    inventado = _odoo_tools.consultar_cotizacion.func(folio=FOLIO_INVENTADO)
    check(f"{FOLIO_INVENTADO} -> NO_ENCONTRADA", inventado.startswith("NO_ENCONTRADA"), inventado[:50])
    check("le prohibe inventar", "NO inventes" in inventado)

    sin_args = json.loads(_odoo_tools.consultar_cotizacion.func())
    check("sin argumentos resuelve por la ficha", sin_args.get("folio") == FOLIO_REAL)

    reenvio = json.loads(_odoo_tools.reenviar_cotizacion.func(folio=FOLIO_REAL))
    check("reenvia la foto de la cotizacion", reenvio.get("imagen_enviada") is True, str(reenvio.get("folio")))


def prueba_8_no_modifica_confirmada() -> None:
    print("\n[8] No modifica una cotizacion ya confirmada")
    request_context.current_subscriber_id.set("p8")
    r = json.loads(_odoo_tools.modificar_cotizacion.func(folio=FOLIO_CONFIRMADO, quitar=[]))
    check("se niega", r.get("modificada") is False, f"estado={r.get('estado')}")
    check("manda escalar", "escalar_a_benny" in (r.get("_instruccion") or ""))
    check("inexistente -> NO_ENCONTRADA",
          _odoo_tools.modificar_cotizacion.func(folio="S99999").startswith("NO_ENCONTRADA"))

    check("nombre solo-emoji -> fallback legible",
          _odoo_tools._nombre_partner("🥰").startswith("Cliente WhatsApp"),
          _odoo_tools._nombre_partner("🥰"))
    check("nombre real se respeta", _odoo_tools._nombre_partner("Maria Lopez") == "Maria Lopez")


PRUEBAS = {
    1: prueba_1_historial_integro,
    2: prueba_2_fotos,
    3: prueba_3_ficha,
    4: prueba_4_ventana_24h,
    45: prueba_4b_gates_no_dejan_hueco,
    5: prueba_5_concurrencia,
    6: prueba_6_bitacora,
    7: prueba_7_cotizacion_real,
    8: prueba_8_no_modifica_confirmada,
}


def main() -> None:
    pedidas = [int(a) for a in sys.argv[1:] if a.isdigit()] or sorted(PRUEBAS)
    print(f"DB temporal: {cfg.db_path}")
    for n in pedidas:
        PRUEBAS[n]()
    print("\n" + "=" * 70)
    if _FALLOS:
        print(f"FALLARON {len(_FALLOS)}: {_FALLOS}")
        sys.exit(1)
    print(f"TODO EN VERDE ({len(pedidas)} pruebas)")


if __name__ == "__main__":
    main()
