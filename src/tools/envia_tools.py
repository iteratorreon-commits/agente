"""Tool de cotizacion de paqueteria con la Shipping API de envia.com.

Endpoint: POST {base}/ship/rate/  (auth: Authorization: Bearer <token>).
Esquema verificado en docs.envia.com:
- state = codigo de 2 letras (CO, NL, VE...). weightUnit/lengthUnit en minuscula (kg/cm).
- shipment.type = "domestic" (string). settings.currency = "MXN".
El origen es el almacen (Manuel Acuna #79, Torreon CP 27000, estado CO).
"""
from __future__ import annotations

import json
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import httpx
from anthropic import beta_tool

from ..config import cfg

# Estados de Mexico -> codigo de 2 letras que usa envia.com (ISO 3166-2:MX).
_ESTADOS = {
    "aguascalientes": "AG", "baja california": "BC", "baja california sur": "BS",
    "campeche": "CM", "chiapas": "CS", "chihuahua": "CH", "coahuila": "CO",
    "colima": "CL", "ciudad de mexico": "CDMX", "cdmx": "CDMX", "distrito federal": "CDMX",
    "durango": "DG", "guanajuato": "GT", "guerrero": "GR", "hidalgo": "HG",
    "jalisco": "JA", "mexico": "MX", "estado de mexico": "MX", "michoacan": "MI",
    "morelos": "MO", "nayarit": "NA", "nuevo leon": "NL", "oaxaca": "OA",
    "puebla": "PU", "queretaro": "QT", "quintana roo": "QR", "san luis potosi": "SL",
    "sinaloa": "SI", "sonora": "SO", "tabasco": "TB", "tamaulipas": "TM",
    "tlaxcala": "TL", "veracruz": "VE", "yucatan": "YU", "zacatecas": "ZA",
}


def _codigo_estado(valor: str) -> str:
    """Convierte un nombre de estado a su codigo de 2 letras. Si ya es codigo, lo respeta."""
    v = (valor or "").strip()
    if 2 <= len(v) <= 4 and v.upper() in {c for c in _ESTADOS.values()}:
        return v.upper()
    norm = "".join(
        c for c in unicodedata.normalize("NFD", v.lower()) if unicodedata.category(c) != "Mn"
    ).strip()
    return _ESTADOS.get(norm, v.upper()[:4])


@beta_tool
def cotizar_envio(
    destino_cp: str,
    destino_estado: str,
    destino_ciudad: str,
    peso_kg: float = 1.0,
    largo_cm: float = 30.0,
    ancho_cm: float = 30.0,
    alto_cm: float = 20.0,
) -> str:
    """Cotiza el costo de envio real con envia.com desde el almacen al destino del cliente.

    Usala cuando ya tienes el codigo postal del cliente y necesitas darle el costo de envio,
    o para saber si el pedido alcanza envio gratis (>$4,000 lo cubre la empresa). NO inventes
    tarifas: si esta tool falla, dile al cliente que en un momento le confirmas el costo y escala.

    Args:
        destino_cp: Codigo postal del destino.
        destino_estado: Estado del destino (nombre o codigo, ej. 'Nayarit' o 'NA').
        destino_ciudad: Ciudad del destino (ej. 'Tepic').
        peso_kg: Peso estimado del paquete en kg (default 1.0).
        largo_cm: Largo del paquete en cm.
        ancho_cm: Ancho del paquete en cm.
        alto_cm: Alto del paquete en cm.
    """
    if not cfg.envia_token:
        return (
            "ERROR_CONFIG: falta ENVIA_API_TOKEN. No puedo cotizar envio; "
            "dile al cliente que en un momento le confirmas el costo y escala a Benny."
        )
    if not cfg.envia_origen_cp:
        return "ERROR_CONFIG: falta ENVIA_ORIGEN_CP (CP del almacen). Escala."

    base_payload = {
        "origin": {
            "name": "ITERA",
            "phone": "0000000000",
            "street": "Manuel Acuna",
            "number": "79",
            "city": cfg.envia_origen_ciudad,
            "state": _codigo_estado(cfg.envia_origen_estado),
            "country": "MX",
            "postalCode": cfg.envia_origen_cp,
        },
        "destination": {
            "name": "Cliente",
            "phone": "0000000000",
            "city": destino_ciudad,
            "state": _codigo_estado(destino_estado),
            "country": "MX",
            "postalCode": destino_cp,
        },
        "packages": [
            {
                "type": "box",
                "content": "Ropa",
                "amount": 1,
                "weight": peso_kg,
                "weightUnit": "KG",
                "lengthUnit": "CM",
                "dimensions": {"length": largo_cm, "width": ancho_cm, "height": alto_cm},
            }
        ],
        "settings": {"currency": "MXN"},
    }
    headers = {
        "Authorization": f"Bearer {cfg.envia_token}",
        "Content-Type": "application/json",
    }
    url = f"{cfg.envia_base_url.rstrip('/')}/ship/rate/"

    # envia.com exige un carrier por request; consultamos las paqueterias de ITERA en paralelo.
    def _rate(carrier: str) -> list[dict]:
        payload = {**base_payload, "shipment": {"type": 1, "carrier": carrier}}
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=40.0)
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        if not isinstance(data, dict) or data.get("meta") == "error":
            return []
        return data.get("data") or []

    opciones: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(cfg.envia_carriers) or 1) as pool:
        for res in pool.map(_rate, cfg.envia_carriers):
            opciones.extend(res)

    if not opciones:
        return (
            f"SIN_TARIFAS: envia.com no devolvio tarifas para CP {destino_cp} en ninguna paqueteria. "
            "Dile al cliente que en un momento le confirmas el costo y escala o confirma el CP."
        )

    resultado = []
    for o in opciones:
        resultado.append(
            {
                "paqueteria": o.get("carrier"),
                "servicio": o.get("service") or o.get("serviceDescription"),
                "precio": o.get("totalPrice") or o.get("total"),
                "dias_estimados": o.get("deliveryEstimate") or o.get("deliveryDate"),
            }
        )
    # Ordenar por precio ascendente (mejor opcion primero).
    resultado.sort(key=lambda x: (x["precio"] is None, x["precio"] or 0))
    return json.dumps({"opciones": resultado[:8], "moneda": "MXN"}, ensure_ascii=False)
