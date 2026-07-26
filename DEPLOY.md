# Deploy del agente vendedor en Render

Objetivo: dejar el agente con una **URL HTTPS estable, siempre encendida y con disco
persistente**, para olvidarnos del túnel cloudflared. Arranca en **modo sombra** (procesa
y registra, pero NO le contesta al cliente) para validar sin exponer errores.

Resumen de piezas:
- `Dockerfile` — imagen (FastAPI + uvicorn, 1 worker).
- `.dockerignore` — evita hornear `.env`, `.venv/`, `*.db` en la imagen.
- `render.yaml` — Blueprint: web service Docker + disco `/data` + health check `/health` + 1 instancia.
- Modo sombra: variable `AGENTE_SHADOW_MODE` (arranca en `1`).

---

## Paso 1 — Repo git (solo esta carpeta, NO todo el workspace)

Desde `AGENTE-VENDEDOR-WA/` en PowerShell:

```powershell
cd "C:\Users\Benny\OneDrive\Imágenes\Documentos\claude\AGENTE-VENDEDOR-WA"
git init
git add .
git status   # CONFIRMA que .env NO aparece en la lista (debe estar ignorado)
git commit -m "Agente vendedor WA: listo para Render (modo sombra)"
```

> ⚠️ Si en `git status` aparece `.env`, DETENTE y avísame: no se debe subir. Debe estar
> cubierto por `.gitignore`.

Crea un repo **privado** en GitHub y súbelo:

```powershell
git branch -M main
git remote add origin https://github.com/<tu-usuario>/agente-vendedor-wa.git
git push -u origin main
```

(Si tienes el CLI `gh`: `gh repo create agente-vendedor-wa --private --source . --push`.)

## Paso 2 — Crear el servicio en Render

1. Entra a https://render.com → **New +** → **Blueprint**.
2. Conecta tu cuenta de GitHub y elige el repo `agente-vendedor-wa`.
3. Render detecta `render.yaml`. Revisa que diga plan **Starter**, disco **datos → /data**,
   health check **/health**. Aplica.

## Paso 3 — Cargar las variables secretas (Environment)

En el servicio → **Environment**, agrega las variables marcadas `sync: false`. Cópialas
**tal cual** de tu `.env` local. Checklist:

| Variable | Nota |
|---|---|
| `ANTHROPIC_API_KEY` | **Recargar saldo primero** (se quedó sin crédito). |
| `ODOO_URL` | ej. `https://itera6.odoo.com` |
| `ODOO_DB` | base de Odoo (`itera6`) |
| `ODOO_UID` | `91` |
| `ODOO_API_KEY` | secreto |
| `ODOO_WAREHOUSE_ID` | `6` (Acuña) |
| `ODOO_SALES_TEAM_ID` | el que uses (o vacío) |
| `ODOO_PRICELIST_ID` | `11` (Preventa) |
| `ENVIA_API_TOKEN` | token de **producción** |
| `ENVIA_BASE_URL` | **`https://api.envia.com`** (¡NO el `api-test`!) |
| `ENVIA_ORIGEN_CP` | `27000` |
| `ENVIA_ORIGEN_ESTADO` | `CO` |
| `ENVIA_ORIGEN_CIUDAD` | `Torreon` |
| `ENVIA_CARRIERS` | `estafeta,fedex,dhl,paquetexpress` |
| `MANYCHAT_API_TOKEN` | secreto |
| `WEBHOOK_SECRET` | el mismo que pondrás en el header de ManyChat |
| `BENNY_SUBSCRIBER_ID` | `730244317` |
| `NOTIFY_SUBSCRIBER_IDS` | ids de finanzas (coma-separados); vacío = solo Benny |
| `OPENAI_API_KEY` | para transcribir audios |

Ya vienen puestas por el Blueprint (no las toques salvo que quieras): `DB_PATH=/data/agente.db`,
`DECISION_LOG_PATH=/data/logs/decisiones.jsonl`, `AGENTE_SHADOW_MODE=1`, `AGENTE_MODEL`, `AGENTE_EFFORT`.

