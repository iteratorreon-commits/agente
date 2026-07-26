"""Tools de Odoo para el agente vendedor: catalogo, stock y cotizacion.

Campos verificados contra Odoo 17 (ver N8N/proyecto bot conversacional/odoo17-campos.md).
Reglas clave:
- Filtrar publicados: [active=true, sale_ok=true, is_published=true].
- available_quantity es computado NO almacenado -> se LEE, no se filtra en domain.
- Many2one llega como [id, "nombre"]; Many2many como lista de ids.
- Odoo devuelve False (no null) para campos vacios -> normalizar.
- Despacho SIEMPRE desde el almacen configurado (Acuna).
"""
from __future__ import annotations

import base64
import uuid
from typing import Any

import httpx
from anthropic import beta_tool

from .. import ficha as ficha_mod
from .. import precios as precios_mod
from .. import session_store
from ..config import cfg
from ..manychat_api import enviar_mensaje
from ..odoo_client import OdooError, odoo
from ..request_context import current_subscriber_id
from ..telegram_api import enviar_telegram

# IDs de sucursal/ubicacion verificados 2026-07-12 (usage=internal).
# El stock puede vivir en ubicaciones hijas (racks) -> consolidar por prefijo de complete_name.
PREFIJOS_SUCURSAL = ("JZ/", "MZ/", "AC/", "MER/")

# Palabras vacias que NO deben exigirse en la busqueda (ej. "blusa DE encaje").
_STOPWORDS = {
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas", "con",
    "para", "por", "y", "o", "en", "al", "que", "mi", "su", "sus", "tu",
}


def _tokens(query: str) -> list[str]:
    """Parte la busqueda en palabras utiles (sin stopwords, min 2 chars)."""
    toks = [t for t in (query or "").lower().split() if len(t) >= 2 and t not in _STOPWORDS]
    return toks or ([query.strip()] if query and query.strip() else [])


# Envio como linea de la cotizacion (igual que los vendedores humanos, ver S04456):
# carrier "Standard delivery" (id 1) con su producto de servicio (id 48). El costo real
# lo pasa el agente (viene de cotizar_envio); Odoo lo suma al total de la orden para que
# el pago concilie completo (producto + envio).
CARRIER_ENVIO_ID = 1
PRODUCTO_ENVIO_ID = 48


def _umbral_envio_gratis() -> float:
    """Umbral de envio gratis (pesos) leido de politicas.json; 4000 por defecto."""
    try:
        import json as _json

        with open(cfg.knowledge_dir / "politicas.json", encoding="utf-8") as f:
            return float(_json.load(f)["envio"]["gratis_umbral_pesos"])
    except Exception:  # noqa: BLE001
        return 4000.0


def _variantes_por_template(tmpl_ids: list[int]) -> dict[int, list[dict]]:
    """Variantes ACTIVAS de cada template, con su talla y color ya resueltos.

    Es la unica lectura de variantes del modulo: la comparten _resolver_variantes (que arma
    la cotizacion), buscar_catalogo y consultar_stock (que muestran el precio por talla).
    Antes esta resolucion de ptav -> talla/color vivia solo dentro de _resolver_variantes.

    Cada entrada trae 'variant_id', 'talla', 'color' y 'free' (existencia ya sin lo
    reservado), MAS los campos crudos de Odoo que precios.py necesita para calcular el
    precio (standard_price, lst_price, categ_id, product_tmpl_id).
    """
    if not tmpl_ids:
        return {}
    variantes = odoo.search_read(
        "product.product",
        [["product_tmpl_id", "in", [int(t) for t in tmpl_ids]], ["active", "=", True]],
        fields=[
            "id", "product_tmpl_id", "display_name", "product_template_attribute_value_ids",
            "free_qty", "standard_price", "lst_price", "categ_id",
        ],
        limit=2000,
    )
    ptav_ids = sorted({i for v in variantes for i in (v.get("product_template_attribute_value_ids") or [])})
    nombre: dict[int, tuple[str, str]] = {}
    if ptav_ids:
        for a in odoo.search_read(
            "product.template.attribute.value",
            [["id", "in", ptav_ids]],
            fields=["id", "name", "attribute_id"],
            limit=5000,
        ):
            atype = (a["attribute_id"][1] if a.get("attribute_id") else "").upper()
            nombre[a["id"]] = (atype, str(a.get("name") or "").strip().upper())

    por_tmpl: dict[int, list[dict]] = {}
    for v in variantes:
        tmpl = v["product_tmpl_id"][0] if v.get("product_tmpl_id") else None
        talla = color = ""
        for i in v.get("product_template_attribute_value_ids") or []:
            atype, val = nombre.get(i, ("", ""))
            if "TALLA" in atype:
                talla = val
            elif "COLOR" in atype:
                color = val
        por_tmpl.setdefault(tmpl, []).append(
            dict(v, variant_id=v["id"], free=int(v.get("free_qty") or 0), talla=talla, color=color)
        )
    return por_tmpl


def _precios_de_templates(por_tmpl: dict[int, list[dict]]) -> dict[int, dict]:
    """Precio de venta por template: {'precio_desde', 'precio_hasta', 'precio_por_talla'}.

    Se calcula con la MISMA lista de precios con la que crear_cotizacion arma la orden
    (cfg.odoo_pricelist_id), asi que el precio que el agente anuncia es exactamente el que
    va a salir en la cotizacion.

    Si una talla existe en varios colores con precios distintos se queda el MAS BAJO: es el
    que sostiene el "desde $X" que usan los catalogos. Un template sin ningun precio
    calculable devuelve {} y el que llama degrada (nunca inventa un numero).
    """
    todas = [v for lista in por_tmpl.values() for v in lista]
    if not todas:
        return {}
    precio_de = precios_mod.precios_de_variantes(todas)

    salida: dict[int, dict] = {}
    for tmpl, lista in por_tmpl.items():
        por_talla: dict[str, float] = {}
        for v in lista:
            p = precio_de.get(int(v["id"]))
            if p is None:
                continue
            clave = str(v.get("talla") or "unica")
            if clave not in por_talla or p < por_talla[clave]:
                por_talla[clave] = p
        if por_talla:
            valores = list(por_talla.values())
            salida[tmpl] = {
                "precio_desde": min(valores),
                "precio_hasta": max(valores),
                "precio_por_talla": por_talla,
            }
    return salida


