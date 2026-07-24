# Checkpoints — acción humana requerida (viva)

Marcar cada uno al completarlo. El agente NO puede hacer estos pasos por su cuenta.

## 1. Usuario Odoo  ✅ (conectado y probado 2026-07-23)
- Usuario en uso: **uid=91** ("usuario tecnico", login `iteratorreon@gmail.com`),
  API key en `.env`. NOTA: es un usuario técnico existente, no uno acotado solo a
  Ventas. Pendiente decidir si se crea uno dedicado con permisos mínimos, o se deja este.
- `ODOO_WAREHOUSE_ID=6` = almacén **ACUÑA** (lot_stock loc 46). ✅
- `ODOO_PRICELIST_ID=11` = **"Precio Preventa"** — CONFIRMADO por Benny 2026-07-23 como la
  lista por defecto del agente. (Los precios NO están en `list_price` —salen 0/1—; Odoo los
  calcula por lista de precios. Verificado: BLUSA CAMPESINA ENCAJE 6 pz = $480 → $80/pza.)
- Probado end-to-end: auth, buscar_catalogo (tallas/colores reales), consultar_stock
  (730 pz en Acuña), crear_cotizacion (S04519, borrada). Las pruebas quemaron folios
  S04518/S04519 (huecos cosméticos en la secuencia).
- Pendiente: (opcional) `crm.team` dedicado → `ODOO_SALES_TEAM_ID`; confirmar si hay
  staging en Odoo.sh.

## 2. ManyChat  ✅ (conectado y probado 2026-07-23)
- `MANYCHAT_API_TOKEN` en `.env`, cuenta **"Itera mayoreo"** (id 103862911699970, Pro). ✅
- `BENNY_SUBSCRIBER_ID` = `NOTIFY_SUBSCRIBER_IDS` = **730244317** (contacto "Kevin Garcia",
  WhatsApp 5218713878575). El id se sacó del panel de Contactos de ManyChat — la búsqueda
  por `phone` via API devolvía vacío aun con el formato exacto (quirk de ManyChat/WhatsApp).
- Envío probado: escalar_impl y notificar_pago_impl → ManyChat aceptó ambos (enviado). ✅
- `WEBHOOK_SECRET` en `.env` (header `x-itera-token`).
- ⚠️ PENDIENTE agregar `subscriber_id` de **finanzas** a `NOTIFY_SUBSCRIBER_IDS` (esas
  personas deben escribirle al WhatsApp primero, igual que Benny).
- Pendiente config (cuando haya hosting): nodo **External Request** en ManyChat →
  `https://<host>/manychat/inbound` con header `x-itera-token: <WEBHOOK_SECRET>`.
- ⚠️ Ventana 24h: envíos libres funcionan dentro de 24h; notificaciones frías fuera de esa
  ventana exigen **plantilla aprobada** en ManyChat.

## 3. envia.com  ✅ (conectado y probado 2026-07-23)
- Token en `.env` es de **PRODUCCION** (`api.envia.com`); el de sandbox lo rechazaba (401).
  Cotizar (`/ship/rate/`) solo consulta tarifas — no genera guias ni cobra, seguro en prod.
- Origen: **Manuel Acuña #79, Torreón, CP 27000, estado CO** (`ENVIA_ORIGEN_*`). El almacen
  Odoo se llama "ACUÑA" por la calle, pero el despacho fisico es Torreón.
- Esquema correcto (verificado en docs): `shipment.type=1` (entero), `carrier` OBLIGATORIO
  y especifico, `state` = codigo 2 letras. Se consulta 1 request por carrier (en paralelo).
- Probado: FedEx Torreón→Monterrey $260 (2-4 días), →Tepic $260 (4-6 días). ✅
- ⚠️ PENDIENTE: solo **FedEx** devuelve tarifas. Estafeta/DHL/Paquetexpress regresan vacío
  — habilitarlas/contratarlas en el panel de envia.com (o confirmar sus slugs). Config en
  `ENVIA_CARRIERS`. ITERA usa Estafeta como principal, asi que conviene habilitarla.

## 4. Hosting y dominio  ⬜
- Aprovisionar Railway o Render con el `Dockerfile`. TLS/dominio gestionados.
- Subdominio HTTPS estable (ej. `agente.itera.cool`) — requiere acceso DNS de `itera.cool`.

## 5. Validar políticas  ⬜
- Revisar `knowledge/politicas.json` con Benny: mayoreo, envío gratis >$4,000, medios de
  pago y **datos de cuenta** de esta unidad de negocio (NO reusar los del equipo humano
  sin confirmar), garantía, tallas. Cambiar `estado` a "VALIDADO" cuando esté listo.

## 6. Transcripción de audio (opcional)  ⬜
- Si se quieren notas de voz: `OPENAI_API_KEY` propia (no la del proyecto n8n), o
  confirmar si el SDK ya cubre audio nativamente al momento de implementar.
