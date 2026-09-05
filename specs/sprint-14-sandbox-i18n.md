# Sprint 14 — Sandbox, Multi-idioma & Feature Flags (Fase 3)

## Objetivo

Modo sandbox para testing seguro por tenant, soporte multi-idioma con detección automática, y feature flags para rollout gradual de funcionalidades.

## Prerequisitos

- Sprint 13 completado (canal de voz y agente clínico)
- Todos los agentes y canales operativos
- Redis disponible para feature flags cache

## Archivos a Crear

- `app/core/feature_flags.py` — Sistema de feature flags con Redis cache
- `app/services/sandbox.py` — Servicio de sandbox/staging por tenant
- `app/services/i18n.py` — Servicio de internacionalización
- `app/agents/nodes/language_detect.py` — Nodo de detección de idioma
- `app/api/v1/feature_flags.py` — Endpoints admin de feature flags
- `app/api/v1/sandbox.py` — Endpoints de gestión de sandbox
- Modificar `app/models/client.py` — Agregar campo `environment`
- Modificar `app/agents/graph.py` — Agregar nodo `language_detect`
- `tests/unit/test_feature_flags.py`
- `tests/unit/test_sandbox.py`
- `tests/unit/test_i18n.py`
- `tests/integration/test_sandbox_isolation.py`

## Tareas Detalladas

### 1. Modo Sandbox

Cada tenant opera en dos environments aislados:

- **production**: datos reales, agentes activos, canales conectados.
- **sandbox**: copia de configuración para testing sin afectar producción.

Flujo de trabajo:
1. Admin crea sandbox → copia agent_configs, quick_replies, documents metadata.
2. Admin edita configs en sandbox (system prompts, modelos, thresholds).
3. Admin prueba con mensajes de test en sandbox.
4. Admin publica → swap atómico de configs a production.
5. Si hay problemas → rollback a la versión anterior de production.

Implementación recomendada:
- Campo `environment` (enum: `production`, `sandbox`) en `agent_configs`, `quick_replies` y tablas de configuración.
- El middleware inyecta `SET LOCAL app.current_environment = :env` junto con `client_id`.
- Alternativa simplificada: sandbox como tenant clonado con flag `is_sandbox` y `sandbox_of` (FK → clients).

### 2. Publicación Atómica

```python
async def publish_sandbox_to_production(client_id: str, session: AsyncSession):
    """Swap atómico: sandbox configs → production."""
    async with session.begin():
        # 1. Backup current production configs
        await session.execute(text("""
            INSERT INTO config_history (client_id, config_type, config_data, version)
            SELECT :client_id, 'agent_configs', 
                   jsonb_agg(to_jsonb(ac)), nextval('config_version_seq')
            FROM agent_configs ac
            WHERE client_id = :client_id AND environment = 'production'
        """), {"client_id": client_id})
        
        # 2. Delete current production
        await session.execute(text("""
            DELETE FROM agent_configs
            WHERE client_id = :client_id AND environment = 'production'
        """), {"client_id": client_id})
        
        # 3. Copy sandbox → production
        await session.execute(text("""
            INSERT INTO agent_configs (client_id, environment, ...)
            SELECT client_id, 'production', ...
            FROM agent_configs
            WHERE client_id = :client_id AND environment = 'sandbox'
        """), {"client_id": client_id})
```

Rollback: restaurar desde `config_history` con la versión anterior.

### 3. Multi-idioma

Detección automática del idioma del mensaje entrante:

- **Método primario**: biblioteca `langdetect` (Python) — rápida, sin costo de API.
- **Fallback**: GPT-4o-mini con prompt de clasificación (para textos cortos o ambiguos).
- Idiomas soportados: español (`es`), inglés (`en`), portugués (`pt`), italiano (`it`), alemán (`de`), francés (`fr`).
- Fallback si no se detecta: idioma configurado por tenant (default: `es`).
- Configuración por tenant: el admin selecciona el idioma de la interfaz en `/api/v1/settings/preferences`.

Almacenamiento:
- `conversation.metadata.detected_language` — idioma detectado al inicio.
- Se mantiene consistente dentro de la misma conversación (no cambia por mensaje).
- El system prompt del agente incluye: `"Responde siempre en {detected_language}."`.

### 4. Nodo language_detect (LangGraph)

Ubicación en el grafo:
```
START → token_budget_check → language_detect → intent_routing → sentiment_analysis → [...]
```

