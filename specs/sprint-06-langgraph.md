# Sprint 6 — LangGraph: Grafo de Agentes

## Objetivo
Orquestacion completa del flujo conversacional usando LangGraph StateGraph con routing semantico, RAG con strict grounding, control de presupuesto de tokens con degradacion gradual, handoff a humanos, modo entrenamiento y few-shot dinamicos. Al finalizar este sprint, un mensaje entrante recorre el grafo completo y genera una respuesta inteligente o escala a un agente humano.

## Prerequisitos
- Sprint 5 completado: RAG service funcional con `retrieve()`, `retrieve_few_shot_examples()` y `build_grounded_prompt()`
- LangGraph >= 0.2.0 y LangChain >= 0.3.0 instalados
- OpenAI API key configurada para GPT-4o y GPT-4o-mini
- AsyncPostgresSaver de LangGraph para checkpointing
- MessagingProvider (Sprint 4) disponible para envio de respuestas
- Redis accesible para cache de token budgets

## Archivos a Crear
```
app/
  agents/
    __init__.py
    state.py                         # ConversationState TypedDict
    graph.py                         # build_conversation_graph()
    nodes/
      __init__.py
      intent_router.py               # Nodo de routing semantico
      rag_query.py                   # Nodo de RAG con strict grounding
      token_budget.py                # Nodo de control de presupuesto
      respond.py                     # Nodo de envio de respuesta
      human_handoff.py               # Nodo de escalacion a humano
      training_approval.py           # Nodo de modo entrenamiento
  middleware/
    token_budget.py                  # TokenBudgetGuard (implementacion completa)
  tasks/
    ai_processor.py                  # Task Celery que invoca el grafo
tests/
  unit/
    test_intent_routing.py
    test_rag_node.py
    test_token_budget.py
    test_training_approval.py
  integration/
    test_graph_flow.py
```

## Tareas Detalladas

### 1. ConversationState (`app/agents/state.py`)

El estado es el dato central que fluye por todo el grafo. Cada nodo lee lo que necesita y escribe los campos que le corresponden.

```python
from typing import TypedDict

class ConversationState(TypedDict):
    """
    Estado de la conversacion que fluye por el grafo de LangGraph.

    Cada nodo retorna un dict parcial con solo los campos que modifica.
    LangGraph mergea automaticamente con el estado existente (PAT-003).
    NUNCA mutar el estado directamente; siempre retornar un dict nuevo.
    """
    # Contexto de la conversacion (inmutables despues de init)
    client_id: str
    conversation_id: str
    contact_id: str
    channel: str
    message: dict  # NormalizedMessage serializado

    # Resultado del intent routing
    intent: str | None                # greeting, farewell, rag_query, scheduling, complaint, human_request, unknown
    intent_confidence: float | None

    # Resultado del RAG
    rag_context: list[dict] | None    # Chunks recuperados con citaciones
    rag_confidence: float | None      # Confianza promedio de los chunks

    # Respuesta generada
    response_text: str | None

    # Control de presupuesto
    budget_status: str                # "ok" | "degraded" | "exceeded"
    model_to_use: str                 # Modelo a usar segun presupuesto
    budget_usage_pct: float           # Porcentaje de uso actual

    # Handoff
    requires_handoff: bool
    handoff_reason: str | None        # insufficient_context, budget_exceeded, human_request, complaint

    # Modo entrenamiento
    training_mode: bool
    approved_examples: list[dict] | None  # Few-shot dinamicos de approved_responses

    # Error
    error: str | None
```

### 2. build_conversation_graph() (`app/agents/graph.py`)

