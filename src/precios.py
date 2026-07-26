"""Calculo del precio de venta SIN crear una cotizacion.

Por que existe este modulo: hasta ahora el agente no tenia forma de decir un precio sin
crear un sale.order real en Odoo, porque buscar_catalogo no traia precio y su nota le
ordenaba al modelo ir por crear_cotizacion. Preguntar "cuanto cuesta" costaba una orden
en la base, un PDF renderizado y pedirle antes al cliente nombre, telefono y tallas.

Como se calcula (verificado contra Odoo produccion el 2026-07-26):
- product.template.list_price NO sirve: en esta base esta en 0.0 o 1.0.
- El precio real lo arman las reglas de product.pricelist.item sobre el standard_price
  (costo) de la VARIANTE. Por eso el precio cambia por talla: el costo cambia por variante.
- Lista 11 "Precio Preventa" (la que usa el agente): regla global 2366 con
  compute_price=formula, base=standard_price, price_discount=-40, price_round=10
  -> costo x 1.4 redondeado a 10. Y la regla de categoria 2990 ("2026 / ACCESORIO /
  PATRIO") con price_discount=-60 -> costo x 1.6. min_quantity=0 en ambas, asi que el
  precio NO depende de la cantidad.

Por que se replica la formula en vez de pedirsela a Odoo: en esta instancia las tres vias
"oficiales" estan cerradas por RPC — el campo 'price' con context={'pricelist': n} no
existe en el modelo, product.pricelist._get_products_price es privado (Odoo bloquea los
metodos con guion bajo) y price_compute/get_combination_info no existen. Asi que esto es un
port de product.pricelist.item._compute_price de Odoo 17, con su MISMO orden de
operaciones y su MISMO criterio de que regla gana.

Las reglas se LEEN EN VIVO de Odoo (con cache y TTL): si Benny cambia el margen en Odoo,
el agente lo refleja solo, sin tocar codigo.

El precio siempre se calcula con cfg.odoo_pricelist_id, que es la misma lista con la que
crear_cotizacion arma la orden. Asi nunca hay diferencia entre el precio que el agente
anuncio y el que sale en el PDF.
"""
from __future__ import annotations

import math
import time
from typing import Any

from .config import cfg
from .odoo_client import OdooError, odoo

# Las reglas de precios cambian de Pascuas a Ramos; releerlas en cada busqueda seria un
# RPC de mas por mensaje. 10 minutos es suficiente para que un cambio en Odoo se refleje
# solo dentro de la misma jornada.
TTL_CACHE_SEGUNDOS = 600

_CAMPOS_REGLA = [
    "id", "applied_on", "product_id", "product_tmpl_id", "categ_id", "min_quantity",
    "compute_price", "fixed_price", "percent_price", "base", "price_discount",
    "price_surcharge", "price_round", "price_min_margin", "price_max_margin",
    "date_start", "date_end",
]

# {pricelist_id: (momento, reglas)} y (momento, {categ_id: parent_id}).
_cache_reglas: dict[int, tuple[float, list[dict]]] = {}
_cache_categorias: tuple[float, dict[int, int]] | None = None


def _id_de(valor: Any) -> int:
    """Odoo devuelve los many2one como [id, "nombre"] y los vacios como False."""
    if isinstance(valor, (list, tuple)) and valor:
        return int(valor[0])
    return 0


def _float_round(valor: float, redondeo: float) -> float:
    """Equivalente a odoo.tools.float_round(..., rounding_method='HALF-UP').

    NO se puede usar el round() de Python: usa banker's rounding (redondea al par), asi
    que daria 12.5 -> 12 donde Odoo da 13, y el precio anunciado no cuadraria con el de
    la cotizacion.
    """
    if not redondeo:
        return valor
    normalizado = valor / redondeo
    if normalizado >= 0:
        entero = math.floor(normalizado + 0.5)
    else:
        entero = math.ceil(normalizado - 0.5)
    return entero * redondeo


