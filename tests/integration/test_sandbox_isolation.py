"""Sandbox por tenant contra PostgreSQL real (Sprint 14c, ADR-078).

Lo que los dobles de sesion no pueden probar: que el sandbox es un tenant aislado
por RLS, que publicar es atomico (un fallo a mitad no cambia nada), que el
rollback funciona y que las claves propias del tenant (`clinical`, `marketing`,
`feature_flags`) no viajan entre sandbox y produccion.
"""

import json
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.database import engine, tenant_session
from app.services import sandbox as svc

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

PROFESIONALES_PROD = [str(uuid.uuid4())]
CONFIG_PROD: dict[str, Any] = {
    "enabled_agents": ["rag", "clinical"],
    "rag_top_k": 5,
    "clinical": {"professional_contact_ids": PROFESIONALES_PROD},
    "marketing": {"operator_contact_ids": ["m-1"]},
    "feature_flags": {"enable_sandbox": True, "enable_clinical": True},
}
EMBEDDING = "[" + ",".join(["0.01"] * 1536) + "]"


def _url_admin() -> str:
    return os.environ.get(
        "DATABASE_URL_ADMIN",
        "postgresql+asyncpg://test_user:test_password@localhost:5432/test_omnichannel",
    )


class Prod:
    """Ids del tenant de produccion sembrado."""

    def __init__(self, client_id: uuid.UUID, usuario: uuid.UUID) -> None:
        self.client_id = client_id
        self.usuario = usuario


async def _sql(client_id: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    async with tenant_session(client_id) as session:
        resultado = await session.execute(text(sql), {"cid": str(client_id), **params})
        return list(resultado.all()) if resultado.returns_rows else []


async def _sembrar() -> Prod:
    await engine.dispose()
    client_id, usuario = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active, settings) VALUES "
                "(:id, 'Clinica Sol', :slug, 'free', true, CAST(:s AS jsonb))"
            ),
            {
                "id": str(client_id),
                "slug": f"sbx-{client_id.hex[:8]}",
                "s": json.dumps({"default_language": "pt"}),
            },
        )
        await session.execute(
            text(
                "INSERT INTO users (id, client_id, email, password_hash, first_name, last_name, "
                "role) VALUES (:u, :cid, :e, 'x', 'Ana', 'Admin', 'admin')"
            ),
            {"u": str(usuario), "cid": str(client_id), "e": f"{usuario.hex[:8]}@sbx.test"},
        )
        await session.execute(
            text(
                "INSERT INTO agent_configs (client_id, name, system_prompt, model, temperature, "
                "config, is_active) VALUES (:cid, 'Asistente', 'Eres el asistente de Sol.', "
                "'gpt-4o', 0.3, CAST(:cfg AS jsonb), true)"
            ),
            {"cid": str(client_id), "cfg": json.dumps(CONFIG_PROD)},
        )
        for atajo, contenido in (("/hola", "Hola, bienvenido"), ("/horario", "8 a 18")):
            await session.execute(
                text(
                    "INSERT INTO quick_replies (client_id, shortcut, title, content, created_by) "
                    "VALUES (:cid, :a, :a, :c, :u)"
                ),
                {"cid": str(client_id), "a": atajo, "c": contenido, "u": str(usuario)},
            )
        for titulo in ("FAQ", "Precios"):
            doc = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO documents (id, client_id, title, file_url, status, chunk_count) "
                    "VALUES (:d, :cid, :t, :url, 'ready', 2)"
                ),
                {"d": str(doc), "cid": str(client_id), "t": titulo, "url": f"s3://prod/{titulo}"},
            )
            for i in range(2):
                await session.execute(
                    text(
                        "INSERT INTO document_chunks (client_id, document_id, chunk_index, "
                        "content, embedding, token_count) VALUES (:cid, :d, :i, :c, "
                        "CAST(:e AS vector), 10)"
                    ),
                    {
                        "cid": str(client_id),
                        "d": str(doc),
                        "i": i,
                        "c": f"{titulo} fragmento {i}",
                        "e": EMBEDDING,
                    },
                )
    return Prod(client_id, usuario)


