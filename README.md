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
        → transcripción íntegra + ficha      [session_store.py, ficha.py, SQLite]
        → agente (Tool Runner + Sonnet 5)    [agent.py]
             tools: buscar_catalogo, consultar_stock, crear_cotizacion,
                    consultar_cotizacion, modificar_cotizacion,
                    reenviar_cotizacion, enviar_fotos_producto (Odoo)
                    anotar_pedido (ficha)
                    cotizar_envio (envia.com)
                    consultar_playbook (knowledge/)
                    escalar_a_benny, notificar_pago_multiple (Telegram)
        → bitácora de decisiones             [decision_log.py, JSONL]
    → respuesta a ManyChat

Equipo ITERA → Telegram /telegram/inbound
    → "APRENDE: <regla>"              → conocimiento permanente [aprendizajes.py]
    → "CLIENTE <id>: <instrucción>"    → el agente actúa sobre ese cliente
```

Entrada multimodal: **texto**, **imágenes** (visión nativa de Claude — foto de producto,
referencia o comprobante) y **audio** (transcripción, `transcribe.py`).

## Memoria del agente

Dos piezas distintas, a propósito:

- **`mensajes`** — transcripción append-only, una fila por mensaje. El código nunca borra de
  ahí. `AGENTE_MAX_MENSAJES` (default 40) solo limita cuántos se le reenvían al modelo.
- **`conversations.ficha`** — estado estructurado (pedido en curso, cotización vigente con su
  folio y total reales, modelos ya mostrados con su `template_id`, CP y datos del cliente).
  Sobrevive al recorte de la ventana y se le reinyecta cada turno como `<estado_conversacion>`.
  Los datos duros los escriben las tools de forma **determinista**, no dependen de que el
  modelo se acuerde de anotarlos.

Regla: **la ficha va en `messages`, nunca en el `system`.** El system es el prefijo cacheado e
idéntico para todos los clientes; meter datos por cliente ahí rompería el prompt caching.

## Cotizaciones

El agente **lee** de Odoo la cotización que creó (`consultar_cotizacion`), la **reenvía**
(`reenviar_cotizacion`) y puede **ajustarla** (`modificar_cotizacion`) mientras esté en
borrador: valida existencia real por variante, recalcula el total y la regla de envío gratis, le
reenvía la foto actualizada al cliente y espeja el cambio a Telegram. Si la orden ya está
confirmada, se niega y manda escalar. Nunca afirma folio, total ni precio de memoria.

## Correr localmente

```bash
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
cp .env.example .env      # y rellenar los valores reales

# Harness de conversación por consola (sin WhatsApp):
python -m tests.harness_cli

# Pruebas de memoria y cotizaciones (no manda nada real, DB temporal, solo lee Odoo):
python -m tests.pruebas_memoria          # las 8; o un subconjunto: ... 1 3 7

# Pruebas de capacidades con el modelo (la 3 crea un borrador real en Odoo):
python -m tests.pruebas_capacidades

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