```python
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

def build_conversation_graph() -> StateGraph:
    """
    Construye el grafo de conversacion.

    Flujo:
    START → token_budget_check → intent_routing → [router condicional]
                                                   ├─ rag_query → [rag_router]
                                                   │              ├─ training_mode_approval → END
                                                   │              ├─ respond → END
                                                   │              └─ human_handoff → END
                                                   ├─ scheduling → respond → END
                                                   ├─ human_handoff → END
                                                   └─ respond → END (greeting, farewell)
    """
    graph = StateGraph(ConversationState)

    # Agregar nodos
    graph.add_node("token_budget_check", token_budget_check_node)
    graph.add_node("intent_routing", intent_routing_node)
    graph.add_node("rag_query", rag_query_node)
    graph.add_node("respond", respond_node)
    graph.add_node("human_handoff", human_handoff_node)
    graph.add_node("training_mode_approval", training_approval_node)

    # Edge: START → token_budget_check
    graph.set_entry_point("token_budget_check")

    # Edge condicional: token_budget_check → ...
    graph.add_conditional_edges(
        "token_budget_check",
        route_after_budget_check,
        {
            "continue": "intent_routing",
            "exceeded": "human_handoff",
        },
    )

    # Edge condicional: intent_routing → ...
    graph.add_conditional_edges(
        "intent_routing",
        route_after_intent,
        {
            "rag_query": "rag_query",
            "human_handoff": "human_handoff",
            "respond": "respond",  # greeting, farewell
            # "scheduling": "scheduling",  # Sprint 7
        },
    )

    # Edge condicional: rag_query → ...
    graph.add_conditional_edges(
        "rag_query",
        route_after_rag,
        {
            "training_mode": "training_mode_approval",
            "respond": "respond",
            "human_handoff": "human_handoff",
        },
    )

    # Edges terminales
    graph.add_edge("respond", END)
    graph.add_edge("human_handoff", END)
    graph.add_edge("training_mode_approval", END)

    return graph


# === FUNCIONES DE ROUTING ===

def route_after_budget_check(state: ConversationState) -> str:
    """Decide si continuar o escalar por presupuesto agotado."""
    if state["budget_status"] == "exceeded":
        return "exceeded"
    return "continue"

def route_after_intent(state: ConversationState) -> str:
    """Enruta al nodo correcto segun el intent detectado."""
    intent = state.get("intent", "unknown")

    if intent in ("greeting", "farewell"):
        return "respond"
    elif intent == "human_request":
        return "human_handoff"
    elif intent == "rag_query":
        return "rag_query"
    elif intent == "scheduling":
        # Sprint 7 lo implementara; por ahora, tratar como rag_query
        return "rag_query"
    elif intent == "complaint":
        return "human_handoff"
    else:
        # "unknown" → intentar RAG primero
        return "rag_query"

def route_after_rag(state: ConversationState) -> str:
    """Decide si responder, escalar o enviar a aprobacion."""
    if state.get("requires_handoff"):
        return "human_handoff"
    if state.get("training_mode"):
        return "training_mode"
    return "respond"
```

### 3. Checkpointing

```python
async def get_graph_with_checkpointer():
    """
    Crea el grafo compilado con checkpointing en PostgreSQL.

    El checkpointer permite:
    - Persistir estado entre mensajes de la misma conversacion
    - Resume del grafo si se interrumpe
    - Historial de estados para debugging
    """
    graph = build_conversation_graph()

    checkpointer = AsyncPostgresSaver.from_conn_string(
        settings.DATABASE_URL.replace("postgres://", "postgresql+asyncpg://")
    )
    await checkpointer.setup()  # Crea tablas de checkpointing si no existen

    compiled = graph.compile(checkpointer=checkpointer)
    return compiled
```

Uso del grafo con thread_id para persistencia:

```python
config = {"configurable": {"thread_id": f"{client_id}:{conversation_id}"}}
result = await compiled_graph.ainvoke(initial_state, config=config)
```

### 4. Nodo intent_routing (`app/agents/nodes/intent_router.py`)

```python
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

class IntentClassification(BaseModel):
    """Structured output para clasificacion de intents."""
    intent: str = Field(
        description="Intent detectado",
        enum=["greeting", "farewell", "rag_query", "scheduling",
              "complaint", "human_request", "unknown"],
    )
    confidence: float = Field(
        description="Confianza de la clasificacion (0.0 - 1.0)",
        ge=0.0, le=1.0,
    )

async def intent_routing_node(state: ConversationState) -> dict:
    """
    Clasifica el intent del mensaje usando GPT-4o-mini con structured output.

    REGLA: Solo enrutar a agentes HABILITADOS del tenant.
    Consulta agent_configs para verificar que agentes estan activos.
    """
    message_text = state["message"].get("text", "")
    client_id = state["client_id"]
    model_name = state.get("model_to_use", "gpt-4o-mini")

    # 1. Consultar agentes habilitados del tenant
    enabled_agents = await _get_enabled_agents(client_id)

    # 2. Construir prompt de routing
    available_intents = _filter_intents_by_agents(enabled_agents)

    system_prompt = f"""Eres un clasificador de intents. Analiza el mensaje del usuario
y clasifica su intencion en una de las siguientes categorias:

{_format_intent_descriptions(available_intents)}

Si el usuario pide explicitamente hablar con un humano, usa 'human_request'.
Si el mensaje es una queja o reclamo, usa 'complaint'.
Si no estas seguro, usa 'unknown'.
"""

    # 3. Clasificar con structured output
    llm = ChatOpenAI(model=model_name, temperature=0)
    structured_llm = llm.with_structured_output(IntentClassification)

    result = await structured_llm.ainvoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message_text},
    ])

    return {
        "intent": result.intent,
        "intent_confidence": result.confidence,
    }


async def _get_enabled_agents(client_id: str) -> list[str]:
    """Consulta agent_configs para obtener agentes habilitados del tenant."""
    async with tenant_session(UUID(client_id)) as session:
        result = await session.execute(
            select(AgentConfig.agent_type)
            .where(AgentConfig.is_enabled == True)
        )
        return [row[0] for row in result.fetchall()]


def _filter_intents_by_agents(enabled_agents: list[str]) -> list[str]:
    """Filtra intents disponibles segun agentes habilitados."""
    # Intents base siempre disponibles
    intents = ["greeting", "farewell", "human_request", "complaint", "unknown"]

    # Intents que dependen de agentes habilitados
    agent_to_intent = {
        "rag": "rag_query",
        "scheduling": "scheduling",
    }

    for agent_type, intent in agent_to_intent.items():
        if agent_type in enabled_agents:
            intents.append(intent)

    # Si RAG no esta habilitado, queries generales van a human_handoff
    if "rag_query" not in intents:
        intents.append("rag_query")  # Aun asi lo necesitamos para fallback

    return intents
```