## Paso 4 — Primer deploy y salud

- Render construye y despliega. Cuando quede *Live*, prueba:
  `https://agente-vendedor-wa.onrender.com/health` → `{"status":"ok"}`
- La URL estable base es `https://agente-vendedor-wa.onrender.com` (no cambia entre deploys).

## Paso 5 — Apuntar ManyChat a la URL nueva

En ManyChat (cuenta "Itera mayoreo") → flujo del **WhatsApp Default Reply** → la **Solicitud
externa**:
- URL: `POST https://agente-vendedor-wa.onrender.com/manychat/inbound`
- Header: `x-itera-token` = valor de `WEBHOOK_SECRET`
- Body: `{"subscriber_id":"{{Id de contacto}}","text":"{{Última entrada de texto}}"}`
- SIN "Respuesta del contacto" (User Input) y SIN Send Message (el server entrega directo).

## Paso 6 — Validar en modo sombra

Con `AGENTE_SHADOW_MODE=1`, el cliente **no recibe nada**; el agente sí procesa. Revisa qué
**hubiera** contestado:
- Render → tu servicio → pestaña **Logs**: cada turno imprime una línea `DECISION {...}`
  con `respuesta`, `tools_invocadas` y `entrega: "sombra (no enviado al cliente)"`.
- Las **escalaciones y avisos de pago SÍ te llegan** a tu WhatsApp (los internos no se suprimen).
- Tip: si tú mismo escribes desde tu número (`BENNY_SUBSCRIBER_ID`), a ti SÍ te responde
  (los internos no se suprimen) → puedes probar interactivo sin exponer a clientes.

## Paso 7 — Salir de sombra (cuando estés conforme)

Cambia `AGENTE_SHADOW_MODE` a `0` en Environment → **Save** (redeploy automático). A partir
de ahí el agente ya le responde a los clientes reales.

## Paso 8 — Canal interno por Telegram (escalaciones y avisos de pago)

Reemplaza a WhatsApp para lo interno. **Por qué:** WhatsApp/Meta solo deja enviar dentro de
la ventana de 24 h desde el último mensaje del destinatario, así que una escalación de
madrugada se perdía en silencio justo cuando más se necesitaba. Telegram no tiene esa
ventana, y además separa el canal interno del de clientes.

Orden (importa, porque el bot te dice tu `chat_id` pero necesita el webhook vivo):

1. En Telegram, habla con **@BotFather** → `/newbot` → nombre → usuario. Copia el **token**.
2. En Render → Environment, pon `TELEGRAM_BOT_TOKEN` y `TELEGRAM_WEBHOOK_SECRET`
   (cualquier cadena larga que inventes). **Save** → redeploy.
3. Registra el webhook abriendo esta URL en el navegador (sustituye TOKEN y SECRET):
   ```
   https://api.telegram.org/botTOKEN/setWebhook?url=https://agente-vendedor-wa.onrender.com/telegram/inbound&secret_token=SECRET
   ```
   Debe contestar `{"ok":true,...}`.
4. Escríbele **`/start`** a tu bot. Te responde con tu `chat_id`.
5. Pon ese número en `TELEGRAM_CHAT_ID` (y los de finanzas en `TELEGRAM_NOTIFY_CHAT_IDS`,
   separados por coma). **Save** → redeploy.

Listo: las escalaciones y los avisos de pago llegan a Telegram, y ahí mismo tienes dos comandos.

### Los dos comandos del canal interno

**`APRENDE: <la regla>`** — la guarda como conocimiento permanente y la aplica en todas las
conversaciones.

**`CLIENTE <subscriber_id>: <instrucción>`** — le da una orden al agente sobre la conversación
de un cliente concreto. La respuesta se le envía **al cliente**, no a ti, y el agente usa sus
herramientas para cumplirla:

```
CLIENTE 1491137321: mándale otra vez su cotización
CLIENTE 1491137321: recuérdale qué lleva y pregunta si avanzamos
CLIENTE 1491137321: mándale la foto de la Camisa Guayabera
```

