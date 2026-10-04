"""GET/PUT /api/v1/admin/users: listar, editar, cambiar de rol y desactivar (Sprint 15, fase 2).

La base se sustituye por dobles. Lo que se prueba son las reglas que impiden que alguien
se quede sin acceso o escale privilegios; el recorrido contra PostgreSQL real con RLS esta
en `tests/integration/test_admin_users.py`.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from app.api.v1 import admin as modulo
from app.core.security import verify_password
from app.models.user import User
from tests.unit.agent_doubles import FakeSession, parchear_tenant_session

URL = "/api/v1/admin/users"
AHORA = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _usuario(**kw: Any) -> User:
    return User(
        id=kw.pop("id", uuid.uuid4()),
        client_id=kw.pop("client_id", uuid.uuid4()),
        email=kw.pop("email", "ana@example.com"),
        password_hash=kw.pop("password_hash", "hash-viejo"),
        first_name=kw.pop("first_name", "Ana"),
        last_name=kw.pop("last_name", "Perez"),
        role=kw.pop("role", "agent"),
        is_active=kw.pop("is_active", True),
        last_login_at=None,
        created_at=AHORA,
        updated_at=AHORA,
        **kw,
    )


class _Sesion(FakeSession):
    async def refresh(self, obj: Any) -> None:  # los defaults ya vienen puestos
        return None


def _preparar(
    monkeypatch: pytest.MonkeyPatch, objetivo: User | None, quedan_admins: int = 1
) -> _Sesion:
    # Orden de execute(): el usuario objetivo y, si hace falta, el recuento de admins.
    sesion = _Sesion(resultados=[objetivo, quedan_admins])
    parchear_tenant_session(monkeypatch, modulo, sesion)
    return sesion


class TestListar:
    async def test_lista_con_total_y_filtra_por_tenant(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        tenant_a_id: uuid.UUID,
    ) -> None:
        sesion = _Sesion(resultados=[2, [_usuario(), _usuario(email="b@example.com")]])
        parchear_tenant_session(monkeypatch, modulo, sesion)

        r = await authenticated_client_factory(role="admin").get(URL)

        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert cuerpo["total"] == 2
        assert len(cuerpo["items"]) == 2
        assert all("password" not in u for u in cuerpo["items"])
        assert "users.client_id =" in str(sesion.executed[0])

    async def test_los_filtros_entran_en_la_consulta(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sesion = _Sesion(resultados=[0, []])
        parchear_tenant_session(monkeypatch, modulo, sesion)

        await authenticated_client_factory(role="admin").get(
            URL, params={"search": "ana", "role": "agent", "is_active": "false"}
        )

        sql = str(sesion.executed[1]).lower()
        assert "users.role =" in sql
        assert "users.is_active =" in sql
        assert "like" in sql  # ilike se compila como lower(...) LIKE lower(...)

    @pytest.mark.parametrize("rol", ["agent", "supervisor", "medical"])
    async def test_solo_administradores(self, authenticated_client_factory: Any, rol: str) -> None:
        r = await authenticated_client_factory(role=rol).get(URL)

        assert r.status_code == 403


class TestEditar:
    async def test_cambia_nombre_y_rol(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        objetivo = _usuario(role="agent")
        _preparar(monkeypatch, objetivo)

        r = await authenticated_client_factory(role="admin").put(
            f"{URL}/{objetivo.id}", json={"first_name": "Aurora", "role": "supervisor"}
        )

        assert r.status_code == 200, r.text
        assert (objetivo.first_name, objetivo.role) == ("Aurora", "supervisor")
        assert r.json()["role"] == "supervisor"

    async def test_solo_cambia_lo_que_se_envia(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        objetivo = _usuario(first_name="Ana", last_name="Perez", role="agent")
        _preparar(monkeypatch, objetivo)

        await authenticated_client_factory(role="admin").put(
            f"{URL}/{objetivo.id}", json={"last_name": "Gomez"}
        )

        assert (objetivo.first_name, objetivo.last_name, objetivo.role) == ("Ana", "Gomez", "agent")

    async def test_restablecer_la_contrasena_guarda_un_hash_nuevo(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        objetivo = _usuario()
        _preparar(monkeypatch, objetivo)

        r = await authenticated_client_factory(role="admin").put(
            f"{URL}/{objetivo.id}", json={"password": "otra-clave-larga"}
        )

        assert r.status_code == 200
        assert objetivo.password_hash != "hash-viejo"
        assert verify_password("otra-clave-larga", objetivo.password_hash)
        assert "password" not in r.text

    async def test_un_usuario_inexistente_o_de_otro_tenant_es_404(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _preparar(monkeypatch, None)

        r = await authenticated_client_factory(role="admin").put(
            f"{URL}/{uuid.uuid4()}", json={"first_name": "X"}
        )

        assert r.status_code == 404

    @pytest.mark.parametrize(
        "cuerpo",
        [
            {},
            {"first_name": None},
            {"role": "super_admin"},
            {"role": "root"},
            {"password": "corta"},
        ],
    )
    async def test_cuerpos_invalidos_son_422(
        self, authenticated_client_factory: Any, cuerpo: dict[str, Any]
    ) -> None:
        r = await authenticated_client_factory(role="admin").put(
            f"{URL}/{uuid.uuid4()}", json=cuerpo
        )

        assert r.status_code == 422


class TestReglasDeAcceso:
    async def test_nadie_cambia_su_propio_rol(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        yo = uuid.uuid4()
        objetivo = _usuario(id=yo, role="admin")
        _preparar(monkeypatch, objetivo)

        r = await authenticated_client_factory(role="admin", user_id=yo).put(
            f"{URL}/{yo}", json={"role": "agent"}
        )

        assert r.status_code == 400
        assert objetivo.role == "admin"

    async def test_nadie_se_desactiva_a_si_mismo(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        yo = uuid.uuid4()
        objetivo = _usuario(id=yo, role="admin")
        _preparar(monkeypatch, objetivo)

        r = await authenticated_client_factory(role="admin", user_id=yo).put(
            f"{URL}/{yo}", json={"is_active": False}
        )

        assert r.status_code == 400
        assert objetivo.is_active is True

    async def test_si_puede_cambiarse_el_nombre_o_la_contrasena(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        yo = uuid.uuid4()
        objetivo = _usuario(id=yo, role="admin")
        _preparar(monkeypatch, objetivo)

        r = await authenticated_client_factory(role="admin", user_id=yo).put(
            f"{URL}/{yo}", json={"first_name": "Nuevo", "role": "admin"}
        )

        assert r.status_code == 200
        assert objetivo.first_name == "Nuevo"

    async def test_un_admin_no_toca_a_un_super_admin(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        objetivo = _usuario(role="super_admin")
        _preparar(monkeypatch, objetivo)

        r = await authenticated_client_factory(role="admin").put(
            f"{URL}/{objetivo.id}", json={"is_active": False}
        )

        assert r.status_code == 403
        assert objetivo.is_active is True

    async def test_un_super_admin_si_puede_tocar_a_otro_super_admin(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        objetivo = _usuario(role="super_admin")
        _preparar(monkeypatch, objetivo, quedan_admins=1)

        r = await authenticated_client_factory(role="super_admin").put(
            f"{URL}/{objetivo.id}", json={"is_active": False}
        )

        assert r.status_code == 200
        assert objetivo.is_active is False


class TestUltimoAdministrador:
    @pytest.mark.parametrize("cuerpo", [{"is_active": False}, {"role": "agent"}])
    async def test_el_tenant_no_se_queda_sin_administrador_activo(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        cuerpo: dict[str, Any],
    ) -> None:
        objetivo = _usuario(role="admin")
        _preparar(monkeypatch, objetivo, quedan_admins=0)

        r = await authenticated_client_factory(role="super_admin").put(
            f"{URL}/{objetivo.id}", json=cuerpo
        )

        assert r.status_code == 409
        assert r.json()["error_code"] == "CONFLICT"
        assert (objetivo.role, objetivo.is_active) == ("admin", True)

    async def test_si_queda_otro_administrador_se_puede(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        objetivo = _usuario(role="admin")
        _preparar(monkeypatch, objetivo, quedan_admins=1)

        r = await authenticated_client_factory(role="super_admin").put(
            f"{URL}/{objetivo.id}", json={"is_active": False}
        )

        assert r.status_code == 200
        assert objetivo.is_active is False

    async def test_degradar_a_un_agent_no_cuenta_el_recuento(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Solo se comprueba cuando quien deja de administrar es un administrador."""
        objetivo = _usuario(role="agent")
        sesion = _preparar(monkeypatch, objetivo, quedan_admins=0)

        r = await authenticated_client_factory(role="admin").put(
            f"{URL}/{objetivo.id}", json={"is_active": False}
        )

        assert r.status_code == 200
        assert len(sesion.executed) == 1

    async def test_reactivar_a_alguien_nunca_dispara_la_regla(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        objetivo = _usuario(role="admin", is_active=False)
        _preparar(monkeypatch, objetivo, quedan_admins=0)

        r = await authenticated_client_factory(role="super_admin").put(
            f"{URL}/{objetivo.id}", json={"is_active": True}
        )

        assert r.status_code == 200
        assert objetivo.is_active is True