def _reglas(pricelist_id: int) -> list[dict]:
    """Reglas de la lista de precios, ordenadas como las ordena Odoo (gana la primera).

    El _order de product.pricelist.item es "applied_on, min_quantity desc, categ_id desc,
    id desc": primero las de variante, luego las de producto, luego las de categoria y al
    final la global. Los valores de applied_on empiezan con digito ('0_product_variant',
    '1_product', '2_product_category', '3_global') justamente para que ese orden salga del
    orden alfabetico.
    """
    ahora = time.monotonic()
    guardado = _cache_reglas.get(pricelist_id)
    if guardado and ahora - guardado[0] < TTL_CACHE_SEGUNDOS:
        return guardado[1]

    reglas = odoo.search_read(
        "product.pricelist.item",
        [["pricelist_id", "=", int(pricelist_id)]],
        fields=_CAMPOS_REGLA,
        limit=5000,
    )
    reglas.sort(
        key=lambda r: (
            str(r.get("applied_on") or ""),
            -float(r.get("min_quantity") or 0),
            -_id_de(r.get("categ_id")),
            -int(r.get("id") or 0),
        )
    )
    _cache_reglas[pricelist_id] = (ahora, reglas)
    return reglas


def _padres_de_categoria() -> dict[int, int]:
    """Mapa categoria -> categoria padre, para resolver el 'parent_of' de las reglas.

    Una regla sobre "2026 / ACCESORIO / PATRIO" tambien aplica a sus subcategorias, asi
    que hay que poder subir por el arbol.
    """
    global _cache_categorias
    ahora = time.monotonic()
    if _cache_categorias and ahora - _cache_categorias[0] < TTL_CACHE_SEGUNDOS:
        return _cache_categorias[1]

    filas = odoo.search_read(
        "product.category", [], fields=["id", "parent_id"], limit=5000
    )
    padres = {int(f["id"]): _id_de(f.get("parent_id")) for f in filas}
    _cache_categorias = (ahora, padres)
    return padres


def _categoria_coincide(categ_producto: int, categ_regla: int, padres: dict[int, int]) -> bool:
    """True si la categoria del producto es la de la regla o desciende de ella."""
    visitadas = set()
    actual = categ_producto
    while actual and actual not in visitadas:
        if actual == categ_regla:
            return True
        visitadas.add(actual)
        actual = padres.get(actual, 0)
    return False


def _aplica(regla: dict, variante: dict, cantidad: float, padres: dict[int, int]) -> bool:
    """Port de product.pricelist.item._is_applicable_for para una VARIANTE.

    Ojo con el orden: en Odoo la condicion de categoria y la de producto son ramas
    excluyentes de un if/elif, no filtros acumulativos.
    """
    if float(regla.get("min_quantity") or 0) and cantidad < float(regla["min_quantity"]):
        return False

    categ_regla = _id_de(regla.get("categ_id"))
    if categ_regla:
        return _categoria_coincide(_id_de(variante.get("categ_id")), categ_regla, padres)

    tmpl_regla = _id_de(regla.get("product_tmpl_id"))
    prod_regla = _id_de(regla.get("product_id"))
    if tmpl_regla:
        return _id_de(variante.get("product_tmpl_id")) == tmpl_regla
    if prod_regla:
        return int(variante.get("id") or 0) == prod_regla
    return True


def _vigente(regla: dict, hoy: str) -> bool:
    inicio, fin = regla.get("date_start"), regla.get("date_end")
    if inicio and str(inicio) > hoy:
        return False
    if fin and str(fin) < hoy:
        return False
    return True


def _precio_base(regla: dict, variante: dict) -> float | None:
    """Precio de partida de la formula. None si la regla se apoya en otra lista de precios
    (base='pricelist'), que hoy no se usa y no vale la pena resolver a ciegas."""
    base = regla.get("base") or "list_price"
    if base == "standard_price":
        return float(variante.get("standard_price") or 0)
    if base == "list_price":
        # En una variante, price_compute('list_price') devuelve lst_price (= list_price
        # del template + price_extra de sus atributos).
        return float(variante.get("lst_price") or 0)
    return None