def _resolver_variantes(lineas: list[dict]) -> tuple[list, list]:
    """Resuelve cada linea {template_id, talla, color, cantidad} a su VARIANTE exacta y
    verifica existencia real (free_qty = disponible, ya sin lo reservado).

    Devuelve (order_lines, faltantes):
    - order_lines: tuplas (0,0,{product_id, product_uom_qty}) UNA por variante, con SOLO lo
      que se puede completar (capado a lo disponible). Nunca sobre-promete stock.
    - faltantes: lista de lo que no se pudo cubrir, para que el agente ofrezca alternativas:
      {template_id, talla, color, pedido, disponible, faltan} o {..., motivo}.
    """
    tmpl_ids = sorted({int(it["template_id"]) for it in lineas if it.get("template_id")})
    if not tmpl_ids:
        return [], []
    por_tmpl = _variantes_por_template(tmpl_ids)

    order_lines: list = []
    faltantes: list = []
    for it in lineas:
        tmpl = int(it.get("template_id") or 0)
        cant = int(it.get("cantidad") or 0)
        talla = str(it.get("talla") or "").strip().upper()
        color = str(it.get("color") or "").strip().upper()
        if not tmpl or cant <= 0:
            continue
        vlist = por_tmpl.get(tmpl, [])
        if not vlist:
            faltantes.append({"template_id": tmpl, "talla": talla, "color": color, "pedido": cant, "motivo": "sin_variantes"})
            continue
        cand = None
        if talla or color:
            for info in vlist:
                if (not talla or info["talla"] == talla) and (not color or info["color"] == color):
                    cand = info
                    break
        elif len(vlist) == 1:
            cand = vlist[0]
        if cand is None:
            faltantes.append({"template_id": tmpl, "talla": talla, "color": color, "pedido": cant, "motivo": "variante_no_existe"})
            continue
        disp = max(cand["free"], 0)
        surtir = min(cant, disp)
        if surtir < cant:
            faltantes.append({
                "template_id": tmpl, "talla": cand["talla"] or talla, "color": cand["color"] or color,
                "pedido": cant, "disponible": disp, "faltan": cant - disp,
            })
        if surtir > 0:
            order_lines.append((0, 0, {"product_id": cand["variant_id"], "product_uom_qty": surtir}))
    return order_lines, faltantes


def _norm(v: Any) -> Any:
    """Odoo devuelve False para vacios; lo convierte a '' para texto."""
    return "" if v is False else v


def _nombre_partner(nombre: str) -> str:
    """Nombre usable para el contacto de Odoo.

    En produccion quedo un res.partner llamado literalmente '🥰' (partner 20621, orden S04552):
    el modelo tomo como nombre lo que el cliente habia escrito. Si no hay ninguna letra, se
    cae a un nombre identificable por el subscriber de WhatsApp.
    """
    limpio = (nombre or "").strip()
    if any(c.isalpha() for c in limpio):
        return limpio[:120]
    sid = current_subscriber_id.get()
    return f"Cliente WhatsApp {sid}" if sid else "Cliente WhatsApp"


def _leer_orden(order_id: int) -> dict | None:
    """Lee una sale.order con sus lineas reales. None si no existe."""
    filas = odoo.search_read(
        "sale.order",
        [["id", "=", int(order_id)]],
        fields=[
            "name", "state", "partner_id", "amount_untaxed", "amount_total",
            "carrier_id", "date_order", "note",
        ],
        limit=1,
    )
    if not filas:
        return None
    orden = filas[0]
    orden["lineas"] = odoo.search_read(
        "sale.order.line",
        [["order_id", "=", int(order_id)]],
        fields=["id", "name", "product_id", "product_uom_qty", "price_unit", "price_subtotal", "is_delivery"],
        limit=200,
        order="id asc",
    )
    return orden


def _buscar_orden(folio: str = "", order_id: int = 0, telefono: str = "") -> dict | None:
    """Resuelve a que sale.order se refiere el agente, en orden de confiabilidad.

    1. order_id o folio explicitos (lo que devolvio una tool).
    2. La cotizacion vigente de la ficha del cliente.
    3. La tabla de enlace subscriber -> order (lo que este agente le creo a este cliente).
    4. La ultima orden del partner por telefono.
    """
    if order_id:
        return _leer_orden(int(order_id))
    if folio:
        filas = odoo.search_read(
            "sale.order", [["name", "=", folio.strip().upper()]], fields=["id"], limit=1
        )
        return _leer_orden(filas[0]["id"]) if filas else None

    sid = current_subscriber_id.get()
    if sid:
        vigente = (session_store.cargar_ficha(sid).get("cotizacion_vigente") or {}).get("order_id")
        if vigente:
            orden = _leer_orden(int(vigente))
            if orden:
                return orden
        for reg in session_store.cotizaciones_de(sid):
            orden = _leer_orden(int(reg["order_id"]))
            if orden:
                return orden

    if telefono:
        partners = odoo.search_read(
            "res.partner",
            ["|", ["phone", "=", telefono], ["mobile", "=", telefono]],
            fields=["id"],
            limit=1,
        )
        if partners:
            filas = odoo.search_read(
                "sale.order",
                [["partner_id", "=", partners[0]["id"]]],
                fields=["id"],
                limit=1,
                order="id desc",
            )
            if filas:
                return _leer_orden(filas[0]["id"])
    return None


def _imagen_existente(order_id: int) -> str:
    """URL de la foto de la cotizacion ya generada, si existe (evita re-renderizar el PDF)."""
    try:
        att = odoo.search_read(
            "ir.attachment",
            [
                ["res_model", "=", "sale.order"],
                ["res_id", "=", int(order_id)],
                ["mimetype", "=", "image/png"],
                ["public", "=", True],
            ],
            fields=["id"],
            limit=1,
            order="id desc",
        )
    except OdooError:
        return ""
    return f"{cfg.odoo_url.rstrip('/')}/web/image/{att[0]['id']}" if att else ""


