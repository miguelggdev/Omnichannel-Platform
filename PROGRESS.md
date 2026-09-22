# PROGRESS.md — Estado del Proyecto

> Cada sesión de Claude lee este archivo al inicio y lo actualiza al terminar.
> Es el "cerebro compartido" entre sesiones.

---

## Estado Actual

- **Fase:** 1 — MVP Core
- **Sprint 8 CERRADO:** Dev B mergeado en [PR #18](https://github.com/miguelggdev/Omnichannel-Platform/pull/18), Dev A mergeado en [PR #22](https://github.com/miguelggdev/Omnichannel-Platform/pull/22). Las 4 features de `specs/sprint-08-addendum-ops.md` siguen sin asignar a nadie en la matriz.
- **Sprint 7 CERRADO:** mergeado a `main` completo (Dev B en PR #16, Dev A en [PR #19](https://github.com/miguelggdev/Omnichannel-Platform/pull/19), BUG-016 en PR #17). Ya no aplican los 503 de ADR-038: `contact_unifier.py`, `calendar_tools.py`, `scheduling.py`, `agent_action_log.py` y sus migraciones (004/005) están en `main`.
- **BUG-025 CERRADO:** el login (`POST /api/v1/auth/login`) ya funciona contra un rol sujeto a RLS real (`app_user`, `NOBYPASSRLS`) — `public.auth_lookup_user()` (migración `007_auth_lookup_function`), función `SECURITY DEFINER` acotada a un email exacto. Ver ADR-046 y BUG-025 en MEMORY.md.
- **Última actualización:** 2026-09-21
- **Última sesión:** Sesión 30 — **Cierre del PR #28 de Dev B.** No se rebasó: su diff contra `main` ya era duplicado de #26/#30 y, tal cual, borraba 17 archivos de #27, #31 y #32 (`phone_unification.py`, `contact_request.py`, `transcription.py`, los scripts de la migración 009 y sus tests). Lo único que aportaba sobre lo mergeado eran sus tests de integración, y de esos solo faltaba la transcripción (entrada y reposición de Webchat ya están en `test_multichannel.py`): portado a la API actual en `tests/integration/test_audio_transcription.py` (PR #33) — el audio sin texto no llega al grafo, `_guardar_transcripcion()` escribe `content` sin perder `media_url` ni `message_type`, y un tenant no toca el mensaje de otro aunque tenga su UUID. **Verificado en local contra PostgreSQL real** (pgvector/pg16 en Docker, `app_user` con `NOBYPASSRLS`, migraciones hasta 009), replicando el job `test-integration` del CI.
- **Sesión 29** — **Cierre del Sprint 9** (PRs #25–#32). Mergeados: Telegram y Email (#25), Whisper (#26), unificación de contactos por teléfono verificado (#27, ADR-057), botón `request_contact` `/vincular` (#29, ADR-058), Webchat por WebSocket (#30, ADR-059) y el script de despliegue de la migración 009 (#31). En revisión: **#32** (cola `media` para Whisper, ADR-060) y de paso un fallo de fondo en `docker-compose.yml`: los workers que **envían** mensajes y la API no declaraban las credenciales de los canales, los secretos de webhook ni el Webchat (`test_compose_workers.py` lo vigila). **Coordinación:** el PR #28 de Dev B (Webchat + Whisper) duplica lo ya mergeado y está en conflicto; se le pidió rebasarlo dejando solo lo que aporta (la cola `media`, ya aplicada en #32, y sus tests e2e).
- **Sesión 27** — **Cierre de lo que dejó abierto la Sesión 26** (rama `fix/hardening-hallazgos-menores`): Redis (6379) y el dashboard de Traefik (8080) dejan de publicarse en `0.0.0.0` (BUG-033 ampliado; el test vigila cualquier puerto fuera de 80/443), y los hallazgos menores: BUG-035 (login enumerable por tiempo, y de paso `bcrypt` corría en el event loop), BUG-036 (`Conversation.subject` no se anonimizaba en RGPD ni en su rastro), BUG-037 (`{{date}}` siempre en UTC), BUG-038 (los workers de Celery no generaban spans SQL/Redis/httpx) y dos comentarios que decían algo falso. **Dos no se corrigieron, con razón:** `users.email` sin cifrar es una decisión pendiente (exige índice ciego por tenant y cambiar `auth_lookup_user()`) y el índice de `identifier_hash` ya está cubierto por el `UNIQUE`. **`ENCRYPTION_KEY` / ventana de la migración 009:** es operativo y no lo ejecuta el código; queda el runbook en ADR-052 (el orden es parar → migrar → arrancar, porque código nuevo con hashes viejos duplica contactos) y un test de que 008/009 abortan antes de ejecutar nada sin la clave.
- **Sesión 26** — **Revisión de bugs post-Sprint 8 y corrección de los hallazgos de impacto alto y medio** (rama `fix/sprint-08-hallazgos-auditoria`, 12 commits atómicos). 4 agentes en paralelo (RLS/multi-tenancy, async/concurrencia, seguridad, lógica de negocio) sobre lo recién mergeado: 19 hallazgos reales, varios confirmados por más de un agente. **Alto:** BUG-027 (`echo=True` filtraba `ENCRYPTION_KEY` y datos cifrados por stdout), BUG-028 (el borrado RGPD dejaba el dato personal completo en `audit_logs`), BUG-029 (un `sender_identifier` vacío mezclaba conversaciones de clientes finales distintos). **Medio:** BUG-030 (`session.get()` sin `client_id` en 9 sitios; `ContactUnifier.merge()` con defensa circular y sin verificar el destino), BUG-031 (3 entradas de `beat_schedule` a tareas inexistentes), BUG-032 (DLQ contada como éxito, scrape bloqueante, sinks de Loguru síncronos + `force=True` post-fork en Celery), BUG-033 (Jaeger/Prometheus/Grafana en `0.0.0.0`), BUG-034 (mismo `display_name` para teléfonos con los mismos últimos 4 dígitos) y ADR-052 (el índice ciego pasa a ser por tenant, migración `009_blind_index_per_tenant`). **Un hallazgo no aplicaba** (`mark_process_dead`: no hay `Gauge` en el catálogo) y no se implementó. Los menores quedan listados en MEMORY.md. Verificado en local: 720 unitarios passed (1 skipped), `ruff` y `mypy` sin errores nuevos; los tests de integración nuevos (RGPD/audit_logs, índice por tenant, paridad SQL/Python de la migración 009) **los valida el CI real**, no se corrieron local (sin Docker).
- **Sesión 25** — **Cierre del hallazgo del teléfono en claro y merge de [PR #22](https://github.com/miguelggdev/Omnichannel-Platform/pull/22).** Decisión del usuario sobre las tres opciones que dejó abiertas la Sesión 24: opción (a), enmascarar. `app/core/encryption.py::mask_identifier()` (últimos 4 caracteres visibles, resto `*`, mismo largo que el original) y `_resolve_contact()` lo usa al crear el `display_name` en vez del identificador completo. Tests nuevos en `test_encryption.py` (`TestMascara`) y `test_webhook_processor.py` actualizado. Revisión completa del PR antes de este cambio: código de cifrado, migración `008_encrypt_contact_identifiers`, los tres consumidores de `identifier_value` (listener del modelo, `_resolve_contact()`, anonimización RGPD) y BUG-026 (`docker-compose.yml` sin `JWT_SECRET`/`ENCRYPTION_KEY` en los 7 servicios de Celery), todo verificado correcto. CI real antes del cambio: 686 unitarios/123 integración passed, 8 jobs verdes.
- **Sesión 24** — **Entrega de Dev A del Sprint 8** (rama `feature/sprint-08-observability`): OpenTelemetry, logging estructurado con Loguru, métricas Prometheus, los 4 dashboards de Grafana, 13 reglas de alerta y el cifrado de columnas con pgcrypto. Cinco decisiones registradas (ADR-047 a ADR-051), **BUG-026 encontrado y cerrado** (ningún worker de Celery podía arrancar con el `docker-compose.yml` del repo: le faltaban `JWT_SECRET` y, a tres de ellos, `ENCRYPTION_KEY`, ambas obligatorias en `Settings`), **BUG-003 cerrado** y un hallazgo abierto a decisión del usuario (el teléfono sigue en claro en `contacts.display_name`). Cuatro defectos del spec corregidos, el más grave el de §8.4: da por determinista un cifrado que no lo es, y sobre esa premisa la búsqueda de contactos del webhook habría dejado de encontrar a nadie y el `UNIQUE` de identificadores habría dejado de proteger. 47 tests unitarios nuevos (670 en total) y 6 de integración.
- **Sesión 23** — **Fix de BUG-025 (rama `fix/bug-017-login-rls`, ya la había empezado Dev B con la numeración vieja) mergeado a `feature/sprint-08-ops` y luego a `main`.** La rama forkeó antes del merge de renumeración de la Sesión 22, así que volvió a chocar con `main`: su migración `004_audit_gdpr_quick_replies`/`005_auth_lookup_function` se rechaza contra la ya canónica `006_audit_gdpr_quick_replies` — se renombra a `007_auth_lookup_function`, encadenada tras `006`. Su ADR-043 (función SECURITY DEFINER) pasa a ADR-046 (ADR-043/044/045 ya estaban tomados por la renumeración de Dev B). Revisión de la solución: función `STABLE SECURITY DEFINER` con `SET search_path` fijo (evita el hijack clásico de schema en SECURITY DEFINER), acotada a una sola fila por email exacto — no lista, no acepta patrones —, y con una migración que **aborta el despliegue** si el rol que la crea no tiene `BYPASSRLS`/superusuario, en vez de fallar en silencio devolviendo cero filas. Verificado en CI real: `107 passed, 6 skipped, 1 xfailed` en el job de integración de PR #18 antes de este fix (el xfail era justamente `TestLoginBajoRls`); tras aplicar este fix el mismo test pasa a passed y `TestFuncionDeBusqueda` cubre la superficie de la función.
- **Sesión 22** — **Resolución del conflicto de merge entre el Sprint 7 (Dev A, PR #19, ya mergeado) y el Sprint 8 (Dev B, PR #18, rama `feature/sprint-08-ops`)**: ambas ramas partieron del mismo punto de `main` y usaron los mismos números — migración `004`, ADR-040/041/042, BUG-017 — para cosas distintas. Se renumeró la mitad de Dev B: su migración de auditoría/RGPD/quick-replies pasa de `004_audit_gdpr_quick_replies.py` a `006_...` (encadenada tras la `005_agent_action_logs` de Sprint 7), sus tres ADR pasan a ADR-043/044/045, y su hallazgo del login pasa a BUG-025 (el BUG-017 real, de Sprint 7, ya estaba en `main`). `app/core/database.py` fue el único archivo de código con cambios de ambos lados (mi `run_isolated()` cierra Redis, la de Dev B agrega el `ContextVar` de auditoría) y mergeó limpio — están en funciones distintas del mismo archivo. PR #18 mergeado con `--squash --delete-branch` tras verificar CI real: 107 passed/6 skipped/1 xfailed.
- **Sesión 21** — **Revisión de bugs pedida por el usuario sobre todo el proyecto** (no solo Sprint 7): 4 agentes en paralelo (RLS/multi-tenancy, async/concurrencia, seguridad, lógica del grafo), 8 hallazgos reales confirmados, **los 8 corregidos**. De código de Sprint 7 (sesión 20, mismo PR): BUG-017 (traceback expuesto vía `/agent-logs`), BUG-018 (`contact_id` sin fuente legítima para el LLM), BUG-019 (datetimes naive en `TIMESTAMPTZ`), BUG-020 (defensa en profundidad RLS en `calendar_tools.py`/`ContactUnifier.merge()`). De código preexistente (Sprint 3-6, a pedido explícito del usuario tras el primer reporte): BUG-021 (cliente Redis cruzando event loops en Celery, `run_isolated()` ahora también cierra Redis), BUG-022 (race condition en `TokenBudgetGuard.record_usage`, ahora un `UPDATE` atómico), BUG-023 (doble reserva de citas, ahora recheck + `pg_advisory_xact_lock` por tenant), BUG-024 (ejecución parcial de tool calls sin dejar constancia, ahora `partial_results` en el handoff). Detalle completo, con la razón de cada fix, en MEMORY.md. PR #19 mergeado con estos 8 fixes incluidos.
- **Sesión 20** — **Entrega de Dev A del Sprint 7** (branch `feature/sprint-07-scheduling`, [PR #19](https://github.com/miguelggdev/Omnichannel-Platform/pull/19), mergeado). Siete checkpoints, cada uno con tests propios y commit atómico:
  1. `migrations/versions/004_service_types.py` + `app/models/service_type.py` (`ServiceType`, `Appointment`) — client_id + FORCE RLS, sin `ondelete` en las FK (misma convención que `contact_tags`/`document_chunks`), `appointments.status` como `VARCHAR` en vez de un tipo ENUM de Postgres.
  2. `app/services/calendar.py` (`GoogleCalendarService`) — desviación deliberada sobre el spec: en vez de OAuth2 con refresh token por tenant cifrado con pgcrypto (infra de Sprint 8 que no existe todavía), un único **service account** global (`GOOGLE_CALENDAR_CREDENTIALS_JSON`, como ya preveía `.env.example`) cuyo email cada tenant comparte con su propio calendario; `calendar_id`/`timezone` por tenant sin cifrar en `agent_configs.config.scheduling`. Todas las llamadas a `google-api-python-client` (síncrono) pasan por `asyncio.to_thread()`.
  3. `app/agents/tools/calendar_tools.py` — las 5 tools LangChain. `config: RunnableConfig` (no `dict`) para que LangChain excluya `client_id` del schema que ve el LLM (verificado con un test que recorre todas las tools); `@tool(parse_docstring=True)` en vez de los `*Input` `BaseModel` del spec, que se declaraban pero nunca se conectaban a ninguna tool.
  4. `app/agents/nodes/scheduling.py` + integración en `graph.py` — fix sobre el spec: `tool_fn.ainvoke(args, config=tool_config)` con `config` como parámetro real de `.ainvoke()`, no metido dentro del dict de argumentos (esa forma deja el `RunnableConfig` vacío; confirmado con un smoke test). Nuevo nodo `scheduling` con su propio routing (`route_after_scheduling`); ya no cae a `rag_query` como placeholder de Sprint 6.
  5. `app/services/contact_unifier.py` (`ContactUnifier.merge()`) — solo `merge()`, no el `resolve_contact()` del spec (esa resolución automática ya vive en `webhook_processor._resolve_contact()` desde Sprint 4, probada, con su propia protección de ciclos). Reemplazó en `test_contacts.py` el test del degradado 503 (premisa caducada) por uno del camino feliz end-to-end.
  6. Addendum de Agent Activity Logging: `app/models/agent_action_log.py`, `migrations/versions/005_agent_action_logs.py`, `app/services/agent_logger.py`, `app/agents/middleware/logging_middleware.py`. El middleware se reescribió contra la arquitectura real (`tenant_session()` propia por nodo, no una sesión inyectada en el estado) y es best-effort: si falla escribir el log, se registra y se descarta sin tumbar la respuesta al contacto. Cada nodo se envuelve con `logged_node()` al registrarse en `graph.py` (un único punto de integración, no seis módulos tocados). Sacó `TestDegradacion` de `test_agent_logging.py` (premisa caducada, igual que ContactUnifier).
- **Sesión 20 (Dev B, en paralelo)** — **Entrega de Dev B del Sprint 8** (branch `feature/sprint-08-ops`, [PR #18](https://github.com/miguelggdev/Omnichannel-Platform/pull/18)): rastro de auditoría por trigger de PostgreSQL, endpoints de RGPD, CRUD de respuestas rápidas con variables, backup diario y prueba de restauración mensual, y el test end-to-end del recorrido completo. Verificado en CI real: **554 unitarios passed** (cobertura 87.37%) y **107 de integración/e2e passed, 1 xfailed**. Tres decisiones registradas (ADR-043/044/045 tras la renumeración) y **BUG-025 encontrado y confirmado contra Postgres real: el login no funciona contra un rol sujeto a RLS** — crítico, abierto, decisión de arquitectura pendiente. Cuatro defectos de la spec corregidos, entre ellos el nombre de las tareas de mantenimiento (habrían bloqueado la cola de webhooks durante cada backup) y `pg_restore` sin `--exit-on-error`, que daba por bueno un backup a medias.
- **Sesión 19** — **Mergeados a `main`: PR #16 (Sprint 7 Dev B) y PR #17 (BUG-016).** Revisión completa del PR #16 sin hallazgos propios (ver Sesión 18). De esa revisión salió BUG-016: el bot seguía respondiendo con el grafo de IA a conversaciones ya escaladas a un humano (`human_active`/`waiting_human`), porque `webhook_processor.py` reutiliza cualquier conversación no cerrada y nunca miraba el status antes de encolar `ai_processor.py`. No lo introdujo el PR #16 — es un hueco de Sprint 6 que la nueva máquina de estados de `ConversationLifecycle` dejó en evidencia. Corregido en rama aparte (`fix/bot-no-responde-tras-handoff`) con `HUMAN_OWNED_STATUSES` como corte y 5 tests nuevos ([PR #17](https://github.com/miguelggdev/Omnichannel-Platform/pull/17), CI verificado en logs reales: 285 unitarios passed/1 skipped, 57 integración passed/6 skipped). Ambos PRs mergeados con `--squash --delete-branch`, ramas remotas y locales limpias. **Próximo paso: arranca la entrega de Dev A del Sprint 7** (`calendar_tools.py`, `scheduling.py`, `contact_unifier.py`, migración de `service_types`, y la mitad de Dev A del addendum de Agent Activity Logging) — el contrato exacto de `ContactUnifier(session).merge(source_id=..., target_id=...)` y de las columnas de `AgentActionLog` ya quedó fijado por el código de Dev B (`contacts.py`, `agent_logs.py`, `schemas/agent_log.py`, `test_agent_logging.py`).
- **Sesión 18** — **Entrega de Dev B del Sprint 7** (branch `feature/sprint-07-crm`, [PR #16](https://github.com/miguelggdev/Omnichannel-Platform/pull/16)): la API del CRM completa (contactos, conversaciones, etiquetas, notas internas), la máquina de estados del ciclo de vida, el worker de auto-cierre de Celery Beat y los endpoints de consulta del addendum de Agent Activity Logging. 20 endpoints nuevos, 163 tests unitarios y 26 de integración contra Postgres real con RLS. Tres decisiones registradas: el auto-cierre itera tenant por tenant en vez de usar un rol `BYPASSRLS` (ADR-037), los endpoints que esperan entregas de Dev A degradan con 503 en vez de tumbar el arranque de la API (ADR-038), y `waiting_human -> resolved` se agrega a la máquina de estados porque sin esa transición un handoff que nadie atiende no se puede cerrar nunca (ADR-039). De paso, dos defectos de la spec que habrían pasado a producción: el nombre de la tarea de auto-cierre no coincidía con el que `beat_schedule` declara desde Sprint 2 (habría quedado sin Beat y sin cola) y `previous_status` se leía después de transicionar, devolviendo el estado nuevo en los dos campos.
- **Sesión 17** — Revisión de bugs pedida por el usuario al cerrar el Sprint 6. Dos hallazgos, ambos cerrados ([PR #15](https://github.com/miguelggdev/Omnichannel-Platform/pull/15)):
  - **BUG-015** — `_tenant.py::_as_agents()` trataba `config.enabled_agents: []` (deshabilitar todos los agentes a propósito) igual que "no configurado", y caía al default (`["rag"]`). Ahora distingue ausente/tipo inválido (default) de lista vacía real (se respeta). Se agregó `tests/unit/test_tenant_settings.py`, cobertura que no existía.
  - **Corrección de ADR-036** — `graph.py` prefería `DATABASE_URL_DIRECT` para el checkpointer, pero `docker-compose.yml` no le pasa esa variable a ningún worker de Celery: era código muerto. Simplificado a usar siempre `DATABASE_URL` (el pooler), que además es la elección correcta para un pool que se abre y cierra en cada mensaje.
  - Verificado en CI real: **280 unitarios passed, 1 skipped** y **57 passed, 6 skipped** en integración.
- **Sesión 16** — **Entrega de Dev A del Sprint 6** ([PR #14](https://github.com/miguelggdev/Omnichannel-Platform/pull/14)): `app/agents/state.py` y `app/agents/graph.py` (`build_conversation_graph()`, routing condicional, `get_graph_with_checkpointer()`). Sprint 6 completo (Dev A + Dev B). Dos bugs encontrados y arreglados de paso, fuera de la entrega propia: **BUG-014** (`ai_processor` nunca se agregó a `TASK_MODULES`, el worker de `ai_inference` no conocía la tarea) y el `min_size`/`max_size` del pool de `psycopg` del checkpointer (encontrado en CI real). Dos hallazgos de infraestructura que no estaban en la spec: `requirements.txt` no declaraba `psycopg[binary,pool]` (sin eso, `psycopg` v3 no se puede importar sin `libpq` del sistema), y `checkpointer.setup()` no se puede llamar en runtime porque hace `CREATE TABLE` y el rol de la app solo tiene DML — las tablas del checkpointer se crean en `migrations/versions/003_langgraph_checkpoints.py`. Ver MEMORY.md (ADR-036, BUG-014). Verificado en CI real, no solo checkmarks: **273 unitarios passed, 1 skipped** y **57 passed, 6 skipped** en integración, incluyendo un test nuevo que corre `ainvoke()` completo con el checkpointer contra Postgres real.
- **Sesión 15** — **Entrega de Dev B del Sprint 6** (branch `feature/sprint-06-nodes`): los 6 nodos del grafo, `TokenBudgetGuard` completo y el worker `ai_processor`. 74 tests unitarios nuevos y 8 de integración contra Postgres real con RLS ([PR #13](https://github.com/miguelggdev/Omnichannel-Platform/pull/13)). Verificado en CI leyendo el log, no el checkmark: **253 unitarios passed** y **56 passed / 6 skipped** en integración con el rol `app_user`. De paso salió un efecto que no estaba previsto: al dejar de ser un stub, `_enqueue_ai_processing()` hacía que los 8 tests de `test_webhook_flow.py` se colgaran contra el broker de Celery, que el job de integración no levanta. Falta la entrega de Dev A (`app/agents/state.py`, `app/agents/graph.py`).
- **Sesión 14** — Revisión general de bugs sobre `main` post-Sprint 5 (pedida por el usuario). Encontrados y arreglados los cuatro hallazgos:
  - **BUG-010** — `RAGService` rompía contra Postgres real por falta de cast `::vector` ([PR #11](https://github.com/miguelggdev/Omnichannel-Platform/pull/11), verificado en CI real con 5 tests de integración nuevos: 46 passed, 6 skipped).
  - **BUG-011** — el engine async de `app/core/database.py` (singleton de módulo) se reusaba entre `asyncio.run()` de cada tarea de Celery, mismo root cause que BUG-006 pero sin mitigar en producción ([PR #12](https://github.com/miguelggdev/Omnichannel-Platform/pull/12), `run_isolated()` nuevo).
  - **BUG-012** — `DocumentPipeline` marcaba `completed` con `chunk_count=0` cuando OCR/extracción no producía texto ([PR #12](https://github.com/miguelggdev/Omnichannel-Platform/pull/12), `EmptyDocumentError` nuevo). Al escribir el test se destapó un bug relacionado sin arreglar: `_TEXT_EXTRACTORS` no soporta `"txt"` pese a que `ALLOWED_TYPES` lo acepta en la subida — todo `.txt` falla hoy con "tipo no soportado". Ver MEMORY.md, nota en BUG-012.
  - Ver MEMORY.md (BUG-010/011/012) para el detalle de cada uno. Verificado en CI real (Postgres), no solo checkmarks: PR #12 dejó la suite de integración en 48 passed, 6 skipped.
  - Una sospecha inicial de bug en el pin de `redis` (mypy fallaba en un venv local sin `types-redis`) se descartó: es un falso positivo, CI ya instala `types-redis` aparte y con eso mypy queda limpio.
- **Sesión 13** — **Entrega de Dev A del Sprint 5** ([PR #10](https://github.com/miguelggdev/Omnichannel-Platform/pull/10)): `app/services/{chunker,embedding,ocr,document_pipeline,rag}.py`. Sprint 5 completo (Dev A + Dev B). Verificado en CI real: 41 passed contra el rol `app_user` (no-superusuario, `NOBYPASSRLS`) — confirma que la RLS de BUG-009 sigue genuina en este PR también. 173 tests unitarios, `ruff`/`mypy` limpios.
- **Sesión 12** —
  1. Revisión y fix de [PR #5](https://github.com/miguelggdev/Omnichannel-Platform/pull/5) (Sprint 4, Dev B): bug de `channel` en `_enqueue_ai_processing`, `retry_backoff` sin efecto, env vars faltantes en `docker-compose.yml`.
  2. BUG-005 / issue [#6](https://github.com/miguelggdev/Omnichannel-Platform/issues/6) resuelto de raíz: migración `002_rls_policies.py` con RLS en las 18 tablas, CI corregido para sembrar el schema vía `alembic upgrade head` (no `init.sql`) y correr `tests/integration/` con `--run-db` (antes se saltaba entero, silenciosamente, desde Sprint 1). De paso salieron a la luz y se arreglaron 4 bugs más que ningún test había ejecutado nunca contra Postgres real: `SET LOCAL` con bind params, `db_engine` de test con scope de sesión vs. event loop por test, casts `::vector` pegados a un bind param, y doble consumo de un `Result` de SQLAlchemy.
  3. [PR #4](https://github.com/miguelggdev/Omnichannel-Platform/pull/4) (deuda de lint) revisado y mergeado.
  4. PR #5 actualizado contra `main` y verificado: 2 bugs más en `test_webhook_flow.py` (mismo patrón de `clients.max_agents` inexistente, y el engine singleton de `app.core.database` reusado entre tests con loops distintos). PR #4 y #5 mergeados.
  5. **Entrega de Dev A del Sprint 4** ([PR #8](https://github.com/miguelggdev/Omnichannel-Platform/pull/8)): `app/services/messaging/` (ABC, `YCloudProvider`, `MetaProvider`, factory) y `NormalizedMessage`. Sprint 4 completo.
  6. **Corrección importante (ver BUG-009 en MEMORY.md):** lo que en el punto 2 llamé "RLS real activado" en CI **era un falso positivo**. Dev B encontró, en [PR #9](https://github.com/miguelggdev/Omnichannel-Platform/pull/9) (Sprint 5), que el rol de Postgres en CI era superusuario (bypassa RLS siempre, incluso con `FORCE`) y que además `assert_rls_isolation()` estaba probando aislamiento MVCC, no RLS, desde Sprint 1 — los dos defectos se enmascaraban entre sí. Revisé el fix a fondo (diff, logs reales de CI, verificación local) antes de mergear: `ci.yml` ahora crea un rol `app_user` no-superusuario para correr la suite, y el helper de aislamiento cambia el tenant activo dentro de la misma transacción no commiteada en vez de cruzar conexiones. CI real: 41 passed (antes 20, con el falso positivo). **Esta vez sí es una verificación genuina de RLS.**
  7. Revisado y mergeado [PR #9](https://github.com/miguelggdev/Omnichannel-Platform/pull/9) (Sprint 5, Dev B): CRUD de documentos, Supabase Storage, worker de ingesta, BUG-007 (fixture de auth roto desde Sprint 3) y BUG-008 (`created_at` sin refresh en async). 156 tests unitarios + 41 de integración, `ruff`/`mypy` limpios. Falta la entrega de Dev A (`document_pipeline`, `chunker`, `embedding`, `ocr`, `rag`).

---

## Sprint 1: Schema DDL & Arquitectura

> **Corregido 2026-09-10 (BUG-005, issue #6):** la migración baseline de Alembic
> (`migrations/versions/001_baseline.py`) no habilitaba RLS en ninguna tabla —
> las líneas de abajo que dan por hecho "RLS aislamiento verificado" se referían
> únicamente a `supabase/init/init.sql`, que ADR-020 dejó de ejecutar contra
> Supabase Cloud. Una base creada solo con Alembic quedaba sin aislamiento entre
> tenants. Fix: `migrations/versions/002_rls_policies.py` agrega
> ENABLE/FORCE/CREATE POLICY a las 18 tablas reales, y `.github/workflows/ci.yml`
> ahora siembra el schema de test vía `alembic upgrade head` (no `init.sql`) y
> corre `tests/integration/` con `--run-db` (antes se saltaba entero, sin que CI
> avisara). `tests/unit/test_rls_isolation.py` se eliminó: sus 9 tests tenían
> `@pytest.mark.skip` individual desde Sprint 1 ("activar cuando init.sql esté
> ejecutado", algo que su propio `--run-db` nunca disparaba) y probaban columnas
> que ya no existen (`contacts.phone_number`, `users.full_name`); quedó
> completamente superado por `tests/integration/test_rls_all_tables.py`.

### Completado
- [x] Diseño de arquitectura general (SDD publicado como artifact)
- [x] Definición de 24 tablas (18 MVP + 6 Fase 2)
- [x] Definición de patrones RLS con SET LOCAL
- [x] Especificaciones detalladas en `specs/sprint-01-schema.md`
- [x] DDL ejecutable completo (`supabase/init/init.sql`) — 520+ líneas
- [x] Extensiones: pgvector, pgcrypto, uuid-ossp
- [x] 18 tablas MVP con constraints, FKs, ENUMs
- [x] 6 tablas Fase 2 en archivo separado (`supabase/init/phase2_tables.sql`)
- [x] Políticas RLS con FORCE en las 24 tablas (patrón `current_setting('app.current_client_id')::uuid`)
- [x] Índices: 44 B-tree, 8 GIN (JSONB), 2 HNSW (embeddings m=16 ef=200)
- [x] Roles app_user y app_admin con ALTER DEFAULT PRIVILEGES
- [x] Funciones helper: update_updated_at_column(), encrypt_identifier(), decrypt_identifier()
- [x] DDL verificado contra PostgreSQL 16 + pgvector 0.6.0 — ejecución sin errores
- [x] Idempotencia verificada (segunda ejecución sin errores)
- [x] RLS aislamiento verificado con rol no-superuser (test_app_user)
- [x] Test de aislamiento RLS en Python (`tests/unit/test_rls_isolation.py`) — 9 tests (skip hasta Sprint 2 DB)
- [x] Contract file Sprint 1 (`contracts/sprint-01.json`)
- [x] Harness Engineering completo: hooks, smoke-test, contracts, loop-failure template

### En Progreso
_(nada en progreso)_

### Pendiente
- [ ] Diagrama Mermaid de arquitectura en repo
- [ ] Commit inicial y push a GitHub (usuario debe ejecutar manualmente)
- [ ] Crear rama `feature/sprint-01-ddl` desde `main`

### Bloqueadores
- Push a GitHub requiere ejecución manual desde terminal del usuario (credenciales no disponibles en sesión cloud)

### Infraestructura CI/CD & QA (Sesión 6)
- [x] `.pre-commit-config.yaml` — ruff, mypy, detect-secrets, sqlfluff, commitizen
- [x] `.github/workflows/ci.yml` — 8 stages: lint → typecheck → test-unit → test-integration → migration-check → security → docker-build → frontend
- [x] `tests/conftest.py` — Fixtures expandidas con rls_harness, tenant sessions, API client, TestDataFactory
- [x] `tests/integration/test_rls_all_tables.py` — 25 tests de aislamiento RLS (todas las tablas)
- [x] `grafana/dashboards/token-budget-monitoring.json` — 10 paneles: tokens/tenant, tokens/nodo, costos, eficiencia, alertas
- [x] `pyproject.toml` — Actualizado: reglas de seguridad, coverage, commitizen, bandit
- [x] `.secrets.baseline` — Baseline para detect-secrets
- [x] `docs/dev-playbook.html` — Dev Playbook con 8 agentes + 6 roles secundarios

### Notas para la Próxima Sesión
- Recordar el bug del alias SQL en WHERE: usar `1 - (embedding <=> :query_embedding) > :threshold` en lugar de `similarity > :threshold`
- Superusers bypasean RLS incluso con FORCE — siempre testear con rol app_user
- `conversations` tiene los 7 estados: new, active, waiting, resolved, escalated, bot, snoozed
- Sprint 2 (Docker) puede comenzar inmediatamente

---

## Sprint 2: Infraestructura Docker

> **Corregido 2026-09-09:** este apartado describía 16 servicios con `supabase-db`,
> `supabase-auth`, `supabase-storage`, `supabase-realtime` y `pgbouncer`, y notas con
> `PGBOUNCER_URL`. Eso quedó obsoleto con ADR-020 (Supabase Cloud). El
> `docker-compose.yml` real en `main` ya tenía 12 servicios desde el commit `33f62eb`;
> era este archivo el que estaba desactualizado.

### Completado — Dev A
- [x] `docker-compose.yml` — 12 servicios orquestados con health checks (sin Postgres/Auth/Storage: ADR-020)
- [x] `Dockerfile` — Multi-stage (builder + runner) con tesseract-ocr, usuario no-root
- [x] `app/tasks/celery_config.py` — 6 colas (webhooks, ai_inference, documents, notifications, bulk, lead_enrichment) + beat schedule
- [x] `.dockerignore` — 45 reglas de exclusion del contexto Docker
- [x] `.env.example` — `DATABASE_URL` (pooler Supavisor 6543) + `DATABASE_URL_DIRECT` (migraciones Alembic)

### Completado — Dev B (branch `feature/sprint-02-traefik`, sesión 9)
- [x] `traefik/traefik.yml` — Configuracion estatica Traefik v3 (entrypoints, providers, logging)
- [x] `traefik/traefik.yml` — `ping` habilitado: sin él el healthcheck `traefik healthcheck` de compose nunca pasaba a `healthy` (ADR-027)
- [x] `traefik/traefik.yml` — `metrics.prometheus` sobre el entryPoint interno `traefik` (8080)
- [x] `traefik/dynamic/middlewares.yml` — Rate limiting, security headers, compresion, CORS
- [x] `traefik/dynamic/tls.yml` — Certificados autofirmados para desarrollo
- [x] `prometheus/prometheus.yml` — Jobs `prometheus`, `traefik`, `api`. El archivo faltaba y compose ya lo montaba, así que el contenedor no arrancaba
- [x] `grafana/provisioning/datasources/prometheus.yml` — Datasource Prometheus (`uid: prometheus`)
- [x] `grafana/provisioning/dashboards/dashboards.yml` — Provider de dashboards tipo file
- [x] `docker-compose.yml` — Montaje de Grafana corregido para que el provisioning se aplique (archivo de Dev A, tocado con autorización explícita y aislado en el commit `005dfb6` — **revisar en el PR**)
- [x] `scripts/wait-for-it.sh` — Script de espera TCP para dependencias
- [x] `scripts/healthcheck.sh` — Verifica los 12 servicios, espera hasta 120s, exit 0/1/2/3

### Servicios Docker (12) — ADR-020
1. traefik (API Gateway v3)
2. api (FastAPI x2 replicas)
3. redis (Cache + Broker, 3 DBs separadas)
4. celery-webhooks (c=4)
5. celery-ai (c=2)
6. celery-documents (c=2)
7. celery-notifications (c=2)
8. celery-bulk (c=1)
9. celery-lead-enrichment (c=2, ADR-022)
10. celery-beat (Scheduler)
11. prometheus (Metricas)
12. grafana (Dashboards)

> PostgreSQL, Auth, Storage y Realtime los provee **Supabase Cloud**. El pooling lo
> maneja Supavisor (puerto 6543). No hay `pgbouncer` ni contenedores `supabase-*`.

### Pendiente
- [ ] Validacion funcional con `docker compose up -d` (requiere entorno Docker del usuario) — `scripts/healthcheck.sh` automatiza la verificación
- [ ] Verificar `SET LOCAL` contra el Transaction Pooler de Supabase Cloud (sustituye a la vieja verificación de `SHOW pools` de pgBouncer)
- [ ] Correr la migración inicial de Alembic contra `DATABASE_URL_DIRECT` (Dev A) — sustituye a la vieja verificación de `init.sql`
- [x] BUG-003: `grafana/dashboards/token-budget-monitoring.json` apuntaba al datasource `supabase-db`, inexistente con ADR-020 — **cerrado en la sesión 24**: el dashboard se retira y lo reemplaza `tenant_usage.json`, que cubre lo mismo leyendo de Prometheus

### Bloqueadores
- **`mypy` no está instalado en el entorno** (`No module named mypy` en todos los intérpretes). El Gate 2 pre-PR de METHODOLOGY §7 lo exige. Solución: `pip install mypy`
- Deuda de lint preexistente en `main`: `ruff check .` reporta 8 errores en `tests/conftest.py` (UP035, 3×SIM117) y `tests/integration/test_rls_all_tables.py` (4×S608). No los introduce Sprint 2; el Gate 1 (`ruff check app/`) está verde

### Notas para la Proxima Sesion
- celery-beat no tiene healthcheck (es scheduler, no endpoint): `scripts/healthcheck.sh` lo evalúa como `running`, no como `healthy`
- Redis separado en 3 DBs: 0=broker, 1=results, 2=cache
- Traefik redirecciona HTTP→HTTPS automaticamente
- Workers Celery y API usan `DATABASE_URL` (pooler Supavisor). `PGBOUNCER_URL` ya no existe
- El puerto 8080 de Traefik sirve dashboard, `/ping` y `/metrics`: cerrarlo al exterior en el hardening del Sprint 8 (ADR-027)
- `accessLog` de Traefik va a stdout, desviación consciente de la spec justificada en ADR-026
- Los tests marcados `db` (34) siguen en `skipped` hasta tener la BD accesible con `--run-db`
- Sprint 3 (FastAPI Core & Auth) completado — PR #2 mergeado
- Sprint 4 (Webhook Receiver & MessagingProvider) puede comenzar inmediatamente

---

## Sprint 3: FastAPI Core & Auth

### Completado — Dev A (PR #2, 61 archivos, +3460 líneas)
- [x] `app/main.py` — App factory `create_app()` con lifespan, CORS, exception handlers
- [x] `app/core/config.py` — `Settings` (Pydantic BaseSettings) con validación de JWT_SECRET y ENCRYPTION_KEY
- [x] `app/core/database.py` — AsyncEngine + async sessionmaker con pool pre-ping
- [x] `app/core/security.py` — JWT encode/decode, password hashing (bcrypt), token creation
- [x] `app/core/dependencies.py` — `get_db`, `get_current_user`, `get_current_active_user`
- [x] `app/middleware/tenant_context.py` — `TenantContextMiddleware` con `SET LOCAL app.current_client_id`
- [x] `app/middleware/logging_middleware.py` — Request/response logging con correlation ID
- [x] `app/models/` — SQLAlchemy 2.0 models: User, Client, Conversation, Message, Contact, KnowledgeDocument, etc.
- [x] `app/schemas/` — Pydantic v2 schemas para auth, users, tenants, health
- [x] `app/api/v1/auth.py` — Login, register, refresh, me endpoints
- [x] `app/api/v1/tenants.py` — CRUD tenants (admin)
- [x] `app/api/v1/health.py` — Health check endpoint
- [x] `app/api/deps.py` — Dependency injection helpers
- [x] `migrations/env.py` — Alembic async config con `run_async_migrations()`
- [x] `migrations/versions/001_initial_schema.py` — Initial migration (all MVP tables)
- [x] `tests/unit/` — 27 tests pasando (auth, middleware, health, config, schemas)
- [x] `.github/workflows/ci.yml` — CI pipeline 8 stages, todas pasando green
- [x] Docker Build smoke test con env vars dummy

### Notas para la Próxima Sesión
- `get_settings()` se ejecuta al importar `app/main.py` — requiere env vars incluso para smoke tests
- Los modelos SQLAlchemy usan `Mapped[]` (SQLAlchemy 2.0 style)
- Alembic migration corre contra `DATABASE_URL_DIRECT`, no el pooler
- CI pipeline: lint → typecheck → unit-test → integration → migration-check → security → docker-build → frontend

## Sprint 4: Webhook Receiver & MessagingProvider

### Completado — Dev B (branch `feature/sprint-04-webhooks`, sesión 11)
- [x] `app/api/v1/webhooks.py` — `POST /api/v1/webhooks/{provider}/{channel}`: firma HMAC → parseo → dedup Redis → encolar → 200. No toca la base de datos (presupuesto <100ms)
- [x] `app/api/v1/webhooks.py` — `GET` de verificación: Meta (`hub.mode=subscribe` + `hub.verify_token` → `hub.challenge` en text/plain) y YCloud (challenge simple)
- [x] `app/main.py` — router montado en `/api/v1/webhooks`, que coincide con `WEBHOOK_PATHS_PREFIX` del middleware (los webhooks no pasan por `TenantContextMiddleware`)
- [x] `app/services/dedup.py` — dedup en dos niveles (PAT-001): Redis `SET NX EX 24h` + `webhook_dedup` en PostgreSQL. Fail-open ante caída de Redis (ADR-028) y `release_mark()` si falla el encolado (ADR-029)
- [x] `app/tasks/webhook_processor.py` — tarea `app.tasks.webhook_process_incoming` (cola `webhooks`, retry 5s/25s/125s, DLQ en `dlq:webhook_messages`). Todo el flujo en una transacción con `SET LOCAL`
- [x] `app/tasks/celery_app.py` + `app/tasks/__init__.py` — registro de tareas vía `imports`. Sin esto el worker levantaba las colas pero no conocía ninguna tarea
- [x] `app/core/config.py` + `.env.example` — vars de Meta/YCloud, `DEFAULT_CLIENT_ID` y `extra="ignore"` (BUG-004)
- [x] `app/main.py` + `app/api/internal/health.py` — tipado de `redis.ping()`; `mypy app/ --ignore-missing-imports` pasa de 2 errores a 0
- [x] `tests/unit/test_webhooks.py` (23 tests) y `tests/unit/test_webhook_processor.py` (20 tests) — verdes sin DB
- [x] `tests/unit/test_meta_provider.py` — contrato de MetaProvider, con `importorskip` hasta que Dev A entregue `meta.py`
- [x] `tests/integration/test_webhook_flow.py` — flujo completo contra PostgreSQL (marker `db`) para los 3 canales
- [x] `tests/fixtures/meta_payloads.py` — payloads de Instagram, Facebook y YCloud

### Completado — Dev A (branch `feature/sprint-04-messaging`, sesión 12)
- [x] `app/services/messaging/base.py` — `MessagingProvider` ABC (5 métodos: `parse_webhook`, `validate_signature`, `send_message`, `send_template`, `get_channel_constraints`) + `ChannelConstraints`/`MessageContent`/`TemplateMessage`
- [x] `app/schemas/message.py` — `NormalizedMessage`, `ChannelEnum`, `MessageTypeEnum` (usando `enum.StrEnum`, no `(str, Enum)` — `ruff` UP042 en Python 3.11+)
- [x] `app/services/messaging/ycloud.py` — `YCloudProvider`: parseo texto/media/ubicación, firma HMAC-SHA256 (`X-Ycloud-Signature`, hex sin prefijo), `send_message`/`send_template` contra `settings.YCLOUD_BASE_URL`
- [x] `app/services/messaging/meta.py` — `MetaProvider`: un solo provider para Instagram DM y Facebook Messenger (subcanal en `provider_config["channel"]`), firma `x-hub-signature-256` (`sha256=<hex>`), `send_template` en Instagram lanza `NotImplementedError`
- [x] `app/services/messaging/factory.py` — `get_messaging_provider(provider_name, provider_config=None)`, `_PROVIDERS = {"ycloud": ..., "meta": ...}`
- [x] Activa los 33 tests de Dev B que esperaban esta entrega con `importorskip`: `tests/unit/test_messaging_provider.py` (20) y `tests/unit/test_meta_provider.py` (13). Suite completa: 100/100, `ruff check`/`format` y `mypy app/ --config-file=pyproject.toml` limpios

### Ajuste sobre la spec (`specs/sprint-04-webhooks.md` §5 y §4)
- La factory de la spec construye `MetaProvider()` sin argumentos cuando `provider_config` es falsy — pero `_resolve_provider()` en `webhooks.py` (Dev B, ya en `main`) **siempre** pasa `{"channel": channel}`, nunca `None` ni `{}`. Se ajustó a `provider_class(provider_config or {})` sin la condición `and provider_config`.
- El `__init__` de `MetaProvider` en la spec exige `page_access_token`/`app_secret` en el dict (`provider_config["page_access_token"]`, indexación directa) — pero el endpoint real solo pasa `{"channel": channel}` al recibir un webhook, así que con la spec tal cual el constructor reventaría con `KeyError` en **todo** mensaje entrante de Meta. Se cambió a `.get(..., "")`: ninguno de los dos se usa desde `self` en `send_message`/`send_template` (llegan por `channel_config` en cada llamada) ni en `validate_signature` (el secreto llega por parámetro desde `settings.META_APP_SECRET`), así que no hacía falta que fueran obligatorios.

### Bloqueadores
- ~~**BUG-005 (CRÍTICO):** `migrations/versions/001_baseline.py` no crea RLS~~ — **Resuelto 2026-09-14** (issue [#6](https://github.com/miguelggdev/Omnichannel-Platform/issues/6)): `migrations/versions/002_rls_policies.py` agrega RLS a las 18 tablas, y CI ahora lo verifica de verdad (`alembic upgrade head` + `pytest --run-db`, en vez de `init.sql` + tests silenciosamente saltados). Ver MEMORY.md (BUG-005 y NOTA-002) y la nota en Sprint 1 arriba.
- ~~**Sin la entrega de Dev A, el POST respondía 400**~~ — **Resuelto 2026-09-14**: con `app/services/messaging/` completo, la factory resuelve `ycloud`/`meta` y el POST ya no depende de un import perezoso que fallaba.

### Notas
- La spec asume campos que los modelos de Dev A no tienen: `Message.contact_id` (se usa `sender_type="contact"` + `sender_id`), `ContactIdentifier.is_primary`, `WebhookDedup.processed`
- Los identifiers se guardan en claro: `ContactIdentifier.identifier_value` es `String(255)` con unique en `(client_id, channel, identifier_value)`. El cifrado con pgcrypto que pide CLAUDE.md §3 necesita una columna de hash para poder buscar — pendiente de decidir entre Dev A y Dev B
- Cobertura de los archivos nuevos sin `--run-db`: webhooks.py 93%, dedup.py 78%, webhook_processor.py 85%, celery_app.py 100% (total 86%)

---

## Sprint 5: Pipeline de Documentos & RAG

### Completado — Dev B (branch `feature/sprint-05-rag`, sesión 13)
- [x] `app/api/v1/documents.py` — CRUD completo: subida (valida tipo y 50 MB), listado paginado con filtro por status, detalle con recuento real de chunks, borrado (documento + chunks + archivo) y reprocesado
- [x] `app/services/storage.py` — cliente de Supabase Storage (upload/download/delete) con aislamiento por prefijo de ruta (ADR-032). `download_from_storage()` es la que consumirá el pipeline de Dev A
- [x] `app/tasks/document_ingestion.py` — tarea `app.tasks.document_ingest` (cola documents, 2 reintentos, time_limit 600s / soft 540s). Timeout y ausencia del pipeline no se reintentan: dejan el documento en `failed` con el motivo visible
- [x] `app/main.py` — router montado en `/api/v1/documents`
- [x] `app/core/config.py` + `.env.example` — `SUPABASE_STORAGE_BUCKET`
- [x] `tests/unit/test_documents.py` — 56 tests verdes sin DB (endpoint, permisos por rol, Storage con httpx sustituido, worker)
- [x] `tests/unit/test_rag.py` — contrato del retrieval, con `importorskip` hasta que Dev A entregue `rag.py`
- [x] `tests/integration/test_document_pipeline.py` — flujo contra PostgreSQL real con RLS: aislamiento entre dos tenants a través de los endpoints, borrado de chunks, reprocesado, filtros y paginación
- [x] Deuda de Sprint 4 cerrada: el worker de webhooks usa `NormalizedMessage(**message_data)` y `app.services.messaging.*` sale del override de mypy
- [x] BUG-007 y BUG-008 (ver MEMORY.md)

### Completado — Dev A (branch `feature/sprint-05-pipeline`, sesión 13, [PR #10](https://github.com/miguelggdev/Omnichannel-Platform/pull/10))
- [x] `app/services/chunker.py` — `DocumentChunker`: FAQ (par pregunta/respuesta), tablas (bloques con "|"/tab preservados enteros) y texto general vía `RecursiveCharacterTextSplitter` (chunk_size/overlap en **caracteres**, no tokens — ver desviación de spec abajo)
- [x] `app/services/embedding.py` — `EmbeddingService` (OpenAI `text-embedding-3-small` por defecto), `embed_batch()` con pausa de 0.5s entre lotes de 100
- [x] `app/services/ocr.py` — `OCRService` (Tesseract `spa+eng`), preprocesa a escala de grises + binarización + filtro de mediana; PDFs se rasterizan a 300dpi con PyMuPDF antes de OCR
- [x] `app/services/document_pipeline.py` — `DocumentPipeline.process()`: descarga → detecta si necesita OCR (imagen siempre; PDF con <50 caracteres extraídos) → extrae/OCR → chunkea → embebe → guarda. No atrapa excepciones (las deja subir al manejador de `document_ingestion.py`, que decide reintento o `failed`)
- [x] `app/services/rag.py` — `RAGService.retrieve()` con filtro pre-vectorial de `client_id` y umbral de similitud repetidos en el WHERE (BUG-001-safe, sin alias de SELECT), `retrieve_few_shot_examples()`, `build_grounded_prompt()`
- [x] Activa los 18 tests de contrato de Dev B (`tests/unit/test_rag.py`, `importorskip`). Suite completa: 173 tests unitarios, `ruff`/`mypy` limpios
- [x] CI real verificado: `Integration Tests (RLS + DB)` corrió 41 passed contra el rol `app_user` (no-superusuario, `NOBYPASSRLS`) — no es un checkmark ciego, se leyó el log

### Ajuste sobre la spec (`specs/sprint-05-rag.md`)
- **Chunking por caracteres, no por tokens:** el spec sugiere un `length_function` basado en tokens; el test de contrato de Dev B (`test_respeta_el_tamano_maximo`) verifica `len(chunk.content) <= chunk_size * 1.2` en caracteres. Se dejó el default de `RecursiveCharacterTextSplitter` (`len`); `token_count` se calcula aparte con `tiktoken`, solo como metadata para `document_chunks.token_count`.
- **PyMuPDF en vez de `pdf2image`** para rasterizar PDFs a imagen antes de OCR: `pdf2image` depende del binario de sistema `poppler-utils`, ausente del `Dockerfile`; PyMuPDF es una dependencia de pip autocontenida (motor MuPDF embebido).

### Notas
- Mientras el pipeline de Dev A no estuvo, los documentos subidos quedaban en `failed` con el motivo explícito en `metadata.error`; se recuperaban con `POST /documents/{id}/reprocess` sin volver a subir el archivo
- La spec asume campos que el modelo no tiene (`file_path`, `file_size_bytes`, `uploaded_by`): se usan `file_url`, `file_size` y `metadata.uploaded_by`
- `document_chunks` no declara `ON DELETE CASCADE`, así que el borrado los elimina explícitamente
- Cobertura de los archivos nuevos: `documents.py` 97%, `storage.py` 97%, `document_ingestion.py` 87% (total 95%)
- Se eliminó `test_pipeline_ausente_se_detecta` de `tests/unit/test_documents.py`: su premisa ("Dev A no entregó, el import falla") dejó de ser cierta y el test empezaba a intentar una conexión real a Postgres inexistente en CI unitario

---

## Sprint 6: LangGraph — Grafo de Agentes

### Completado — Dev B (branch `feature/sprint-06-nodes`, sesión 15)
- [x] `app/agents/nodes/token_budget.py` — nodo de presupuesto con los 3 umbrales de ADR-004 (ok / degraded a `gpt-4o-mini` / exceeded con handoff), cache de 60s en Redis con degradación a la base si Redis no responde
- [x] `app/middleware/token_budget.py` — `TokenBudgetGuard.record_usage()` real: escribe `token_usage_logs`, suma al presupuesto del mes e invalida la cache. Best-effort: un fallo de la contabilidad no tumba la conversación
- [x] `app/agents/nodes/intent_router.py` — clasificación con structured output y `gpt-4o-mini` fijo; solo ofrece los intents de agentes habilitados y reencamina los que no lo están (a RAG si hay RAG, a humano si no)
- [x] `app/agents/nodes/rag_query.py` — grounding estricto: sin chunks sobre el umbral no llama al LLM, marca `insufficient_context`. Few-shot con umbral propio (`similarity_threshold`) y system prompt del tenant
- [x] `app/agents/nodes/respond.py` — envía la respuesta (o el texto que toca por intent; el saludo usa `welcome_message` del tenant) y la guarda en `messages`
- [x] `app/agents/nodes/human_handoff.py` — `waiting_human` + motivo y métricas en `conversations.metadata.handoff` + aviso al contacto + notificación al equipo
- [x] `app/agents/nodes/training_approval.py` — la respuesta candidata queda en `pending_responses`; al contacto solo le llega el aviso de revisión
- [x] `app/agents/nodes/_tenant.py`, `_delivery.py`, `_llm.py`, `_notifications.py`, `_state.py` — base compartida (config del tenant, envío + persistencia, cliente de chat, avisos, contrato del estado)
- [x] `app/tasks/ai_processor.py` — tarea `app.tasks.ai_process_response` (cola `ai_inference`, 2 reintentos, 120s/100s), con `run_isolated()` de PR #12. Agotados los intentos, o si el grafo no está, escala a un humano en vez de dejar la conversación muda
- [x] `app/core/config.py` + `.env.example` — `YCLOUD_PHONE_NUMBER_ID` (sin él el nodo `respond` no puede enviar por WhatsApp)
- [x] Deuda de Sprint 4 cerrada: `_enqueue_ai_processing()` deja de ser un stub y encola el grafo de verdad
- [x] 74 tests unitarios nuevos + 8 de integración (`tests/integration/test_graph_flow.py`) contra Postgres real con RLS
- [x] ADR-034, ADR-035 y BUG-013 registrados en MEMORY.md

### Completado — Dev A (branch `feature/sprint-06-graph`, sesión 16, [PR #14](https://github.com/miguelggdev/Omnichannel-Platform/pull/14))
- [x] `app/agents/state.py` — `ConversationState` (`total=False`, igual que la copia temporal de Dev B). `app/agents/nodes/_state.py` pasa a ser un re-export
- [x] `app/agents/graph.py` — `build_conversation_graph()` con los 6 nodos y el routing condicional; `route_after_intent()`/`route_after_rag()` reflejan lo que `intent_router.py`/`rag_query.py` ya resuelven
- [x] `app/agents/graph.py::get_graph_with_checkpointer()` — envoltorio (`_CheckpointedGraph`) que abre un pool de `psycopg`, compila el grafo con `AsyncPostgresSaver` y cierra el pool, todo dentro de la misma llamada (ADR-035, ADR-036)
- [x] `migrations/versions/003_langgraph_checkpoints.py` — crea las 4 tablas del checkpointer; `migrations/env.py` las excluye del diff de `alembic check` (ADR-036)
- [x] `requirements.txt` — `psycopg[binary,pool]`, sin el cual `psycopg` v3 no se puede importar sin `libpq` del sistema
- [x] **BUG-014** (fuera de la entrega propia, encontrado al revisar Dev B): `ai_processor` nunca se agregó a `TASK_MODULES` — el worker de `ai_inference` no conocía la tarea
- [x] `app/schemas/agent_config.py` — evaluado y **no tocado**: ya existe desde Sprint 3 (CRUD genérico) y ningún nodo del grafo lo importa; la configuración efectiva la resuelve `AgentSettings` en `_tenant.py` (ADR-034). El ítem de la spec no era un bloqueador real

### Ajustes sobre la spec (`specs/sprint-06-langgraph.md`)
- **Configuración del agente:** el spec asume `agent_configs.agent_type` / `is_enabled` / `settings`, que no existen. Se usa la fila activa del tenant y su JSONB `config` (ADR-034).
- **Presupuesto:** `token_budgets` se busca por `month` (YYYY-MM), no por `period_start`/`period_end`; `token_usage_logs` no tiene `cost_usd`, así que el costo estimado va al log y no a la base.
- **Nota interna del handoff:** `internal_notes.author_id` es NOT NULL y una nota del bot no tiene autor; el motivo va a `conversations.metadata.handoff` (BUG-013, decisión pendiente).
- **`pending_responses.generated_response`**, no `suggested_answer`.
- **Sin cache del grafo compilado** (spec §12): reusarlo entre tareas de Celery es BUG-006 / BUG-011. La tarea usa `run_isolated()` (PR #12), no `asyncio.run()` (ADR-035).
- **Notificaciones** (`notify_handoff`, `notify_pending_response`): `app/tasks/notifications.py` es de Sprint 8; hasta entonces el aviso se registra en el log y el flujo sigue.

### Notas
- Los nodos **no** atrapan las excepciones del LLM ni de la base: suben a `ai_processor`, que reintenta y, agotados los intentos, escala a un humano. Mismo criterio que `DocumentPipeline` en Sprint 5.
- Los 8 tests de integración corrieron en CI contra Postgres real (`tests/integration/test_graph_flow.py ........`, run 34973790825): 56 passed, 6 skipped en el job completo.
- `_enqueue_ai_processing()` ya no propaga un fallo de encolado: cuando se llega ahí, el mensaje está commiteado y marcado en `webhook_dedup`, así que un reintento se cortaría en la comprobación de duplicado sin volver a encolar. Queda un CRITICAL en el log.

---

## Sprint 7: Agente de Agendamiento & CRM API

### Completado — Dev B (mergeado a `main` en [PR #16](https://github.com/miguelggdev/Omnichannel-Platform/pull/16), sesión 18-19)
- [x] `app/services/conversation_lifecycle.py` — máquina de estados de los 7 estados, único sitio donde viven las transiciones (la usan el endpoint de status, el de asignación y el worker de auto-cierre)
- [x] `app/api/v1/contacts.py` — listado paginado con búsqueda por nombre y filtro por etiqueta, alta, detalle con identificadores/etiquetas/notas, edición parcial, fusión (delegada en `ContactUnifier` de Dev A) e historial de conversaciones
- [x] `app/api/v1/conversations.py` — listado filtrable por estado/canal/agente, detalle con mensajes paginados en orden cronológico, asignación a un agente humano y cambio de estado validado contra la máquina
- [x] `app/api/v1/tags.py` — CRUD de etiquetas del tenant + asignación/baja sobre un contacto (dos routers: `/tags` y `/contacts/{id}/tags/{tag_id}`)
- [x] `app/api/v1/notes.py` — notas internas de un contacto, paginadas, firmadas con el usuario del JWT
- [x] `app/api/v1/agent_logs.py` + `app/schemas/agent_log.py` — traza por conversación, estadísticas por nodo y errores recientes (addendum de Agent Activity Logging; el modelo y el servicio son de Dev A)
- [x] `app/tasks/auto_close.py` — tarea `app.tasks.bulk_auto_close_conversations` (cola `bulk`, 300s/270s): `waiting_client > 24h -> resolved` y `resolved > 7d -> archived`, iterando tenant por tenant con RLS activa (ADR-037)
- [x] `app/schemas/{tag,note,agent_log}.py` nuevos; `contact.py` y `conversation.py` extendidos con los schemas de detalle y de cambio de estado
- [x] `app/main.py` — 20 endpoints nuevos montados; `app/tasks/celery_app.py` — `app.tasks.auto_close` sumado a `TASK_MODULES` (la lección de BUG-014)
- [x] 163 tests unitarios nuevos (`test_conversation_lifecycle`, `test_contacts`, `test_conversations`, `test_tags_notes`, `test_agent_logging`, `test_auto_close`, más los dobles de `crm_doubles.py`) y 26 de integración (`tests/integration/test_crm_api.py`) contra Postgres real con RLS
- [x] ADR-037, ADR-038 y ADR-039 registrados en MEMORY.md
- [x] Verificado en CI real leyendo el log, no el checkmark ([PR #16](https://github.com/miguelggdev/Omnichannel-Platform/pull/16), run 35149784591): **464 unitarios passed, 1 skipped** (cobertura 86.68%, antes 280) y **83 de integración passed, 6 skipped** contra el rol `app_user` (`NOBYPASSRLS`, antes 57). Los 9 jobs en verde, incluidos `alembic check` y el smoke de Docker

### Completado — Dev A (branch `feature/sprint-07-scheduling`, sesión 20, sin PR todavía)
- [x] `migrations/versions/004_service_types.py` + `app/models/service_type.py` (`ServiceType`, `Appointment`)
- [x] `app/services/calendar.py` (`GoogleCalendarService`): service account global en vez de OAuth2 por tenant (ver Estado Actual, Sesión 20, y ADR-040)
- [x] `app/agents/tools/calendar_tools.py`: las 5 tools LangChain (`check_availability`, `create_appointment`, `modify_appointment`, `cancel_appointment`, `list_appointments`)
- [x] `app/agents/nodes/scheduling.py` + integración en `graph.py` (nodo `scheduling`, `route_after_scheduling`)
- [x] `app/services/contact_unifier.py` (`ContactUnifier.merge()`) — `POST /contacts/{id}/merge/{target}` ya no responde 503: el camino feliz completo, incluido en `test_contacts.py`
- [x] Addendum de logging: `app/models/agent_action_log.py`, `migrations/versions/005_agent_action_logs.py`, `app/services/agent_logger.py`, `app/agents/middleware/logging_middleware.py` — los tres endpoints de `/agent-logs` ya no responden 503

### Ajustes sobre la spec (`specs/sprint-07-scheduling-crm.md`)
- **Nombre de la tarea de auto-cierre:** `app.tasks.bulk_auto_close_conversations`, no el `app.tasks.auto_close_conversations` de §14. `celery_config.py` (Sprint 2) ya declara ese nombre exacto en `beat_schedule`, y el routing automático manda a la cola `bulk` lo que empieza por `app.tasks.bulk_*`. Con el nombre del spec la tarea se habría quedado sin entrada en Beat y sin cola. Archivo en `app/tasks/auto_close.py` (el nombre de la matriz de METHODOLOGY.md) y no en `app/tasks/conversation_lifecycle.py`, que se confundiría con el servicio homónimo.
- **RLS en el auto-cierre:** ver ADR-037. La spec deja la decisión abierta entre un rol `BYPASSRLS` y iterar por tenant; se eligió iterar.
- **Máquina de estados:** una sola transición añadida, `waiting_human -> resolved` (ADR-039).
- **`HTTPException` -> `AppException`:** la spec usa `HTTPException` en todo el sprint; CLAUDE.md §3 exige `AppException(status_code, error_code, message)` para no exponer tracebacks y para que el cuerpo del error sea uniforme.
- **`require_role` no es jerárquico:** el addendum lo usa como `require_role("supervisor")` dando por hecho que admin y super_admin quedan incluidos. El real (`app/core/dependencies.py`, Sprint 3) compara por igualdad contra la lista, así que los roles van enumerados en cada endpoint.
- **Campos que el modelo no tiene:** `internal_notes.author_id` (la spec la llama `user_id`), `conversations.created_at` en vez de `started_at`, y `contact_identifiers` sin `is_primary` ni `verified_at`.
- **URL de las etiquetas de un contacto:** `/contacts/{id}/tags/{tag_id}` en vez del `/tags/contacts/{contact_id}/tags/{tag_id}` de §11, que repite el segmento y cuelga la relación del recurso equivocado.
- **Borrado de etiquetas:** la spec confía en un `ON DELETE CASCADE` que la FK de Sprint 1 no declara; `contact_tags` se limpia explícitamente antes del DELETE, igual que `document_chunks` en Sprint 5.
- **`previous_status` en el cambio de estado:** la spec lo lee del objeto **después** de transicionar, con lo que devolvía el estado nuevo en los dos campos. Se captura antes.
- **Google Calendar Service (§3):** el spec modela OAuth2 con refresh token por tenant cifrado con pgcrypto en `agent_configs.settings` — esa columna no existe (`agent_configs.config`, ver más abajo) y el cifrado real es Sprint 8. Se cambió a un service account global; detalle en ADR-040.
- **`agent_configs.settings`/`agent_type`/`is_enabled` (calendar service y scheduling node):** no existen. Hay una sola fila de config por tenant, con `config` JSONB — mismo hallazgo que ya documentó Sprint 6 en `app/agents/nodes/_tenant.py`. `calendar_id`/`timezone` del scheduling agent viven en `config.scheduling.{calendar_id,timezone}`.
- **Contact Unifier (§8):** `resolve_contact()`/`_follow_merge_chain()` no se implementan — esa resolución automática ya existe y está probada en `webhook_processor._resolve_contact()` desde Sprint 4. Solo se construyó `merge()`, el único método que consume el `contacts.py` que ya entregó Dev B. También sin `pgp_sym_encrypt`/`is_primary` (mismo motivo que el calendar service): `merge()` no toca `identifier_value`, así que no depende de si termina cifrado o no.
- **`tool_fn.ainvoke({**args, "config": tool_config})` (scheduling tools, §4-5):** dado así, `config` viaja dentro del dict de argumentos de la tool en vez de como el parámetro `config=` real de `.ainvoke()`. Confirmado con un smoke test contra la versión instalada de `langchain-core`: el `RunnableConfig` que recibe la tool queda vacío y explota con `KeyError` al leer `client_id`. Se usa `tool_fn.ainvoke(args, config=tool_config)`.
- **`*Input` (`CheckAvailabilityInput`, etc., §4):** se declaran en el spec pero nunca se pasan como `args_schema=` a ninguna tool — quedan muertos. Se reemplazan por `@tool(parse_docstring=True)`, que arma el schema que ve el LLM a partir del docstring Google-style que CLAUDE.md ya exige.
- **Addendum de logging — `state["_db_session"]` (§4):** el propio spec ya avisa (nota de reprogramación al inicio del archivo) que esto no existe: cada nodo real resuelve su propia `tenant_session()`. El middleware abre la suya aparte, después de que el nodo corre.
- **Addendum de logging — campos de `ConversationState` en los summary-builders (§4):** `last_user_message`, `detected_language`, `sentiment*`, `rag_results_count`, `next_node`, `action`, `budget_percentage`, `tool_calls`, `approval_required` no existen en el estado real (`app/agents/state.py`, Sprint 6). Los tres helpers (`_build_input_summary`/`_build_output_summary`/`_extract_details`) se reescribieron contra los 12 campos reales.
- **Addendum de logging — `loguru`/`datetime.utcnow()` (§3):** ningún módulo del proyecto usa `loguru` en la práctica (todos hacen `logging.getLogger(__name__)`); se sigue esa convención real. `datetime.utcnow()` está deprecado desde Python 3.12; se usa `datetime.now(timezone.utc)`.

### Notas
- Los 20 endpoints nuevos abren `tenant_session()` a mano en vez de usar la dependency `get_tenant_session`, igual que `documents.py` (ADR-033): la transacción cierra dentro del endpoint y no cuando FastAPI limpia las dependencias.
- Todas las consultas llevan el `client_id` explícito en el WHERE además de correr bajo RLS (CLAUDE.md, restricción 2). Hay un test por endpoint que lo verifica sobre el SQL compilado.
- `tests/unit/crm_doubles.py` extiende los dobles de Sprint 6 (`agent_doubles.py`) con `scalar()`, `refresh()`, `delete()` y el `rowcount` de un UPDATE masivo.
- **Hallazgo de CI (no local):** `requirements.txt` declara `fastapi>=0.110.0` sin techo, así que CI instala 0.141 mientras el entorno local tenía 0.136. Entre esas dos versiones cambió dónde acaban las rutas incluidas: `app.routes` pasó a devolver solo las de la documentación. La app funciona igual (los endpoints responden), pero cualquier test que inspeccione `app.routes` es frágil — el de este sprint mira `openapi()["paths"]`, que es el contrato público. Vale la pena revisar si conviene poner techo a esa dependencia.
- **Hallazgo de CI (no local):** `AsyncSession.execute()` está tipado como `Result[Any]`; según la versión de SQLAlchemy ese tipo expone `rowcount` o no. Un `cast` a `CursorResult` falla en una versión por atributo inexistente y en la otra por cast redundante, así que las dos lecturas de `rowcount` (auto-cierre y borrado de etiquetas) anotan la variable como `Any` con el motivo al lado.
- `tests/unit/test_agent_logging.py` prueba las dos mitades del addendum: el 503 de hoy y el comportamiento completo con un doble del modelo de Dev A, para que su entrega no llegue a ciegas.
- **BUG-016 (encontrado revisando este PR, corregido aparte en [PR #17](https://github.com/miguelggdev/Omnichannel-Platform/pull/17)):** no es un defecto de este sprint sino de Sprint 6 — `webhook_processor.py` reutilizaba conversaciones en `human_active`/`waiting_human` y encolaba el grafo de IA sin mirar el status, así que el bot seguía respondiendo a contactos ya escalados a un humano. La nueva `ConversationLifecycle` de este sprint fue lo que hizo evidente el hueco. Ver BUG-016 en MEMORY.md.

---

## Sprint 8: Observabilidad, Backup & Hardening (HITO MVP)

### Completado — Dev B (branch `feature/sprint-08-ops`, sesión 20, [PR #18](https://github.com/miguelggdev/Omnichannel-Platform/pull/18) mergeado)
- [x] `app/middleware/audit.py` + `app/models/audit_log.py` + migración `006_audit_gdpr_quick_replies` — rastro de auditoría escrito por un trigger de PostgreSQL sobre `contacts`, `conversations` y `messages`, con el autor del cambio publicado en `app.current_user_id` (ADR-043)
- [x] `app/api/v1/quick_replies.py` + `app/schemas/quick_reply.py` + `app/services/quick_reply.py` — CRUD de respuestas rápidas y resolución de `{{contact_name}}`, `{{agent_name}}`, `{{ticket_id}}` y `{{date}}`
- [x] `app/api/v1/admin.py` — export RGPD completo y anonimización sin borrar filas
- [x] `scripts/backup.sh`, `scripts/restore_test.sh` y `app/tasks/maintenance.py` — backup diario (03:00 UTC) y prueba de restauración mensual (día 1, 04:00 UTC), las dos en la cola `bulk` (ADR-045)
- [x] `tests/e2e/test_full_flow.py` — recorrido completo webhook → dedup → grafo → respuesta enviada, con su rastro. `ci.yml` suma `tests/e2e/` al job de integración: sin eso no lo ejecutaba nadie
- [x] 90 tests unitarios nuevos, 22 de integración y 5 e2e
- [x] ADR-043, ADR-044, ADR-045 y BUG-025 registrados en MEMORY.md
- [x] Verificado en CI real leyendo el log, no el checkmark ([PR #18](https://github.com/miguelggdev/Omnichannel-Platform/pull/18), run 35228425188): **554 unitarios passed, 1 skipped** (cobertura 87.37%, antes 464) y **107 de integración + e2e passed, 6 skipped, 1 xfailed** contra el rol `app_user` (`NOBYPASSRLS`). Los 9 jobs en verde
- **Nota de renumeración (sesión 22):** esta rama partió de `main` antes de que el PR #19 (Sprint 7 Dev A) se mergeara, así que usó los mismos números que Sprint 7 para cosas distintas: la migración `004_audit_gdpr_quick_replies` (ahora `006_...`, encadenada tras `005_agent_action_logs`), ADR-040/041/042 (ahora ADR-043/044/045) y BUG-017 (ahora BUG-025 — el BUG-017 real es el de Sprint 7, ya en `main`). Ver la entrada de la sesión 22 en "Estado Actual" y MEMORY.md.
- [x] **BUG-025 cerrado (sesión 23, rama `fix/bug-017-login-rls`):** `public.auth_lookup_user()` (migración `007_auth_lookup_function`), función `SECURITY DEFINER` con `search_path` fijo, único punto del sistema que lee `users`/`clients` sin contexto de tenant. ADR-046. `TestLoginBajoRls` pasó de `xfail` a passed; `TestFuncionDeBusqueda` cubre la superficie de la función.

### Completado — Dev A (rama `feature/sprint-08-observability`, sesión 24)
- [x] `app/core/telemetry.py` — `TracerProvider` global, instrumentación de FastAPI, Celery, SQLAlchemy, Redis y httpx, y propagación W3C entre la API y los workers (ADR-047)
- [x] `app/core/logging.py` — Loguru con `InterceptHandler` en la raíz de `logging`: los ~40 módulos que ya usaban la stdlib salen con `trace_id`/`client_id`/`user_id` sin tocar una sola llamada (ADR-050)
- [x] `app/core/metrics.py` + `app/api/internal/metrics.py` — 12 métricas y el endpoint `/internal/metrics`, en modo multiproceso (ADR-048)
- [x] `app/middleware/observability.py` — `X-Trace-ID`, contexto de log por request y métricas HTTP con la **plantilla** de ruta como etiqueta (nunca la URL concreta)
- [x] `app/tasks/observability.py` — las mismas tres piezas en los workers, por señales de Celery
- [x] Instrumentación de los caminos calientes: tokens y costo (`TokenBudgetGuard`), confianza del intent, latencia y chunks del RAG, handoffs, mensajes por canal y cierre de conversaciones
- [x] `app/core/encryption.py` + migración `008_encrypt_contact_identifiers` — `contact_identifiers.identifier_value` cifrado con pgcrypto e índice ciego HMAC (ADR-051)
- [x] `prometheus/alerts.yml` (13 reglas) y `prometheus/prometheus.yml` con descubrimiento por DNS
- [x] `grafana/dashboards/{system_health,tenant_usage,agent_performance,celery_queues}.json`
- [x] `docker-compose.yml` — servicios `jaeger` y `redis-exporter`; **BUG-026 cerrado** de paso
- [x] ADR-047 a ADR-051, BUG-026 y BUG-003 registrados en MEMORY.md
- [x] 47 tests unitarios nuevos (`test_observability.py`, `test_encryption.py`) y 6 de integración (`tests/integration/test_encryption.py`)

### Verificación en CI real (sesión 24, [PR #22](https://github.com/miguelggdev/Omnichannel-Platform/pull/22), run 35296764949)
Leyendo el log, no el checkmark. **Los 9 jobs en verde:**
- **Unitarios: 686 passed, 1 skipped**, cobertura **88.61%** (antes 554 y 87.37%)
- **Integración + e2e: 123 passed, 6 skipped** contra Postgres y el rol `app_user` (`NOBYPASSRLS`), incluidos los 6 de `test_encryption.py`
- `ruff`, `mypy`, Alembic migration check, security scan y el smoke de Docker: limpios
- **Un fallo real encontrado por CI y corregido:** tres tests de `test_encryption.py` reventaban con "attached to a different loop". No soltaban el pool del engine antes de empezar — `app.core.database.engine` es un singleton de módulo y pytest-asyncio abre un event loop por test. Se agregó la fixture con `await engine.dispose()` que ya usa el resto de la suite, más la limpieza de los tenants que siembra cada test

### Sin asignar — addendum de operaciones
`specs/sprint-08-addendum-ops.md` son 6.187 líneas con cuatro features (panel de Celery/Redis, bot de Telegram para super admin, backup avanzado con replicación, y políticas de seguridad de servidor/Cloudflare/Docker). **La matriz de METHODOLOGY.md §Sprint 8 no asigna ninguna de las cuatro a ningún dev.** Queda como decisión de reparto, no adjudicada por cuenta propia.

### Ajustes sobre la spec — Dev A (`specs/sprint-08-observability.md`)
- **§8.4, "el cifrado es determinístico":** es falso. `pgp_sym_encrypt()` usa IV aleatorio. Con esa premisa, la búsqueda de contactos del webhook (`WHERE identifier_value = :telefono`) no encontraría nunca la fila —un contacto nuevo por cada mensaje entrante— y el `UNIQUE (client_id, channel, identifier_value)` dejaría de proteger de nada. De ahí el índice ciego (ADR-051).
- **§8.2, `impl = Text`:** la columna tiene que ser `BYTEA`, porque `pgp_sym_encrypt()` devuelve bytea. Y al `bind_expression` le falta el `type_coerce(bindvalue, Text)`, sin el cual asyncpg recibe un `str` donde espera `bytes` y **ningún INSERT entra**.
- **§3.3, `app.mount("/metrics", make_asgi_app())`:** sirve el registro de *un* proceso, y tanto la API (`--workers 2` × `replicas: 2`) como los workers (prefork) son varios. Además, una sub-app montada se salta los exception handlers y el middleware de la app principal. El endpoint es una ruta normal bajo `/internal` y usa el registro multiproceso (ADR-048).
- **§3.1, `celery_queue_size`:** la publica `redis-exporter` como `redis_key_size`, no la aplicación (ADR-049).
- **§5, tres alertas sobre series inexistentes:** `token_budget_limit` (el presupuesto vive en la tabla, no en Prometheus), `celery_queue_size` (ver arriba) y `pg_stat_activity_count` (por ADR-020 la base es Supabase Cloud, sin postgres-exporter). Se replantearon sobre métricas reales o se descartaron, en vez de dejar alertas que nunca dispararían.
- **§2.1, `logger.add(..., filter={"sqlalchemy": "WARNING"})`:** ese parámetro agrega un **segundo sink**, que duplica cada línea de WARNING para arriba. El silenciado va con `logging.getLogger(...).setLevel(...)`. Y el sink a `/var/log/app/app.log` no se monta: ningún contenedor tiene ese volumen.
- **§1.2, `SQLAlchemyInstrumentor().instrument(engine=engine)`:** el engine del proyecto es un `AsyncEngine`; hay que pasarle `engine.sync_engine` o no se traza ni una query.
- **§1.2, exporter OTLP incondicional:** solo se monta si hay endpoint configurado (ADR-047).

### Ajustes sobre la spec — Dev B (`specs/sprint-08-observability.md`)
- **Nombre de las tareas de mantenimiento:** `app.tasks.bulk_run_backup` y `app.tasks.bulk_run_restore_test`, no los `app.tasks.maintenance.*` de §7.3. No existe cola `maintenance` y el routing de Sprint 2 es por prefijo de nombre: con el nombre de la spec habrían caído en la cola por defecto (`webhooks`), contra un worker con `prefetch=1`, y un backup de una hora habría bloqueado la recepción de mensajes durante esa hora. Es el mismo defecto que el del auto-cierre en Sprint 7.
- **Extensión del dump:** `.dump`, no `.sql.gz`. `--format=custom` produce un binario que solo lee `pg_restore`; el nombre de la spec invita a intentar `gunzip | psql`.
- **`pg_restore --exit-on-error`:** sin esa opción avisa de los fallos y termina con 0, y la prueba daría por bueno un backup a medias.
- **La prueba de restauración verifica RLS, triggers y la revisión de Alembic,** no solo que las tablas tengan filas: un backup que restaura los datos pero pierde el aislamiento entre tenants no sirve para recuperarse.
- **Variables `RESTORE_*` propias** en vez de heredar `PG*`: la prueba crea y destruye una base entera y no puede apuntar a producción por descuido. En Supabase Cloud además no hay `CREATE DATABASE` disponible para el rol de la aplicación.
- **`message.direction == "incoming"`** (§10.2); el enum real es `inbound`/`outbound`.
- **`quick_replies.shortcut` y `created_by`** los da por hechos §11.1 y el modelo de Sprint 1 nunca los tuvo: los agrega la migración 006, con backfill desde el título antes de poner NOT NULL.
- **El trigger no va sobre `users`,** aunque §9.2 lo pida: ver BUG-025.

### Notas
- **BUG-025 (crítico) — CERRADO.** El login no funcionaba contra un rol sujeto a RLS. Salió al decidir si `users` podía llevar trigger de auditoría, y se comprobó contra Postgres real en vez de razonarlo sobre el papel. Arreglado con una función `SECURITY DEFINER` (migración `007_auth_lookup_function`, ADR-046) a decisión del usuario, entre las tres salidas planteadas. El `xfail` se retiró y en su lugar hay 10 tests de integración. Detalle en MEMORY.md.
- **Desbloqueado por ese arreglo:** `users` puede llevar ya trigger de auditoría (la migración 006 lo dejó fuera por BUG-025). Antes hay que auditar que **todos** los caminos de escritura sobre esa tabla pasen por `tenant_session()`, no solo el del login.
- **Tareas de infraestructura pendientes (las dos en el script que crea el rol de la aplicación, después de los `GRANT`):** un `REVOKE UPDATE, DELETE ON audit_logs` para que el rastro sea realmente append-only, y acotar el `EXECUTE` de `auth_lookup_user()` al rol de la aplicación en vez de dejarlo en PUBLIC. Las dos tienen que ir ahí y no en una migración, porque el provisioning crea ese rol **después** de correr las migraciones.
- **Detalle del `REVOKE` de `audit_logs`:** El `REVOKE ... FROM PUBLIC` de la migración no basta: el `GRANT ... ON ALL TABLES` posterior de CI y de Supabase lo vuelve a conceder. La migración lo dice explícitamente en vez de aparentar una garantía que no da.
- El rastro de auditoría se borra en cascada con su tenant (ADR-044). Si algún día hace falta conservarlo tras la baja de un cliente, la salida es exportarlo antes de borrar, no quitar el CASCADE.

---

## Sprint 9: Canales Adicionales

### Completado — Dev A (rama `feature/sprint-09-telegram-email`, sesión 28)
- [x] `app/services/messaging/telegram.py` — `TelegramProvider` sobre la ABC real (ADR-053): texto, foto (la de mayor resolución), voz/audio/video/documento, ubicación, contacto compartido y botones inline; troceo a 4096; `register_webhook()` y `get_file_url()`
- [x] `app/services/messaging/email_provider.py` — `EmailProvider`: Inbound Parse de SendGrid y Mailgun, SMTP (`aiosmtplib`), hilo por `In-Reply-To`/`References`, recorte del historial citado, anti-bucle (ADR-055)
- [x] Cableado: `factory`, `CHANNEL_PROVIDERS`/`get_channel_config`, `Settings` y `.env.example`, `TemplateNotSupportedError`, `MessageContent.metadata`
- [x] `app/api/v1/webhooks.py` — cabeceras y secretos de los dos canales, lectura de formularios (multipart/urlencoded), `IgnoredWebhookError` (ADR-054) y tope de cuerpo (BUG-040)
- [x] `NormalizedMessage.sender_name` y `_resolve_contact()`: el contacto se crea con el nombre público del remitente en vez de un id/email enmascarado
- [x] BUG-039: las credenciales de las URLs salientes (`access_token` de Meta, token del bot) ya no llegan a los spans
- [x] Tests: 160 unitarios nuevos y 12 de integración contra Postgres real (`tests/integration/test_multichannel.py`); mutaciones deliberadas: 12 de 12 detectadas
- [x] ADR-053, ADR-054, ADR-055, BUG-039 y BUG-040 registrados en MEMORY.md

### Dev B (matriz METHODOLOGY.md §Sprint 9) — entregado
- [x] `WebchatProvider` + `app/api/v1/webchat.py` (WebSocket) + `app/schemas/webchat.py` — PR #30, ADR-059. Falta el widget JS y el media.
- [x] Transcripción de audio con Whisper (`app/tasks/audio_transcription.py`, `app/services/transcription.py`) — PR #26, ADR-056; cola `media` en el PR #32, ADR-060
- [x] `tests/integration/test_audio_transcription.py` — lo único que el PR #28 aportaba sobre lo ya mergeado, portado a la API actual (`_guardar_transcripcion`, `_leer_transcripcion_previa`) — PR #33. El #28 se cerró en vez de rebasarse: el resto de su diff ya estaba en #26/#30 y habría borrado lo de #27, #31 y #32

### Sin resolver en esta entrega (decisiones o trabajo aparte)
- **Unificación de contacto entre canales (criterio 5 del spec):** resuelta (#27, ADR-057), solo por teléfono verificado por el canal y coincidencia inequívoca. El botón `request_contact` (`/vincular`) está en #29 (ADR-058); falta registrar el comando en BotFather.
- **Telegram:** `answerCallbackQuery` no se envía (el botón muestra el reloj unos segundos), sin *throttling* de los 30 msg/s por bot, y el webhook se registra a mano con `register_webhook()` (no hay script).
- **Email:** adjuntos ignorados; el proveedor de Inbound Parse (SendGrid o Mailgun) sigue sin elegirse: el código soporta los dos.
- **Webchat:** falta el widget JS; solo texto (sin media); una suscripción de Redis por conexión (ver ADR-059 para los límites).
- **Migración 009:** falta la ventana de bajo tráfico. `bash scripts/deploy_migration_009.sh` (sin argumentos solo comprueba).
- **Compose:** `test_compose_workers.py` cubre lo declarado y los defaults de `Settings`; una variable que un worker lee por `os.getenv` (fuera de `Settings`) no se vigila.
- **Cuerpo con `Transfer-Encoding: chunked`:** el tope de 32 MB solo aplica a lo que declara `Content-Length` (BUG-040); el resto lo tiene que cortar Traefik/Cloudflare.

---

## Resumen por Sprint

| Sprint | Nombre | Estado | Notas |
|---|---|---|---|
| 1 | Schema DDL & Arquitectura | ✅ Completado | 24 tablas, RLS verificado, DDL idempotente |
| 2 | Infraestructura Docker | ✅ Completado | 12 servicios (ADR-020), Dockerfile multi-stage, Traefik v3, 6 colas Celery |
| 3 | FastAPI Core & Auth | ✅ Completado | 61 archivos, +3460 líneas. Auth JWT, middleware multi-tenant, modelos SQLAlchemy, Alembic, CI 8/8 green |
| 4 | Webhook Receiver & MessagingProvider | ✅ Completado | Dev B (PR #5, mergeado) + Dev A (branch `feature/sprint-04-messaging`, pendiente de PR/merge): endpoint, dedup, worker, MessagingProvider ABC, YCloudProvider, MetaProvider, factory, `NormalizedMessage`. 100/100 tests, RLS verificado en CI real |
| 5 | Pipeline de Documentos & RAG | ✅ Completado | Dev B (PR #9): CRUD de documentos, Storage, worker de ingesta. Dev A (PR #10): chunker, embedding, OCR, DocumentPipeline, RAGService. 173 tests, RLS verificado en CI real (`app_user`, 41 passed) |
| 6 | LangGraph — Grafo de Agentes | ✅ Completado | Dev B (PR #13): 6 nodos, TokenBudgetGuard, `ai_processor`. Dev A (PR #14): `state.py`, `graph.py`, checkpointer con `AsyncPostgresSaver`, migración de tablas. 273 tests unitarios + 57 de integración, RLS/checkpointer verificados en CI real |
| 7 | Agente de Agendamiento & CRM API | ✅ Completado | Dev B (PR #16): CRM API (contactos, conversaciones, etiquetas, notas), ciclo de vida, auto-cierre y endpoints de agent logs — 20 endpoints, 163 tests unitarios + 26 de integración. Dev A (PR #19): calendario, nodo de scheduling, `contact_unifier`, migraciones de `service_types`/`agent_action_log` y 8 bugs encontrados y corregidos en revisión posterior (BUG-017 a BUG-024) |
| 8 | Observabilidad, Backup & Hardening | ✅ Completado | **Hito MVP.** Dev B ([PR #18](https://github.com/miguelggdev/Omnichannel-Platform/pull/18)): auditoría por trigger, RGPD, quick replies, backup/restore y el test e2e; BUG-025 cerrado con `auth_lookup_user()`. Dev A ([PR #22](https://github.com/miguelggdev/Omnichannel-Platform/pull/22)): OpenTelemetry, Loguru, métricas, 4 dashboards, 13 alertas y cifrado pgcrypto con índice ciego (teléfono enmascarado en `display_name`); BUG-026 y BUG-003 cerrados. El addendum de operaciones (4 features) sigue sin asignar en la matriz |
| 9 | Canales Adicionales | ✅ Completado (revisión pendiente de PRs #36/#37) | Fase 2. Telegram y Email (#25), Whisper (#26), unificación de contactos (#27), `/vincular` (#29), Webchat WebSocket (#30), despliegue de la migración 009 (#31), cola `media` (#32), tests de transcripción (#33). `answerCallbackQuery`/throttling de Telegram mergeado (PR #34, ADR-061). Adjuntos de email como metadata mergeado (PR #35, ADR-062). Pendientes de merge: widget JS del Webchat (PR #36, ADR-063), fix de seguridad BUG-041 en `/auth/refresh` (PR #37). PR #28 de Dev B cerrado como duplicado |
| 10 | Templates, Clonación & Sentimiento | ⬜ Pendiente | Fase 2 |
| 11 | Webhooks Salientes & CSAT | ⬜ Pendiente | Fase 2 |
| 12 | Agentes Financiero & Marketing | ⬜ Pendiente | Fase 2 |
| 13 | Canal de Voz & Agente Clínico | ⬜ Pendiente | Fase 3 |
| 14 | Sandbox, Multi-idioma & Feature Flags | ⬜ Pendiente | Fase 3 — i18n expandido a 6 idiomas |
| 15 | Frontend Foundation & Panel Admin | ⬜ Pendiente | Fase 4 — Next.js, theme toggle, responsive, i18n UI |
| 16-19 | Módulo de Lead Management con IA | ⬜ Pendiente | Fase 5 — captura, enriquecimiento, calificación, asignación, follow-up, agenda (`specs/sprint-16-19-lead-management.md`, ADRs 021-025, tablas #27-36). **No listado en `docs/sprint-map.html` ni `METHODOLOGY.md`** — esos dos quedaron desactualizados (dicen "15 sprints"), la spec ya existe |

---

## Features Integradas (Sesión 5)

Las siguientes 11 features fueron diseñadas e integradas en los sprints existentes:

| # | Feature | Sprint(s) | Spec File |
|---|---|---|---|
| 1 | Client Onboarding Flow | Sprint 3 | `specs/sprint-03-addendum-onboarding.md` |
| 2 | Business Personalization | Sprint 3 | `specs/sprint-03-addendum-onboarding.md` |
| 3 | Dark/Light Theme Toggle | Sprint 15 | `specs/sprint-15-frontend.md` |
| 4 | Responsive Design | Sprint 15 | `specs/sprint-15-frontend.md` |
| 5 | Multi-idioma (6 idiomas) | Sprint 14 + 15 | `specs/sprint-14-sandbox-i18n.md` (backend) + `specs/sprint-15-frontend.md` (UI) |
| 6 | Celery/Redis Admin Panel | Sprint 8 | `specs/sprint-08-addendum-ops.md` |
| 7 | Super Admin Telegram Bot | Sprint 8 | `specs/sprint-08-addendum-ops.md` |
| 8 | Agent Activity Logging | Sprint 7 (reprogramado 2026-09-16, ver nota en el spec) | `specs/sprint-07-addendum-agent-logging.md` |
| 9 | Client Management (deactivation/alerts) | Sprint 3 | `specs/sprint-03-addendum-onboarding.md` |
| 10 | Backup & Replication (VPS) | Sprint 8 | `specs/sprint-08-addendum-ops.md` |
| 11 | Security Policies (Cloudflare, firewall) | Sprint 8 | `specs/sprint-08-addendum-ops.md` |
| 12 | Admin Assistant (voz/chat) | Sprint 3 + 8 + 15 | `specs/sprint-03-addendum-admin-assistant.md` |

---

## Métricas de Progreso

- **Tests pasando:** 27 / 27 (unit + security; 34 RLS/DB tests skip hasta conexión BD)
- **Tablas creadas:** 26 / 26 (18 MVP + 6 Fase 2 + 1 agent_action_logs + 1 admin_assistant_history)
- **Endpoints implementados:** 8 (health, auth x4, tenants x3)
- **Agentes LangGraph:** 0 / 7 (nodos)
- **Proveedores de mensajería:** 0 / 2 (YCloud MVP + Meta MVP: Instagram DM + Facebook Messenger)
- **Cobertura de tests:** N/A
- **DDL verificado:** ✅ PostgreSQL 16 + pgvector 0.6.0 (pendiente re-verificar con tabla 26)
- **RLS verificado:** ✅ 24 tablas con aislamiento confirmado (pendiente tabla 26)
- **Sprints especificados:** 15 / 15 (14 originales + 1 Frontend)
- **Features nuevas integradas:** 12 / 12
