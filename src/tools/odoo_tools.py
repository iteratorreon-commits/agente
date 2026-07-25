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
    variantes = odoo.search_read(
        "product.product",
        [["product_tmpl_id", "in", tmpl_ids], ["active", "=", True]],
        fields=["id", "product_tmpl_id", "product_template_attribute_value_ids", "free_qty"],
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
            {"variant_id": v["id"], "free": int(v.get("free_qty") or 0), "talla": talla, "color": color}
        )

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
                {"name": cliente_nombre or "Cliente WhatsApp", "phone": cliente_telefono},
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