def _payload_orden(orden: dict) -> dict:
    """Normaliza una orden leida de Odoo para devolverla al modelo."""
    return {
        "folio": _norm(orden.get("name")),
        "order_id": orden["id"] if "id" in orden else None,
        "estado": _norm(orden.get("state")),
        "cliente": (orden["partner_id"][1] if orden.get("partner_id") else ""),
        "subtotal_sin_impuestos": orden.get("amount_untaxed"),
        "total": orden.get("amount_total"),
        "fecha": _norm(orden.get("date_order")),
        "lineas": [
            {
                "line_id": l["id"],
                "descripcion": _norm(l.get("name")),
                "cantidad": l.get("product_uom_qty"),
                "precio_unitario": l.get("price_unit"),
                "subtotal": l.get("price_subtotal"),
                "es_envio": bool(l.get("is_delivery")),
            }
            for l in orden.get("lineas") or []
        ],
    }


def _imagen_url(template_id: int) -> str:
    return f"{cfg.odoo_url.rstrip('/')}/web/image/product.template/{template_id}/image_1024"


def _pdf_cotizacion_url(order_id: int) -> str:
    """URL PUBLICA del PDF de la cotizacion (portal + access_token, sin login).

    Odoo deja access_token vacio hasta que se comparte; le escribimos un token propio.
    La ruta /my/orders/<id>?...report_type=pdf sirve el PDF real a quien tenga el token
    (verificado: HTTP 200 application/pdf). Es la forma mas simple de 'extraer' el PDF.
    """
    try:
        rows = odoo.search_read(
            "sale.order", [["id", "=", order_id]], fields=["access_token"], limit=1
        )
        token = rows[0].get("access_token") if rows else None
        if not token:
            token = uuid.uuid4().hex
            odoo.execute_kw("sale.order", "write", [[order_id], {"access_token": token}])
        base = cfg.odoo_url.rstrip("/")
        return f"{base}/my/orders/{order_id}?access_token={token}&report_type=pdf&download=true"
    except OdooError:
        return ""


def _imagen_cotizacion_url(order_id: int, pdf_url: str, nombre: str) -> str:
    """Renderiza la 1a pagina del PDF de la cotizacion a PNG y la guarda como adjunto
    PUBLICO en Odoo; devuelve su URL publica (/web/image/<id>).

    WhatsApp NO entrega PDFs por la API pero SI imagenes, asi que mandamos la cotizacion
    como FOTO para que el cliente la vea en el chat. Se hostea en Odoo (dominio estable),
    no en el tunel. Devuelve '' si algo falla (el flujo cae al link pdf_url).
    """
    if not pdf_url:
        return ""
    try:
        import fitz  # PyMuPDF (render PDF -> imagen, sin binarios externos)

        pdf_bytes = httpx.get(pdf_url, timeout=30, follow_redirects=True).content
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        png = doc.load_page(0).get_pixmap(dpi=150).tobytes("png")
        doc.close()
        att_id = odoo.create(
            "ir.attachment",
            {
                "name": f"Cotizacion_{nombre}.png",
                "datas": base64.b64encode(png).decode(),
                "mimetype": "image/png",
                "res_model": "sale.order",
                "res_id": order_id,
                "public": True,
            },
        )
        return f"{cfg.odoo_url.rstrip('/')}/web/image/{att_id}"
    except (OdooError, httpx.HTTPError, Exception):  # noqa: BLE001 — degradar al link
        return ""