### 5. Nodo rag_query (`app/agents/nodes/rag_query.py`)

```python
async def rag_query_node(state: ConversationState) -> dict:
    """
    Nodo de RAG con strict grounding.

    Flujo:
    1. Recuperar chunks relevantes del knowledge base
    2. Recuperar few-shot examples de approved_responses
    3. Si contexto suficiente: generar respuesta con grounding
    4. Si contexto insuficiente: marcar para handoff

    REGLA: NUNCA responder sin contexto suficiente.
    """
    message_text = state["message"].get("text", "")
    client_id = state["client_id"]
    model_name = state.get("model_to_use", "gpt-4o")

    # Obtener configuracion del tenant para RAG
    rag_config = await _get_rag_config(client_id)
    threshold = rag_config.get("threshold", 0.75)
    top_k = rag_config.get("top_k", 5)

    # 1. Recuperar chunks relevantes
    rag_service = RAGService(embedding_service=get_embedding_service(client_id))
    context_chunks = await rag_service.retrieve(
        query=message_text,
        client_id=UUID(client_id),
        top_k=top_k,
        threshold=threshold,
    )

    # 2. Verificar si hay contexto suficiente
    if not context_chunks:
        return {
            "rag_context": [],
            "rag_confidence": 0.0,
            "requires_handoff": True,
            "handoff_reason": "insufficient_context",
        }

    avg_confidence = sum(c.similarity for c in context_chunks) / len(context_chunks)

    # 3. Recuperar few-shot examples
    few_shot_examples = await rag_service.retrieve_few_shot_examples(
        query=message_text,
        client_id=UUID(client_id),
        top_k=3,
        threshold=0.80,  # Threshold mas estricto para few-shot
    )

    # 4. Construir prompt con strict grounding
    grounded_prompt = rag_service.build_grounded_prompt(
        query=message_text,
        context_chunks=context_chunks,
        few_shot_examples=few_shot_examples if few_shot_examples else None,
    )

    # 5. Generar respuesta
    llm = ChatOpenAI(model=model_name, temperature=0.3)
    # Obtener system_prompt del tenant si existe
    tenant_system_prompt = await _get_tenant_system_prompt(client_id, "rag")

    messages = []
    if tenant_system_prompt:
        messages.append({"role": "system", "content": tenant_system_prompt})
    messages.append({"role": "user", "content": grounded_prompt})

    response = await llm.ainvoke(messages)

    # 6. Verificar que el tenant tiene training_mode habilitado
    training_mode = await _is_training_mode_enabled(client_id)

    return {
        "rag_context": [
            {"content": c.content, "similarity": c.similarity, "citation": c.citation}
            for c in context_chunks
        ],
        "rag_confidence": avg_confidence,
        "response_text": response.content,
        "training_mode": training_mode,
        "approved_examples": few_shot_examples,
    }


async def _get_rag_config(client_id: str) -> dict:
    """Obtiene configuracion de RAG del tenant desde agent_configs."""
    async with tenant_session(UUID(client_id)) as session:
        result = await session.execute(
            select(AgentConfig).where(
                AgentConfig.agent_type == "rag",
            )
        )
        config = result.scalar_one_or_none()
        if config:
            return config.settings or {}
        return {}


async def _get_tenant_system_prompt(client_id: str, agent_type: str) -> str | None:
    """Obtiene el system_prompt configurado por el tenant para un agente."""
    async with tenant_session(UUID(client_id)) as session:
        result = await session.execute(
            select(AgentConfig.system_prompt).where(
                AgentConfig.agent_type == agent_type,
            )
        )
        row = result.scalar_one_or_none()
        return row


async def _is_training_mode_enabled(client_id: str) -> bool:
    """Verifica si el tenant tiene training_mode habilitado."""
    async with tenant_session(UUID(client_id)) as session:
        result = await session.execute(
            select(AgentConfig.settings).where(
                AgentConfig.agent_type == "rag",
            )
        )
        row = result.scalar_one_or_none()
        if row:
            return row.get("training_mode", False)
        return False
```

