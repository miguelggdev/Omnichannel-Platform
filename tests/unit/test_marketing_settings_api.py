"""Tests de GET/PUT /api/v1/marketing/settings (ADR-070).

La base se sustituye por `FakeSession` (cola de resultados). Lo que importa: el
RBAC, que un operador tenga que ser un contacto vigente del tenant, y que la
escritura sea una concatenacion JSONB sobre `config.marketing` y no una
reescritura del `config` entero.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.v1 import marketing_settings as modulo
from tests.unit.agent_doubles import FakeSession, parchear_tenant_session

URL = "/api/v1/marketing/settings"


def _config(marketing: dict[str, Any]) -> SimpleNamespace:
    """`AgentConfig` minimo, como lo lee `leer_config_marketing()`."""
    return SimpleNamespace(config={"enabled_agents": ["rag", "marketing"], "marketing": marketing})


class TestLectura:
    async def test_devuelve_operadores_y_plantillas(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        operador = uuid.uuid4()
        parchear_tenant_session(
            monkeypatch,
            modulo,
            FakeSession(
                resultados=[
                    _config(
                        {"operator_contact_ids": [str(operador)], "approved_templates": ["Hola"]}
                    )
                ]
            ),
        )

        response = await authenticated_client.get(URL)

        assert response.status_code == 200
        assert response.json() == {
            "operator_contact_ids": [str(operador)],
            "approved_templates": ["Hola"],
        }

    async def test_basura_escrita_a_mano_no_rompe_el_get(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        valido = uuid.uuid4()
        parchear_tenant_session(
            monkeypatch,
            modulo,
            FakeSession(
                resultados=[
                    _config(
                        {
                            "operator_contact_ids": ["no-es-uuid", str(valido)],
                            "approved_templates": "no-es-lista",
                        }
                    )
                ]
            ),
        )

        response = await authenticated_client.get(URL)

        assert response.json() == {"operator_contact_ids": [str(valido)], "approved_templates": []}

    async def test_sin_agente_devuelve_listas_vacias(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[None]))

        response = await authenticated_client.get(URL)

        assert response.json() == {"operator_contact_ids": [], "approved_templates": []}

    @pytest.mark.parametrize("rol", ["agent", "supervisor"])
    async def test_solo_administradores(self, authenticated_client_factory: Any, rol: str) -> None:
        """Quien decide quien lanza envios masivos es quien administra el tenant."""
        response = await authenticated_client_factory(role=rol).get(URL)

        assert response.status_code == 403


class TestEscritura:
    async def test_autoriza_operadores_vigentes_del_tenant(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        operador = uuid.uuid4()
        sesion = parchear_tenant_session(
            monkeypatch,
            modulo,
            FakeSession(
                resultados=[
                    uuid.uuid4(),  # agent_config activa
                    [operador],  # contactos vigentes
                    None,  # UPDATE
                    _config({"operator_contact_ids": [str(operador)]}),
                ]
            ),
        )

        response = await authenticated_client.put(
            URL, json={"operator_contact_ids": [str(operador), str(operador)]}
        )

        assert response.status_code == 200, response.text
        assert response.json()["operator_contact_ids"] == [str(operador)]
        consulta_contactos = str(sesion.executed[1]).lower()
        assert "contacts.client_id" in consulta_contactos
        assert "merged_into_id is null" in consulta_contactos
        assert "is_gdpr_deleted is false" in consulta_contactos

    async def test_escribe_por_concatenacion_y_no_pisa_el_resto_del_config(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`config` tambien guarda enabled_agents y los umbrales del RAG (BUG-022)."""
        sesion = parchear_tenant_session(
            monkeypatch,
            modulo,
            FakeSession(resultados=[uuid.uuid4(), None, _config({"approved_templates": ["Hola"]})]),
        )

        response = await authenticated_client.put(URL, json={"approved_templates": ["Hola"]})

        assert response.status_code == 200
        update = str(sesion.executed[1])
        assert "config -> 'marketing'" in update
        assert "||" in update
        # Solo plantillas: no se consulto ningun contacto.
        assert len(sesion.executed) == 3

    async def test_un_operador_que_no_es_del_tenant_da_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ajeno = uuid.uuid4()
        sesion = parchear_tenant_session(
            monkeypatch, modulo, FakeSession(resultados=[uuid.uuid4(), []])
        )

        response = await authenticated_client.put(URL, json={"operator_contact_ids": [str(ajeno)]})

        assert response.status_code == 400
        assert str(ajeno) in response.text
        assert len(sesion.executed) == 2, "no llego a escribir"

    async def test_sin_agente_activo_da_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[None]))

        response = await authenticated_client.put(URL, json={"approved_templates": ["Hola"]})

        assert response.status_code == 400
        assert "agente activo" in response.text

    @pytest.mark.parametrize(
        "body",
        [
            {"approved_templates": ["   "]},
            {"approved_templates": ["x" * 5000]},
            {"operator_contact_ids": ["no-es-uuid"]},
        ],
    )
    async def test_valores_invalidos_dan_422(self, authenticated_client: Any, body: dict) -> None:
        response = await authenticated_client.put(URL, json=body)

        assert response.status_code == 422

    async def test_un_agente_no_puede_autorizarse_a_si_mismo(
        self, authenticated_client_factory: Any
    ) -> None:
        response = await authenticated_client_factory(role="agent").put(
            URL, json={"operator_contact_ids": [str(uuid.uuid4())]}
        )

        assert response.status_code == 403