@beta_tool
def buscar_catalogo(
    query: str,
    talla: str = "",
    color: str = "",
    limit: int = 8,
) -> str:
    """Busca productos en el catalogo de Odoo y devuelve su PRECIO, tallas y colores disponibles.

    Usa esta herramienta cuando el cliente pregunta por un producto, modelo o categoria
    (ej. 'blusa campesina', 'vestido Alicia', 'moños tricolor') y tambien cuando solo
    pregunta CUANTO CUESTA algo. Devuelve solo productos publicados y a la venta.

    El precio viene aqui YA CALCULADO, con la misma lista con la que se cotiza: 'precio_desde',
    'precio_hasta' y 'precio_por_talla'. NO hace falta crear una cotizacion para saber un
    precio, y no debes crearla para eso. NO inventes productos ni precios: si esta tool no
    devuelve algo, no existe o no esta disponible, y debes escalar o pedir mas detalle.

    Args:
        query: Texto de busqueda del producto (nombre o parte del nombre).
        talla: Filtro opcional de talla (ej. '6', 'CH', 'M').
        color: Filtro opcional de color (ej. 'rojo', 'blanco').
        limit: Maximo de productos a devolver (default 8).
    """
    # Busqueda por PALABRAS, no por la frase completa: el cliente dice "falda lisa"
    # pero en Odoo es "FALDA TRADICIONAL LISA" (el ilike de la frase no la encuentra).
    # 1) AND de tokens (todas las palabras deben aparecer) -> match preciso.
    # 2) Si no hay, OR de tokens (cualquier palabra) -> alternativas para no dejar al
    #    cliente sin opciones (regla: nunca solo "no hay").
    lim = max(1, min(limit, 20))
    base = [["active", "=", True], ["sale_ok", "=", True], ["is_published", "=", True]]
    tokens = _tokens(query)
    modo = "exacto"
    try:
        dom_and = base + [["name", "ilike", t] for t in tokens]
        productos = odoo.search_read(
            "product.template", dom_and,
            fields=["id", "name", "default_code"], limit=lim, order="name asc",
        )
        if not productos and len(tokens) > 1:
            or_group = ["|"] * (len(tokens) - 1) + [["name", "ilike", t] for t in tokens]
            dom_or = ["&"] * len(base) + base + or_group
            productos = odoo.search_read(
                "product.template", dom_or,
                fields=["id", "name", "default_code"], limit=lim, order="name asc",
            )
            modo = "aproximado"
    except OdooError as exc:
        return f"ERROR_ODOO: {exc}. No pude consultar el catalogo; escala o reintenta."

    if not productos:
        return (
            f"SIN_RESULTADOS: no se encontraron productos publicados para '{query}'. "
            "No inventes; ofrece que el cliente mande una foto o el nombre exacto, o escala."
        )

    ids = [p["id"] for p in productos]
    # Una sola lectura de variantes sirve para las tallas/colores Y para el precio: el
    # precio cambia por talla justamente porque el costo cambia por variante.
    try:
        por_tmpl = _variantes_por_template(ids)
    except OdooError:
        por_tmpl = {}
    precio_de_tmpl = _precios_de_templates(por_tmpl)

    tallas_por_tmpl: dict[int, set[str]] = {}
    colores_por_tmpl: dict[int, set[str]] = {}
    for tmpl, variantes in por_tmpl.items():
        for v in variantes:
            if v.get("talla"):
                tallas_por_tmpl.setdefault(tmpl, set()).add(str(v["talla"]))
            if v.get("color"):
                colores_por_tmpl.setdefault(tmpl, set()).add(str(v["color"]))

    lineas = []
    for p in productos:
        tid = p["id"]
        tallas = sorted(tallas_por_tmpl.get(tid, set()))
        colores = sorted(colores_por_tmpl.get(tid, set()))
        # Filtro suave por talla/color si el cliente lo pidio.
        if talla and tallas and talla.upper() not in [t.upper() for t in tallas]:
            continue
        if color and colores and color.upper() not in [c.upper() for c in colores]:
            continue
        lineas.append(
            {
                "template_id": tid,
                "nombre": _norm(p.get("name")),
                "codigo": _norm(p.get("default_code")),
                "tallas_disponibles": tallas,
                "colores_disponibles": colores,
                "imagen_url": _imagen_url(tid),
                **precio_de_tmpl.get(tid, {}),
            }
        )

    if not lineas:
        return (
            f"SIN_RESULTADOS_TRAS_FILTRO: hay productos para '{query}' pero no en talla='{talla}' "
            f"color='{color}'. Ofrece alternativas (nunca solo 'no hay')."
        )

    import json

    # Deja los modelos (con su template_id, tallas y colores REALES) en la ficha del cliente.
    # Es determinista a proposito: si dependiera de que el modelo se acuerde de anotarlos, al
    # siguiente turno volveria a inventar colores como paso en produccion ("veo que en el
    # catalogo hay tambien negro y rosa" con tools_invocadas: []).
    ficha_mod.agregar_modelos(lineas)

    nota_match = (
        "coincidencia EXACTA con lo que pidio el cliente."
        if modo == "exacto"
        else "NO hubo match exacto; estos son APROXIMADOS (coinciden con alguna palabra). "
        "Ofrecelos como alternativas ('no tengo tal cual X, pero mira estos parecidos'), "
        "no como el producto exacto."
    )
    return json.dumps(
        {
            "productos": lineas,
            "_match": modo,
            "_nota_match": nota_match,
            "_nota_precio": (
                "El precio YA viene aqui y es el REAL: 'precio_desde' y 'precio_hasta' son el rango "
                "del modelo y 'precio_por_talla' el precio exacto de cada talla. Estan calculados "
                "con la misma lista de precios con la que se hace la cotizacion, asi que coinciden "
                "con lo que saldra en ella. "
                "COMO USARLO: dile el precio EN ESTE MISMO TURNO como '*desde $X*' (asi lo dicen "
                "los catalogos, porque el precio sube en tallas grandes). Si el cliente pregunta por "
                "una talla concreta, contestale el exacto de 'precio_por_talla' SIN llamar ninguna "
                "otra tool. "
                "NUNCA crees una cotizacion solo para averiguar un precio: la cotizacion es para "
                "cerrar un pedido ya confirmado, no para consultar. "
                "Es precio de MAYOREO, que aplica desde 6 piezas; si el cliente quiere menos, "
                "aclaraselo y ofrecele la tienda en linea (no inventes un precio de menudeo). "
                "Si un producto viene SIN precio, no lo inventes ni lo deduzcas de otro modelo: dile "
                "que se lo confirmas en un momento y escala."
            ),
        },
        ensure_ascii=False,
    )


@beta_tool
def consultar_stock(template_id: int) -> str:
    """Consulta la existencia real (en vivo) de un producto en Odoo, consolidada por sucursal.

    Usa esta tool para confirmar disponibilidad antes de prometer piezas. Devuelve cantidad
    disponible por sucursal (JZ, MZ, AC/Acuna, MER) y tambien el PRECIO por talla, para que no
    tengas que volver a buscar el modelo solo para decir cuanto cuesta.
    El despacho de este canal es desde Acuna, pero las rutas de Odoo surten de otras sucursales
    si hace falta, asi que informa el total.
    NO inventes cantidades ni des numeros crudos de stock al cliente; usa 'disponible/pocas/agotado'.

    Args:
        template_id: El id de product.template (lo devuelve buscar_catalogo).
    """
    try:
        variantes = _variantes_por_template([template_id]).get(template_id, [])
        if not variantes:
            return "SIN_VARIANTES: el producto no tiene variantes activas. Escala si es raro."
        var_ids = [v["id"] for v in variantes]
        quants = odoo.search_read(
            "stock.quant",
            [
                ["product_id", "in", var_ids],
                ["location_id.usage", "=", "internal"],
            ],
            fields=["product_id", "location_id", "quantity", "reserved_quantity"],
            limit=1000,
        )
    except OdooError as exc:
        return f"ERROR_ODOO: {exc}. No pude confirmar stock; escala o reintenta."

    por_sucursal: dict[str, float] = {}
    for q in quants:
        loc_name = q["location_id"][1] if q.get("location_id") else ""
        disponible = (q.get("quantity") or 0) - (q.get("reserved_quantity") or 0)
        for pref in PREFIJOS_SUCURSAL:
            if loc_name.startswith(pref):
                key = pref.rstrip("/")
                por_sucursal[key] = por_sucursal.get(key, 0) + disponible
                break

    precio = _precios_de_templates({template_id: variantes}).get(template_id, {})

    # Respaldo DETERMINISTA de la ficha: si el agente consulto existencia de este modelo, es el
    # que esta sobre la mesa. anotar_pedido lo decide el modelo y a veces no lo llama, asi que al
    # menos la identidad del producto queda registrada sin depender de eso.
    nombre_tmpl = str(variantes[0].get("display_name") or "").split(" (")[0].strip()
    if nombre_tmpl:
        ficha_mod.agregar_modelos(
            [{"template_id": template_id, "nombre": nombre_tmpl, **precio}]
        )

    total = sum(por_sucursal.values())
    # Etiqueta honesta de escasez (no numeros crudos al LLM->cliente).
    if total <= 0:
        etiqueta = "agotado"
    elif total < 6:
        etiqueta = "pocas_piezas"
    else:
        etiqueta = "disponible"

    import json

    return json.dumps(
        {
            "template_id": template_id,
            "estado": etiqueta,
            "total_disponible": total,
            "por_sucursal": {k: por_sucursal.get(k, 0) for k in ("JZ", "MZ", "AC", "MER")},
            "despacho": "Acuna (AC); rutas de Odoo surten de otras sucursales si falta",
            **precio,
            "_nota_precio": (
                "El precio de este modelo va aqui mismo: dilo como '*desde $X*' y da el exacto de "
                "'precio_por_talla' si preguntan por una talla. No crees una cotizacion para "
                "averiguar un precio. Es precio de mayoreo (desde 6 piezas)."
            ),
        },
        ensure_ascii=False,
    )