### 6. Nodo token_budget_check (`app/agents/nodes/token_budget.py`)

```python
async def token_budget_check_node(state: ConversationState) -> dict:
    """
    Verifica el presupuesto de tokens del tenant.

    Niveles de degradacion:
    - < 90%: OK → usa el modelo configurado del tenant
    - 90-99%: DEGRADED → cambia a gpt-4o-mini para ahorrar tokens
    - >= 100%: EXCEEDED → marca para handoff a humano (no gastar mas tokens)

    El uso se cachea en Redis para evitar queries frecuentes a la DB.
    """
    client_id = state["client_id"]

    # 1. Obtener uso actual (Redis cache con fallback a DB)
    usage = await _get_token_usage(client_id)

    usage_pct = (usage["used_tokens"] / usage["max_tokens"] * 100) if usage["max_tokens"] > 0 else 0

    # 2. Determinar modelo a usar segun el tenant
    tenant_model = await _get_tenant_model(client_id)

    # 3. Aplicar logica de degradacion
    if usage_pct >= 100:
        return {
            "budget_status": "exceeded",
            "model_to_use": tenant_model,  # No importa, no se usara
            "budget_usage_pct": usage_pct,
            "requires_handoff": True,
            "handoff_reason": "budget_exceeded",
        }
    elif usage_pct >= 90:
        return {
            "budget_status": "degraded",
            "model_to_use": "gpt-4o-mini",  # Degradar a modelo mas barato
            "budget_usage_pct": usage_pct,
            "requires_handoff": False,
        }
    else:
        return {
            "budget_status": "ok",
            "model_to_use": tenant_model,
            "budget_usage_pct": usage_pct,
            "requires_handoff": False,
        }


async def _get_token_usage(client_id: str) -> dict:
    """
    Obtiene el uso de tokens del periodo actual.
    Usa Redis como cache con TTL de 60 segundos.
    """
    redis_client = get_redis()
    cache_key = f"token_budget:{client_id}"

    # Intentar cache
    cached = await redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    # Consultar DB
    async with tenant_session(UUID(client_id)) as session:
        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(TokenBudget).where(
                TokenBudget.period_start <= now.date(),
                TokenBudget.period_end >= now.date(),
            )
        )
        budget = result.scalar_one_or_none()

        if not budget:
            # Sin presupuesto configurado → sin limite
            usage = {"max_tokens": 999_999_999, "used_tokens": 0}
        else:
            usage = {"max_tokens": budget.max_tokens, "used_tokens": budget.used_tokens}

    # Cachear en Redis (60s TTL)
    await redis_client.set(cache_key, json.dumps(usage), ex=60)

    return usage


async def _get_tenant_model(client_id: str) -> str:
    """Obtiene el modelo configurado por el tenant."""
    async with tenant_session(UUID(client_id)) as session:
        result = await session.execute(
            select(AgentConfig.model_name).where(
                AgentConfig.agent_type == "rag",
            )
        )
        model = result.scalar_one_or_none()
        return model or "gpt-4o"
```

### 7. TokenBudgetGuard Middleware — Implementacion Completa (`app/middleware/token_budget.py`)

```python
class TokenBudgetGuard:
    """
    Registra el uso de tokens despues de cada invocacion de LLM.
    Se llama desde los nodos que usan LLM (intent_routing, rag_query, respond).
    """

    @staticmethod
    async def record_usage(
        client_id: str,
        conversation_id: str | None,
        model_used: str,
        prompt_tokens: int,
        completion_tokens: int,
    ):
        """
        Registra uso de tokens en DB y actualiza cache de Redis.
        """
        total_tokens = prompt_tokens + completion_tokens
        cost_usd = _calculate_cost(model_used, prompt_tokens, completion_tokens)

        async with tenant_session(UUID(client_id)) as session:
            # 1. Log granular
            log_entry = TokenUsageLog(
                client_id=UUID(client_id),
                conversation_id=UUID(conversation_id) if conversation_id else None,
                model_used=model_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=cost_usd,
            )
            session.add(log_entry)

            # 2. Actualizar presupuesto del periodo
            now = datetime.now(timezone.utc)
            result = await session.execute(
                select(TokenBudget).where(
                    TokenBudget.period_start <= now.date(),
                    TokenBudget.period_end >= now.date(),
                )
            )
            budget = result.scalar_one_or_none()

            if budget:
                budget.used_tokens += total_tokens

            await session.commit()

        # 3. Invalidar cache de Redis
        redis_client = get_redis()
        await redis_client.delete(f"token_budget:{client_id}")


def _calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calcula el costo en USD segun el modelo."""
    # Precios por 1M tokens (actualizar segun pricing vigente)
    pricing = {
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    }

    model_pricing = pricing.get(model, pricing["gpt-4o-mini"])
    cost = (
        (prompt_tokens / 1_000_000) * model_pricing["input"]
        + (completion_tokens / 1_000_000) * model_pricing["output"]
    )
    return round(cost, 6)
```

