"""Tests del CRUD de templates de tenant (Sprint 10, Dev B).

La base se sustituye por dobles (corren sin `--run-db`) y el encolado real de
Celery por un registro. Lo que se prueba es el RBAC —estas dos tablas no tienen
RLS, asi que el control de acceso es enteramente el de los endpoints—, que el
snapshot no se filtre a un `admin`, y las validaciones que tienen que fallar en
el request y no minutos despues en el worker.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, ClassVar

import pytest
from sqlalchemy.dialects import postgresql

from app.api.v1 import templates as modulo
from app.models.tenant_template import TemplateInstantiation, TenantTemplate
from tests.unit.agent_doubles import FakeResult, FakeSession, fake_tenant_session

URL = "/api/v1/admin/templates"
AHORA = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
SNAPSHOT = {
    "version": "1.0",
    "agent_configs": [{"name": "asistente", "system_prompt": "SECRETO DEL PROMPT"}],
    "quick_replies": [{}, {}],
    "tags": [{}],
    "documents": [{"file_url": "tenant-origen/doc.pdf"}],
}


class _Sesion:
    """Doble de `AsyncSessionLocal()`: cola de resultados y registro de escrituras."""

    def __init__(self, resultados: list[Any] | None = None) -> None:
        self.resultados = list(resultados or [])
        self.ejecutadas: list[Any] = []
        self.agregados: list[Any] = []
        self.objetos: dict[Any, Any] = {}

    async def __aenter__(self) -> "_Sesion":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    @asynccontextmanager
    async def begin(self) -> Any:
        yield self

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        self.ejecutadas.append(stmt)
        valor = self.resultados.pop(0) if self.resultados else None
        return _Resultado(valor)

    async def scalar(self, stmt: Any) -> Any:
        self.ejecutadas.append(stmt)
        return self.resultados.pop(0) if self.resultados else 0

    def add(self, obj: Any) -> None:
        self.agregados.append(obj)

    async def flush(self) -> None:
        for obj in self.agregados:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            self.objetos[obj.id] = obj

    async def refresh(self, obj: Any) -> None:
        obj.created_at = obj.created_at or AHORA
        obj.version = obj.version or "1.0"
        obj.is_public = bool(obj.is_public)

    async def get(self, _modelo: Any, pk: Any) -> Any:
        return self.objetos.get(pk)


class _Resultado(FakeResult):
    def first(self) -> Any:
        return self._valor


def _fabrica(sesion: _Sesion) -> Any:
    return lambda: sesion


def _template(**campos: Any) -> TenantTemplate:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "name": "Clinica base",
        "description": None,
        "source_client_id": uuid.uuid4(),
        "config": SNAPSHOT,
        "created_by": uuid.uuid4(),
        "is_public": True,
        "version": "1.0",
        "created_at": AHORA,
    }
    base.update(campos)
    return TenantTemplate(**base)


@pytest.fixture
def encolados(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Sustituye `clone_tenant_from_template.delay()` y devuelve lo encolado."""
    registro: list[dict[str, Any]] = []

    class _Task:
        @staticmethod
        def delay(**kwargs: Any) -> None:
            registro.append(kwargs)

    import app.tasks.tenant_operations as tareas

    monkeypatch.setattr(tareas, "clone_tenant_from_template", _Task)
    return registro


# ─── POST /admin/templates ────────────────────────────────────────────────────


