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

---

### Notas / límites conocidos
- **1 sola instancia** (SQLite no se comparte). Con ~100 conversaciones/día sobra.
- El procesamiento es en segundo plano (BackgroundTasks). Si el proceso reinicia justo en
  medio de un turno, ese turno se pierde (a este volumen, riesgo bajo). Si crece mucho,
  migrar a una cola durable + Postgres.
- El gasto grande NO es Render (~$8/mo): son los tokens del modelo. Desde la migración a
  Sonnet 5 con prompt caching el costo por turno baja ~70% respecto a Opus 4.8. El precio
  introductorio de Sonnet 5 ($2/$10 por millón) termina el 31-ago-2026 y sube a $3/$15.
- El prompt caching cachea las 8 tools + el system prompt (~4k tokens) en un solo
  breakpoint. Se invalida cuando Benny manda un `APRENDE:` y se vuelve a calentar solo.
  Si `cache_read` sale 0 turno tras turno en la bitácora, algo rompió el prefijo.
- El gate de quejas hoy no atrapa frases como "está mal la guía" (solo equivocad/garantía/
  devolución/…): ampliar patrones en `src/escalation_rules.py` si se quiere más cobertura.
