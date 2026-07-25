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

from ..config import cfg
from ..manychat_api import enviar_mensaje
from ..odoo_client import OdooError, odoo
from ..request_context import current_subscriber_id

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


def _norm(v: Any) -> Any:
    """Odoo devuelve False para vacios; lo convierte a '' para texto."""
    return "" if v is False else v


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
    """Busca productos en el catalogo de Odoo por nombre y devuelve precio, codigo y tallas/colores disponibles.

    Usa esta herramienta cuando el cliente pregunta por un producto, modelo o categoria
    (ej. 'blusa campesina', 'vestido Alicia', 'moños tricolor'). Devuelve solo productos
    publicados y a la venta. NO inventes productos ni precios: si esta tool no devuelve
    algo, no existe o no esta disponible, y debes escalar o pedir mas detalle.

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
    # Traer tallas/colores de todos los templates de una vez.
    try:
        atributos = odoo.search_read(
            "product.template.attribute.value",
            [["product_tmpl_id", "in", ids], ["ptav_active", "=", True]],
            fields=["name", "attribute_id", "product_tmpl_id"],
            limit=500,
        )
    except OdooError:
        atributos = []

    tallas_por_tmpl: dict[int, set[str]] = {}
    colores_por_tmpl: dict[int, set[str]] = {}
    for a in atributos:
        tmpl = a["product_tmpl_id"][0] if a.get("product_tmpl_id") else None
        attr_name = (a["attribute_id"][1] if a.get("attribute_id") else "").upper()
        val = _norm(a.get("name"))
        if tmpl is None or not val:
            continue
        if "TALLA" in attr_name:
            tallas_por_tmpl.setdefault(tmpl, set()).add(str(val))
        elif "COLOR" in attr_name:
            colores_por_tmpl.setdefault(tmpl, set()).add(str(val))

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
            }
        )

    if not lineas:
        return (
            f"SIN_RESULTADOS_TRAS_FILTRO: hay productos para '{query}' pero no en talla='{talla}' "
            f"color='{color}'. Ofrece alternativas (nunca solo 'no hay')."
        )

    import json

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
                "Los precios NO vienen en esta busqueda (Odoo los calcula por lista de precios). "
                "El precio real se obtiene al crear la cotizacion con crear_cotizacion. Recuerda: "
                "mayoreo desde 6 piezas."
            ),
        },
        ensure_ascii=False,
    )


@beta_tool
def consultar_stock(template_id: int) -> str:
    """Consulta la existencia real (en vivo) de un producto en Odoo, consolidada por sucursal.

    Usa esta tool para confirmar disponibilidad antes de prometer piezas. Devuelve cantidad
    disponible por sucursal (JZ, MZ, AC/Acuna, MER). El despacho de este canal es desde Acuna,
    pero las rutas de Odoo surten de otras sucursales si hace falta, asi que informa el total.
    NO inventes cantidades ni des numeros crudos de stock al cliente; usa 'disponible/pocas/agotado'.

    Args:
        template_id: El id de product.template (lo devuelve buscar_catalogo).
    """
    try:
        variantes = odoo.search_read(
            "product.product",
            [["product_tmpl_id", "=", template_id], ["active", "=", True]],
            fields=["id", "display_name"],
            limit=200,
        )
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
        },
        ensure_ascii=False,
    )


@beta_tool
def crear_cotizacion(
    cliente_nombre: str,
    cliente_telefono: str,
    lineas: list[dict],
    notas: str = "",
) -> str:
    """Crea una cotizacion (sale.order en borrador) en Odoo para el cliente.

    Usala solo cuando ya confirmaste con el cliente los modelos, tallas y cantidades, y
    validaste stock. La orden se crea en estado BORRADOR (draft), despacho desde Acuna, y
    NO confirma ningun pago. Devuelve el numero de orden y el total. Si algo falla, NO
    inventes un folio: informa el error y escala.

    Args:
        cliente_nombre: Nombre del cliente.
        cliente_telefono: Telefono del cliente (se usa para buscar/crear el contacto).
        lineas: Lista de items, cada uno {"template_id": int, "cantidad": int}.
        notas: Notas internas opcionales para la cotizacion.
    """
    if not lineas:
        return "ERROR_ARGUMENTOS: la cotizacion no tiene lineas. Confirma el pedido primero."
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
                {"name": cliente_nombre or "Cliente WhatsApp", "phone": cliente_telefono},
            )

        # Resolver una variante por template (la primera activa).
        order_lines = []
        for item in lineas:
            tmpl = item.get("template_id")
            cant = int(item.get("cantidad") or 0)
            if not tmpl or cant <= 0:
                continue
            var = odoo.search_read(
                "product.product",
                [["product_tmpl_id", "=", tmpl], ["active", "=", True]],
                fields=["id"],
                limit=1,
            )
            if not var:
                return f"ERROR_PRODUCTO: template {tmpl} sin variante activa; no cotizo a ciegas."
            order_lines.append((0, 0, {"product_id": var[0]["id"], "product_uom_qty": cant}))

        if not order_lines:
            return "ERROR_ARGUMENTOS: ninguna linea valida. Revisa template_id y cantidad."

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

    pdf_url = _pdf_cotizacion_url(order_id)
    imagen_url = _imagen_cotizacion_url(order_id, pdf_url, nombre)

    # Enviar la cotizacion como FOTO al cliente (WhatsApp entrega imagenes, no PDFs).
    imagen_enviada = False
    sid = current_subscriber_id.get()
    if imagen_url and sid:
        imagen_enviada = bool(enviar_mensaje(sid, "", imagen_url=imagen_url).get("ok"))

    import json

    return json.dumps(
        {
            "numero_orden": nombre,
            "order_id": order_id,
            "total": total,
            "pdf_url": pdf_url,
            "imagen_cotizacion_url": imagen_url,
            "imagen_enviada": imagen_enviada,
            "estado": "borrador (no confirmado, no aparta piezas hasta el pago)",
            "_instruccion_cotizacion": (
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
