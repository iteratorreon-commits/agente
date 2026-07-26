"""Ficha de la conversacion: el estado ESTRUCTURADO de cada cliente.

Es lo que la vieja columna `slots` prometia y nunca llego a ser (se cargaba, se pasaba y se
guardaba sin que nada la escribiera). Aqui viven los datos que el agente NO puede permitirse
reconstruir de memoria: el folio y el total de la cotizacion vigente, los modelos que ya le
mostro al cliente con sus template_id, el CP y costo de envio, y el pedido que va dictando.

Por que hace falta, con el caso real: el 25-jul el agente le recito a Benny la cotizacion
"S04541 por $10,300" con `tools_invocadas: []`. Ese folio NO EXISTE en Odoo (la orden real era
S04552 por $2,800) y encima le mezclo un producto de la orden de otro cliente. Lo saco todo de
su propia prosa de turnos anteriores, porque los resultados de las tools se tiraban al terminar
cada turno. La ficha corta eso: los datos duros se escriben aqui de forma DETERMINISTA desde las
tools (no depende de que el modelo se acuerde de anotarlos) y se le reinyectan cada turno.

Dos vias de escritura, las dos necesarias:
- Determinista, desde las tools: crear_cotizacion -> cotizacion_vigente, buscar_catalogo ->
  modelos_mostrados, cotizar_envio -> envio.
- Del modelo, con la tool anotar_pedido: solo el modelo sabe que fue dictando el cliente a lo
  largo de varios mensajes sueltos.

DONDE se inyecta (importante): al final de `messages`, NO en el system. El bloque system es el
prefijo cacheado (~10,552 tokens) y es identico para todos los clientes; meter datos por cliente
ahi romperia el prompt caching que baja el turno a ~$0.019.
"""
from __future__ import annotations

import json

from . import session_store
from .request_context import current_subscriber_id

# Topes para que la ficha no crezca sin limite (se reenvia cada turno sin cachear).
MAX_MODELOS = 12
MAX_LINEAS_PEDIDO = 40
MAX_PREGUNTADO = 12


def _sid(subscriber_id: str = "") -> str:
    """Destinatario del turno en curso. Las tools no reciben el subscriber_id como argumento,
    llega por el ContextVar que setea _procesar."""
    return subscriber_id or current_subscriber_id.get() or ""


# --------------------------------------------------------------------------------------
# Escrituras deterministas (las llaman las tools)
# --------------------------------------------------------------------------------------


def set_cotizacion(
    folio: str,
    order_id: int,
    total: float | None,
    estado: str = "draft",
    pdf_url: str = "",
    imagen_url: str = "",
    subscriber_id: str = "",
) -> None:
    """Registra la cotizacion vigente del cliente. La escribe crear_cotizacion/modificar_cotizacion."""
    sid = _sid(subscriber_id)
    if not sid:
        return

    def cambio(f: dict) -> dict:
        f["cotizacion_vigente"] = {
            "folio": folio,
            "order_id": int(order_id or 0),
            "total": total,
            "estado": estado,
            "pdf_url": pdf_url,
            "imagen_url": imagen_url,
        }
        return f

    session_store.actualizar_ficha(sid, cambio)


def agregar_modelos(productos: list[dict], subscriber_id: str = "") -> None:
    """Recuerda los modelos que se le mostraron al cliente (con su template_id).

    Es lo que permite resolver despues un "de este quiero 6" o una respuesta a un mensaje
    citado: WhatsApp/ManyChat no nos dicen a que mensaje respondio el cliente, pero si sabemos
    que modelos estuvieron sobre la mesa.
    """
    sid = _sid(subscriber_id)
    if not sid or not productos:
        return

    def cambio(f: dict) -> dict:
        actuales = f.get("modelos_mostrados") or []
        por_id = {int(m["template_id"]): m for m in actuales if m.get("template_id")}
        for p in productos:
            tid = int(p.get("template_id") or 0)
            if not tid:
                continue
            # FUSIONA, no reemplaza: consultar_stock solo aporta el nombre, y no debe borrar las
            # tallas y colores que buscar_catalogo ya habia guardado para ese modelo.
            previo = por_id.get(tid, {})
            por_id[tid] = {
                "template_id": tid,
                "nombre": p.get("nombre") or previo.get("nombre") or "",
                "tallas": p.get("tallas_disponibles") or p.get("tallas") or previo.get("tallas") or [],
                "colores": (
                    p.get("colores_disponibles") or p.get("colores") or previo.get("colores") or []
                ),
                # El precio tambien se guarda: sin esto, al turno siguiente el agente solo
                # podria repetirlo de memoria, que es justo lo que la regla 7 le prohibe.
                "precio_desde": p.get("precio_desde", previo.get("precio_desde")),
                "precio_hasta": p.get("precio_hasta", previo.get("precio_hasta")),
                "precio_por_talla": p.get("precio_por_talla") or previo.get("precio_por_talla") or {},
            }
        # Los mas recientes al final; se conservan los ultimos MAX_MODELOS.
        f["modelos_mostrados"] = list(por_id.values())[-MAX_MODELOS:]
        return f

    session_store.actualizar_ficha(sid, cambio)