def _calcular(regla: dict, variante: dict) -> float | None:
    """Port de product.pricelist.item._compute_price. El ORDEN importa y es el de Odoo:
    descuento -> redondeo -> recargo -> margenes."""
    modo = regla.get("compute_price")

    if modo == "fixed":
        return float(regla.get("fixed_price") or 0)

    base = _precio_base(regla, variante)
    if base is None:
        return None

    if modo == "percentage":
        return base - (base * (float(regla.get("percent_price") or 0) / 100.0))

    if modo != "formula":
        return None

    tope = base
    precio = base - (base * (float(regla.get("price_discount") or 0) / 100.0))
    redondeo = float(regla.get("price_round") or 0)
    if redondeo:
        precio = _float_round(precio, redondeo)
    recargo = float(regla.get("price_surcharge") or 0)
    if recargo:
        precio += recargo
    margen_min = float(regla.get("price_min_margin") or 0)
    if margen_min:
        precio = max(precio, tope + margen_min)
    margen_max = float(regla.get("price_max_margin") or 0)
    if margen_max:
        precio = min(precio, tope + margen_max)
    return precio


def leer_variantes(
    template_ids: list[int] | tuple[int, ...] = (),
    variant_ids: list[int] | tuple[int, ...] = (),
) -> list[dict]:
    """Lee de Odoo las variantes con lo justo para poder calcular su precio."""
    dominio: list[Any] = [["active", "=", True]]
    if variant_ids:
        dominio.append(["id", "in", [int(i) for i in variant_ids]])
    elif template_ids:
        dominio.append(["product_tmpl_id", "in", [int(i) for i in template_ids]])
    else:
        return []
    return odoo.search_read(
        "product.product",
        dominio,
        fields=["id", "product_tmpl_id", "standard_price", "lst_price", "categ_id"],
        limit=3000,
    )


def precios_de_variantes(
    variantes: list[dict], pricelist_id: int = 0, cantidad: float = 1
) -> dict[int, float | None]:
    """Precio unitario de cada variante ya leida. {variant_id: precio | None}.

    Recibe las variantes ya leidas para que quien ya las tenia (buscar_catalogo,
    consultar_stock) no pague un search_read de mas.

    Devuelve None cuando no se puede afirmar un precio: sin regla aplicable, con un tipo
    de regla no soportado, o con un resultado <= 0. Odoo en ese caso caeria al list_price
    del producto, pero en esta base el list_price esta sin mantener (0.0 / 1.0), asi que
    anunciarlo seria prometerle al cliente un precio inventado. Es una divergencia
    deliberada: quien llama debe degradar a "el precio se le confirma al cotizar".
    """
    if not variantes:
        return {}
    lista = int(pricelist_id or cfg.odoo_pricelist_id or 0)
    if not lista:
        return {int(v["id"]): None for v in variantes}

    try:
        reglas = _reglas(lista)
        padres = _padres_de_categoria() if any(_id_de(r.get("categ_id")) for r in reglas) else {}
    except OdooError:
        return {int(v["id"]): None for v in variantes}

    hoy = time.strftime("%Y-%m-%d %H:%M:%S")
    vigentes = [r for r in reglas if _vigente(r, hoy)]

    precios: dict[int, float | None] = {}
    for v in variantes:
        vid = int(v["id"])
        precio = None
        for regla in vigentes:
            if _aplica(regla, v, cantidad, padres):
                precio = _calcular(regla, v)
                break
        precios[vid] = precio if (precio is not None and precio > 0) else None
    return precios


def precios_por_variante(
    template_ids: list[int] | tuple[int, ...] = (),
    variant_ids: list[int] | tuple[int, ...] = (),
    pricelist_id: int = 0,
    cantidad: float = 1,
) -> dict[int, float | None]:
    """Igual que precios_de_variantes, pero leyendo las variantes de Odoo."""
    try:
        variantes = leer_variantes(template_ids=template_ids, variant_ids=variant_ids)
    except OdooError:
        return {}
    return precios_de_variantes(variantes, pricelist_id=pricelist_id, cantidad=cantidad)


def limpiar_cache() -> None:
    """Olvida las reglas cacheadas (lo usan las pruebas y sirve tras cambiar precios en Odoo)."""
    global _cache_categorias
    _cache_reglas.clear()
    _cache_categorias = None
