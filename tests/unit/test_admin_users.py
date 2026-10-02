"""Tests de POST /api/v1/admin/users: alta de usuarios del tenant (rol `medical`, ADR-073)."""

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.api.v1 import admin as modulo
from app.core.security import verify_password
from app.models.user import User
from tests.unit.agent_doubles import FakeSession, parchear_tenant_session

URL = "/api/v1/admin/users"


def _cuerpo(**cambios: Any) -> dict[str, Any]:
    return {
        "email": "doctora@clinica.example",
        "password": "contrasena-segura-1",
        "first_name": "Ana",
        "last_name": "Medina",
        "role": "medical",
        **cambios,
    }


class _SesionDeUsuarios(FakeSession):
    """FakeSession que completa los defaults que pone la base al insertar."""

    def __init__(self, error_en_flush: Exception | None = None) -> None:
        super().__init__()
        self._error_en_flush = error_en_flush

    async def flush(self) -> None:
        if self._error_en_flush is not None:
            raise self._error_en_flush
        await super().flush()

    async def refresh(self, obj: Any) -> None:
        ahora = datetime.now(timezone.utc)
        obj.is_active = True
        obj.last_login_at = None
        obj.created_at = ahora
        obj.updated_at = ahora


class TestAltaDeUsuarios:
    async def test_un_admin_crea_un_medico_en_su_tenant(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        tenant_a_id: uuid.UUID,
    ) -> None:
        sesion = parchear_tenant_session(monkeypatch, modulo, _SesionDeUsuarios())
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.post(URL, json=_cuerpo())

        assert response.status_code == 201, response.text
        cuerpo = response.json()
        assert cuerpo["role"] == "medical"
        assert cuerpo["client_id"] == str(tenant_a_id)
        assert "password" not in cuerpo
        assert "password_hash" not in cuerpo
        (creado,) = sesion.agregados_de(User)
        assert creado.client_id == tenant_a_id
        # Se guarda un hash bcrypt, nunca el password.
        assert creado.password_hash != "contrasena-segura-1"
        assert verify_password("contrasena-segura-1", creado.password_hash)

    async def test_el_tenant_sale_del_token_no_del_cuerpo(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        tenant_a_id: uuid.UUID,
    ) -> None:
        sesion = parchear_tenant_session(monkeypatch, modulo, _SesionDeUsuarios())
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.post(URL, json=_cuerpo(client_id=str(uuid.uuid4())))

        assert response.status_code == 201, response.text
        assert sesion.agregados_de(User)[0].client_id == tenant_a_id

    @pytest.mark.parametrize("rol", ["super_admin", "root", ""])
    async def test_no_se_puede_asignar_super_admin_ni_un_rol_inventado(
        self, authenticated_client_factory: Any, rol: str
    ) -> None:
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.post(URL, json=_cuerpo(role=rol))

        assert response.status_code in (400, 422)

    @pytest.mark.parametrize("rol", ["agent", "supervisor", "medical"])
    async def test_solo_administradores_dan_de_alta(
        self, authenticated_client_factory: Any, rol: str
    ) -> None:
        cliente = authenticated_client_factory(role=rol)

        response = await cliente.post(URL, json=_cuerpo())

        assert response.status_code == 403

    async def test_email_duplicado_da_409(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        error = IntegrityError("INSERT INTO users", {}, Exception("users_email_key"))
        parchear_tenant_session(monkeypatch, modulo, _SesionDeUsuarios(error_en_flush=error))
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.post(URL, json=_cuerpo())

        assert response.status_code == 409
        assert response.json()["error_code"] == "DUPLICATE"

    @pytest.mark.parametrize(
        "cambios",
        [{"password": "corta"}, {"email": "no-es-email"}, {"first_name": ""}],
    )
    async def test_datos_invalidos_se_rechazan(
        self, authenticated_client_factory: Any, cambios: dict[str, Any]
    ) -> None:
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.post(URL, json=_cuerpo(**cambios))

        assert response.status_code in (400, 422)