```python
async def language_detect_node(state: ConversationState) -> dict:
    """Detecta idioma del mensaje entrante."""
    text = state["message"].text
    if not text:
        return {"detected_language": state.get("detected_language", "es")}
    
    # Usar idioma existente de la conversación si ya fue detectado
    if state.get("detected_language"):
        return {}
    
    try:
        detected = detect(text)  # langdetect
        if detected in SUPPORTED_LANGUAGES:  # {"es","en","pt","it","de","fr"}
            return {"detected_language": detected}
    except LangDetectException:
        pass
    
    # Fallback: GPT-4o-mini
    detected = await detect_with_llm(text)
    return {"detected_language": detected or "es"}
```

### 4b. API de Preferencia de Idioma de Interfaz

```python
# Agregar en app/api/v1/settings.py (o preferences.py)

SUPPORTED_UI_LANGUAGES = {"es", "en", "pt", "it", "de", "fr"}

class UserPreferencesUpdate(BaseModel):
    """Actualización de preferencias del usuario."""
    ui_language: str | None = Field(
        None,
        pattern="^(es|en|pt|it|de|fr)$",
        description="Idioma de la interfaz: es, en, pt, it, de, fr"
    )
    theme: str | None = Field(
        None,
        pattern="^(light|dark|system)$",
        description="Tema visual: light, dark, system"
    )

@router.put("/api/v1/settings/preferences")
async def update_user_preferences(
    data: UserPreferencesUpdate,
    db: TenantSession,
    user: CurrentUser,
) -> dict:
    """Actualizar preferencias de idioma y tema del usuario."""
    updates = {}
    if data.ui_language:
        updates["ui_language"] = data.ui_language
    if data.theme:
        updates["theme"] = data.theme
    
    # Guardar en users.settings JSONB
    await db.execute(
        text("""
            UPDATE users 
            SET settings = COALESCE(settings, '{}'::jsonb) || :updates
            WHERE id = :user_id
        """),
        {"updates": json.dumps(updates), "user_id": str(user.id)},
    )
    await db.commit()
    return {"updated": updates}


@router.get("/api/v1/settings/preferences")
async def get_user_preferences(
    db: TenantSession,
    user: CurrentUser,
) -> dict:
    """Obtener preferencias actuales del usuario."""
    result = await db.execute(
        text("SELECT settings FROM users WHERE id = :user_id"),
        {"user_id": str(user.id)},
    )
    row = result.first()
    settings = row.settings if row and row.settings else {}
    return {
        "ui_language": settings.get("ui_language", "es"),
        "theme": settings.get("theme", "system"),
    }
```

### 4c. Traducciones Backend (Mensajes del Sistema)

Los mensajes del sistema (errores, notificaciones, alertas) también deben estar traducidos:

```python
# app/services/i18n.py

SYSTEM_MESSAGES = {
    "es": {
        "budget_exceeded": "Su presupuesto de tokens se ha agotado. Contacte a su administrador.",
        "service_suspended": "El servicio ha sido suspendido. Contacte al administrador.",
        "handoff_message": "Le transfiero con un agente humano. Por favor espere un momento.",
        "out_of_hours": "Estamos fuera de horario de atención. Nuestro horario es {hours}.",
        "welcome": "¡Hola! Soy el asistente virtual de {business_name}. ¿En qué puedo ayudarle?",
        "farewell": "¡Gracias por contactarnos! Si necesita algo más, no dude en escribirnos.",
        "csat_prompt": "¿Cómo calificaría su experiencia? (1-5 estrellas)",
    },
    "en": {
        "budget_exceeded": "Your token budget has been exceeded. Please contact your administrator.",
        "service_suspended": "The service has been suspended. Please contact the administrator.",
        "handoff_message": "I'm transferring you to a human agent. Please wait a moment.",
        "out_of_hours": "We are outside business hours. Our schedule is {hours}.",
        "welcome": "Hello! I'm the virtual assistant for {business_name}. How can I help you?",
        "farewell": "Thank you for contacting us! If you need anything else, don't hesitate to write.",
        "csat_prompt": "How would you rate your experience? (1-5 stars)",
    },
    "pt": {
        "budget_exceeded": "Seu orçamento de tokens foi excedido. Entre em contato com o administrador.",
        "service_suspended": "O serviço foi suspenso. Entre em contato com o administrador.",
        "handoff_message": "Estou transferindo você para um agente humano. Aguarde um momento.",
        "out_of_hours": "Estamos fora do horário de atendimento. Nosso horário é {hours}.",
        "welcome": "Olá! Sou o assistente virtual de {business_name}. Como posso ajudá-lo?",
        "farewell": "Obrigado por nos contatar! Se precisar de mais alguma coisa, não hesite em escrever.",
        "csat_prompt": "Como você avaliaria sua experiência? (1-5 estrelas)",
    },
    "it": {
        "budget_exceeded": "Il budget di token è stato superato. Contattare l'amministratore.",
        "service_suspended": "Il servizio è stato sospeso. Contattare l'amministratore.",
        "handoff_message": "La trasferisco a un agente umano. Attenda un momento.",
        "out_of_hours": "Siamo fuori dall'orario di lavoro. Il nostro orario è {hours}.",
        "welcome": "Ciao! Sono l'assistente virtuale di {business_name}. Come posso aiutarla?",
        "farewell": "Grazie per averci contattato! Se ha bisogno di altro, non esiti a scriverci.",
        "csat_prompt": "Come valuterebbe la sua esperienza? (1-5 stelle)",
    },
    "de": {
        "budget_exceeded": "Ihr Token-Budget wurde überschritten. Bitte kontaktieren Sie Ihren Administrator.",
        "service_suspended": "Der Service wurde ausgesetzt. Bitte kontaktieren Sie den Administrator.",
        "handoff_message": "Ich verbinde Sie mit einem menschlichen Agenten. Bitte warten Sie einen Moment.",
        "out_of_hours": "Wir sind außerhalb der Geschäftszeiten. Unsere Öffnungszeiten sind {hours}.",
        "welcome": "Hallo! Ich bin der virtuelle Assistent von {business_name}. Wie kann ich Ihnen helfen?",
        "farewell": "Vielen Dank für Ihre Kontaktaufnahme! Wenn Sie weitere Fragen haben, schreiben Sie uns.",
        "csat_prompt": "Wie würden Sie Ihre Erfahrung bewerten? (1-5 Sterne)",
    },
    "fr": {
        "budget_exceeded": "Votre budget de tokens a été dépassé. Veuillez contacter votre administrateur.",
        "service_suspended": "Le service a été suspendu. Veuillez contacter l'administrateur.",
        "handoff_message": "Je vous transfère à un agent humain. Veuillez patienter un instant.",
        "out_of_hours": "Nous sommes en dehors des heures d'ouverture. Nos horaires sont {hours}.",
        "welcome": "Bonjour ! Je suis l'assistant virtuel de {business_name}. Comment puis-je vous aider ?",
        "farewell": "Merci de nous avoir contactés ! Si vous avez besoin d'autre chose, n'hésitez pas.",
        "csat_prompt": "Comment évalueriez-vous votre expérience ? (1-5 étoiles)",
    },
}


def get_system_message(
    key: str,
    language: str = "es",
    **kwargs: str,
) -> str:
    """Obtener mensaje del sistema en el idioma indicado.
    
    Args:
        key: Clave del mensaje (ej: 'welcome', 'farewell').
        language: Código de idioma ISO 639-1.
        **kwargs: Variables para interpolación (ej: business_name, hours).
        
    Returns:
        Mensaje traducido con variables interpoladas.
    """
    lang_msgs = SYSTEM_MESSAGES.get(language, SYSTEM_MESSAGES["es"])
    msg = lang_msgs.get(key, SYSTEM_MESSAGES["es"].get(key, key))
    return msg.format(**kwargs) if kwargs else msg
```

### 5. Feature Flags

Sistema basado en Redis con fallback a base de datos:

```python
class FeatureFlags:
    """Feature flags por tenant con cache en Redis."""
    
    CACHE_TTL = 300  # 5 minutos
    
    async def is_enabled(
        self, client_id: str, flag: str, default: bool = False
    ) -> bool:
        """Verifica si un feature flag está activo para el tenant."""
        cache_key = f"ff:{client_id}:{flag}"
        
        # 1. Check Redis cache
        cached = await self.redis.get(cache_key)
        if cached is not None:
            return self._parse_flag_value(cached)
        
        # 2. Check agent_configs.settings
        value = await self._load_from_db(client_id, flag)
        
        # 3. Cache result
        await self.redis.setex(cache_key, self.CACHE_TTL, str(value))
        
        return value if value is not None else default
    
    async def is_enabled_percentage(
        self, client_id: str, flag: str, entity_id: str
    ) -> bool:
        """Rollout gradual: flag con porcentaje (0-100)."""
        percentage = await self._get_percentage(client_id, flag)
        if percentage is None:
            return False
        # Hash determinístico para consistencia por entidad
        hash_val = int(hashlib.md5(
            f"{client_id}:{flag}:{entity_id}".encode()
        ).hexdigest()[:8], 16) % 100
        return hash_val < percentage
    
    async def get_all(self, client_id: str) -> dict[str, bool | int]:
        """Retorna todos los feature flags del tenant."""
        pass
    
    async def set_flag(
        self, client_id: str, flag: str, value: bool | int
    ) -> None:
        """Actualiza un feature flag e invalida cache."""
        pass
```