El `subscriber_id` viene en cada escalación que te llega. `#1491137321 ...` es un atajo
equivalente. Al terminar te confirma por Telegram **qué le envió** y con qué tools.

**Límite de la ventana de 24 h.** WhatsApp solo deja enviar libre dentro de las 24 h desde el
último mensaje del cliente. Si ya pasaron, el agente **no lo intenta** y te avisa por Telegram
para que le escribas tú desde ManyChat; en cuanto el cliente conteste, la ventana se reabre y
el agente vuelve a poder atenderlo. (Para reactivar fuera de ventana Meta exige una plantilla
aprobada, y además tiene que llevar **botón de respuesta**: sin él la ventana no se reabre y los
mensajes siguientes tampoco se entregan. Eso no está implementado.)

**Un mensaje suelto NO corre el agente**, a propósito: solo lo corre la forma explícita
`CLIENTE <id>: ...`. Así un mensaje tuyo nunca se confunde con una conversación de venta —que
es justo lo que pasaba en WhatsApp, donde un "CONTESTA AL CLIENTE" tuyo disparó
`notificar_pago_multiple`.

**Si falta el token, no se pierde nada:** el código detecta que Telegram no está configurado
y manda las escalaciones por WhatsApp como antes (las órdenes sí necesitan Telegram).

---

### Notas / límites conocidos
- **1 sola instancia** (SQLite no se comparte). Con ~100 conversaciones/día sobra.
- **Memoria de conversación.** La transcripción completa vive en la tabla `mensajes` de
  `/data/agente.db` (append-only: el código nunca borra de ahí) y el estado estructurado en la
  columna `conversations.ficha`. `AGENTE_MAX_MENSAJES` (default 40) solo limita cuántos mensajes
  se le reenvían al modelo, no cuántos se guardan. Antes se guardaban 20 entradas y se recortaba
  **al escribir**, así que el hilo viejo se borraba del disco y el agente volvía a preguntar
  tallas ya confirmadas.
- **La ficha va en `messages`, nunca en el `system`.** El bloque system es el prefijo cacheado y
  es idéntico para todos los clientes; meter datos por cliente ahí invalidaría el prompt caching
  en cada turno. Si `cache_read` cae a 0, revisar eso primero.
- **Límites de ManyChat que no se pueden arreglar en código:** el *caption* de las fotos y el
  *mensaje citado* (cuando el cliente responde citando un mensaje anterior) **no llegan**.
  ManyChat solo expone "Última entrada de texto", y con una foto mete ahí la URL y descarta el
  texto — confirmado por ManyChat en su comunidad. La API de WhatsApp Cloud sí los manda
  (`image.caption`, `context.id`), pero ManyChat está en medio. Mitigado con el historial íntegro
  y la ficha: el agente conserva los modelos que ya mostró (con su `template_id`) y resuelve
  "de este quiero 6", o pregunta una sola cosa concreta.
- El procesamiento es en segundo plano (BackgroundTasks). Si el proceso reinicia justo en
  medio de un turno, ese turno se pierde (a este volumen, riesgo bajo). Si crece mucho,
  migrar a una cola durable + Postgres.
- El gasto grande NO es Render (~$8/mo): son los tokens del modelo. Desde la migración a
  Sonnet 5 con prompt caching el costo por turno baja ~70% respecto a Opus 4.8. El precio
  introductorio de Sonnet 5 ($2/$10 por millón) termina el 31-ago-2026 y sube a $3/$15.
- El prompt caching cachea las 12 tools + el system prompt en un solo
  breakpoint. Se invalida cuando Benny manda un `APRENDE:` y se vuelve a calentar solo.
  Si `cache_read` sale 0 turno tras turno en la bitácora, algo rompió el prefijo.
- Las escalaciones y avisos de pago van por **Telegram** (ver Paso 8). Si el token falta,
  caen a WhatsApp, donde están sujetas a la ventana de 24 h de Meta y pueden perderse.
