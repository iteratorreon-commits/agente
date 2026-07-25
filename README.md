# Agente vendedor virtual de WhatsApp — ITERA (unidad de negocio nueva)

Vendedor de IA para WhatsApp construido con el **Tool Runner de la Messages API de Anthropic**
(`claude-sonnet-5`). Combina el estilo de los tres mejores vendedores humanos de ITERA
(Luis, Francisco, Miranda), cotiza contra Odoo en vivo, cotiza paquetería con envia.com,
y sabe cuándo escalar a Benny en vez de improvisar.

> **Este proyecto es independiente.** No toca el bot de n8n (`N8N/`) ni reutiliza sus
> credenciales. **Odoo es la única fuente de verdad** de catálogo, precio y stock.

## Arquitectura

```
Cliente WhatsApp → ManyChat (External Request, header x-itera-token)
    → FastAPI /manychat/inbound
        → gate determinista (pago / queja)  [escalation_rules.py]
        → memoria de sesión                  [session_store.py, SQLite]
        → agente (Tool Runner + Sonnet 5)    [agent.py]
             tools: buscar_catalogo, consultar_stock, crear_cotizacion (Odoo)
                    cotizar_envio (envia.com)
                    consultar_playbook (knowledge/)
                    escalar_a_benny, notificar_pago_multiple (ManyChat Send API)
        → bitácora de decisiones             [decision_log.py, JSONL]
    → respuesta a ManyChat
```

Entrada multimodal: **texto**, **imágenes** (visión nativa de Claude — foto de producto,
referencia o comprobante) y **audio** (transcripción, `transcribe.py`).

## Correr localmente

```bash
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
cp .env.example .env      # y rellenar los valores reales

# Harness de conversación por consola (Fase 2, sin WhatsApp):
python -m tests.harness_cli

# Servicio web:
uvicorn src.main:app --reload --port 8000
```

## Checkpoints (acción humana antes de producción)

Ver `docs/checkpoints.md`. En resumen: usuario Odoo dedicado + almacén Acuña + Sales Team;
cuenta/flow ManyChat nuevos + `subscriber_id` de Benny/finanzas; API key de envia.com;
hosting (Railway/Render) + subdominio HTTPS; validar `knowledge/politicas.json` con Benny;
(opcional) API key OpenAI para transcripción de audio.

## Fases

0. Scaffolding + conocimiento minado ✅ (este repo)
1. Tools de Odoo aisladas (usuario dedicado)
2. Núcleo del agente sin canal (harness CLI)
3. envia.com en sandbox
4. Escalación/notificación aisladas
5. Integración end-to-end en staging
6. Piloto controlado
7. Producción
8. Loop de mejora continua (skill `revisar-agente-vendedor`)

## Loop de mejora

Cada turno se registra en `logs/decisiones.jsonl`. La skill `revisar-agente-vendedor`
(en `.claude/skills/`) lee esa bitácora, agrupa escalaciones/errores/dudas y propone
cambios al `playbook.json`/`politicas.json` que **Benny aprueba** antes de aplicarse.
Nada se actualiza solo.
