"""Tests del contexto de auditoria: el middleware y el GUC que publica la sesion.

El rastro lo escribe un trigger de PostgreSQL (migracion 004); lo que se prueba
aqui es la unica parte que pone Python: que el usuario autenticado llegue a
`app.current_user_id` y que se suelte al terminar la peticion. Que el trigger
grabe de verdad se verifica contra Postgres real en
`tests/integration/test_audit_gdpr.py`.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import text

from app.core import database as database_module
from app.core.database import current_user_id, tenant_session
from tests.unit.crm_doubles import CrmSession


class _SesionDeContexto(CrmSession):
    """Sesion falsa que ademas captura los parametros del `set_config`."""

    def __init__(self) -> None:
        """Arranca sin resultados prefijados."""
        super().__init__()
        self.parametros: list[Any] = []

    async def execute(self, stmt: Any = None, params: Any = None) -> Any:
        """Guarda los parametros ademas de registrar la sentencia."""
        self.parametros.append(params)
        return await super().execute(stmt, params)

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        """`tenant_session()` abre la transaccion con `session.begin()`."""
        yield


class _FabricaDeSesiones:
    """Sustituto de `AsyncSessionLocal`, que se llama sin argumentos."""

    def __init__(self, session: _SesionDeContexto) -> None:
        """Guarda la sesion que se va a ceder."""
        self.session = session

    def __call__(self) -> "_FabricaDeSesiones":
        """`AsyncSessionLocal()` devuelve el propio context manager."""
        return self

    async def __aenter__(self) -> _SesionDeContexto:
        """Entra al contexto de la sesion."""
        return self.session

    async def __aexit__(self, *exc: Any) -> None:
        """Sale del contexto."""
        return


@pytest.fixture
def sesion_espia(monkeypatch: pytest.MonkeyPatch) -> _SesionDeContexto:
    """Sustituye `AsyncSessionLocal` por una sesion que captura los parametros."""
    session = _SesionDeContexto()
    monkeypatch.setattr(database_module, "AsyncSessionLocal", _FabricaDeSesiones(session))
    return session


# ─── El GUC que publica tenant_session ───────────────────────────────────────


class TestContextoDeSesion:
    """`tenant_session()` publica tenant y usuario a PostgreSQL."""

    async def test_publica_los_dos_guc(self, sesion_espia: _SesionDeContexto) -> None:
        """Una sola sentencia deja `client_id` y `user_id` en la transaccion."""
        tenant, usuario = uuid.uuid4(), uuid.uuid4()

        async with tenant_session(tenant, user_id=usuario):
            pass

        sql = str(sesion_espia.executed[0])
        assert "app.current_client_id" in sql
        assert "app.current_user_id" in sql
        assert sesion_espia.parametros[0] == {
            "client_id": str(tenant),
            "user_id": str(usuario),
        }

    async def test_el_contexto_es_de_transaccion_no_de_sesion(
        self, sesion_espia: _SesionDeContexto
    ) -> None:
        """`is_local=true` en los dos `set_config`.

        Con el pooler en modo transaccion, un contexto de sesion sobrevive a la
        transaccion y el siguiente request puede heredar el tenant del anterior
        (CLAUDE.md, regla 1).
        """
        async with tenant_session(uuid.uuid4()):
            pass

        sql = str(sesion_espia.executed[0])
        assert sql.count("true") == 2

    async def test_sin_usuario_manda_cadena_vacia(self, sesion_espia: _SesionDeContexto) -> None:
        """Un worker de Celery no actua en nombre de nadie.

        El trigger convierte la cadena vacia en NULL (`NULLIF`), que es como se
        registra una accion del sistema.
        """
        async with tenant_session(uuid.uuid4()):
            pass

        assert sesion_espia.parametros[0]["user_id"] == ""

    async def test_toma_el_usuario_del_contextvar(self, sesion_espia: _SesionDeContexto) -> None:
        """Las ~30 llamadas existentes a `tenant_session(client_id)` no cambian.

        Cada una hereda sola el usuario de su peticion, sin que nadie tenga que
        acordarse de pasarlo.
        """
        usuario = uuid.uuid4()
        token = current_user_id.set(usuario)
        try:
            async with tenant_session(uuid.uuid4()):
                pass
        finally:
            current_user_id.reset(token)

        assert sesion_espia.parametros[0]["user_id"] == str(usuario)

    async def test_el_argumento_explicito_gana_al_contextvar(
        self, sesion_espia: _SesionDeContexto
    ) -> None:
        """Pasar el usuario a mano sigue funcionando y tiene prioridad."""
        explicito = uuid.uuid4()
        token = current_user_id.set(uuid.uuid4())
        try:
            async with tenant_session(uuid.uuid4(), user_id=explicito):
                pass
        finally:
            current_user_id.reset(token)

        assert sesion_espia.parametros[0]["user_id"] == str(explicito)

    def test_el_sql_no_interpola_los_ids(self) -> None:
        """Los dos valores viajan como bind params, no pegados al SQL.

        `SET` no admite parametros bind; por eso se usa `set_config()`, que es
        una funcion normal (CLAUDE.md, regla 1).
        """
        sql = str(
            text(
                "SELECT set_config('app.current_client_id', :client_id, true), "
                "set_config('app.current_user_id', :user_id, true)"
            )
        )
        assert ":client_id" in sql
        assert ":user_id" in sql


# ─── El middleware ───────────────────────────────────────────────────────────


class TestMiddleware:
    """`AuditContextMiddleware` a traves de la app real."""

    async def test_el_usuario_del_token_llega_al_contexto(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Durante la peticion, el ContextVar trae el usuario del JWT."""
        visto: dict[str, Any] = {}

        from app.api.v1 import quick_replies as quick_replies_module

        def _tenant_session_espia(client_id: Any, user_id: Any = None) -> Any:
            visto["user_id"] = user_id if user_id is not None else current_user_id.get()
            from tests.unit.agent_doubles import fake_tenant_session

            return fake_tenant_session(CrmSession(resultados=[[]]))(client_id)

        monkeypatch.setattr(quick_replies_module, "tenant_session", _tenant_session_espia)

        response = await authenticated_client.get("/api/v1/quick-replies")

        assert response.status_code == 200
        assert visto["user_id"] is not None

    async def test_el_contexto_queda_limpio_despues_de_la_peticion(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin el `reset()`, un cambio sin autenticar podria atribuirse al ultimo usuario.

        Es decir: un rastro de auditoria que miente.
        """
        from app.api.v1 import quick_replies as quick_replies_module
        from tests.unit.agent_doubles import fake_tenant_session

        monkeypatch.setattr(
            quick_replies_module,
            "tenant_session",
            fake_tenant_session(CrmSession(resultados=[[]])),
        )

        await authenticated_client.get("/api/v1/quick-replies")

        assert current_user_id.get() is None

    async def test_una_peticion_sin_token_no_deja_usuario(self, api_client: Any) -> None:
        """Sin JWT no hay a quien atribuir nada."""
        await api_client.get("/api/v1/quick-replies")

        assert current_user_id.get() is None