### 8. Nodo respond (`app/agents/nodes/respond.py`)

```python
async def respond_node(state: ConversationState) -> dict:
    """
    Envia la respuesta al contacto via MessagingProvider.

    Flujo:
    1. Si no hay response_text (greeting/farewell), generar uno rapido
    2. Enviar via MessagingProvider al canal correspondiente
    3. Guardar mensaje outbound en DB
    4. Registrar uso de tokens
    5. Actualizar conversation.last_message_at
    """
    client_id = state["client_id"]
    conversation_id = state["conversation_id"]
    contact_id = state["contact_id"]
    channel = state["channel"]
    response_text = state.get("response_text")

    # 1. Generar respuesta si no existe (greeting, farewell)
    if not response_text:
        intent = state.get("intent", "unknown")
        if intent == "greeting":
            response_text = "Hola! En que puedo ayudarte hoy?"
        elif intent == "farewell":
            response_text = "Hasta luego! Si necesitas algo mas, no dudes en escribirme."
        else:
            response_text = "Disculpa, no entendi tu mensaje. Podrias reformularlo?"

    # 2. Obtener identifier del contacto para este canal
    contact_identifier = await _get_contact_identifier(client_id, contact_id, channel)

    # 3. Enviar via MessagingProvider
    provider = get_messaging_provider_for_channel(channel)
    channel_config = await _get_channel_config(client_id, channel)

    external_id = await provider.send_message(
        to=contact_identifier,
        content=MessageContent(text=response_text),
        channel_config=channel_config,
    )

    # 4. Guardar mensaje outbound en DB
    async with tenant_session(UUID(client_id)) as session:
        message = Message(
            client_id=UUID(client_id),
            conversation_id=UUID(conversation_id),
            user_id=None,  # Bot, no humano
            direction="outbound",
            message_type="text",
            content=response_text,
            external_message_id=external_id,
        )
        session.add(message)

        # 5. Actualizar last_message_at
        conversation = await session.get(Conversation, UUID(conversation_id))
        if conversation:
            conversation.last_message_at = datetime.now(timezone.utc)

        await session.commit()

    # 6. Registrar uso de tokens (si se uso LLM)
    # Los tokens se registran en los nodos que invocan el LLM (intent_router, rag_query)
    # Este nodo solo maneja el envio

    return {"response_text": response_text}
```

### 9. Nodo human_handoff (`app/agents/nodes/human_handoff.py`)

```python
async def human_handoff_node(state: ConversationState) -> dict:
    """
    Escala la conversacion a un agente humano.

    Acciones:
    1. Cambiar status de conversacion a 'waiting_human'
    2. Crear nota interna con la razon del handoff
    3. Notificar al agente asignado o al pool de agentes
    4. Enviar mensaje al contacto informando la transferencia
    """
    client_id = state["client_id"]
    conversation_id = state["conversation_id"]
    contact_id = state["contact_id"]
    channel = state["channel"]
    handoff_reason = state.get("handoff_reason", "unknown")

    # Mensajes personalizados segun la razon
    HANDOFF_MESSAGES = {
        "insufficient_context": "No tengo suficiente informacion para responder tu consulta. Te estoy transfiriendo con un agente humano que podra ayudarte mejor.",
        "budget_exceeded": "Te estoy transfiriendo con un agente humano para atenderte personalmente.",
        "human_request": "Entendido, te transfiero con un agente humano ahora mismo.",
        "complaint": "Lamento la situacion. Te transfiero con un agente especializado para resolver tu caso.",
    }

    async with tenant_session(UUID(client_id)) as session:
        # 1. Actualizar status de conversacion
        conversation = await session.get(Conversation, UUID(conversation_id))
        if conversation:
            conversation.status = "waiting_human"
            conversation.updated_at = datetime.now(timezone.utc)

        # 2. Crear nota interna
        note_content = f"Handoff automatico. Razon: {handoff_reason}."
        if state.get("rag_confidence") is not None:
            note_content += f" Confianza RAG: {state['rag_confidence']:.2f}."
        if state.get("budget_usage_pct") is not None:
            note_content += f" Uso de presupuesto: {state['budget_usage_pct']:.1f}%."

        note = InternalNote(
            client_id=UUID(client_id),
            contact_id=UUID(contact_id),
            user_id=None,  # Generado por el sistema
            content=note_content,
        )
        # NOTA: internal_notes.user_id es NOT NULL en el schema.
        # Considerar crear un "system user" por tenant, o hacer user_id nullable
        # para notas automaticas del bot.
        session.add(note)

        await session.commit()

    # 3. Enviar mensaje al contacto
    handoff_message = HANDOFF_MESSAGES.get(handoff_reason, HANDOFF_MESSAGES["insufficient_context"])

    contact_identifier = await _get_contact_identifier(client_id, contact_id, channel)
    provider = get_messaging_provider_for_channel(channel)
    channel_config = await _get_channel_config(client_id, channel)

    await provider.send_message(
        to=contact_identifier,
        content=MessageContent(text=handoff_message),
        channel_config=channel_config,
    )

    # 4. Notificar al agente humano (via cola notifications)
    from app.tasks.notifications import notify_handoff
    notify_handoff.delay(
        client_id=client_id,
        conversation_id=conversation_id,
        reason=handoff_reason,
    )

    return {
        "requires_handoff": True,
        "handoff_reason": handoff_reason,
        "response_text": handoff_message,
    }
```

