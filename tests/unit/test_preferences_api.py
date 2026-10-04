"""Tests de `/api/v1/settings/preferences` (Sprint 14b, ADR-077)."""

import json
import uuid
from typing import Any

import pytest

from app.api.v1 import preferences as modulo
from tests.unit.agent_doubles import FakeSession, parchear_tenant_session

URL = "/api/v1/settings/preferences"
ROLES = ["super_admin", "admin", "supervisor", "agent", "medical"]


def _sesion(monkeypatch: pytest.MonkeyPatch, *resultados: Any) -> FakeSession:
    return parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=list(resultados)))


class TestLeer:
    @pytest.mark.parametrize("rol", ROLES)
    async def test_cualquier_rol_lee_las_suyas(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch, rol: str
    ) -> None:
        _sesion(monkeypatch, ({"ui_language": "fr", "theme": "dark"},))
        cliente = authenticated_client_factory(role=rol)

        response = await cliente.get(URL)

        assert response.status_code == 200, response.text
        assert response.json() == {"ui_language": "fr", "theme": "dark"}

    async def test_sin_nada_guardado_devuelve_los_defaults(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _sesion(monkeypatch, ({},))
        cliente = authenticated_client_factory(role="agent")

        response = await cliente.get(URL)

        assert response.json() == {"ui_language": "es", "theme": "system"}

    async def test_un_valor_guardado_a_mano_que_es_basura_cae_al_default(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _sesion(monkeypatch, ({"ui_language": "klingon", "theme": 7},))
        cliente = authenticated_client_factory(role="agent")

        response = await cliente.get(URL)

        assert response.json() == {"ui_language": "es", "theme": "system"}

    async def test_un_usuario_que_ya_no_existe_da_404(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _sesion(monkeypatch, None)
        cliente = authenticated_client_factory(role="agent")

        response = await cliente.get(URL)

        assert response.status_code == 404

    async def test_sin_token_no_hay_acceso(self, api_client: Any) -> None:
        response = await api_client.get(URL)

        assert response.status_code in (401, 403)


class TestCambiar:
    @pytest.mark.parametrize("rol", ROLES)
    async def test_cualquier_rol_cambia_las_suyas(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch, rol: str
    ) -> None:
        _sesion(monkeypatch, ("id",))
        cliente = authenticated_client_factory(role=rol)

        response = await cliente.put(URL, json={"ui_language": "de", "theme": "light"})

        assert response.status_code == 200, response.text
        assert response.json() == {"updated": {"ui_language": "de", "theme": "light"}}

    async def test_solo_se_guarda_lo_enviado_y_para_el_usuario_del_token(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        tenant_a_id: uuid.UUID,
    ) -> None:
        sesion = _sesion(monkeypatch, ("id",))
        usuario = uuid.uuid4()
        cliente = authenticated_client_factory(role="agent", user_id=usuario)

        await cliente.put(URL, json={"theme": "dark"})

        assert json.loads(sesion.params[0]["parche"]) == {"theme": "dark"}
        assert sesion.params[0]["user_id"] == str(usuario)
        assert sesion.params[0]["client_id"] == str(tenant_a_id)

    async def test_el_usuario_y_el_tenant_salen_del_token_no_del_cuerpo(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sesion = _sesion(monkeypatch, ("id",))
        usuario = uuid.uuid4()
        cliente = authenticated_client_factory(role="agent", user_id=usuario)

        await cliente.put(
            URL,
            json={"theme": "dark", "user_id": str(uuid.uuid4()), "client_id": str(uuid.uuid4())},
        )

        assert sesion.params[0]["user_id"] == str(usuario)
        assert "user_id" not in json.loads(sesion.params[0]["parche"])

    async def test_no_se_pueden_guardar_claves_ajenas(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sesion = _sesion(monkeypatch, ("id",))
        cliente = authenticated_client_factory(role="admin")

        await cliente.put(URL, json={"theme": "dark", "is_admin": True, "role": "super_admin"})

        assert json.loads(sesion.params[0]["parche"]) == {"theme": "dark"}

    @pytest.mark.parametrize(
        "cuerpo",
        [{"ui_language": "nl"}, {"ui_language": "ES"}, {"theme": "azul"}, {"ui_language": 3}],
    )
    async def test_valores_invalidos_se_rechazan(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        cuerpo: dict[str, Any],
    ) -> None:
        _sesion(monkeypatch, ("id",))
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.put(URL, json=cuerpo)

        assert response.status_code in (400, 422)

    @pytest.mark.parametrize("cuerpo", [{}, {"ui_language": None, "theme": None}])
    async def test_sin_ninguna_preferencia_da_400(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        cuerpo: dict[str, Any],
    ) -> None:
        _sesion(monkeypatch, ("id",))
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.put(URL, json=cuerpo)

        assert response.status_code == 400

    async def test_un_usuario_que_ya_no_existe_da_404(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _sesion(monkeypatch, None)
        cliente = authenticated_client_factory(role="agent")

        response = await cliente.put(URL, json={"theme": "dark"})

        assert response.status_code == 404