@beta_tool
def crear_cotizacion(
    cliente_nombre: str,
    cliente_telefono: str,
    lineas: list[dict],
    costo_envio: float = 0.0,
    notas: str = "",
) -> str:
    """Crea una cotizacion (sale.order en borrador) en Odoo para el cliente.

    Usala solo cuando ya confirmaste con el cliente los modelos, tallas y cantidades, y
    validaste stock. La orden se crea en estado BORRADOR (draft), despacho desde Acuna, y
    NO confirma ningun pago. Devuelve el numero de orden y el total. Si algo falla, NO
    inventes un folio: informa el error y escala.

    ENVIO: si ya cotizaste el envio (cotizar_envio) y tienes el costo, pasalo en 'costo_envio'
    para que quede como LINEA de la cotizacion (el pago concilia completo, producto + envio).
    Regla automatica de envio gratis: si el subtotal de PRODUCTOS supera el umbral (politicas,
    $4,000), el envio NO se agrega aunque mandes costo_envio (corre por cuenta de la empresa).

    Args:
        cliente_nombre: Nombre del cliente.
        cliente_telefono: Telefono del cliente (se usa para buscar/crear el contacto).
        lineas: UNA entrada por variante (talla+color): cada una
            {"template_id": int, "talla": str, "color": str, "cantidad": int}. Ej. si el
            cliente quiere 2 por talla en 2 colores, manda una linea por cada talla+color
            (NO agrupes todo en una). 'talla'/'color' pueden ir vacios si el producto no
            tiene esa dimension (ej. un accesorio de una sola variante).
        costo_envio: Costo del envio (de cotizar_envio) a agregar como linea. 0 = sin envio aun.
        notas: Notas internas opcionales para la cotizacion.
    """
    if not lineas:
        return "ERROR_ARGUMENTOS: la cotizacion no tiene lineas. Confirma el pedido primero."
    import json

    faltantes: list = []
    try:
        # Buscar o crear el contacto por telefono.
        partners = odoo.search_read(
            "res.partner",
            ["|", ["phone", "=", cliente_telefono], ["mobile", "=", cliente_telefono]],
            fields=["id"],
            limit=1,
        )
        if partners:
            partner_id = partners[0]["id"]
        else:
            partner_id = odoo.create(
                "res.partner",
                {"name": _nombre_partner(cliente_nombre), "phone": cliente_telefono},
            )

        # Resolver la VARIANTE exacta (talla+color) de cada linea y verificar existencia
        # real por variante (no sobre-promete): una linea por variante, capada a lo disponible.
        order_lines, faltantes = _resolver_variantes(lineas)
        if not order_lines:
            return (
                "SIN_STOCK_COMPLETABLE: ninguna de las variantes pedidas tiene existencia "
                "para completar. NO inventes; ofrece alternativas (otra talla/color/modelo) "
                "o escala. Detalle faltantes=" + json.dumps(faltantes, ensure_ascii=False)
            )

        values: dict[str, Any] = {
            "partner_id": partner_id,
            "order_line": order_lines,
        }
        if cfg.odoo_pricelist_id:
            values["pricelist_id"] = cfg.odoo_pricelist_id
        if cfg.odoo_warehouse_id:
            values["warehouse_id"] = cfg.odoo_warehouse_id
        if cfg.odoo_sales_team_id:
            values["team_id"] = cfg.odoo_sales_team_id
        if notas:
            values["note"] = notas

        order_id = odoo.create("sale.order", values)
        orden = odoo.search_read(
            "sale.order",
            [["id", "=", order_id]],
            fields=["name", "amount_total"],
            limit=1,
        )
        nombre = orden[0]["name"] if orden else str(order_id)
        total = orden[0]["amount_total"] if orden else None
    except OdooError as exc:
        return f"ERROR_ODOO: {exc}. No se creo la cotizacion; escala o reintenta."

    # --- Envio como linea estandar de la cotizacion ---
    # 'total' aqui es el subtotal de PRODUCTOS (aun sin envio). Regla: si supera el umbral,
    # el envio es GRATIS y NO se agrega; debajo del umbral se agrega el costo real como
    # linea de entrega (carrier "Standard delivery"), igual que los vendedores humanos.
    subtotal_productos = total or 0.0
    umbral = _umbral_envio_gratis()
    envio_gratis = subtotal_productos > umbral
    envio_aplicado = 0.0
    if not envio_gratis and costo_envio and costo_envio > 0:
        try:
            odoo.execute_kw(
                "sale.order", "write", [[order_id], {"carrier_id": CARRIER_ENVIO_ID}]
            )
            odoo.create(
                "sale.order.line",
                {
                    "order_id": order_id,
                    "product_id": PRODUCTO_ENVIO_ID,
                    "name": "Standard delivery",
                    "product_uom_qty": 1,
                    "price_unit": float(costo_envio),
                    "is_delivery": True,
                },
            )
            envio_aplicado = float(costo_envio)
            orden2 = odoo.search_read(
                "sale.order", [["id", "=", order_id]], fields=["amount_total"], limit=1
            )
            if orden2:
                total = orden2[0]["amount_total"]
        except OdooError:
            # Si el envio no se pudo agregar, la cotizacion (sin envio) sigue siendo valida.
            envio_aplicado = 0.0

    pdf_url = _pdf_cotizacion_url(order_id)
    imagen_url = _imagen_cotizacion_url(order_id, pdf_url, nombre)

    # Enviar la cotizacion como FOTO al cliente (WhatsApp entrega imagenes, no PDFs).
    imagen_enviada = False
    sid = current_subscriber_id.get()
    if imagen_url and sid:
        imagen_enviada = bool(enviar_mensaje(sid, "", imagen_url=imagen_url).get("ok"))

    # Dejar constancia de que esta orden es de este cliente de WhatsApp, y guardar folio/total
    # en la ficha. Sin esto el agente no tiene de donde leer su propia cotizacion al turno
    # siguiente y termina recitando de memoria un folio que no existe (caso real: S04541).
    if sid:
        session_store.registrar_cotizacion(sid, order_id, nombre)
    ficha_mod.set_cotizacion(
        folio=nombre,
        order_id=order_id,
        total=total,
        estado="draft",
        pdf_url=pdf_url,
        imagen_url=imagen_url,
    )
    ficha_mod.set_cliente(nombre=cliente_nombre, telefono=cliente_telefono)

    return json.dumps(
        {
            "numero_orden": nombre,
            "order_id": order_id,
            "total": total,
            "envio_gratis": envio_gratis,
            "costo_envio_aplicado": envio_aplicado,
            "faltantes": faltantes,
            "pdf_url": pdf_url,
            "imagen_cotizacion_url": imagen_url,
            "imagen_enviada": imagen_enviada,
            "estado": "borrador (no confirmado, no aparta piezas hasta el pago)",
            "_instruccion_cotizacion": (
                "La cotizacion se armo con UNA linea por variante (talla+color) y SOLO con lo "
                "que hay en existencia. Si 'faltantes' NO esta vacio, dile con claridad al "
                "cliente que de esas tallas/colores solo se pudo completar lo disponible "
                "(ej. 'de la talla 8 en negro solo pudimos completar 3') y ofrece completar "
                "las que faltan en OTRA talla/color o modelo parecido (nunca solo 'no hay'). "
                "El 'total' YA incluye el envio (o es gratis): si envio_gratis=true, dile que "
                "su envio es GRATIS por superar los $4,000 🤩; si costo_envio_aplicado>0, que su "
                "envio (${costo_envio_aplicado}) ya viene incluido en el total. "
                "Si imagen_enviada=true, YA se le mando al cliente su cotizacion como FOTO: "
                "confirmaselo (ej. 'Le envie su cotizacion <numero_orden> por $<total> 📸'). "
                "Ademas comparte pdf_url como link por si quiere descargar el PDF. "
                "Si imagen_enviada=false pero hay pdf_url, comparte al menos ese link. "
                "Nunca inventes folio, total ni links si vienen vacios."
            ),
        },
        ensure_ascii=False,
    )


