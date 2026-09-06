# Sprint 3 — Addendum: Admin Assistant (Asistente de Administración)

> **Feature #12** — Asistente conversacional integrado en el panel de administración.
> Permite a cada administrador de tenant interactuar con la plataforma mediante
> chat de texto y/o voz para consultar métricas, configurar su negocio y recibir
> diagnósticos del sistema. En modo voz, el asistente **ejecuta acciones sobre
> la interfaz en tiempo real** (navegación, llenado de formularios, toggles)
> mientras narra al administrador lo que está haciendo — patrón inspirado en
> [WebMCP](https://developer.chrome.com/docs/ai/webmcp) de Chrome.

---

## Resumen ejecutivo

El Admin Assistant es un widget embebido en el panel de administración (Sprint 15)
que funciona como un Copilot interno para los administradores de cada tenant.
Utiliza Claude como LLM, Edge TTS (voz `es-CO-SalomeNeural`) para respuestas de
voz, Web Speech API del navegador para STT, y el RAG existente de la plataforma
(pgvector) para contexto de documentación.

**No es un canal de atención al cliente final.** Es una herramienta de productividad
exclusiva para usuarios con rol `admin` o `super_admin`.

---

## Stack tecnológico

| Componente | Tecnología | Costo adicional |
|---|---|---|
| **LLM** | Claude (Anthropic API) — ya usado por la plataforma | $0 (incluido) |
| **TTS** | Edge TTS — `es-CO-SalomeNeural` (voces neurales Microsoft) | $0 (gratuito, sin API key) |
| **STT** | Web Speech API del navegador (nativo Chrome/Edge/Safari) | $0 (nativo del browser) |
| **RAG** | pgvector — docs de plataforma + config específica del tenant | $0 (ya existe) |
| **Backend** | FastAPI WebSocket `/ws/admin-assistant` | $0 (ya existe) |
| **Frontend** | Widget React en panel admin (shadcn/ui) | $0 (Sprint 15) |

---

## Funcionalidades

### F1: Consultas en lenguaje natural (solo lectura)

El administrador puede hacer preguntas sobre su negocio en lenguaje natural.
El asistente traduce a queries SQL parametrizadas vía function calling de Claude.

**Ejemplos de consultas soportadas:**

```
"¿Cuántas conversaciones tuve esta semana?"
"¿Cuántos tokens me quedan este mes?"
"¿Cuál fue el agente que más tokens consumió?"
"¿Hay mensajes sin responder?"
"¿Cuántos contactos nuevos llegaron hoy?"
"Dame un resumen del uso de la plataforma este mes"
"¿Cuáles son las conversaciones escaladas a humano?"
```

**Implementación — Tools de lectura (Claude function calling):**

| Tool | Descripción | Query subyacente |
|---|---|---|
| `query_conversations` | Estadísticas de conversaciones (count, by status, by date range) | `SELECT ... FROM conversations WHERE client_id = :cid` |
| `query_token_usage` | Uso de tokens (actual, límite, porcentaje, por modelo, por nodo) | `SELECT ... FROM token_budgets / token_usage_log` |
| `query_contacts` | Estadísticas de contactos (total, nuevos, por canal) | `SELECT ... FROM contacts WHERE client_id = :cid` |
| `query_messages` | Estadísticas de mensajes (count, sin respuesta, por canal) | `SELECT ... FROM messages JOIN conversations` |
| `query_agents_performance` | Rendimiento de nodos del grafo (tokens, duración, eficiencia) | `SELECT ... FROM agent_action_logs` |
| `get_system_status` | Estado de webhook, último mensaje, canal activo | Multi-table check |

**Seguridad:**
- Todos los tools de lectura aplican filtro `client_id` automático (RLS).
- El LLM NO genera SQL arbitrario. Los tools son funciones Python predefinidas con queries parametrizadas.
- No se exponen datos de otros tenants bajo ninguna circunstancia.

---

### F2: Configuración guiada por conversación (escritura con confirmación)

El administrador puede solicitar cambios de configuración mediante lenguaje natural.
El asistente muestra un preview del cambio y requiere confirmación explícita
antes de ejecutar.

**Ejemplos:**

```
"Cambia el mensaje de bienvenida a: Hola, bienvenido a Dental Plus"
"Activa el agente de agendamiento"
"Cambia el horario de atención a 8am-6pm de lunes a viernes"
"Desactiva las notificaciones por email"
"Sube el límite de tokens a 100,000"  ← solo super_admin
```

**Implementación — Tools de escritura:**

| Tool | Descripción | Requiere confirmación |
|---|---|---|
| `update_welcome_message` | Cambia mensaje de bienvenida del canal | Sí |
| `toggle_agent_node` | Activa/desactiva un nodo del grafo | Sí |
| `update_business_hours` | Modifica horarios de atención | Sí |
| `update_agent_config` | Modifica configuración del agente IA | Sí |
| `update_client_settings` | Modifica settings generales del tenant | Sí |

**Flujo de confirmación:**

```
Admin: "Cambia el mensaje de bienvenida a: Hola, soy Sara de Dental Plus"
Asistente: "Entendido. Voy a cambiar el mensaje de bienvenida:
  • Actual: 'Bienvenido, ¿en qué puedo ayudarte?'
  • Nuevo: 'Hola, soy Sara de Dental Plus'
  ¿Confirmas el cambio? (sí/no)"
Admin: "sí"
Asistente: "Listo, mensaje de bienvenida actualizado."
```

**Seguridad:**
- Tools de escritura NUNCA ejecutan sin confirmación del usuario.
- Acciones destructivas (eliminar contactos, borrar conversaciones) NO están
  disponibles vía el asistente.
- Cambios de plan o límite de tokens solo disponibles para `super_admin`.
- Cada cambio genera un registro en `audit_logs` (Fase 2).

---

### F3: Onboarding interactivo

Cuando un administrador nuevo inicia sesión por primera vez, el asistente ofrece
un recorrido guiado paso a paso.

**Flujo del onboarding:**

```
1. "¡Bienvenido a la plataforma! Soy tu asistente. ¿Quieres que te guíe
   en la configuración inicial?" (sí/no)

2. Datos del negocio:
   "¿Cuál es el nombre de tu negocio?"
   "¿En qué sector operas?" (salud, comercio, educación, servicios, otro)
   "¿Cuál es tu zona horaria?"

3. Canal de comunicación:
   "¿Qué canal quieres configurar primero?" (WhatsApp, Instagram, Facebook)
   "Para WhatsApp necesitas tu API key de YCloud. ¿Ya la tienes?"
   → Si no: enlace a docs de configuración

4. Base de conocimiento:
   "¿Tienes documentos (PDF, Word, texto) con información de tu negocio?"
   → Guía para subir documentos en el panel

5. Personalización:
   "¿Cómo quieres que se llame tu asistente virtual?"
   "¿Quieres que use un tono formal o casual?"

6. Prueba:
   "¡Todo listo! ¿Quieres hacer una prueba enviando un mensaje de prueba?"
```

**Implementación:**
- El estado del onboarding se almacena en `clients.settings` como
  `{"onboarding_completed": true/false, "onboarding_step": 3}`.
- Cada paso mapea a un tool de escritura (con confirmación automática
  durante el flujo de onboarding).
- El onboarding se puede retomar si se interrumpe.

---

### F4: Diagnóstico rápido del sistema

El administrador puede preguntar por el estado de salud de su configuración.
El asistente ejecuta una batería de checks y reporta.

**Ejemplos:**

```
"¿Por qué no están llegando mensajes?"
"¿El webhook está funcionando?"
"¿Mi agente está respondiendo correctamente?"
"Diagnostica mi configuración"
```

**Checks que ejecuta el tool `diagnose_system`:**

| Check | Qué verifica | Resultado posible |
|---|---|---|
| Webhook status | Último webhook recibido, timestamp | "Último webhook hace 5 min ✅" / "Sin webhooks en 24h ⚠️" |
| Token budget | Porcentaje usado | "Tokens: 45,000/50,000 (90%) ⚠️ Zona de degradación" |
| Canal activo | Config de canal existente y válida | "WhatsApp configurado ✅" / "Sin canal configurado ❌" |
| Agente activo | agent_config habilitado | "Agente 'Sara' activo ✅" |
| Último mensaje | Timestamp del último mensaje procesado | "Último mensaje: hace 2 horas" |
| Errores recientes | agent_action_logs con status error (últimas 24h) | "3 errores en las últimas 24h ⚠️" |
| Base de conocimiento | Documentos cargados y chunks generados | "15 documentos, 340 chunks ✅" |

---

### F5: Resúmenes automáticos (texto, sin voz)

Resumen periódico que se puede consultar bajo demanda o programar.

**Bajo demanda:**
```
"Dame el resumen de hoy"
"¿Qué pasó esta semana?"
```

**Programado (via Celery Beat):**
- Resumen diario a las 8:00 AM (hora del tenant)
- Se almacena en `clients.settings.last_daily_summary`
- Se envía como notificación in-app al admin al iniciar sesión
- Opcionalmente se envía por email (si el admin lo configura)

**Formato del resumen:**
```
📊 Resumen del 5 de septiembre:
• 47 conversaciones (39 resueltas por bot, 5 por humano, 3 activas)
• 12 contactos nuevos
• Tokens: 12,400/50,000 (24.8%)
• 0 escalaciones sin atender
• Canal más activo: WhatsApp (89%)
• Sin errores del sistema
```

---

### F6: UI Actions — Acciones sobre la interfaz (patrón WebMCP)

El asistente no solo responde con texto/voz: **ejecuta acciones visibles en la
interfaz del panel admin** mientras narra lo que está haciendo. El administrador
ve cómo la pantalla navega, resalta elementos, llena formularios y activa toggles,
todo controlado por el agente conversacional.

Este patrón está inspirado en el estándar
[WebMCP](https://developer.chrome.com/docs/ai/webmcp) de Chrome, donde los
sitios web exponen herramientas determinísticas (tools) que los agentes de IA
invocan directamente, en lugar de hacer DOM scraping o simular clicks ciegos.

**Principio clave:** dos registros de herramientas separados:

| Registro | Ejecución | Propósito |
|---|---|---|
| **Backend Tools** | Servidor (Python) | Leer/escribir DATOS (DB, config, métricas) |
| **Frontend UI Tools** | Navegador (React) | Controlar la INTERFAZ (navigate, highlight, fill, toggle) |

Ambos se declaran como tools de Claude (function calling). El servidor ejecuta
los backend tools localmente; los frontend tools se reenvían al navegador vía
WebSocket como mensajes `ui_action` que el frontend ejecuta.

**Frontend UI Tools disponibles:**

| Tool | Acción en el navegador | Ejemplo |
|---|---|---|
| `ui_navigate` | `router.push(route)` — navega a una sección del panel | `/dashboard/settings/agent` |
| `ui_highlight` | Flash/pulse CSS en un elemento identificado por `data-assistant-id` | Resaltar el campo "mensaje de bienvenida" |
| `ui_fill_form` | Setea el valor de un input/textarea vía React state | Escribir nuevo mensaje en el campo |
| `ui_toggle` | Cambia el estado de un switch | Activar/desactivar nodo del agente |
| `ui_show_notification` | Muestra un toast (shadcn/ui `Sonner`) | "Configuración actualizada ✅" |
| `ui_open_modal` | Abre un modal/dialog específico | Modal de confirmación de cambio |
| `ui_scroll_to` | `scrollIntoView()` suave hacia un elemento | Scroll al panel de token budget |

**Flujo completo de voz + acciones (ejemplo):**

```
Admin: (presiona 🎙️ "Hablar") "Quiero cambiar el mensaje de bienvenida"

1. STT (Web Speech API) → texto al servidor

2. Claude analiza → decide que necesita:
   a) ui_navigate → /dashboard/settings/agent
   b) Leer el mensaje actual (backend tool: get_agent_config)
   c) ui_highlight → campo welcome_message

3. Ejecución simultánea:
   VOZ: "Voy a llevarte a la configuración del agente..."
   UI:  → Navegación automática a Settings > Agent
   VOZ: "Aquí está el mensaje actual. Voy a resaltarlo..."
   UI:  → Highlight pulsante en el campo de welcome_message

4. Claude pregunta:
   VOZ: "El mensaje actual es 'Bienvenido'. ¿Cuál quieres poner?"

5. Admin dice: "Ponle: Hola, soy Sara de Dental Plus"

6. Claude ejecuta:
   VOZ: "Perfecto, voy a escribir el nuevo mensaje..."
   UI:  → ui_fill_form: escribe el texto en el campo
   VOZ: "¿Quieres que lo guarde?"
   WS:  → confirm_action (preview del cambio)

7. Admin: "Sí, guárdalo"

8. Claude ejecuta:
   BACKEND: → update_welcome_message() (escribe en DB)
   UI:      → ui_show_notification("Mensaje actualizado ✅")
   VOZ:     "Listo, tu mensaje de bienvenida ya fue actualizado."
```

**Registro de UI Tools en el frontend (React):**

Cada página del admin panel registra sus tools disponibles al montar. El registro
viaja al servidor cuando el WebSocket conecta, y se actualiza en cada navegación.

```typescript
// hooks/useAssistantTools.ts
import { useAssistantContext } from '@/contexts/AssistantContext';

export function useAssistantTools() {
  const { registerTools } = useAssistantContext();

  useEffect(() => {
    registerTools([
      {
        name: 'ui_navigate',
        description: 'Navegar a una sección del panel admin',
        inputSchema: {
          type: 'object',
          properties: {
            route: { type: 'string', description: 'Ruta del panel' },
          },
          required: ['route'],
        },
      },
      {
        name: 'ui_highlight',
        description: 'Resaltar un elemento en la página actual',
        inputSchema: {
          type: 'object',
          properties: {
            elementId: {
              type: 'string',
              description: 'data-assistant-id del elemento',
            },
            duration: { type: 'number', description: 'ms (default 3000)' },
          },
          required: ['elementId'],
        },
      },
      // ... más tools según la página actual
    ]);
  }, []);
}
```

**Elementos del admin panel anotados para el asistente:**

Cada elemento interactuable del panel lleva un atributo `data-assistant-id`
y opcionalmente `data-assistant-description` para que Claude sepa qué puede
manipular en la página actual.

```html
<!-- Settings > Agent Config -->
<input
  data-assistant-id="welcome_message"
  data-assistant-description="Mensaje de bienvenida del agente"
  value={agentConfig.welcomeMessage}
/>

<Switch
  data-assistant-id="scheduling_node_toggle"
  data-assistant-description="Activa o desactiva el nodo de agendamiento"
  checked={agentConfig.schedulingEnabled}
/>
```

**Contexto de página (page context):**

Cuando el admin navega (manual o via `ui_navigate`), el frontend envía un
mensaje `page_context` por WebSocket para informar al agente qué tools de UI
están disponibles y qué elementos existen en la página actual:

```json
// Cliente → Servidor (automático al navegar)
{
  "type": "page_context",
  "route": "/dashboard/settings/agent",
  "available_elements": [
    { "id": "welcome_message", "type": "input", "description": "Mensaje de bienvenida", "value": "Bienvenido" },
    { "id": "scheduling_node_toggle", "type": "switch", "description": "Nodo de agendamiento", "value": true },
    { "id": "tone_selector", "type": "select", "description": "Tono del agente", "value": "formal", "options": ["formal", "casual", "friendly"] }
  ]
}
```

Esto le da a Claude contexto en tiempo real de qué hay en pantalla y qué
puede manipular, sin necesidad de DOM scraping.

---

### Botón "Hablar" — UX del widget

El widget del Admin Assistant tiene dos modos de entrada:

| Modo | Activación | Comportamiento |
|---|---|---|
| **Chat** (default) | Click en el ícono de chat (💬) en la esquina inferior derecha | Input de texto, respuesta en texto |
| **Voz** | Click en botón "Hablar" (🎙️) dentro del widget | STT activo, respuesta en voz + UI Actions |

**Estado del botón "Hablar":**

```
🎙️ Inactivo  → "Hablar" (gris, listo para activar)
🔴 Escuchando → "Escuchando..." (rojo pulsante, STT activo)
⏳ Procesando → "Procesando..." (animación, esperando Claude)
🔊 Hablando  → "Hablando..." (azul, TTS reproduciendo + acciones ejecutando)
```

Cuando el agente está en modo "Hablando", las UI Actions se ejecutan en
sincronía con la narración de voz. El admin ve la pantalla moverse sola
mientras escucha la explicación.

**Interrupción:** El admin puede interrumpir al agente en cualquier momento
tocando el botón 🎙️ otra vez. Esto:
1. Detiene el TTS inmediatamente
2. Cancela UI Actions pendientes
3. Activa STT para escuchar la nueva instrucción

---

## Arquitectura

### Diagrama de componentes

```
┌──────────────────────────────────────────────────────────────┐
│                  Panel Admin (Next.js)                        │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐    │
│  │           AdminAssistantWidget (React)                │    │
│  │  ┌──────────────┐ ┌──────────┐ ┌────────────────┐   │    │
│  │  │  Chat Area   │ │ "Hablar" │ │  🎤 STT        │   │    │
│  │  │  (mensajes)  │ │  🎙️ FAB  │ │  (Web Speech)  │   │    │
│  │  └──────────────┘ └──────────┘ └────────────────┘   │    │
│  └─────────┬──────────────────────────────┬─────────────┘    │
│            │                              │                   │
│            │ WebSocket (wss://)           │ UI Action Executor│
│            │                              │ (React handlers)  │
│  ┌─────────▼─────────────────────┐  ┌────▼───────────────┐  │
│  │ WS Message Router             │  │ UI Tool Registry    │  │
│  │                               │  │                     │  │
│  │ text_chunk → Chat display     │  │ ui_navigate()       │  │
│  │ audio_chunk → Audio playback  │  │ ui_highlight()      │  │
│  │ confirm_action → Modal        │  │ ui_fill_form()      │  │
│  │ ui_action ──────────────────────► ui_toggle()         │  │
│  │ done → Reset state            │  │ ui_show_notification│  │
│  └───────────────────────────────┘  │ ui_open_modal()     │  │
│                                      │ ui_scroll_to()      │  │
│  ┌───────────────────────────────┐  └────────────────────┘  │
│  │ Page Context Reporter         │                           │
│  │ data-assistant-id annotations │──── page_context ────┐    │
│  └───────────────────────────────┘                      │    │
└─────────────────────┬───────────────────────────────────┼────┘
                      │ WebSocket (wss://)                │
┌─────────────────────▼───────────────────────────────────▼────┐
│  FastAPI — /ws/admin-assistant                               │
│                                                              │
│  ┌───────────────┐   ┌───────────────────────────────────┐  │
│  │ Claude API    │   │ Backend Tool Registry              │  │
│  │ (streaming)   │◄──┤                                     │  │
│  │               │   │ READ:                              │  │
│  │ tools =       │   │  query_conversations()             │  │
│  │  backend_tools│   │  query_token_usage()               │  │
│  │  + ui_tools   │   │  query_contacts()                  │  │
│  │  (merged)     │   │  query_messages()                  │  │
│  └─────┬─────────┘   │  query_agents_performance()        │  │
│        │             │  get_system_status()               │  │
│        │             │  diagnose_system()                 │  │
│        │             │                                     │  │
│        │             │ WRITE (con confirmación):           │  │
│   ┌────▼────┐        │  update_welcome_message()          │  │
│   │ Router  │        │  toggle_agent_node()               │  │
│   │         │        │  update_business_hours()           │  │
│   │ backend │──exec──►  update_agent_config()             │  │
│   │ tool?   │        │  update_client_settings()          │  │
│   │         │        └───────────────────────────────────┘  │
│   │ ui      │                                               │
│   │ tool? ──│──forward──► WebSocket → Frontend ejecuta      │
│   └─────────┘                                               │
│        │                                                     │
│        ▼                                                     │
│  ┌───────────┐   ┌──────────────────────────┐               │
│  │ Edge TTS  │   │ RAG Context              │               │
│  │ (stream)  │   │ - Docs de la plataforma  │               │
│  │ SalomeNeural  │ - Config del tenant      │               │
│  └───────────┘   │ - Help articles          │               │
│                   └──────────────────────────┘               │
└──────────────────────────────────────────────────────────────┘
```

**Flujo de tool routing en el servidor:**

Cuando Claude invoca un tool, el servidor distingue:

1. **Backend tool** (prefijo `query_*`, `update_*`, `get_*`, `diagnose_*`):
   se ejecuta localmente en Python → resultado regresa a Claude como tool_result.

2. **UI tool** (prefijo `ui_*`): se reenvía al frontend como mensaje
   `ui_action` vía WebSocket → el frontend ejecuta la acción en el DOM →
   envía `ui_action_result` de vuelta → el servidor lo entrega a Claude
   como tool_result.

Ambos tipos se declaran como tools en la misma llamada a Claude, así el modelo
decide cuáles usar según el contexto de la conversación y la página actual.

### WebSocket Protocol

```json
// ═══════════════════════════════════════════
// CLIENTE → SERVIDOR
// ═══════════════════════════════════════════

// Mensaje de texto o transcripción de voz
{
  "type": "message",
  "content": "¿Cuántas conversaciones tuve hoy?",
  "voice": false
}

// Contexto de página actual (enviado automáticamente al navegar)
{
  "type": "page_context",
  "route": "/dashboard/settings/agent",
  "available_elements": [
    { "id": "welcome_message", "type": "input", "description": "Mensaje de bienvenida", "value": "Bienvenido" },
    { "id": "scheduling_node_toggle", "type": "switch", "description": "Nodo de agendamiento", "value": true }
  ]
}

// Respuesta a confirmación de acción de escritura
{
  "type": "confirm",
  "confirm_id": "uuid-del-cambio",
  "accepted": true
}

// Resultado de una acción de UI ejecutada en el frontend
{
  "type": "ui_action_result",
  "action_id": "uuid-de-la-accion",
  "success": true,
  "context": {
    "current_route": "/dashboard/settings/agent",
    "element_value": "Hola, soy Sara de Dental Plus"
  }
}

// Interrupción: el admin presiona 🎙️ mientras el agente habla
{
  "type": "interrupt"
}

// ═══════════════════════════════════════════
// SERVIDOR → CLIENTE
// ═══════════════════════════════════════════

// Streaming de texto (respuesta parcial de Claude)
{
  "type": "text_chunk",
  "content": "Hoy tuviste "
}

// Confirmación requerida antes de ejecutar tool de escritura
{
  "type": "confirm_action",
  "action": "update_welcome_message",
  "preview": {
    "current": "Bienvenido, ¿en qué puedo ayudarte?",
    "new": "Hola, soy Sara de Dental Plus"
  },
  "confirm_id": "uuid-del-cambio"
}

// Audio TTS (Edge TTS streaming)
{
  "type": "audio_chunk",
  "data": "base64-encoded-audio-chunk",
  "format": "mp3"
}

// ── NUEVO: Acción de UI para ejecutar en el frontend ──
{
  "type": "ui_action",
  "action": "navigate",
  "params": {
    "route": "/dashboard/settings/agent"
  },
  "action_id": "uuid-de-la-accion",
  "narration": "Voy a llevarte a la configuración del agente..."
}

{
  "type": "ui_action",
  "action": "highlight",
  "params": {
    "elementId": "welcome_message",
    "duration": 3000
  },
  "action_id": "uuid-de-la-accion-2",
  "narration": "Aquí puedes ver el mensaje actual."
}

{
  "type": "ui_action",
  "action": "fill_form",
  "params": {
    "elementId": "welcome_message",
    "value": "Hola, soy Sara de Dental Plus"
  },
  "action_id": "uuid-de-la-accion-3"
}

{
  "type": "ui_action",
  "action": "toggle",
  "params": {
    "elementId": "scheduling_node_toggle",
    "value": false
  },
  "action_id": "uuid-de-la-accion-4"
}

{
  "type": "ui_action",
  "action": "show_notification",
  "params": {
    "message": "Mensaje de bienvenida actualizado ✅",
    "variant": "success"
  },
  "action_id": "uuid-de-la-accion-5"
}

// Fin de la respuesta completa
{
  "type": "done"
}
```

**Sincronización voz + UI Actions:**

El campo `narration` en los mensajes `ui_action` contiene el texto que el agente
debe narrar mientras ejecuta la acción. El frontend:

1. Reproduce el audio TTS de la narración.
2. Ejecuta la UI action correspondiente en paralelo.
3. Envía `ui_action_result` al servidor.

Esto crea la experiencia de que el agente "habla y hace" al mismo tiempo.

---

## Configuración por tenant

### Campos en tabla `clients`

```sql
-- Nuevos campos (ALTER TABLE o incluir en CREATE TABLE)
admin_assistant_enabled       BOOLEAN NOT NULL DEFAULT true
admin_assistant_voice_enabled BOOLEAN NOT NULL DEFAULT false
```

- `admin_assistant_enabled`: habilita/deshabilita el widget de chat del
  asistente en el panel admin. Activado por defecto.
- `admin_assistant_voice_enabled`: habilita/deshabilita la funcionalidad
  de voz (STT + TTS). Desactivado por defecto (opt-in).

### Configuración en `clients.settings` (JSONB)

```json
{
  "admin_assistant": {
    "system_prompt_override": null,
    "voice_model": "es-CO-SalomeNeural",
    "voice_speed": "+15%",
    "daily_summary_enabled": false,
    "daily_summary_email": null,
    "daily_summary_hour": 8,
    "onboarding_completed": false,
    "onboarding_step": 0,
    "max_tokens_per_query": 4096,
    "allowed_write_tools": [
      "update_welcome_message",
      "toggle_agent_node",
      "update_business_hours",
      "update_agent_config"
    ]
  }
}
```

---

## Modelo de datos del historial de conversaciones

El historial del asistente admin se almacena en una tabla dedicada, separada
de las conversaciones de clientes finales.

```sql
CREATE TABLE IF NOT EXISTS admin_assistant_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            VARCHAR(20) NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content         TEXT NOT NULL,
    tool_name       VARCHAR(100),
    tool_args       JSONB,
    tool_result     JSONB,
    tokens_used     INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Índices
CREATE INDEX IF NOT EXISTS idx_admin_assistant_history_client
    ON admin_assistant_history(client_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_assistant_history_user
    ON admin_assistant_history(user_id, created_at DESC);

-- RLS
ALTER TABLE admin_assistant_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE admin_assistant_history FORCE ROW LEVEL SECURITY;

CREATE POLICY admin_assistant_history_isolation ON admin_assistant_history
    FOR ALL
    USING (client_id = current_setting('app.current_client_id')::uuid)
    WITH CHECK (client_id = current_setting('app.current_client_id')::uuid);

-- Retención: limpiar mensajes > 90 días (Celery Beat task)
```

**Total de tablas: 26** (25 existentes + 1 nueva)

---

## Seguridad

| Aspecto | Implementación |
|---|---|
| Autenticación | Solo usuarios con JWT válido y rol `admin` o `super_admin` |
| Aislamiento | Todos los queries filtrados por `client_id` vía RLS |
| SQL Injection | No hay SQL generado por LLM. Tools usan queries parametrizadas |
| Rate limiting | Max 30 mensajes/minuto por usuario (FastAPI middleware) |
| Tokens | Max 4,096 tokens por respuesta. Consumo se descuenta del budget del tenant |
| Audit trail | Cada tool de escritura registra en `audit_logs` (Fase 2) |
| Voice data | El audio STT se procesa en el navegador (Web Speech API). No se envía audio al servidor. El servidor solo recibe texto |
| TTS | Edge TTS genera audio en el servidor y se envía como chunks base64. No se almacena |

---

## Dependencias y sprint mapping

| Componente | Sprint | Notas |
|---|---|---|
| Backend: WebSocket endpoint + tools de lectura | Sprint 3 | Junto con onboarding |
| Backend: Tools de escritura + confirmación | Sprint 3 | Requiere agent_configs |
| Backend: Edge TTS integration | Sprint 3 | pip install edge-tts |
| Backend: UI tool routing (forward a frontend) | Sprint 3 | Router backend/ui tools |
| Backend: Diagnóstico | Sprint 8 | Requiere observabilidad |
| Backend: Resúmenes programados | Sprint 8 | Requiere Celery Beat |
| Frontend: Widget React + botón "Hablar" | Sprint 15 | shadcn/ui, FAB, estados |
| Frontend: Web Speech API (STT) | Sprint 15 | Chrome/Edge/Safari |
| Frontend: UI Action Executor | Sprint 15 | Handlers para ui_* tools |
| Frontend: Page Context Reporter | Sprint 15 | data-assistant-id annotations |
| Frontend: Audio playback + interrupción | Sprint 15 | Web Audio API, streaming |
| DDL: tabla `admin_assistant_history` | Sprint 1 (DDL) | Ya en init.sql |
| DDL: campos en `clients` | Sprint 1 (DDL) | Ya en init.sql |

---

## Requisitos de infraestructura

- **edge-tts**: `pip install edge-tts` (sin API key, sin costos)
- **anthropic**: ya existe como dependencia (Claude API)
- **websockets**: FastAPI ya soporta WebSocket nativamente
- **Web Speech API**: nativo del navegador, no requiere instalación

**Latencia estimada del pipeline:**

| Paso | Latencia |
|---|---|
| STT (Web Speech API, navegador) | ~500ms |
| WebSocket round-trip | ~50ms |
| RAG retrieval (pgvector) | ~100ms |
| Claude response (streaming) | ~800ms primer token |
| Edge TTS (streaming) | ~300ms primer chunk |
| **Total percibido** | **~1.5s** (con streaming se siente más rápido) |

---

## Variables de entorno adicionales

```env
# Admin Assistant (en .env)
ANTHROPIC_API_KEY=sk-ant-...          # Ya existe para el pipeline principal
ADMIN_ASSISTANT_MODEL=claude-sonnet-4-20250514  # Modelo para el asistente (más económico que opus)
ADMIN_ASSISTANT_MAX_TOKENS=4096       # Max tokens por respuesta
ADMIN_ASSISTANT_RATE_LIMIT=30         # Mensajes por minuto por usuario
```

**Nota:** No se requiere API key de Edge TTS. No se requiere configuración
adicional de STT (Web Speech API es del navegador).

---

## Testing

### Tests unitarios

```python
# tests/unit/test_admin_assistant_tools.py
class TestAdminAssistantReadTools:
    """Tests para tools de solo lectura."""
    async def test_query_conversations_filters_by_client_id(self): ...
    async def test_query_token_usage_returns_budget(self): ...
    async def test_query_contacts_count(self): ...
    async def test_diagnose_system_all_checks(self): ...

class TestAdminAssistantWriteTools:
    """Tests para tools de escritura."""
    async def test_update_welcome_message_requires_confirmation(self): ...
    async def test_toggle_agent_node_updates_config(self): ...
    async def test_write_tool_creates_audit_log(self): ...
    async def test_write_tool_without_confirmation_raises(self): ...

class TestAdminAssistantSecurity:
    """Tests de seguridad."""
    async def test_agent_role_cannot_access(self): ...
    async def test_rls_isolation_between_tenants(self): ...
    async def test_rate_limiting(self): ...
    async def test_super_admin_only_tools(self): ...
```

### Tests de integración

```python
# tests/integration/test_admin_assistant_ws.py
class TestAdminAssistantWebSocket:
    """Tests del WebSocket endpoint."""
    async def test_ws_connection_requires_auth(self): ...
    async def test_ws_message_returns_response(self): ...
    async def test_ws_confirmation_flow(self): ...
    async def test_ws_audio_chunks_when_voice_enabled(self): ...

class TestAdminAssistantUIActions:
    """Tests del flujo de UI Actions."""
    async def test_ui_tool_forwarded_to_frontend(self): ...
    async def test_ui_action_result_returned_to_claude(self): ...
    async def test_page_context_updates_available_tools(self): ...
    async def test_ui_navigate_sends_correct_route(self): ...
    async def test_ui_highlight_sends_element_id(self): ...
    async def test_ui_fill_form_requires_page_context(self): ...
    async def test_interrupt_cancels_pending_ui_actions(self): ...
    async def test_mixed_backend_and_ui_tools_in_same_turn(self): ...
```