def set_envio(
    cp: str = "", ciudad: str = "", costo: float | None = None, subscriber_id: str = ""
) -> None:
    """Guarda el destino y el costo de envio ya cotizado (lo escribe cotizar_envio)."""
    sid = _sid(subscriber_id)
    if not sid:
        return

    def cambio(f: dict) -> dict:
        envio = f.get("envio") or {}
        if cp:
            envio["cp"] = cp
        if ciudad:
            envio["ciudad"] = ciudad
        if costo is not None:
            envio["costo"] = costo
        f["envio"] = envio
        return f

    session_store.actualizar_ficha(sid, cambio)


def set_cliente(nombre: str = "", telefono: str = "", subscriber_id: str = "") -> None:
    """Guarda nombre/telefono ya confirmados, para no volver a pedirlos."""
    sid = _sid(subscriber_id)
    if not sid or not (nombre or telefono):
        return

    def cambio(f: dict) -> dict:
        cli = f.get("cliente") or {}
        if nombre:
            cli["nombre"] = nombre
        if telefono:
            cli["telefono"] = telefono
        f["cliente"] = cli
        return f

    session_store.actualizar_ficha(sid, cambio)


def set_pedido(
    lineas: list[dict] | None = None,
    ya_preguntado: list[str] | None = None,
    subscriber_id: str = "",
) -> dict:
    """Reemplaza el pedido en curso y/o agrega datos ya preguntados. La usa anotar_pedido."""
    sid = _sid(subscriber_id)
    if not sid:
        return {}

    def cambio(f: dict) -> dict:
        if lineas is not None:
            f["pedido_en_curso"] = [
                {
                    "modelo": str(l.get("modelo") or ""),
                    "template_id": int(l.get("template_id") or 0),
                    "talla": str(l.get("talla") or ""),
                    "color": str(l.get("color") or ""),
                    "cantidad": int(l.get("cantidad") or 0),
                }
                for l in lineas[:MAX_LINEAS_PEDIDO]
            ]
        if ya_preguntado:
            previos = f.get("ya_preguntado") or []
            for p in ya_preguntado:
                if p and p not in previos:
                    previos.append(p)
            f["ya_preguntado"] = previos[-MAX_PREGUNTADO:]
        return f

    return session_store.actualizar_ficha(sid, cambio)


# --------------------------------------------------------------------------------------
# Render para el prompt
# --------------------------------------------------------------------------------------


def _monto(valor: float) -> str:
    """$70 en vez de $70.0. El agente copia estos numeros tal cual a WhatsApp."""
    return f"{valor:.2f}".rstrip("0").rstrip(".")


