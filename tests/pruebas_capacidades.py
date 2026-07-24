# -*- coding: utf-8 -*-
"""Pruebas de CAPACIDADES del agente vendedor con 5 escenarios calcados de
conversaciones reales de ManyChat (transcripts de Luis/Francisco/Miranda).

SEGURIDAD: intercepta TODO envio saliente por WhatsApp (parcha enviar_mensaje en
manychat_api, odoo_tools y manychat_tools). NADA real llega a clientes ni a Benny;
los envios se registran en un recorder para poder mostrarlos en el reporte.

Efecto real que SI ocurre (es el punto de la prueba de cotizacion): la Prueba 3
crea un sale.order en BORRADOR en Odoo (no confirma nada, no aparta stock, no cobra).

Uso:  .venv\\Scripts\\python -m tests.pruebas_capacidades
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- Interceptor de envios (NADA real sale) -------------------------------
_ENVIOS: list[dict] = []


def _stub_enviar_mensaje(subscriber_id: str, texto: str, imagen_url: str | None = None) -> dict:
    _ENVIOS.append({"a": subscriber_id, "texto": texto, "imagen_url": imagen_url})
    return {"ok": True, "detalle": "STUB (no enviado real)"}


# Parchar en TODOS los modulos que ya importaron el nombre.
import src.manychat_api as _mc_api  # noqa: E402
import src.tools.odoo_tools as _odoo_tools  # noqa: E402
import src.tools.manychat_tools as _mc_tools  # noqa: E402

_mc_api.enviar_mensaje = _stub_enviar_mensaje
_odoo_tools.enviar_mensaje = _stub_enviar_mensaje
_mc_tools.enviar_mensaje = _stub_enviar_mensaje

import base64  # noqa: E402

import httpx  # noqa: E402

from src import escalation_rules, request_context  # noqa: E402
from src.agent import responder  # noqa: E402
from src.config import cfg  # noqa: E402
from src.tools.manychat_tools import escalar_impl, notificar_pago_impl  # noqa: E402

SID_CLIENTE = "PRUEBA-CLIENTE"  # destinatario ficticio (recorder, no real)


def _bloque_imagen(url: str) -> dict:
    """Baja los bytes de la imagen del lado del servidor y arma un bloque base64.

    En produccion ManyChat manda una URL de su CDN (permitida). En la prueba usamos una
    imagen de Odoo, cuyo robots.txt bloquea el fetch remoto de la API de Anthropic, asi
    que la incrustamos como base64 (equivale a que el cliente 'suba' la foto)."""
    resp = httpx.get(url, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    media = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if media not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        media = "image/jpeg"
    b64 = base64.standard_b64encode(resp.content).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}}

# URL publica de una foto real de Odoo (BLUSA CAMPESINA ENCAJE, tmpl 11444).
# Simula que el cliente manda una captura del catalogo.
FOTO_BLUSA = f"{cfg.odoo_url.rstrip('/')}/web/image/product.template/11444/image_1024"


def _quien(sid: str) -> str:
    if sid == SID_CLIENTE:
        return "CLIENTE"
    if sid == cfg.benny_subscriber_id:
        return "BENNY (escalacion)"
    return f"NOTIF {sid}"


def _correr_turno(messages: list[dict], texto: str, image_url: str = "") -> dict:
    """Replica src.main._procesar para UN mensaje: gate -> agente -> entrega (stub).

    Muta 'messages' agregando el turno del cliente y la respuesta del agente.
    Devuelve un dict con lo observado en el turno.
    """
    global _ENVIOS
    _ENVIOS = []
    request_context.current_subscriber_id.set(SID_CLIENTE)
    tiene_imagen = bool(image_url)

    # --- Gate determinista (antes del LLM) ---
    accion = escalation_rules.evaluar(texto, tiene_imagen)
    if accion and accion["tipo"] == "pago":
        notificar_pago_impl(
            mensaje=f"Cliente {SID_CLIENTE} reporta un pago. Texto: {texto or '(solo imagen)'}",
            comprobante_url=image_url,
        )
        _stub_enviar_mensaje(SID_CLIENTE, accion["mensaje_cliente"])
        messages.append({"role": "user", "content": texto})
        messages.append({"role": "assistant", "content": accion["mensaje_cliente"]})
        return {"gate": "pago", "tools": [], "respuesta": accion["mensaje_cliente"],
                "envios": list(_ENVIOS), "seg": 0.0}

    if accion and accion["tipo"] == "queja":
        escalar_impl(
            motivo="Queja/garantia detectada por gate",
            contexto=f"Cliente {SID_CLIENTE}: {texto}",
            urgente=True,
        )
        _stub_enviar_mensaje(SID_CLIENTE, accion["mensaje_cliente"])
        messages.append({"role": "user", "content": texto})
        messages.append({"role": "assistant", "content": accion["mensaje_cliente"]})
        return {"gate": "queja", "tools": [], "respuesta": accion["mensaje_cliente"],
                "envios": list(_ENVIOS), "seg": 0.0}

    # --- Camino normal: agente ---
    contenido: list[dict] = []
    if texto:
        contenido.append({"type": "text", "text": texto})
    if image_url:
        contenido.append(_bloque_imagen(image_url))
    if not contenido:
        contenido.append({"type": "text", "text": "(mensaje sin texto)"})

    messages.append({"role": "user", "content": contenido})
    t0 = time.time()
    respuesta, tools = responder(messages)
    seg = time.time() - t0
    # Entrega al cliente (stub) como en produccion.
    _stub_enviar_mensaje(SID_CLIENTE, respuesta)
    messages.append({"role": "assistant", "content": respuesta})
    return {"gate": None, "tools": tools, "respuesta": respuesta,
            "envios": list(_ENVIOS), "seg": seg}


# --------------------------------------------------------------------------
# 5 escenarios calcados de conversaciones reales de ManyChat.
# Cada uno: (id, titulo, base_real, turnos[list de (texto, image_url)], que_evalua)
# --------------------------------------------------------------------------
PRUEBAS = [
    {
        "id": 1,
        "titulo": "Apertura + dinamica de compra + precio de mayoreo (cliente nuevo)",
        "base": "Conv 22945/23220 (Francisco/Luis): 'me interesa hacer un pedido' / "
                "'hay cantidad minima para el precio de preventa?'",
        "evalua": "Saludo calido de 'usted'; explica la dinamica; mayoreo DESDE 6 piezas "
                  "(no inventa numeros); invita a combinar/surtir.",
        "turnos": [
            ("Hola buenas tardes, me interesa hacer un pedido para fiestas patrias 🇲🇽", ""),
            ("Oiga y hay una cantidad minima para que me de el precio de preventa/mayoreo?", ""),
        ],
    },
    {
        "id": 2,
        "titulo": "Multimodal: cliente manda FOTO + busqueda de catalogo + envio de fotos",
        "base": "Conv 23198 (Miranda): el cliente manda una foto '[image] Esta blusa' y "
                "pregunta si la tienen y el precio.",
        "evalua": "Interpreta la imagen; usa buscar_catalogo; envia fotos con "
                  "enviar_fotos_producto; NO inventa precio (lo da la cotizacion); mayoreo desde 6.",
        "turnos": [
            ("Hola, vi esta blusita y me interesa. La tienen? me la puede mostrar y decir el precio?",
             FOTO_BLUSA),
        ],
    },
    {
        "id": 3,
        "titulo": "Pedido complejo multi-talla -> stock -> cotizacion como FOTO",
        "base": "Conv 23220 (Luis): el cliente manda un pedido grande por tallas y pide "
                "'me puede confirmar y cotizar'.",
        "evalua": "Confirma el detalle; valida existencias (consultar_stock); crea la "
                  "cotizacion (crear_cotizacion, borrador en Odoo); confirma folio+total y "
                  "comparte el pdf_url; entrega la cotizacion como foto.",
        "turnos": [
            ("Le paso mi pedido porfa, me confirma existencias y me cotiza:\n"
             "Blusa campesina encaje: talla 2 = 5pz, talla 4 = 5pz, talla 6 = 5pz, talla 8 = 5pz.\n"
             "Blusa campesina tri negra: talla 2 = 5pz, talla 4 = 5pz, talla 6 = 5pz, talla 8 = 5pz.\n"
             "Va a nombre de Prueba Capacidades, mi tel es 0000000001", ""),
            ("Si esta correcto, dejemelo asi porfa", ""),
        ],
    },
    {
        "id": 4,
        "titulo": "Cotizacion de envio (CP) + incentivo de envio gratis >$4,000",
        "base": "Conv 23201/23220 (Luis): el cliente pregunta el costo de envio / da su CP.",
        "evalua": "Pide/usa el CP; cotiza con cotizar_envio (envia.com en vivo); da el costo; "
                  "menciona el envio gratis arriba de $4,000 como incentivo para subir ticket.",
        "turnos": [
            ("Y cuanto me saldria el envio? mi codigo postal es 93400, Papantla Veracruz", ""),
        ],
    },
    {
        "id": 5,
        "titulo": "Gates de seguridad deterministas: queja + reporte de pago",
        "base": "Conv 23198 (guia equivocada) + cierre tipico 'Listo, ya deposite [comprobante]'.",
        "evalua": "5a QUEJA: el gate intercepta ANTES del LLM, escala a Benny, responde disculpa "
                  "(no promete compensaciones). 5b PAGO: el gate intercepta, notifica para validacion "
                  "humana, y NUNCA confirma el pago por su cuenta.",
        "turnos": [
            ("Oigan me llego la guia equivocada, yo pedi ocurre por Paquete Express y me la "
             "mandaron por Estafeta ❌", ""),
            ("Listo, ya deposite en el Oxxo, aqui esta mi comprobante 🧾", "https://ejemplo/comprobante.jpg"),
        ],
        "turnos_independientes": True,  # cada turno arranca sesion nueva (casos distintos)
    },
]


def _fmt_envios(envios: list[dict]) -> str:
    if not envios:
        return "    (sin envios salientes)"
    out = []
    for e in envios:
        destino = _quien(e["a"])
        if e["imagen_url"]:
            txt = (e["texto"][:60] + "…") if e["texto"] else "(solo imagen)"
            out.append(f"    -> {destino}: 🖼️ IMAGEN {e['imagen_url']}  | {txt}")
        else:
            t = e["texto"].replace("\n", " ")
            out.append(f"    -> {destino}: {t[:200]}")
    return "\n".join(out)


def main() -> None:
    # Argumentos opcionales: ids de pruebas a correr (ej. 'python -m tests.pruebas_capacidades 2 4').
    sel = {int(a) for a in sys.argv[1:] if a.isdigit()}
    pruebas = [p for p in PRUEBAS if not sel or p["id"] in sel]

    print("=" * 78)
    print("PRUEBAS DE CAPACIDADES — AGENTE VENDEDOR ITERA")
    print(f"modelo={cfg.model} effort={cfg.effort}  |  envios WhatsApp INTERCEPTADOS (nada real sale)")
    if sel:
        print(f"(corriendo solo pruebas: {sorted(sel)})")
    print("=" * 78)

    for p in pruebas:
        print(f"\n\n########## PRUEBA {p['id']}: {p['titulo']} ##########")
        print(f"[base real] {p['base']}")
        print(f"[evalua]    {p['evalua']}")
        messages: list[dict] = []
        indep = p.get("turnos_independientes", False)
        for i, (texto, img) in enumerate(p["turnos"], 1):
            if indep:
                messages = []  # cada caso arranca limpio
            etiqueta_img = "  [+FOTO]" if img else ""
            print(f"\n  --- turno {i} ---")
            print(f"  CLIENTE> {texto}{etiqueta_img}")
            r = _correr_turno(messages, texto, img)
            if r["gate"]:
                print(f"  [GATE DETERMINISTA: {r['gate']}]  (no paso al LLM)")
            else:
                tj = ", ".join(r["tools"]) if r["tools"] else "(ninguna)"
                print(f"  [tools: {tj}]  [{r['seg']:.1f}s]")
            print(f"  AGENTE > {r['respuesta']}")
            print("  [envios salientes de este turno]")
            print(_fmt_envios(r["envios"]))

    print("\n\n" + "=" * 78)
    print("FIN DE LAS PRUEBAS")
    print("=" * 78)


if __name__ == "__main__":
    main()