class TestAlta:
    async def test_super_admin_crea_el_template_con_el_snapshot(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        origen = uuid.uuid4()
        user_id = uuid.uuid4()
        sesion = _Sesion()
        monkeypatch.setattr(modulo, "AsyncSessionLocal", _fabrica(sesion))
        monkeypatch.setattr(
            modulo, "tenant_session", fake_tenant_session(FakeSession(resultados=[origen]))
        )

        async def _snapshot(_self: Any, client_id: uuid.UUID) -> dict[str, Any]:
            assert client_id == origen
            return SNAPSHOT

        monkeypatch.setattr(modulo.TenantCloner, "create_snapshot", _snapshot)
        cliente = authenticated_client_factory(role="super_admin", user_id=user_id)

        response = await cliente.post(
            URL, json={"name": "Clinica base", "source_client_id": str(origen), "is_public": True}
        )

        assert response.status_code == 201, response.text
        cuerpo = response.json()
        assert cuerpo["summary"] == {
            "agent_configs": 1,
            "quick_replies": 2,
            "tags": 1,
            "documents": 1,
        }
        assert cuerpo["created_by"] == str(user_id)
        assert sesion.agregados[0].config == SNAPSHOT

    async def test_un_admin_no_puede_crear_templates(
        self, authenticated_client_factory: Any
    ) -> None:
        response = await authenticated_client_factory(role="admin").post(
            URL, json={"name": "x", "source_client_id": str(uuid.uuid4())}
        )

        assert response.status_code == 403

    async def test_tenant_origen_inexistente_da_404(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            modulo, "tenant_session", fake_tenant_session(FakeSession(resultados=[None]))
        )

        response = await authenticated_client_factory(role="super_admin").post(
            URL, json={"name": "x", "source_client_id": str(uuid.uuid4())}
        )

        assert response.status_code == 404


# ─── GET /admin/templates ─────────────────────────────────────────────────────


class TestListado:
    async def test_un_admin_ve_solo_publicos_o_de_su_tenant_y_sin_snapshot(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El snapshot lleva prompts y rutas del tenant origen: no sale en el listado."""
        sesion = _Sesion(resultados=[1, [_template()]])
        monkeypatch.setattr(modulo, "AsyncSessionLocal", _fabrica(sesion))
        mi_tenant = uuid.uuid4()

        response = await authenticated_client_factory(role="admin", client_id=mi_tenant).get(URL)

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert "config" not in item
        assert "SECRETO" not in response.text
        assert item["summary"]["documents"] == 1
        sql = str(sesion.ejecutadas[1].compile(dialect=postgresql.dialect()))
        assert "tenant_templates.is_public IS true OR tenant_templates.source_client_id" in sql

    async def test_super_admin_ve_todos(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sesion = _Sesion(resultados=[2, [_template(), _template(is_public=False)]])
        monkeypatch.setattr(modulo, "AsyncSessionLocal", _fabrica(sesion))

        response = await authenticated_client_factory(role="super_admin").get(URL)

        assert response.json()["total"] == 2
        sql = str(sesion.ejecutadas[1].compile(dialect=postgresql.dialect()))
        assert "is_public" not in sql.split("FROM")[1]

    async def test_un_agente_no_lista(self, authenticated_client_factory: Any) -> None:
        response = await authenticated_client_factory(role="agent").get(URL)

        assert response.status_code == 403


# ─── GET /admin/templates/{id} ────────────────────────────────────────────────


class TestDetalle:
    async def test_super_admin_ve_el_snapshot(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        template = _template()
        monkeypatch.setattr(modulo, "AsyncSessionLocal", _fabrica(_Sesion(resultados=[template])))

        response = await authenticated_client_factory(role="super_admin").get(
            f"{URL}/{template.id}"
        )

        assert response.status_code == 200
        assert response.json()["config"] == SNAPSHOT

    async def test_un_admin_no_ve_el_snapshot_ni_de_un_publico(
        self, authenticated_client_factory: Any
    ) -> None:
        response = await authenticated_client_factory(role="admin").get(f"{URL}/{uuid.uuid4()}")

        assert response.status_code == 403

    async def test_inexistente_da_404(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(modulo, "AsyncSessionLocal", _fabrica(_Sesion(resultados=[None])))

        response = await authenticated_client_factory(role="super_admin").get(
            f"{URL}/{uuid.uuid4()}"
        )

        assert response.status_code == 404


# ─── POST /admin/templates/{id}/instantiate ──────────────────────────────────


class TestInstanciar:
    PEDIDO: ClassVar[dict[str, Any]] = {
        "tenant_name": "Clinica Norte",
        "admin_email": "admin@norte.co",
    }

    async def test_encola_la_clonacion_y_devuelve_202(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        encolados: list,
    ) -> None:
        template = _template()
        sesion = _Sesion(resultados=[template, None])
        monkeypatch.setattr(modulo, "AsyncSessionLocal", _fabrica(sesion))

        response = await authenticated_client_factory(role="super_admin").post(
            f"{URL}/{template.id}/instantiate", json=self.PEDIDO
        )

        assert response.status_code == 202, response.text
        instanciacion = sesion.agregados[0]
        assert isinstance(instanciacion, TemplateInstantiation)
        assert response.json() == {
            "instantiation_id": str(instanciacion.id),
            "status": "pending",
        }
        assert encolados == [
            {
                "template_id": str(template.id),
                "instantiation_id": str(instanciacion.id),
                "new_tenant_name": "Clinica Norte",
                "admin_email": "admin@norte.co",
                "custom_overrides": None,
            }
        ]

    async def test_email_en_uso_da_409_y_no_encola(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        encolados: list,
    ) -> None:
        """`users.email` es unico global: descubrirlo en el worker seria tarde."""
        template = _template()
        sesion = _Sesion(resultados=[template, (1,)])
        monkeypatch.setattr(modulo, "AsyncSessionLocal", _fabrica(sesion))

        response = await authenticated_client_factory(role="super_admin").post(
            f"{URL}/{template.id}/instantiate", json=self.PEDIDO
        )

        assert response.status_code == 409
        assert encolados == []
        assert "auth_lookup_user" in str(sesion.ejecutadas[1])

    @pytest.mark.parametrize(
        "overrides",
        [
            {"agent_configs": {"asistente": {"id": str(uuid.uuid4())}}},
            {"agent_configs": {"asistente": {"client_id": str(uuid.uuid4())}}},
            {"clients": {"plan": "enterprise"}},
            {"agent_configs": ["no-es-un-objeto"]},
        ],
    )
    async def test_overrides_fuera_de_lo_permitido_dan_422(
        self,
        authenticated_client_factory: Any,
        encolados: list,
        overrides: dict[str, Any],
    ) -> None:
        """Antes llegaban como **kwargs a AgentConfig(...) dentro de la task."""
        response = await authenticated_client_factory(role="super_admin").post(
            f"{URL}/{uuid.uuid4()}/instantiate", json={**self.PEDIDO, "overrides": overrides}
        )

        assert response.status_code == 422
        assert encolados == []

    async def test_overrides_permitidos_pasan(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        encolados: list,
    ) -> None:
        template = _template()
        monkeypatch.setattr(
            modulo, "AsyncSessionLocal", _fabrica(_Sesion(resultados=[template, None]))
        )
        overrides = {"agent_configs": {"asistente": {"system_prompt": "Hola", "temperature": 0.2}}}

        response = await authenticated_client_factory(role="super_admin").post(
            f"{URL}/{template.id}/instantiate", json={**self.PEDIDO, "overrides": overrides}
        )

        assert response.status_code == 202
        assert encolados[0]["custom_overrides"] == overrides

    async def test_si_no_se_puede_encolar_da_503_y_marca_fallida(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        template = _template()
        sesion = _Sesion(resultados=[template, None])
        monkeypatch.setattr(modulo, "AsyncSessionLocal", _fabrica(sesion))

        class _BrokerCaido:
            @staticmethod
            def delay(**_kwargs: Any) -> None:
                raise ConnectionError("broker caido")

        import app.tasks.tenant_operations as tareas

        monkeypatch.setattr(tareas, "clone_tenant_from_template", _BrokerCaido)

        response = await authenticated_client_factory(role="super_admin").post(
            f"{URL}/{template.id}/instantiate", json=self.PEDIDO
        )

        assert response.status_code == 503
        assert sesion.agregados[0].status == "failed"

    async def test_un_admin_no_instancia(self, authenticated_client_factory: Any) -> None:
        response = await authenticated_client_factory(role="admin").post(
            f"{URL}/{uuid.uuid4()}/instantiate", json=self.PEDIDO
        )

        assert response.status_code == 403


# ─── GET /admin/templates/instantiations/{id} ────────────────────────────────


class TestAvance:
    async def test_devuelve_estado_y_progreso(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instanciacion = TemplateInstantiation(
            id=uuid.uuid4(),
            template_id=uuid.uuid4(),
            target_client_id=uuid.uuid4(),
            status="completed",
            progress={"step": "documents_queued", "documents_total": 3},
            error_message=None,
            started_at=AHORA,
            completed_at=AHORA,
            created_at=AHORA,
        )
        monkeypatch.setattr(
            modulo, "AsyncSessionLocal", _fabrica(_Sesion(resultados=[instanciacion]))
        )

        response = await authenticated_client_factory(role="super_admin").get(
            f"{URL}/instantiations/{instanciacion.id}"
        )

        assert response.status_code == 200
        assert response.json()["progress"]["documents_total"] == 3
        assert response.json()["target_client_id"] == str(instanciacion.target_client_id)

    async def test_un_admin_no_ve_el_avance(self, authenticated_client_factory: Any) -> None:
        """El progreso puede traer el password temporal del admin nuevo (ADR-064)."""
        response = await authenticated_client_factory(role="admin").get(
            f"{URL}/instantiations/{uuid.uuid4()}"
        )

        assert response.status_code == 403

    async def test_inexistente_da_404(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(modulo, "AsyncSessionLocal", _fabrica(_Sesion(resultados=[None])))

        response = await authenticated_client_factory(role="super_admin").get(
            f"{URL}/instantiations/{uuid.uuid4()}"
        )

        assert response.status_code == 404