### 10. Nodo training_mode_approval (`app/agents/nodes/training_approval.py`)

```python
async def training_approval_node(state: ConversationState) -> dict:
    """
    Nodo de modo entrenamiento.

    Si el tenant tiene training_mode habilitado:
    1. Guardar respuesta candidata en pending_responses (status: pending)
    2. NO enviar la respuesta al contacto
    3. Notificar al supervisor para revision
    4. Enviar mensaje al contacto indicando que la respuesta esta siendo revisada

    Si training_mode NO esta habilitado:
    Este nodo no deberia ejecutarse (el routing lo evita).
    """
    client_id = state["client_id"]
    conversation_id = state["conversation_id"]
    response_text = state.get("response_text", "")
    message_text = state["message"].get("text", "")
    channel = state["channel"]
    contact_id = state["contact_id"]

    async with tenant_session(UUID(client_id)) as session:
        # 1. Guardar respuesta pendiente
        pending = PendingResponse(
            client_id=UUID(client_id),
            conversation_id=UUID(conversation_id),
            question=message_text,
            suggested_answer=response_text,
            status="pending",
        )
        session.add(pending)
        await session.commit()

    # 2. Enviar mensaje al contacto
    waiting_message = "Tu consulta esta siendo procesada. Un agente revisara la respuesta en breve."

    contact_identifier = await _get_contact_identifier(client_id, contact_id, channel)
    provider = get_messaging_provider_for_channel(channel)
    channel_config = await _get_channel_config(client_id, channel)

    await provider.send_message(
        to=contact_identifier,
        content=MessageContent(text=waiting_message),
        channel_config=channel_config,
    )

    # 3. Notificar al supervisor
    from app.tasks.notifications import notify_pending_response
    notify_pending_response.delay(
        client_id=client_id,
        conversation_id=conversation_id,
        pending_response_id=str(pending.id),
    )

    return {
        "response_text": None,  # No se envio la respuesta real
        "training_mode": True,
    }
```

### 11. Task Celery para invocar el grafo (`app/tasks/ai_processor.py`)

```python
@shared_task(
    name="app.tasks.ai_process_response",
    bind=True,
    max_retries=2,
    queue="ai_inference",
    acks_late=True,
    time_limit=120,      # 2 minutos max
    soft_time_limit=100,
)
def process_ai_response(
    self,
    client_id: str,
    conversation_id: str,
    contact_id: str,
    channel: str,
    message_data: dict,
):
    """
    Invoca el grafo de LangGraph para procesar un mensaje.
    Cola: ai_inference (concurrencia 2)
    """
    import asyncio
    try:
        asyncio.run(_invoke_graph(
            client_id=client_id,
            conversation_id=conversation_id,
            contact_id=contact_id,
            channel=channel,
            message_data=message_data,
        ))
    except Exception as exc:
        logger.error(f"Error en grafo AI: {exc}", exc_info=True)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        # Si falla despues de retries, hacer handoff automatico
        asyncio.run(_emergency_handoff(client_id, conversation_id, contact_id, channel))


async def _invoke_graph(
    client_id: str,
    conversation_id: str,
    contact_id: str,
    channel: str,
    message_data: dict,
):
    """Ejecuta el grafo de conversacion."""
    compiled_graph = await get_graph_with_checkpointer()

    initial_state: ConversationState = {
        "client_id": client_id,
        "conversation_id": conversation_id,
        "contact_id": contact_id,
        "channel": channel,
        "message": message_data,
        "intent": None,
        "intent_confidence": None,
        "rag_context": None,
        "rag_confidence": None,
        "response_text": None,
        "budget_status": "ok",
        "model_to_use": "gpt-4o",
        "budget_usage_pct": 0.0,
        "requires_handoff": False,
        "handoff_reason": None,
        "training_mode": False,
        "approved_examples": None,
        "error": None,
    }

    config = {"configurable": {"thread_id": f"{client_id}:{conversation_id}"}}

    result = await compiled_graph.ainvoke(initial_state, config=config)
    logger.info(f"Grafo completado para {conversation_id}: intent={result.get('intent')}, "
                f"handoff={result.get('requires_handoff')}")


async def _emergency_handoff(client_id, conversation_id, contact_id, channel):
    """Handoff de emergencia cuando el grafo falla despues de retries."""
    logger.critical(f"Emergency handoff for conversation {conversation_id}")
    # Implementar handoff basico sin grafo
    ...
```