@beta_tool
def consultar_cotizacion(folio: str = "", order_id: int = 0, telefono: str = "") -> str:
    """Lee de Odoo una cotizacion ya creada, con su folio, total y TODAS sus lineas reales.

    Usala SIEMPRE antes de hablarle al cliente de su cotizacion: para recordarle que lleva,
    confirmarle el total, revisar si falta algo o antes de modificarla. Sin argumentos toma la
    cotizacion vigente de esta conversacion.

    Esta tool es la UNICA fuente valida del folio, el total y las lineas. NUNCA los digas de
    memoria: el folio y el monto cambian, y un dato inventado le promete al cliente algo que el
    negocio no va a sostener.

    Args:
        folio: Numero de la cotizacion (ej. 'S04552'). Opcional.
        order_id: Id interno de la orden, si lo tienes de otra tool. Opcional.
        telefono: Telefono del cliente, para buscar su ultima orden. Opcional.
    """
    import json

    try:
        orden = _buscar_orden(folio=folio, order_id=order_id, telefono=telefono)
    except OdooError as exc:
        return f"ERROR_ODOO: {exc}. No pude leer la cotizacion; escala o reintenta."

    if not orden:
        pedido = folio or (f"order_id {order_id}" if order_id else "esta conversacion")
        return (
            f"NO_ENCONTRADA: no existe ninguna cotizacion para {pedido}. NO inventes un folio "
            "ni un total. Si el cliente cree tener una cotizacion, pidele el folio o escala; si "
            "todavia no se le ha creado, dile que se la preparas y usa crear_cotizacion."
        )

    datos = _payload_orden(orden)
    oid = orden["id"]
    pdf_url = _pdf_cotizacion_url(oid)
    imagen_url = _imagen_existente(oid)
    confirmada = datos["estado"] not in ("draft", "sent")

    # Refresca la ficha con los datos REALES (pueden haber cambiado desde que se creo).
    ficha_mod.set_cotizacion(
        folio=datos["folio"],
        order_id=oid,
        total=datos["total"],
        estado=datos["estado"],
        pdf_url=pdf_url,
        imagen_url=imagen_url,
    )

    datos.update(
        {
            "order_id": oid,
            "pdf_url": pdf_url,
            "imagen_cotizacion_url": imagen_url,
            "modificable": not confirmada,
            "_instruccion": (
                "Estos son los datos REALES de Odoo. Usa EXACTAMENTE este folio, este total y "
                "estas lineas; no los redondees ni los completes con nada que recuerdes. Las "
                "lineas con es_envio=true son el costo de envio, no producto. "
                + (
                    "La cotizacion YA ESTA CONFIRMADA: no se puede modificar; si el cliente "
                    "quiere cambios, escala con escalar_a_benny."
                    if confirmada
                    else "Esta en borrador: si el cliente pide cambios, usa modificar_cotizacion."
                )
            ),
        }
    )
    return json.dumps(datos, ensure_ascii=False)