async def _borrar(*tenants: uuid.UUID) -> None:
    """Limpieza con el superusuario del CI: sin RLS y con los triggers desactivados."""
    admin = create_async_engine(_url_admin())
    try:
        async with admin.begin() as conexion:
            await conexion.execute(text("SET session_replication_role = replica"))
            sandboxes = (
                (
                    await conexion.execute(
                        text(
                            "SELECT sandbox_client_id FROM tenant_sandboxes WHERE client_id = ANY(:t)"
                        ),
                        {"t": [str(t) for t in tenants]},
                    )
                )
                .scalars()
                .all()
            )
            todos = [str(t) for t in tenants] + [str(s) for s in sandboxes]
            tablas = (
                (
                    await conexion.execute(
                        text(
                            "SELECT table_name FROM information_schema.columns "
                            "WHERE column_name = 'client_id' AND table_schema = 'public'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            for tabla in tablas:
                await conexion.execute(
                    text(f"DELETE FROM {tabla} WHERE client_id = ANY(CAST(:t AS uuid[]))"),  # noqa: S608
                    {"t": todos},
                )
            await conexion.execute(
                text("DELETE FROM clients WHERE id = ANY(CAST(:t AS uuid[]))"), {"t": todos}
            )
    finally:
        await admin.dispose()


@pytest_asyncio.fixture
async def prod() -> AsyncGenerator[Prod, None]:
    datos = await _sembrar()
    yield datos
    await _borrar(datos.client_id)


@pytest_asyncio.fixture
async def con_sandbox(prod: Prod) -> tuple[Prod, uuid.UUID]:
    sandbox_id = await svc.crear_sandbox(prod.client_id, prod.usuario)
    return prod, sandbox_id


async def _agente(client_id: uuid.UUID) -> Any:
    (fila,) = await _sql(
        client_id,
        "SELECT system_prompt, temperature, config FROM agent_configs WHERE client_id = :cid",
    )
    return fila


async def _atajos(client_id: uuid.UUID) -> list[str]:
    return [f[0] for f in await _sql(client_id, "SELECT shortcut FROM quick_replies ORDER BY 1")]


# ─── Creacion y aislamiento ──────────────────────────────────────────────────


async def test_el_sandbox_clona_la_configuracion_y_la_base_de_conocimiento(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    prod, sandbox_id = con_sandbox

    (cliente,) = await _sql(
        sandbox_id, "SELECT name, is_sandbox, settings FROM clients WHERE id = :cid"
    )
    agente = await _agente(sandbox_id)
    docs = await _sql(sandbox_id, "SELECT title, file_url, metadata FROM documents ORDER BY 1")
    (chunks,) = await _sql(sandbox_id, "SELECT count(*) FROM document_chunks")
    (iguales,) = await _sql(
        sandbox_id,
        "SELECT count(*) FROM document_chunks WHERE embedding <=> CAST(:e AS vector) < 0.0001",
        e=EMBEDDING,
    )
    (presupuesto,) = await _sql(sandbox_id, "SELECT total_budget FROM token_budgets")

    assert cliente.name == "Clinica Sol (sandbox)"
    assert cliente.is_sandbox is True
    assert cliente.settings == {"default_language": "pt"}
    assert agente.system_prompt == "Eres el asistente de Sol."
    assert await _atajos(sandbox_id) == ["/hola", "/horario"]
    assert [d.title for d in docs] == ["FAQ", "Precios"]
    assert chunks[0] == 4
    assert iguales[0] == 4  # los embeddings viajan intactos
    assert docs[0].metadata["sandbox_copy_of"]
    assert presupuesto[0] == get_settings().SANDBOX_TOKEN_BUDGET

    estado = await svc.obtener_estado(prod.client_id)
    assert estado["exists"] is True
    assert estado["sandbox_client_id"] == sandbox_id


async def test_el_sandbox_no_hereda_las_claves_que_son_del_tenant(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    """`clinical` y `marketing` llevan ids de contactos de produccion: no tienen sentido alla."""
    _, sandbox_id = con_sandbox

    config = (await _agente(sandbox_id)).config

    assert "clinical" not in config
    assert "marketing" not in config
    assert config["enabled_agents"] == ["rag", "clinical"]
    assert config["rag_top_k"] == 5
    assert config["feature_flags"] == CONFIG_PROD["feature_flags"]


async def test_un_tenant_no_ve_los_datos_del_otro(con_sandbox: tuple[Prod, uuid.UUID]) -> None:
    """RLS: ni produccion ve el sandbox ni el sandbox ve produccion."""
    prod, sandbox_id = con_sandbox

    desde_prod = await _sql(
        prod.client_id, "SELECT 1 FROM agent_configs WHERE client_id = :otro", otro=str(sandbox_id)
    )
    desde_sandbox = await _sql(
        sandbox_id, "SELECT 1 FROM agent_configs WHERE client_id = :otro", otro=str(prod.client_id)
    )
    cliente_visible = await _sql(
        prod.client_id, "SELECT 1 FROM clients WHERE id = :otro", otro=str(sandbox_id)
    )

    assert desde_prod == desde_sandbox == cliente_visible == []


async def test_editar_el_sandbox_no_toca_produccion(con_sandbox: tuple[Prod, uuid.UUID]) -> None:
    prod, sandbox_id = con_sandbox

    await svc.actualizar_config_sandbox(
        prod.client_id,
        {"system_prompt": "Prompt nuevo", "temperature": 0.9, "config": {"rag_top_k": 12}},
    )

    sandbox = await _agente(sandbox_id)
    produccion = await _agente(prod.client_id)
    assert sandbox.system_prompt == "Prompt nuevo"
    assert sandbox.config["rag_top_k"] == 12
    assert sandbox.config["enabled_agents"] == ["rag", "clinical"]  # se mezcla, no se reemplaza
    assert produccion.system_prompt == "Eres el asistente de Sol."
    assert produccion.config["rag_top_k"] == 5


async def test_el_config_del_sandbox_rechaza_las_claves_del_tenant(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    prod, sandbox_id = con_sandbox

    with pytest.raises(svc.ClaveDelTenantError) as error:
        await svc.actualizar_config_sandbox(
            prod.client_id, {"system_prompt": "x", "config": {"clinical": {}, "rag_top_k": 1}}
        )

    assert error.value.claves == ["clinical"]
    # No cambio nada, ni siquiera lo permitido que venia en la misma peticion.
    assert (await _agente(sandbox_id)).system_prompt == "Eres el asistente de Sol."


async def test_un_tenant_solo_puede_tener_un_sandbox(con_sandbox: tuple[Prod, uuid.UUID]) -> None:
    prod, _ = con_sandbox

    with pytest.raises(svc.SandboxYaExisteError):
        await svc.crear_sandbox(prod.client_id)


async def test_un_sandbox_no_puede_tener_su_propio_sandbox(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    _, sandbox_id = con_sandbox

    with pytest.raises(svc.SandboxDeSandboxError):
        await svc.crear_sandbox(sandbox_id)


async def test_sin_sandbox_las_operaciones_fallan_limpio(prod: Prod) -> None:
    assert await svc.obtener_estado(prod.client_id) == {"exists": False}
    for operacion in (
        svc.publicar(prod.client_id),
        svc.reiniciar_sandbox(prod.client_id),
        svc.leer_config_sandbox(prod.client_id),
    ):
        with pytest.raises(svc.SandboxNoExisteError):
            await operacion


# ─── Publicacion atomica ─────────────────────────────────────────────────────


async def _editar_sandbox(prod: Prod, sandbox_id: uuid.UUID) -> None:
    """Cambia el prompt, la configuracion y las respuestas rapidas del sandbox."""
    await svc.actualizar_config_sandbox(
        prod.client_id,
        {
            "system_prompt": "Prompt publicado",
            "temperature": 0.9,
            "config": {"rag_top_k": 12, "enabled_agents": ["rag"]},
        },
    )
    await _sql(sandbox_id, "DELETE FROM quick_replies WHERE shortcut = '/horario'")
    await _sql(
        sandbox_id,
        "INSERT INTO quick_replies (client_id, shortcut, title, content) "
        "VALUES (:cid, '/promo', 'Promo', '2x1')",
    )


async def test_publicar_lleva_la_configuracion_a_produccion_y_conserva_las_claves_del_tenant(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    prod, sandbox_id = con_sandbox
    await _editar_sandbox(prod, sandbox_id)

    version = await svc.publicar(prod.client_id, prod.usuario)

    produccion = await _agente(prod.client_id)
    assert version == 1
    assert produccion.system_prompt == "Prompt publicado"
    assert produccion.temperature == pytest.approx(0.9)
    assert produccion.config["rag_top_k"] == 12
    assert produccion.config["enabled_agents"] == ["rag"]
    # Lo que es del tenant sigue siendo el de produccion.
    assert produccion.config["clinical"] == {"professional_contact_ids": PROFESIONALES_PROD}
    assert produccion.config["marketing"] == {"operator_contact_ids": ["m-1"]}
    assert produccion.config["feature_flags"] == CONFIG_PROD["feature_flags"]
    assert await _atajos(prod.client_id) == ["/hola", "/promo"]
    estado = await svc.obtener_estado(prod.client_id)
    assert estado["last_published_at"] is not None
    assert estado["versions"] == 1


async def test_publicar_guarda_antes_lo_que_habia_en_produccion(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    prod, sandbox_id = con_sandbox
    await _editar_sandbox(prod, sandbox_id)

    await svc.publicar(prod.client_id, prod.usuario)

    historial = await svc.listar_historial(prod.client_id)
    assert [(h["version"], h["reason"], h["created_by"]) for h in historial] == [
        (1, "publish", prod.usuario)
    ]
    (guardado,) = await _sql(
        prod.client_id,
        "SELECT config_data FROM config_history WHERE config_type = 'agent_configs' AND version = 1",
    )
    assert guardado.config_data[0]["system_prompt"] == "Eres el asistente de Sol."


async def test_un_fallo_a_mitad_de_la_publicacion_no_cambia_nada(
    con_sandbox: tuple[Prod, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomicidad: aunque los agentes ya se hubieran reemplazado, todo se deshace."""
    prod, sandbox_id = con_sandbox
    await _editar_sandbox(prod, sandbox_id)

    async def _revienta(*_: Any, **__: Any) -> None:
        raise RuntimeError("fallo a mitad de la publicacion")

    monkeypatch.setattr(svc, "_reemplazar_respuestas", _revienta)

    with pytest.raises(RuntimeError, match="a mitad"):
        await svc.publicar(prod.client_id, prod.usuario)

    produccion = await _agente(prod.client_id)
    assert produccion.system_prompt == "Eres el asistente de Sol."
    assert produccion.config["rag_top_k"] == 5
    assert await _atajos(prod.client_id) == ["/hola", "/horario"]
    assert await svc.listar_historial(prod.client_id) == []
    assert (await svc.obtener_estado(prod.client_id))["last_published_at"] is None


async def test_no_se_publica_un_sandbox_sin_agente_para_no_vaciar_produccion(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    prod, sandbox_id = con_sandbox
    await _sql(sandbox_id, "DELETE FROM agent_configs WHERE client_id = :cid")

    with pytest.raises(svc.SinConfiguracionError):
        await svc.publicar(prod.client_id)

    assert (await _agente(prod.client_id)).system_prompt == "Eres el asistente de Sol."
    assert await svc.listar_historial(prod.client_id) == []


# ─── Rollback ────────────────────────────────────────────────────────────────


async def test_revertir_restaura_la_version_anterior(con_sandbox: tuple[Prod, uuid.UUID]) -> None:
    prod, sandbox_id = con_sandbox
    await _editar_sandbox(prod, sandbox_id)
    await svc.publicar(prod.client_id, prod.usuario)

    resultado = await svc.revertir(prod.client_id, prod.usuario)

    assert resultado == {"restored": 1, "saved_as": 2}
    produccion = await _agente(prod.client_id)
    assert produccion.system_prompt == "Eres el asistente de Sol."
    assert produccion.config["rag_top_k"] == 5
    assert await _atajos(prod.client_id) == ["/hola", "/horario"]


async def test_revertir_no_deshace_lo_que_cambio_en_las_claves_del_tenant(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    """Si se agrego un profesional clinico despues de publicar, volver atras no lo borra."""
    prod, sandbox_id = con_sandbox
    await _editar_sandbox(prod, sandbox_id)
    await svc.publicar(prod.client_id, prod.usuario)
    nuevos = [*PROFESIONALES_PROD, str(uuid.uuid4())]
    await _sql(
        prod.client_id,
        "UPDATE agent_configs SET config = jsonb_set(config, '{clinical}', CAST(:c AS jsonb)) "
        "WHERE client_id = :cid",
        c=json.dumps({"professional_contact_ids": nuevos}),
    )

    await svc.revertir(prod.client_id, prod.usuario)

    assert (await _agente(prod.client_id)).config["clinical"] == {
        "professional_contact_ids": nuevos
    }


async def test_volver_atras_tambien_se_puede_deshacer(con_sandbox: tuple[Prod, uuid.UUID]) -> None:
    prod, sandbox_id = con_sandbox
    await _editar_sandbox(prod, sandbox_id)
    await svc.publicar(prod.client_id, prod.usuario)
    await svc.revertir(prod.client_id, prod.usuario)  # guarda lo publicado como v2

    resultado = await svc.revertir(prod.client_id, prod.usuario, version=2)

    assert resultado["restored"] == 2
    assert (await _agente(prod.client_id)).system_prompt == "Prompt publicado"
    versiones = [(h["version"], h["reason"]) for h in await svc.listar_historial(prod.client_id)]
    assert versiones == [(3, "rollback"), (2, "rollback"), (1, "publish")]


async def test_revertir_sin_historial_o_a_una_version_inexistente_falla_sin_tocar_nada(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    prod, _ = con_sandbox

    with pytest.raises(svc.VersionInexistenteError):
        await svc.revertir(prod.client_id)
    await svc.publicar(prod.client_id, prod.usuario)
    with pytest.raises(svc.VersionInexistenteError):
        await svc.revertir(prod.client_id, version=99)

    assert [h["version"] for h in await svc.listar_historial(prod.client_id)] == [1]


# ─── Reset y mensajes de prueba ──────────────────────────────────────────────


async def test_reiniciar_descarta_los_cambios_y_vuelve_a_copiar_produccion(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    prod, sandbox_id = con_sandbox
    await _editar_sandbox(prod, sandbox_id)

    await svc.reiniciar_sandbox(prod.client_id)

    agente = await _agente(sandbox_id)
    assert agente.system_prompt == "Eres el asistente de Sol."
    assert agente.config["rag_top_k"] == 5
    assert await _atajos(sandbox_id) == ["/hola", "/horario"]
    (docs,) = await _sql(sandbox_id, "SELECT count(*) FROM documents")
    (chunks,) = await _sql(sandbox_id, "SELECT count(*) FROM document_chunks")
    assert (docs[0], chunks[0]) == (2, 4)  # no se duplican al reiniciar
    assert (await svc.obtener_estado(prod.client_id))["reset_at"] is not None


# Tablas con `client_id` que un sandbox no puede llenar (no hay usuarios, ni canales reales,
# ni historia clinica, ni campanas): por eso el reset no las toca. Si aparece una tabla nueva,
# este test obliga a decidir si el reset debe vaciarla.
_NO_PURGADAS = {
    "audit_logs",  # el rastro; su trigger lo reescribiria con cada borrado
    "token_budgets",  # el consumo del mes sobrevive al reset: es el tope de gasto del sandbox
    "token_usage_logs",
    "call_records",
    "campaigns",
    "clinical_records",
    "config_history",
    # Leads (Sprint 16, ADR-082): ni se clonan (son datos de negocio con PII) ni se purgan, porque
    # nada de un sandbox los crea: no tiene usuarios, y `capture_lookup_source()` excluye los
    # tenants sandbox. REVISAR en los Sprints 17-18: si un agente crea leads desde una
    # conversacion, `leads` pasa a `_TABLAS_DE_PRUEBA` para que el reset los borre.
    # `lead_scores` (Sprint 17) sigue a `leads`: sin leads no hay scores; ON DELETE CASCADE.
    "lead_activities",
    "lead_pipeline_stages",
    "lead_scores",
    # Secuencias (Sprint 18): las crea un usuario del panel y un sandbox no tiene usuarios ni
    # leads; las inscripciones siguen a `leads` (ON DELETE CASCADE).
    "lead_sequence_enrollments",
    "lead_sequence_steps",
    "lead_sequences",
    "lead_sources",
    "leads",
    "patient_consents",
    "service_types",
    "tags",
    "tenant_sandboxes",
    "tenant_webhooks",
    "users",
}


async def test_el_reset_cubre_todas_las_tablas_con_client_id() -> None:
    admin = create_async_engine(_url_admin())
    try:
        async with admin.connect() as conexion:
            tablas = set(
                (
                    await conexion.execute(
                        text(
                            "SELECT table_name FROM information_schema.columns "
                            "WHERE column_name = 'client_id' AND table_schema = 'public'"
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await admin.dispose()

    cubiertas = set(svc._TABLAS_DE_PRUEBA) | set(svc._TABLAS_CLONADAS) | _NO_PURGADAS

    assert tablas - cubiertas == set(), (
        f"tablas nuevas sin decidir en el reset: {tablas - cubiertas}"
    )
    assert (set(svc._TABLAS_DE_PRUEBA) | set(svc._TABLAS_CLONADAS)) <= tablas  # sin erratas


async def test_un_mensaje_de_prueba_recorre_el_grafo_real_sin_enviar_nada_fuera(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    """Con el presupuesto del sandbox agotado el grafo escala a humano sin llamar al LLM:
    queda probado el canal `sandbox` de punta a punta (contacto, proveedor, mensaje guardado)."""
    prod, sandbox_id = con_sandbox
    await _sql(
        sandbox_id, "UPDATE token_budgets SET used_tokens = total_budget + 1 WHERE client_id = :cid"
    )

    resultado = await svc.enviar_mensaje_de_prueba(
        prod.client_id, "Hola, necesito ayuda", prod.usuario
    )

    assert resultado["requires_handoff"] is True
    assert resultado["handoff_reason"] == "budget_exceeded"
    mensajes = await _sql(
        sandbox_id, "SELECT direction, content FROM messages ORDER BY created_at, direction DESC"
    )
    assert [m.direction for m in mensajes] == ["inbound", "outbound"]
    assert mensajes[0].content == "Hola, necesito ayuda"
    (canal,) = await _sql(sandbox_id, "SELECT DISTINCT channel FROM contact_identifiers")
    assert canal.channel == "sandbox"
    # Produccion no se entero de nada.
    assert await _sql(prod.client_id, "SELECT 1 FROM messages") == []
    assert await _sql(prod.client_id, "SELECT 1 FROM conversations") == []


async def test_una_conversacion_que_paso_a_un_humano_se_reabre_en_el_siguiente_mensaje(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    """En el sandbox no hay a quien atender un handoff: el bot no volveria a contestar."""
    prod, sandbox_id = con_sandbox
    await _sql(
        sandbox_id, "UPDATE token_budgets SET used_tokens = total_budget + 1 WHERE client_id = :cid"
    )
    primero = await svc.enviar_mensaje_de_prueba(prod.client_id, "uno", prod.usuario)

    segundo = await svc.enviar_mensaje_de_prueba(prod.client_id, "dos", prod.usuario)

    assert segundo["conversation_id"] != primero["conversation_id"]
    estados = await _sql(sandbox_id, "SELECT status FROM conversations ORDER BY created_at")
    assert [e.status for e in estados] == ["resolved", "waiting_human"]


async def test_reiniciar_borra_las_conversaciones_de_prueba(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    prod, sandbox_id = con_sandbox
    await _sql(
        sandbox_id, "UPDATE token_budgets SET used_tokens = total_budget + 1 WHERE client_id = :cid"
    )
    await svc.enviar_mensaje_de_prueba(prod.client_id, "hola", prod.usuario)

    await svc.reiniciar_sandbox(prod.client_id)

    for tabla in svc._TABLAS_DE_PRUEBA:
        filas = await _sql(sandbox_id, f"SELECT 1 FROM {tabla} WHERE client_id = :cid")  # noqa: S608
        assert filas == [], tabla


# ─── Ajustes tras la revision de codigo ──────────────────────────────────────


async def test_reiniciar_no_devuelve_el_presupuesto_gastado(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    """Si el reset lo repusiera, mensaje + reset en bucle saltaria el tope de gasto en el LLM."""
    prod, sandbox_id = con_sandbox
    await _sql(
        sandbox_id, "UPDATE token_budgets SET used_tokens = total_budget + 7 WHERE client_id = :cid"
    )

    await svc.reiniciar_sandbox(prod.client_id)

    (presupuesto,) = await _sql(
        sandbox_id, "SELECT used_tokens > total_budget AS agotado FROM token_budgets"
    )
    assert presupuesto.agotado is True
    respuesta = await svc.enviar_mensaje_de_prueba(prod.client_id, "hola", prod.usuario)
    assert respuesta["handoff_reason"] == "budget_exceeded"


async def test_los_barridos_de_celery_no_recorren_los_sandbox(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    """Auto-cierre, CSAT y campanas barren `list_active_client_ids()`: un sandbox no debe entrar."""
    prod, sandbox_id = con_sandbox

    filas = await _sql(prod.client_id, "SELECT * FROM public.list_active_client_ids()")

    activos = {str(f[0]) for f in filas}
    assert str(prod.client_id) in activos
    assert str(sandbox_id) not in activos


async def test_el_config_del_sandbox_no_trae_las_claves_del_tenant_y_se_puede_reenviar(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    """Lo que devuelve el GET se puede mandar en un PUT sin que este lo rechace."""
    prod, _ = con_sandbox

    leido = await svc.leer_config_sandbox(prod.client_id)

    assert not [k for k in leido["config"] if k in svc.CLAVES_DEL_TENANT]
    reenviado = await svc.actualizar_config_sandbox(prod.client_id, {"config": leido["config"]})
    assert reenviado["config"] == leido["config"]


async def test_un_null_en_un_campo_obligatorio_no_cambia_nada(
    con_sandbox: tuple[Prod, uuid.UUID],
) -> None:
    prod, sandbox_id = con_sandbox

    with pytest.raises(svc.CampoNoAnulableError):
        await svc.actualizar_config_sandbox(prod.client_id, {"name": None, "system_prompt": "zzz"})

    assert (await _agente(sandbox_id)).system_prompt == "Eres el asistente de Sol."


async def test_un_texto_se_puede_borrar_con_null(con_sandbox: tuple[Prod, uuid.UUID]) -> None:
    prod, sandbox_id = con_sandbox

    await svc.actualizar_config_sandbox(prod.client_id, {"system_prompt": None})

    assert (await _agente(sandbox_id)).system_prompt is None