### 12. Cache del grafo por tenant

```python
from functools import lru_cache
from typing import Any

# Cache de grafos compilados por tenant
_graph_cache: dict[str, Any] = {}

async def get_compiled_graph(client_id: str):
    """
    Obtiene el grafo compilado para un tenant.

    El grafo se instancia una vez y se reutiliza.
    La configuracion del tenant se lee en cada invocacion (no se cachea en el grafo).
    """
    if client_id not in _graph_cache:
        _graph_cache[client_id] = await get_graph_with_checkpointer()
    return _graph_cache[client_id]
```

### 13. Tests

#### test_intent_routing.py

```python
@pytest.mark.asyncio
async def test_greeting_detected():
    """Mensaje de saludo se clasifica como 'greeting'."""
    state = make_state(message_text="Hola, buenos dias!")
    result = await intent_routing_node(state)
    assert result["intent"] == "greeting"

@pytest.mark.asyncio
async def test_human_request_detected():
    """Pedido de agente humano se clasifica como 'human_request'."""
    state = make_state(message_text="Quiero hablar con una persona real")
    result = await intent_routing_node(state)
    assert result["intent"] == "human_request"

@pytest.mark.asyncio
async def test_rag_query_detected():
    """Pregunta sobre el negocio se clasifica como 'rag_query'."""
    state = make_state(message_text="Cual es el horario de atencion?")
    result = await intent_routing_node(state)
    assert result["intent"] == "rag_query"

@pytest.mark.asyncio
async def test_disabled_agent_not_routed():
    """Intent no enruta a agente deshabilitado."""
    # Deshabilitar scheduling agent
    # Enviar mensaje de cita
    # Verificar que no enruta a scheduling
    ...
```

#### test_rag_node.py

```python
@pytest.mark.asyncio
async def test_rag_with_context_generates_response():
    """Con contexto suficiente, genera respuesta con citacion."""
    # Insertar chunks con alta similaridad
    state = make_state(message_text="Cual es el horario?")
    result = await rag_query_node(state)
    assert result["response_text"] is not None
    assert result["rag_confidence"] > 0.75
    assert not result["requires_handoff"]

@pytest.mark.asyncio
async def test_rag_without_context_triggers_handoff():
    """Sin contexto suficiente, marca para handoff."""
    state = make_state(message_text="Algo completamente irrelevante xyz")
    result = await rag_query_node(state)
    assert result["requires_handoff"] == True
    assert result["handoff_reason"] == "insufficient_context"

@pytest.mark.asyncio
async def test_few_shot_examples_injected():
    """Few-shot de approved_responses se inyectan en el prompt."""
    # Insertar approved_responses con alta similaridad
    state = make_state(message_text="Cual es el precio?")
    result = await rag_query_node(state)
    assert result.get("approved_examples") is not None
    assert len(result["approved_examples"]) > 0
```

#### test_token_budget.py

```python
@pytest.mark.asyncio
async def test_budget_ok():
    """Uso < 90% retorna status ok con modelo del tenant."""
    # Configurar budget: max=10000, used=5000
    state = make_state()
    result = await token_budget_check_node(state)
    assert result["budget_status"] == "ok"
    assert result["model_to_use"] != "gpt-4o-mini"

@pytest.mark.asyncio
async def test_budget_degraded():
    """Uso 90-99% retorna status degraded y cambia a gpt-4o-mini."""
    # Configurar budget: max=10000, used=9200
    state = make_state()
    result = await token_budget_check_node(state)
    assert result["budget_status"] == "degraded"
    assert result["model_to_use"] == "gpt-4o-mini"

@pytest.mark.asyncio
async def test_budget_exceeded():
    """Uso >= 100% retorna status exceeded y marca handoff."""
    # Configurar budget: max=10000, used=10500
    state = make_state()
    result = await token_budget_check_node(state)
    assert result["budget_status"] == "exceeded"
    assert result["requires_handoff"] == True
    assert result["handoff_reason"] == "budget_exceeded"
```

