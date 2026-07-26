# -*- coding: utf-8 -*-
"""Prueba de regresion del calculo de precios (src/precios.py).

Que verifica y por que: precios.py replica la formula de listas de precios de Odoo 17 para
poder decir un precio SIN crear una cotizacion. Si esa replica se desalinea de Odoo, el
agente le anunciaria al cliente un precio y la cotizacion le saldria con otro. Esta prueba
lo detecta comparando contra la unica verdad disponible: el price_unit que Odoo mismo
escribio en las lineas de ordenes REALES.

Solo lee de Odoo. No crea, no modifica y no envia nada.

Uso:  .venv\\Scripts\\python -m tests.pruebas_precios
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import precios  # noqa: E402
from src.config import cfg  # noqa: E402
from src.odoo_client import odoo  # noqa: E402

# Casos verificados a mano el 2026-07-26 contra ordenes reales (variante: precio). Son el
# ancla fija: aunque no hubiera ordenes recientes, estos tienen que seguir dando igual.
#
# Los tres ultimos (costo 75 -> 105) son los que de verdad muerden: caen JUSTO en la mitad
# del redondeo a 10. Odoo redondea HALF-UP y da $110; el round() de Python redondea al par y
# daria $100. Sin ellos, romper el redondeo solo bajaba la coincidencia general a 96% y la
# prueba pasaba igual, que es exactamente el falso OK que hay que evitar.
ANCLAS = {
    44477: 70.0, 44963: 90.0, 44849: 80.0, 44850: 110.0, 45047: 310.0,
    36555: 30.0,   # accesorio patrio: usa la regla de categoria (x1.6), no la global
    44857: 110.0, 44595: 110.0, 44596: 110.0,   # mitad exacta del redondeo
}

ORDENES_A_REVISAR = 20

# Coincidencia minima contra ordenes reales. No es 100% porque las ordenes viejas conservan
# el precio del costo que el producto tenia ese dia (ver _prueba_contra_ordenes). Un error
# de formula tumba este porcentaje muy por debajo del umbral, no lo roza.
MINIMO_COINCIDENCIA = 95.0
MINIMO_LINEAS = 30


def _prueba_redondeo() -> tuple[int, int]:
    """El redondeo de Odoo es HALF-UP: los empates suben SIEMPRE.

    El round() de Python redondea al par (105 -> 100, 125 -> 120) y produciria precios 10
    pesos por debajo de la cotizacion en todos los modelos cuyo costo cae en la mitad.
    """
    print("\n== Redondeo HALF-UP (empates hacia arriba) ==")
    casos = [(105, 10, 110), (115, 10, 120), (125, 10, 130), (104.9, 10, 100), (32, 10, 30)]
    ok = 0
    for valor, paso, esperado in casos:
        obtenido = precios._float_round(valor, paso)
        bien = abs(obtenido - esperado) < 0.01
        ok += bien
        print(f"  [{'OK ' if bien else 'FALLA'}] {valor} -> {obtenido} (esperado {esperado})")
    return ok, len(casos)


def _prueba_anclas() -> tuple[int, int]:
    print("\n== Anclas verificadas a mano ==")
    calculados = precios.precios_por_variante(variant_ids=list(ANCLAS))
    ok = 0
    for vid, esperado in ANCLAS.items():
        obtenido = calculados.get(vid)
        bien = obtenido is not None and abs(obtenido - esperado) < 0.01
        ok += bien
        print(f"  [{'OK ' if bien else 'FALLA'}] variante {vid}: calculado={obtenido} esperado={esperado}")
    return ok, len(ANCLAS)


def _prueba_contra_ordenes() -> tuple[int, int]:
    """Compara el precio calculado contra el price_unit real de ordenes ya existentes.

    Se descartan las lineas que no sirven como referencia: is_delivery (es el envio, su
    precio lo pone envia.com), discount != 0 (descuento manual) y price_unit == 0.

    Por que se exige un PORCENTAJE y no un 100%: Odoo congela el price_unit al crear la
    linea, asi que una orden vieja conserva el precio del costo que tenia el producto ese
    dia. Cuando a un producto le cambian el costo, su linea vieja deja de cuadrar aunque la
    formula sea correcta (verificado con S04484: a sus dos productos les cambiaron el costo
    el 2026-07-26, cinco dias despues de la orden). Ese desfase afecta a unas cuantas
    lineas; en cambio un error en la formula -redondeo, orden de operaciones, regla mal
    elegida- desploma la coincidencia de golpe, que es justo lo que este umbral detecta.
    """
    lista = cfg.odoo_pricelist_id
    print(f"\n== Contra ordenes reales de la lista de precios {lista} ==")
    ordenes = odoo.search_read(
        "sale.order",
        [["pricelist_id", "=", lista]],
        fields=["id", "name"],
        limit=ORDENES_A_REVISAR,
        order="id desc",
    )
    if not ordenes:
        print("  (sin ordenes con esa lista de precios; solo aplican las anclas)")
        return 0, 0

    lineas = odoo.search_read(
        "sale.order.line",
        [["order_id", "in", [o["id"] for o in ordenes]], ["is_delivery", "=", False]],
        fields=["id", "order_id", "product_id", "price_unit", "discount", "create_date"],
        limit=1000,
    )
    utiles = [
        l for l in lineas
        if not l.get("discount") and (l.get("price_unit") or 0) > 0 and l.get("product_id")
    ]
    if not utiles:
        print("  (ninguna linea utilizable como referencia)")
        return 0, 0

    var_ids = sorted({l["product_id"][0] for l in utiles})
    calculados = precios.precios_por_variante(variant_ids=var_ids)
    tocado = {
        v["id"]: str(v.get("write_date") or "")
        for v in odoo.search_read(
            "product.product", [["id", "in", var_ids]], fields=["id", "write_date"], limit=3000
        )
    }
    folio = {o["id"]: o["name"] for o in ordenes}

    ok, difieren = 0, []
    for l in utiles:
        vid = l["product_id"][0]
        real = float(l["price_unit"])
        calc = calculados.get(vid)
        if calc is not None and abs(calc - real) < 0.01:
            ok += 1
            continue
        motivo = (
            "al producto le cambiaron el costo despues"
            if tocado.get(vid, "") > str(l.get("create_date") or "")
            else "precio puesto a mano o regla distinta"
        )
        difieren.append((folio.get(l["order_id"][0], "?"), l["product_id"][1], real, calc, motivo))

    pct = 100.0 * ok / len(utiles)
    print(f"  {ok}/{len(utiles)} lineas coinciden al centavo ({pct:.1f}%)")
    for f, nombre, real, calc, motivo in difieren[:10]:
        print(f"    difiere: {f} {nombre}: Odoo={real} calculado={calc} -> {motivo}")
    if len(difieren) > 10:
        print(f"    ... y {len(difieren) - 10} mas")
    return ok, len(utiles)


def main() -> int:
    print("=" * 72)
    print("PRUEBA DE PRECIOS - solo lecturas contra Odoo, no crea ni modifica nada")
    print("=" * 72)
    precios.limpiar_cache()

    ok_r, total_r = _prueba_redondeo()
    ok_a, total_a = _prueba_anclas()
    ok_o, total_o = _prueba_contra_ordenes()

    print("\n" + "=" * 72)
    fallo = False

    if ok_r != total_r:
        print(f"FALLA: el redondeo no es HALF-UP ({total_r - ok_r} casos mal).")
        fallo = True

    if ok_a != total_a:
        print(f"FALLA: {total_a - ok_a} de las {total_a} anclas ya no dan el precio verificado.")
        fallo = True

    if total_o < MINIMO_LINEAS:
        print(f"ATENCION: solo {total_o} lineas reales para comparar (minimo {MINIMO_LINEAS}).")
        fallo = True
    else:
        pct = 100.0 * ok_o / total_o
        if pct < MINIMO_COINCIDENCIA:
            print(f"FALLA: solo {pct:.1f}% coincide con Odoo (minimo {MINIMO_COINCIDENCIA}%).")
            fallo = True
        else:
            print(f"OK: {pct:.1f}% de las lineas reales coincide (minimo {MINIMO_COINCIDENCIA}%).")

    if fallo:
        print("Revisa si cambiaron las reglas de la lista de precios en Odoo.")
        return 1
    print("El precio que anuncia el agente es el mismo que saldra en la cotizacion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