@beta_tool
def reenviar_cotizacion(folio: str = "", order_id: int = 0) -> str:
    """Reenvia al cliente por WhatsApp la FOTO de una cotizacion ya creada.

    Usala cuando el cliente pide que le mandes otra vez su cotizacion, cuando dice que no le
    llego, o al retomar una conversacion. No crea nada nuevo ni cambia el pedido.

    Args:
        folio: Numero de la cotizacion (ej. 'S04552'). Sin argumentos usa la de esta conversacion.
        order_id: Id interno de la orden, si lo tienes. Opcional.
    """
    import json

    sid = current_subscriber_id.get()
    if not sid:
        return "NO_ENVIADO: sin destinatario en contexto."
    try:
        orden = _buscar_orden(folio=folio, order_id=order_id)
    except OdooError as exc:
        return f"ERROR_ODOO: {exc}. No pude leer la cotizacion; escala o reintenta."
    if not orden:
        return (
            "NO_ENCONTRADA: no hay ninguna cotizacion que reenviar. NO inventes un folio; "
            "pregunta al cliente el folio o armale una nueva con crear_cotizacion."
        )

    oid = orden["id"]
    nombre = _norm(orden.get("name")) or str(oid)
    pdf_url = _pdf_cotizacion_url(oid)
    # Reutiliza la foto ya generada; solo re-renderiza si no existe.
    imagen_url = _imagen_existente(oid) or _imagen_cotizacion_url(oid, pdf_url, nombre)
    if not imagen_url:
        return json.dumps(
            {
                "imagen_enviada": False,
                "folio": nombre,
                "pdf_url": pdf_url,
                "_instruccion": "No se pudo generar la foto. Comparte al menos el pdf_url.",
            },
            ensure_ascii=False,
        )

    enviada = bool(enviar_mensaje(sid, "", imagen_url=imagen_url).get("ok"))
    return json.dumps(
        {
            "imagen_enviada": enviada,
            "folio": nombre,
            "total": orden.get("amount_total"),
            "estado": _norm(orden.get("state")),
            "pdf_url": pdf_url,
            "_instruccion": (
                "Si imagen_enviada=true, ya le llego la foto de su cotizacion: confirmaselo "
                "usando este folio y este total, no otros."
            ),
        },
        ensure_ascii=False,
    )


def _recalcular_envio(order_id: int, costo_envio: float = 0.0) -> dict:
    """Ajusta la linea de envio de la orden segun la regla de envio gratis.

    Se llama despues de cambiar productos: si el subtotal cruzo el umbral, el envio que estaba
    cobrado hay que QUITARLO (y al reves). Sin esto, modificar una cotizacion dejaba un envio
    cobrado sobre un pedido que ya calificaba para envio gratis.
    """
    lineas = odoo.search_read(
        "sale.order.line",
        [["order_id", "=", int(order_id)]],
        fields=["id", "price_subtotal", "is_delivery", "price_unit"],
        limit=200,
    )
    linea_envio = next((l for l in lineas if l.get("is_delivery")), None)
    subtotal_productos = sum(
        (l.get("price_subtotal") or 0) for l in lineas if not l.get("is_delivery")
    )
    umbral = _umbral_envio_gratis()
    gratis = subtotal_productos > umbral

    # Costo a aplicar: el que se pase, o el que ya tenia la linea de envio.
    costo = float(costo_envio or 0) or float((linea_envio or {}).get("price_unit") or 0)

    if gratis and linea_envio:
        odoo.execute_kw("sale.order.line", "unlink", [[linea_envio["id"]]])
        return {"envio_gratis": True, "costo_envio_aplicado": 0.0}
    if not gratis and costo > 0:
        if linea_envio:
            odoo.execute_kw(
                "sale.order.line", "write", [[linea_envio["id"]], {"price_unit": costo}]
            )
        else:
            odoo.execute_kw("sale.order", "write", [[int(order_id)], {"carrier_id": CARRIER_ENVIO_ID}])
            odoo.create(
                "sale.order.line",
                {
                    "order_id": int(order_id),
                    "product_id": PRODUCTO_ENVIO_ID,
                    "name": "Standard delivery",
                    "product_uom_qty": 1,
                    "price_unit": costo,
                    "is_delivery": True,
                },
            )
        return {"envio_gratis": False, "costo_envio_aplicado": costo}
    return {"envio_gratis": gratis, "costo_envio_aplicado": 0.0}


