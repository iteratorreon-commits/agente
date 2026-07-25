"""Carga de configuracion desde variables de entorno (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _split_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _int_env(name: str, default: int = 0) -> int:
    """Lee un entero de env de forma tolerante (vacio, espacios o basura -> default)."""
    raw = (os.getenv(name) or "").strip()
    # Toma solo el prefijo de digitos por si quedo un comentario pegado.
    digits = ""
    for ch in raw:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else default


def _bool_env(name: str, default: bool = False) -> bool:
    """Lee un booleano de env de forma tolerante (1/true/yes/on/si -> True)."""
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "si", "sí", "verdadero"}


@dataclass(frozen=True)
class Config:
    # Anthropic
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    model: str = os.getenv("AGENTE_MODEL", "claude-opus-4-8")
    effort: str = os.getenv("AGENTE_EFFORT", "medium")

    # Modo sombra: el agente procesa y registra su respuesta, pero NO la envia al
    # cliente (los mensajes a Benny/finanzas SI pasan, para no perder escalaciones ni
    # avisos de pago). Sirve para validar en produccion sin exponer errores al cliente.
    shadow_mode: bool = _bool_env("AGENTE_SHADOW_MODE", False)

    # Odoo
    odoo_url: str = os.getenv("ODOO_URL", "https://itera6.odoo.com")
    odoo_db: str = os.getenv("ODOO_DB", "")
    odoo_uid: int = _int_env("ODOO_UID")
    odoo_api_key: str = os.getenv("ODOO_API_KEY", "")
    odoo_warehouse_id: int = _int_env("ODOO_WAREHOUSE_ID")
    odoo_sales_team_id: int = _int_env("ODOO_SALES_TEAM_ID")
    # Lista de precios que cotiza el agente. 11 = "Precio Preventa", 7 = "PRECIO A MAYORISTAS".
    odoo_pricelist_id: int = _int_env("ODOO_PRICELIST_ID", 11)

    # envia.com
    envia_token: str = os.getenv("ENVIA_API_TOKEN", "")
    envia_base_url: str = os.getenv("ENVIA_BASE_URL", "https://api-test.envia.com")
    envia_origen_cp: str = os.getenv("ENVIA_ORIGEN_CP", "")
    envia_origen_estado: str = os.getenv("ENVIA_ORIGEN_ESTADO", "CO")
    envia_origen_ciudad: str = os.getenv("ENVIA_ORIGEN_CIUDAD", "Torreon")
    envia_carriers: list[str] = field(
        default_factory=lambda: _split_ids(
            os.getenv("ENVIA_CARRIERS") or "estafeta,fedex,dhl,paquetexpress"
        )
    )

    # ManyChat
    manychat_token: str = os.getenv("MANYCHAT_API_TOKEN", "")
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
    notify_subscriber_ids: list[str] = field(
        default_factory=lambda: _split_ids(os.getenv("NOTIFY_SUBSCRIBER_IDS"))
    )
    benny_subscriber_id: str = os.getenv("BENNY_SUBSCRIBER_ID", "")

    # OpenAI (transcripcion)
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")

    # Almacenamiento
    db_path: str = os.getenv("DB_PATH", str(BASE_DIR / "agente.db"))
    decision_log_path: str = os.getenv(
        "DECISION_LOG_PATH", str(BASE_DIR / "logs" / "decisiones.jsonl")
    )

    knowledge_dir: Path = BASE_DIR / "knowledge"
    # Conocimiento aprendido en vivo (Benny ensena por WhatsApp con 'APRENDE:'). Se guarda
    # aparte del knowledge del repo porque cambia en runtime -> en produccion va al disco
    # persistente (/data), si no se borraria en cada deploy.
    aprendizajes_path: str = os.getenv(
        "APRENDIZAJES_PATH", str(BASE_DIR / "knowledge" / "aprendizajes.json")
    )


cfg = Config()