Flags predefinidos:
- `enable_voice` — Canal de voz (Sprint 13)
- `enable_clinical` — Agente clínico (Sprint 13)
- `enable_marketing` — Agente marketing (Sprint 12)
- `enable_financial` — Agente financiero (Sprint 12)
- `enable_sentiment` — Análisis de sentimiento (Sprint 10)
- `enable_sandbox` — Modo sandbox
- `enable_csat` — Encuestas CSAT (Sprint 11)
- `enable_outgoing_webhooks` — Webhooks salientes (Sprint 11)
- `enable_reranking` — Re-ranking RAG con cross-encoder (Sprint 12)

### 6. Endpoints de Feature Flags

```
GET  /api/v1/admin/feature-flags           — Listar todos los flags del tenant
PUT  /api/v1/admin/feature-flags/{flag}     — Actualizar flag (value: bool | int para %)
```

Requiere RBAC: `admin` o `super_admin`.

### 7. Integración en el Grafo

El `intent_routing` consulta feature flags antes de enrutar:

```python
async def intent_routing_node(state: ConversationState) -> dict:
    client_id = state["client_id"]
    feature_flags = FeatureFlags(redis)
    
    # Determinar agentes disponibles
    available_agents = ["rag_query", "human_handoff"]  # Siempre disponibles
    
    if await feature_flags.is_enabled(client_id, "enable_scheduling"):
        available_agents.append("scheduling")
    if await feature_flags.is_enabled(client_id, "enable_financial"):
        available_agents.append("financial")
    if await feature_flags.is_enabled(client_id, "enable_marketing"):
        available_agents.append("marketing")
    if await feature_flags.is_enabled(client_id, "enable_clinical"):
        available_agents.append("clinical")
    
    # Router solo enruta a agentes disponibles
    intent = await route_intent(state["message"], available_agents)
    return {"intent": intent}
```

### 8. Tests

**Feature Flags:**
- Flag habilitado → agente recibe routing
- Flag deshabilitado → agente NO recibe routing (fallback a RAG o handoff)
- Rollout gradual al 50% → ~50% de entidades activadas (test estadístico)
- Cache invalidation: cambiar flag → siguiente request ve nuevo valor

**Sandbox:**
- Configs editadas en sandbox NO afectan production
- Publicación atómica: swap exitoso, production tiene configs nuevas
- Rollback: restaura versión anterior de production
- Datos de sandbox aislados (RLS por environment)

**Multi-idioma (6 idiomas):**
- Texto en español → `detected_language = "es"`
- Texto en inglés → `detected_language = "en"`
- Texto en portugués → `detected_language = "pt"`
- Texto en italiano → `detected_language = "it"`
- Texto en alemán → `detected_language = "de"`
- Texto en francés → `detected_language = "fr"`
- Texto ambiguo → fallback a idioma del tenant
- Respuesta del agente en el idioma detectado
- Interfaz del frontend traducida completamente (ver Sprint 15)

## Criterios de Aceptación

- Sandbox aislado de production por RLS
- Publicación atómica funciona sin pérdida de datos ni downtime
- Rollback restaura el estado anterior correctamente
- Detección de idioma correcta para los 6 idiomas soportados (es, en, pt, it, de, fr)
- El agente responde en el idioma del contacto
- El admin puede seleccionar idioma de interfaz en configuración
- Feature flags controlan activación/desactivación de cada agente
- Rollout gradual distribuye correctamente según porcentaje
- Cache Redis se invalida al cambiar un flag

## Notas Técnicas

- El sandbox se implementa preferiblemente como filtro adicional de RLS (`environment`) y no como tenant clonado, para evitar duplicar datos.
- La publicación atómica es una transacción PostgreSQL: backup → delete → copy en un solo `BEGIN...COMMIT`.
- Feature flags en Redis con TTL de 5 minutos balancean performance vs. freshness.
- La detección de idioma con `langdetect` es prácticamente gratis (local, sin API call). Solo usar GPT como fallback para textos muy cortos (<10 caracteres).
- Los 6 idiomas soportados (es, en, pt, it, de, fr) cubren la mayoría de mercados LATAM, Europa y Norteamérica. `langdetect` soporta los 6 nativamente.
- La traducción completa de la interfaz de usuario se implementa en el Sprint 15 (Frontend) usando next-intl con archivos JSON por idioma.
- Los feature flags son el mecanismo central para activar módulos de Fase 2 y Fase 3 de forma granular por tenant.
- El hash determinístico para rollout gradual garantiza que la misma entidad siempre obtiene el mismo resultado (consistencia).

## Dependencias

- Este es el último sprint del plan. Después de completarlo, la plataforma tiene todas las funcionalidades de las 3 fases.
- Considerar: load testing, penetration testing y documentación de API (OpenAPI/Swagger) como tareas post-sprint.