@beta_tool
def modificar_cotizacion(
    folio: str = "",
    order_id: int = 0,
    agregar: list[dict] | None = None,
    quitar: list[int] | None = None,
    cambiar_cantidad: list[dict] | None = None,
    costo_envio: float = 0.0,
) -> str:
    """Modifica una cotizacion en BORRADOR: agrega, quita o cambia la cantidad de sus lineas.

    Usala cuando el cliente ya tiene cotizacion y pide un cambio ('quitame las faldas',
    'subele a 4 la talla 6', 'agregale 2 blusas mas'). Valida existencia real por variante y
    recalcula el total y el envio (incluida la regla de envio gratis). Al terminar le reenvia al
    cliente la foto actualizada.

    Solo funciona si la cotizacion esta en BORRADOR. Si ya esta confirmada, esta tool te lo dice
    y entonces debes escalar con escalar_a_benny: no le prometas al cliente un cambio que no se
    pudo hacer. Llama primero a consultar_cotizacion para ver las lineas y sus line_id.

    Args:
        folio: Numero de la cotizacion (ej. 'S04552'). Sin argumentos usa la de esta conversacion.
        order_id: Id interno de la orden, si lo tienes. Opcional.
        agregar: Lineas nuevas, UNA por variante:
            {"template_id": int, "talla": str, "color": str, "cantidad": int}.
        quitar: Lista de line_id a eliminar (los devuelve consultar_cotizacion).
        cambiar_cantidad: [{"line_id": int, "cantidad": int}]. cantidad 0 elimina la linea.
        costo_envio: Costo de envio a aplicar si el pedido queda por debajo del umbral. Opcional.
    """
    import json

    try:
        orden = _buscar_orden(folio=folio, order_id=order_id)
    except OdooError as exc:
        return f"ERROR_ODOO: {exc}. No se modifico nada; escala o reintenta."
    if not orden:
        return (
            "NO_ENCONTRADA: no hay cotizacion que modificar. NO inventes un folio; pide el folio "
            "al cliente o crea una nueva con crear_cotizacion."
        )

    oid = orden["id"]
    nombre = _norm(orden.get("name")) or str(oid)
    estado = _norm(orden.get("state"))
    if estado not in ("draft", "sent"):
        return json.dumps(
            {
                "modificada": False,
                "folio": nombre,
                "estado": estado,
                "_instruccion": (
                    f"La cotizacion {nombre} YA ESTA CONFIRMADA (estado {estado}) y no se puede "
                    "modificar desde aqui. NO le digas al cliente que ya se cambio. Escala con "
                    "escalar_a_benny explicando el cambio que pide, y dile al cliente que se lo "
                    "confirmas en un momento."
                ),
            },
            ensure_ascii=False,
        )

    faltantes: list = []
    hechos: list[str] = []
    try:
        # 1) Quitar lineas.
        ids_quitar = [int(i) for i in (quitar or []) if int(i or 0)]
        for c in cambiar_cantidad or []:
            if int(c.get("cantidad") or 0) <= 0 and c.get("line_id"):
                ids_quitar.append(int(c["line_id"]))
        if ids_quitar:
            odoo.execute_kw("sale.order.line", "unlink", [ids_quitar])
            hechos.append(f"quitadas {len(ids_quitar)} linea(s)")

        # 2) Cambiar cantidades (capadas a la existencia real de la variante).
        for c in cambiar_cantidad or []:
            lid, cant = int(c.get("line_id") or 0), int(c.get("cantidad") or 0)
            if not lid or cant <= 0 or lid in ids_quitar:
                continue
            linea = odoo.search_read(
                "sale.order.line", [["id", "=", lid]], fields=["product_id"], limit=1
            )
            if not linea:
                continue
            var_id = linea[0]["product_id"][0]
            disp = odoo.search_read(
                "product.product", [["id", "=", var_id]], fields=["free_qty"], limit=1
            )
            libre = int((disp[0].get("free_qty") if disp else 0) or 0)
            surtir = min(cant, max(libre, 0))
            if surtir <= 0:
                faltantes.append({"line_id": lid, "pedido": cant, "disponible": libre})
                continue
            if surtir < cant:
                faltantes.append({"line_id": lid, "pedido": cant, "disponible": surtir})
            odoo.execute_kw("sale.order.line", "write", [[lid], {"product_uom_qty": surtir}])
            hechos.append(f"linea {lid} a {surtir} pz")

        # 3) Agregar lineas nuevas, con la misma validacion por variante que crear_cotizacion.
        if agregar:
            nuevas, falt_nuevas = _resolver_variantes(agregar)
            faltantes.extend(falt_nuevas)
            for _, _, vals in nuevas:
                odoo.create(
                    "sale.order.line",
                    {
                        "order_id": oid,
                        "product_id": vals["product_id"],
                        "product_uom_qty": vals["product_uom_qty"],
                    },
                )
            if nuevas:
                hechos.append(f"agregadas {len(nuevas)} linea(s)")

        # 4) Recalcular envio con la regla de envio gratis sobre el subtotal NUEVO.
        envio = _recalcular_envio(oid, costo_envio)
    except OdooError as exc:
        return (
            f"ERROR_ODOO: {exc}. La cotizacion {nombre} pudo quedar a medias; NO le confirmes el "
            "cambio al cliente y escala con escalar_a_benny."
        )

    actualizada = _leer_orden(oid)
    datos = _payload_orden(actualizada) if actualizada else {"folio": nombre}
    pdf_url = _pdf_cotizacion_url(oid)
    # La foto vieja ya no refleja el pedido: se genera de nuevo.
    imagen_url = _imagen_cotizacion_url(oid, pdf_url, nombre)

    sid = current_subscriber_id.get()
    imagen_enviada = False
    if imagen_url and sid:
        imagen_enviada = bool(enviar_mensaje(sid, "", imagen_url=imagen_url).get("ok"))

    ficha_mod.set_cotizacion(
        folio=datos.get("folio") or nombre,
        order_id=oid,
        total=datos.get("total"),
        estado=datos.get("estado") or "draft",
        pdf_url=pdf_url,
        imagen_url=imagen_url,
    )

    # Espejo a Telegram: el agente acaba de escribir en una orden real de Odoo, Benny tiene que
    # poder verlo sin entrar al sistema.
    detalle_envio = (
        "gratis" if envio.get("envio_gratis") else f"${envio.get('costo_envio_aplicado')}"
    )
    enviar_telegram(
        "✏️ Cotizacion modificada por el agente\n\n"
        f"Cliente {sid or 'sin identificar'}\n"
        f"Folio {datos.get('folio') or nombre} (order_id {oid})\n"
        f"Cambios: {'; '.join(hechos) or 'sin cambios efectivos'}\n"
        f"Nuevo total: ${datos.get('total')}\n"
        f"Envio: {detalle_envio}"
    )

    datos.update(
        {
            "modificada": True,
            "cambios_aplicados": hechos,
            "faltantes": faltantes,
            "pdf_url": pdf_url,
            "imagen_cotizacion_url": imagen_url,
            "imagen_enviada": imagen_enviada,
            **envio,
            "_instruccion": (
                "Usa EXACTAMENTE el folio y el total nuevos que vienen aqui. Si 'faltantes' no "
                "esta vacio, dile al cliente con claridad que de esas tallas/colores solo se pudo "
                "completar lo disponible y ofrece alternativas (nunca solo 'no hay'). Si "
                "imagen_enviada=true, ya le llego la cotizacion actualizada como foto: "
                "confirmaselo. Si envio_gratis=true, avisale que su envio quedo GRATIS."
            ),
        }
    )
    return json.dumps(datos, ensure_ascii=False)


@beta_tool
def enviar_fotos_producto(template_ids: list[int]) -> str:
    """Envia al cliente las FOTOS de los productos indicados, por WhatsApp (una imagen por producto).

    Usala cuando el cliente pide ver un modelo o cuando le presentas opciones: manda las fotos
    de los productos que le estas mostrando para que las vea. Pasa los template_id (los devuelve
    buscar_catalogo) de SOLO los productos relevantes (no todos), maximo 5. Despues de enviarlas,
    en tu texto describe brevemente cada modelo. No repitas el envio de la misma foto en el turno.

    Args:
        template_ids: Lista de template_id de product.template cuyas fotos enviar (max 5).
    """
    sid = current_subscriber_id.get()
    if not sid:
        return "NO_ENVIADO: sin destinatario en contexto (no se pudo mandar la foto)."
    if not template_ids:
        return "NO_ENVIADO: no diste template_ids."
    enviadas, fallidas = [], []
    for tid in template_ids[:5]:
        res = enviar_mensaje(sid, "", imagen_url=_imagen_url(int(tid)))
        (enviadas if res.get("ok") else fallidas).append(tid)

    import json

    return json.dumps(
        {"fotos_enviadas": enviadas, "fallidas": fallidas},
        ensure_ascii=False,
    )