#### test_graph_flow.py (integracion)

```python
@pytest.mark.asyncio
async def test_full_rag_flow():
    """Flujo completo: mensaje → intent → RAG → respond."""
    # 1. Insertar documentos procesados con chunks
    # 2. Enviar mensaje via webhook
    # 3. Verificar que el grafo completo se ejecuto
    # 4. Verificar respuesta enviada al canal
    # 5. Verificar registro de tokens

@pytest.mark.asyncio
async def test_handoff_by_low_confidence():
    """Query sin contexto activa handoff."""
    # 1. Enviar mensaje sin contexto en knowledge base
    # 2. Verificar handoff activado
    # 3. Verificar conversation.status = 'waiting_human'

@pytest.mark.asyncio
async def test_handoff_by_budget():
    """Presupuesto agotado activa handoff."""
    # 1. Configurar budget usado al 100%
    # 2. Enviar mensaje
    # 3. Verificar handoff sin invocar LLM

@pytest.mark.asyncio
async def test_training_mode_retains_response():
    """Training mode guarda respuesta en pending sin enviar."""
    # 1. Habilitar training_mode en agent_configs
    # 2. Enviar mensaje con contexto suficiente
    # 3. Verificar pending_responses tiene la respuesta
    # 4. Verificar que la respuesta NO se envio al contacto

@pytest.mark.asyncio
async def test_state_persists_between_messages():
    """Estado del grafo persiste entre mensajes de la misma conversacion."""
    # 1. Enviar primer mensaje
    # 2. Enviar segundo mensaje en la misma conversacion
    # 3. Verificar que el checkpointer mantiene el estado
```

## Criterios de Aceptacion
- [ ] Mensaje de prueba recorre el grafo completo (token_budget → intent → RAG → respond)
- [ ] Intent routing clasifica correctamente: greeting, farewell, rag_query, human_request
- [ ] Solo se enruta a agentes habilitados del tenant (agent_configs.is_enabled)
- [ ] Strict grounding activa handoff cuando no hay contexto suficiente (rag_confidence < threshold)
- [ ] Token budget enforcement funciona en los 3 niveles: ok, degraded, exceeded
- [ ] Budget degraded (90-99%) cambia automaticamente a gpt-4o-mini
- [ ] Budget exceeded (>=100%) hace handoff sin gastar tokens
- [ ] Estado persiste entre mensajes de la misma conversacion (checkpointing)
- [ ] Training mode retiene respuesta en pending_responses sin enviar al contacto
- [ ] Few-shot examples de approved_responses se seleccionan por similaridad semantica (threshold 0.80)
- [ ] Citaciones incluidas en respuestas RAG
- [ ] Handoff genera nota interna con razon y metricas

## Notas Tecnicas

### PAT-003: Actualizacion parcial del estado
Cada nodo retorna un `dict` parcial con SOLO los campos que modifica. LangGraph mergea automaticamente el dict parcial con el estado completo existente. NUNCA mutar el estado directamente; siempre retornar un dict nuevo.

Ejemplo correcto:
```python
async def my_node(state: ConversationState) -> dict:
    return {"intent": "greeting", "intent_confidence": 0.95}
```

Ejemplo incorrecto:
```python
async def my_node(state: ConversationState) -> dict:
    state["intent"] = "greeting"  # NUNCA HACER ESTO
    return state
```

### Cache del grafo
El grafo se instancia una vez por tenant y se reutiliza. La configuracion del tenant (system_prompt, modelo, thresholds) se lee en tiempo de ejecucion dentro de cada nodo, no al instanciar el grafo. Esto permite cambiar la configuracion sin reiniciar.

### Modelo por nodo
- `intent_routing`: siempre usa `gpt-4o-mini` (tarea simple, costo bajo)
- `rag_query`: usa el modelo del tenant (o `gpt-4o-mini` si budget degradado)
- `respond`: no usa LLM directamente (solo envia la respuesta generada por rag_query)

### AsyncPostgresSaver
El checkpointer de LangGraph necesita su propia conexion a PostgreSQL (no pasa por pgBouncer). Usar `DATABASE_URL` directo, no `PGBOUNCER_URL`, para el checkpointer.

## Dependencias para Sprint 7
- Grafo funcional con todos los nodos base operativos
- Intent routing capaz de detectar intent "scheduling"
- MessagingProvider disponible para envio de respuestas
- TokenBudgetGuard registrando uso de tokens correctamente
- Checkpointing funcional para conversaciones multi-turno (necesario para scheduling agent)