def _texto_precio(modelo: dict) -> str:
    """Precio de un modelo, agrupando las tallas que cuestan lo mismo.

    Se agrupa a proposito: "precio $70 (tallas 2,4,6,8); $90 (tallas 10,12)" dice lo mismo
    que listar talla por talla y ocupa la mitad. La ficha se reenvia CADA turno y no esta
    cacheada, asi que cada token de aqui se paga completo en todos los mensajes.
    """
    por_talla = modelo.get("precio_por_talla") or {}
    if por_talla:
        agrupado: dict[float, list[str]] = {}
        for talla, precio in por_talla.items():
            agrupado.setdefault(float(precio), []).append(str(talla))
        if len(agrupado) == 1:
            return f"precio ${_monto(next(iter(agrupado)))}"
        # Separador " / " y no "; ": el "; " ya separa un modelo del siguiente.
        partes = [
            f"${_monto(precio)} (tallas {','.join(tallas)})"
            for precio, tallas in sorted(agrupado.items())
        ]
        return "precio " + " / ".join(partes)

    desde, hasta = modelo.get("precio_desde"), modelo.get("precio_hasta")
    if desde is None:
        return ""
    if desde == hasta:
        return f"precio ${_monto(desde)}"
    return f"precio desde ${_monto(desde)} hasta ${_monto(hasta)}"


def render(ficha: dict) -> str:
    """Bloque <estado_conversacion> que se antepone al turno del cliente.

    Devuelve "" si la ficha esta vacia, para no gastar tokens en un cliente nuevo.
    """
    if not ficha:
        return ""

    partes: list[str] = []

    cot = ficha.get("cotizacion_vigente") or {}
    if cot.get("folio"):
        partes.append(
            f"Cotizacion vigente: folio {cot['folio']} (order_id {cot.get('order_id')}), "
            f"total ${cot.get('total')}, estado {cot.get('estado')}. "
            "Estos son los UNICOS datos validos de su cotizacion; si necesitas el detalle de "
            "lineas o confirmar el total, usa consultar_cotizacion."
        )

    pedido = ficha.get("pedido_en_curso") or []
    if pedido:
        detalle = "; ".join(
            f"{l.get('modelo')} talla {l.get('talla') or '-'} "
            f"color {l.get('color') or 'sin definir'} x{l.get('cantidad')}"
            for l in pedido
        )
        partes.append(f"Pedido que el cliente ya dicto (NO se lo vuelvas a preguntar): {detalle}")

    modelos = ficha.get("modelos_mostrados") or []
    if modelos:
        detalle = "; ".join(
            f"[template_id {m['template_id']}] {m.get('nombre')} "
            f"tallas {','.join(map(str, m.get('tallas') or [])) or '-'} "
            f"colores {','.join(map(str, m.get('colores') or [])) or '-'}"
            + (f" {_texto_precio(m)}" if _texto_precio(m) else "")
            for m in modelos
        )
        partes.append(
            "Modelos que YA le mostraste, con su precio REAL de mayoreo (usa estos template_id; "
            "si el cliente dice 'este' o responde a un mensaje anterior, el referente esta aqui). "
            "Estos precios son validos para decirselos al cliente sin volver a consultar nada: "
            f"{detalle}"
        )

    envio = ficha.get("envio") or {}
    if envio:
        partes.append(
            f"Envio: CP {envio.get('cp') or '-'}, {envio.get('ciudad') or '-'}, "
            f"costo cotizado ${envio.get('costo')}"
        )

    cli = ficha.get("cliente") or {}
    if cli.get("nombre") or cli.get("telefono"):
        partes.append(
            f"Datos del cliente ya confirmados (no los vuelvas a pedir): "
            f"nombre {cli.get('nombre') or '-'}, telefono {cli.get('telefono') or '-'}"
        )

    preguntado = ficha.get("ya_preguntado") or []
    if preguntado:
        partes.append("Ya le preguntaste (no repitas): " + "; ".join(preguntado))

    if not partes:
        return ""

    return (
        "<estado_conversacion>\n"
        "Resumen de lo que ya se sabe de ESTA conversacion. Es un registro del sistema, no un "
        "mensaje del cliente. Tiene prioridad sobre lo que creas recordar.\n- "
        + "\n- ".join(partes)
        + "\n</estado_conversacion>"
    )


def cargar_y_render(subscriber_id: str) -> str:
    """Atajo: lee la ficha del subscriber y la renderiza."""
    return render(session_store.cargar_ficha(subscriber_id))


def resumen_json(subscriber_id: str) -> str:
    """Ficha completa en JSON (para depuracion y para el canal interno de ordenes)."""
    return json.dumps(session_store.cargar_ficha(subscriber_id), ensure_ascii=False, indent=1)
