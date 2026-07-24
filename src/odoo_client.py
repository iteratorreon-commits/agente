"""Cliente JSON-RPC minimo para Odoo 17.

Replica el patron probado en .claude/skills/reporte-vendedores/genera-datos.ps1:
POST a /jsonrpc, service="object", method="execute_kw",
args=(DB, UID, API_KEY, Modelo, Metodo, args_posicionales, kwargs).
Autenticacion SIEMPRE con API key (el login por password via RPC esta bloqueado).
"""
from __future__ import annotations

from typing import Any

import httpx

from .config import cfg


class OdooError(RuntimeError):
    """Error devuelto por Odoo o de transporte."""


class OdooClient:
    def __init__(self) -> None:
        self.url = f"{cfg.odoo_url.rstrip('/')}/jsonrpc"
        self.db = cfg.odoo_db
        self.uid = cfg.odoo_uid
        self.key = cfg.odoo_api_key
        self._client = httpx.Client(timeout=30.0)

    def execute_kw(
        self,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """Llama a un metodo del ORM de Odoo. Devuelve el resultado o lanza OdooError."""
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "object",
                "method": "execute_kw",
                "args": [
                    self.db,
                    self.uid,
                    self.key,
                    model,
                    method,
                    args or [],
                    kwargs or {},
                ],
            },
            "id": 1,
        }
        try:
            resp = self._client.post(self.url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:  # transporte
            raise OdooError(f"Fallo de red al llamar Odoo: {exc}") from exc

        if "error" in data:
            msg = (
                data["error"].get("data", {}).get("message")
                or data["error"].get("message")
                or str(data["error"])
            )
            raise OdooError(f"Odoo devolvio error en {model}.{method}: {msg}")
        return data.get("result")

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"fields": fields, "limit": limit}
        if order:
            kwargs["order"] = order
        return self.execute_kw(model, "search_read", [domain], kwargs) or []

    def create(self, model: str, values: dict[str, Any]) -> int:
        return self.execute_kw(model, "create", [values])


# Instancia unica reutilizable.
odoo = OdooClient()
