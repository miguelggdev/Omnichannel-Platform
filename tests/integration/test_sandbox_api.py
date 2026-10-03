"""`/api/v1/sandbox` de punta a punta por HTTP, contra PostgreSQL y Redis reales (ADR-078)."""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from tests.integration.test_sandbox_isolation import (
    PROFESIONALES_PROD,
    Prod,
    _agente,
    _atajos,
    _borrar,
    _sembrar,
    _sql,
)

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

URL = "/api/v1/sandbox"
FLAGS = "/api/v1/admin/feature-flags/enable_sandbox"


@pytest_asyncio.fixture
async def prod() -> AsyncGenerator[Prod, None]:
    datos = await _sembrar()
    yield datos
    await _borrar(datos.client_id)


@pytest.fixture
def admin(prod: Prod, authenticated_client_factory: Any) -> Any:
    return authenticated_client_factory(
        role="admin", client_id=prod.client_id, user_id=prod.usuario
    )


async def test_el_recorrido_completo_crea_edita_publica_y_revierte(prod: Prod, admin: Any) -> None:
    assert (await admin.get(URL)).json()["exists"] is False

    creado = await admin.post(URL)
    assert creado.status_code == 201, creado.text
    assert creado.json()["exists"] is True
    assert (await admin.post(URL)).status_code == 409

    editado = await admin.put(
        f"{URL}/agent-config",
        json={"system_prompt": "Prompt desde el sandbox", "config": {"rag_top_k": 20}},
    )
    assert editado.status_code == 200, editado.text
    assert editado.json()["system_prompt"] == "Prompt desde el sandbox"
    assert editado.json()["config"]["rag_top_k"] == 20
    # Produccion sigue como estaba.
    assert (await _agente(prod.client_id)).system_prompt == "Eres el asistente de Sol."

    publicado = await admin.post(f"{URL}/publish")
    assert publicado.status_code == 200, publicado.text
    assert publicado.json() == {"version": 1}
    produccion = await _agente(prod.client_id)
    assert produccion.system_prompt == "Prompt desde el sandbox"
    assert produccion.config["rag_top_k"] == 20
    assert produccion.config["clinical"] == {"professional_contact_ids": PROFESIONALES_PROD}

    historial = (await admin.get(f"{URL}/history")).json()
    assert [(h["version"], h["reason"]) for h in historial] == [(1, "publish")]
    assert historial[0]["created_by"] == str(prod.usuario)

    revertido = await admin.post(f"{URL}/rollback", json={})
    assert revertido.status_code == 200, revertido.text
    assert revertido.json() == {"restored": 1, "saved_as": 2}
    assert (await _agente(prod.client_id)).system_prompt == "Eres el asistente de Sol."
    assert await _atajos(prod.client_id) == ["/hola", "/horario"]

    reiniciado = await admin.post(f"{URL}/reset")
    assert reiniciado.status_code == 200, reiniciado.text
    assert reiniciado.json()["reset_at"] is not None


async def test_las_claves_del_tenant_no_se_editan_en_el_sandbox(prod: Prod, admin: Any) -> None:
    await admin.post(URL)

    respuesta = await admin.put(
        f"{URL}/agent-config", json={"system_prompt": "x", "config": {"feature_flags": {}}}
    )

    assert respuesta.status_code == 400
    assert "feature_flags" in respuesta.json()["message"]
    assert (await admin.get(f"{URL}/agent-config")).json()["system_prompt"] != "x"


async def test_apagar_la_flag_corta_el_acceso_y_encenderla_lo_devuelve(
    prod: Prod, admin: Any
) -> None:
    """La flag se aplica de verdad: pasa por la API de flags, la base y el cache de Redis."""
    assert (await admin.get(URL)).status_code == 200

    assert (await admin.put(FLAGS, json={"value": False})).status_code == 200
    bloqueado = await admin.get(URL)
    assert bloqueado.status_code == 403
    assert "enable_sandbox" in bloqueado.json()["message"]

    assert (await admin.put(FLAGS, json={"value": True})).status_code == 200
    assert (await admin.get(URL)).status_code == 200


async def test_un_tenant_sin_la_flag_no_puede_crear_sandbox(
    authenticated_client_factory: Any,
) -> None:
    prod = await _sembrar()
    try:
        await _sql(
            prod.client_id,
            "UPDATE agent_configs SET config = config - 'feature_flags' WHERE client_id = :cid",
        )
        cliente = authenticated_client_factory(
            role="admin", client_id=prod.client_id, user_id=prod.usuario
        )

        respuesta = await cliente.post(URL)

        assert respuesta.status_code == 403
        assert (await _sql(prod.client_id, "SELECT 1 FROM tenant_sandboxes")) == []
    finally:
        await _borrar(prod.client_id)


async def test_un_agente_no_puede_publicar(
    prod: Prod, admin: Any, authenticated_client_factory: Any
) -> None:
    await admin.post(URL)
    agente = authenticated_client_factory(
        role="agent", client_id=prod.client_id, user_id=uuid.uuid4()
    )

    assert (await agente.post(f"{URL}/publish")).status_code == 403
    assert (await agente.post(f"{URL}/rollback", json={})).status_code == 403


async def test_el_mensaje_de_prueba_responde_por_http(prod: Prod, admin: Any) -> None:
    """Con el presupuesto del sandbox agotado el grafo escala sin llamar al LLM."""
    await admin.post(URL)
    sandbox = (await admin.get(URL)).json()["sandbox_client_id"]
    await _sql(
        uuid.UUID(sandbox),
        "UPDATE token_budgets SET used_tokens = total_budget + 1 WHERE client_id = :cid",
    )

    respuesta = await admin.post(f"{URL}/messages", json={"text": "Hola, necesito ayuda"})

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["requires_handoff"] is True
    assert cuerpo["handoff_reason"] == "budget_exceeded"
    assert cuerpo["response"]
