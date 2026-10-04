"""Tests de `/api/v1/sandbox` (Sprint 14c, ADR-078): RBAC, flag y errores de la API."""

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import sandbox as modulo
from app.services import sandbox as servicio

URL = "/api/v1/sandbox"
AHORA = datetime(2026, 10, 3, tzinfo=timezone.utc)

#: (metodo, ruta, cuerpo): todas las rutas de la API.
RUTAS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("post", "", None),
    ("get", "", None),
    ("post", "/reset", None),
    ("get", "/agent-config", None),
    ("put", "/agent-config", {"system_prompt": "x"}),
    ("post", "/messages", {"text": "hola"}),
    ("post", "/publish", None),
    ("get", "/history", None),
    ("post", "/rollback", {}),
]

AGENTE = {
    "name": "Asistente",
    "system_prompt": "Hola",
    "welcome_message": None,
    "model": "gpt-4o",
    "temperature": 0.3,
    "max_tokens": 1024,
    "training_mode": False,
    "similarity_threshold": 0.8,
    "handoff_message": None,
    "config": {"rag_top_k": 5},
    "is_active": True,
}


class _Flags:
    """Doble de `FeatureFlags`: `habilitado` decide lo que contesta `is_enabled`."""

    habilitado = True

    async def is_enabled(self, client_id: Any, flag: str, default: bool = False) -> bool:
        assert flag == "enable_sandbox"
        return type(self).habilitado


@pytest.fixture(autouse=True)
def _flag_encendida(monkeypatch: pytest.MonkeyPatch) -> None:
    _Flags.habilitado = True
    monkeypatch.setattr(modulo, "FeatureFlags", _Flags)


def _servicio(monkeypatch: pytest.MonkeyPatch, **funciones: Any) -> dict[str, AsyncMock]:
    """Sustituye funciones del servicio por mocks; las no indicadas fallan si se llaman."""
    mocks: dict[str, AsyncMock] = {}
    for nombre in (
        "crear_sandbox",
        "obtener_estado",
        "reiniciar_sandbox",
        "leer_config_sandbox",
        "actualizar_config_sandbox",
        "enviar_mensaje_de_prueba",
        "publicar",
        "listar_historial",
        "revertir",
    ):
        mock = funciones.get(nombre) or AsyncMock(
            side_effect=AssertionError(f"{nombre} no esperada")
        )
        monkeypatch.setattr(servicio, nombre, mock)
        mocks[nombre] = mock
    return mocks


async def _llamar(cliente: Any, metodo: str, ruta: str, cuerpo: dict[str, Any] | None) -> Any:
    kwargs = {} if cuerpo is None else {"json": cuerpo}
    return await getattr(cliente, metodo)(f"{URL}{ruta}", **kwargs)


class TestAcceso:
    @pytest.mark.parametrize("rol", ["agent", "supervisor", "medical"])
    @pytest.mark.parametrize(("metodo", "ruta", "cuerpo"), RUTAS)
    async def test_solo_administradores(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        rol: str,
        metodo: str,
        ruta: str,
        cuerpo: dict[str, Any] | None,
    ) -> None:
        _servicio(monkeypatch)
        cliente = authenticated_client_factory(role=rol)

        respuesta = await _llamar(cliente, metodo, ruta, cuerpo)

        assert respuesta.status_code == 403

    @pytest.mark.parametrize(("metodo", "ruta", "cuerpo"), RUTAS)
    async def test_sin_la_flag_el_tenant_no_entra(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        metodo: str,
        ruta: str,
        cuerpo: dict[str, Any] | None,
    ) -> None:
        """Un tenant que no pidio el sandbox no debe poder gastar tokens en pruebas."""
        _Flags.habilitado = False
        _servicio(monkeypatch)
        cliente = authenticated_client_factory(role="admin")

        respuesta = await _llamar(cliente, metodo, ruta, cuerpo)

        assert respuesta.status_code == 403
        assert "enable_sandbox" in respuesta.json()["message"]

    async def test_sin_token_no_hay_acceso(self, api_client: Any) -> None:
        assert (await api_client.get(URL)).status_code in (401, 403)


class TestRespuestas:
    async def test_crear_devuelve_201_con_el_estado_y_pasa_al_usuario_del_token(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        tenant_a_id: uuid.UUID,
    ) -> None:
        sandbox = uuid.uuid4()
        usuario = uuid.uuid4()
        mocks = _servicio(
            monkeypatch,
            crear_sandbox=AsyncMock(return_value=sandbox),
            obtener_estado=AsyncMock(
                return_value={"exists": True, "sandbox_client_id": sandbox, "created_at": AHORA}
            ),
        )
        cliente = authenticated_client_factory(role="admin", user_id=usuario)

        respuesta = await cliente.post(URL)

        assert respuesta.status_code == 201, respuesta.text
        assert respuesta.json()["sandbox_client_id"] == str(sandbox)
        mocks["crear_sandbox"].assert_awaited_once_with(tenant_a_id, usuario)

    async def test_estado_sin_sandbox(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _servicio(monkeypatch, obtener_estado=AsyncMock(return_value={"exists": False}))
        cliente = authenticated_client_factory(role="admin")

        respuesta = await cliente.get(URL)

        assert respuesta.json() == {
            "exists": False,
            "sandbox_client_id": None,
            "created_at": None,
            "reset_at": None,
            "last_published_at": None,
            "versions": 0,
        }

    async def test_editar_solo_envia_al_servicio_lo_que_vino(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mocks = _servicio(monkeypatch, actualizar_config_sandbox=AsyncMock(return_value=AGENTE))
        cliente = authenticated_client_factory(role="admin")

        respuesta = await cliente.put(
            f"{URL}/agent-config", json={"temperature": 0.5, "config": {"rag_top_k": 9}}
        )

        assert respuesta.status_code == 200, respuesta.text
        assert mocks["actualizar_config_sandbox"].await_args.args[1] == {
            "temperature": 0.5,
            "config": {"rag_top_k": 9},
        }

    async def test_mensaje_de_prueba(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conversacion = uuid.uuid4()
        mocks = _servicio(
            monkeypatch,
            enviar_mensaje_de_prueba=AsyncMock(
                return_value={
                    "conversation_id": conversacion,
                    "response": "Hola, soy el bot",
                    "intent": "greeting",
                    "requires_handoff": False,
                    "handoff_reason": None,
                    "detected_language": "es",
                }
            ),
        )
        cliente = authenticated_client_factory(role="admin")

        respuesta = await cliente.post(
            f"{URL}/messages", json={"text": "hola", "new_conversation": True}
        )

        assert respuesta.json()["response"] == "Hola, soy el bot"
        assert mocks["enviar_mensaje_de_prueba"].await_args.args[1] == "hola"
        assert mocks["enviar_mensaje_de_prueba"].await_args.args[3] is True

    async def test_publicar_historial_y_rollback(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mocks = _servicio(
            monkeypatch,
            publicar=AsyncMock(return_value=3),
            listar_historial=AsyncMock(
                return_value=[
                    {"version": 3, "reason": "publish", "created_at": AHORA, "created_by": None}
                ]
            ),
            revertir=AsyncMock(return_value={"restored": 3, "saved_as": 4}),
        )
        cliente = authenticated_client_factory(role="admin")

        assert (await cliente.post(f"{URL}/publish")).json() == {"version": 3}
        assert (await cliente.get(f"{URL}/history")).json()[0]["version"] == 3
        respuesta = await cliente.post(f"{URL}/rollback", json={"version": 3})
        assert respuesta.json() == {"restored": 3, "saved_as": 4}
        assert mocks["revertir"].await_args.args[2] == 3


class TestErrores:
    @pytest.mark.parametrize(
        ("metodo", "ruta", "cuerpo", "funcion", "excepcion", "codigo"),
        [
            ("post", "", None, "crear_sandbox", servicio.SandboxYaExisteError("x"), 409),
            ("post", "", None, "crear_sandbox", servicio.SandboxDeSandboxError("x"), 400),
            ("post", "/reset", None, "reiniciar_sandbox", servicio.SandboxNoExisteError("x"), 404),
            (
                "get",
                "/agent-config",
                None,
                "leer_config_sandbox",
                servicio.SandboxNoExisteError("x"),
                404,
            ),
            (
                "get",
                "/agent-config",
                None,
                "leer_config_sandbox",
                servicio.SinConfiguracionError("sin agente"),
                400,
            ),
            (
                "put",
                "/agent-config",
                {"system_prompt": "x"},
                "actualizar_config_sandbox",
                servicio.SandboxNoExisteError("x"),
                404,
            ),
            (
                "put",
                "/agent-config",
                {"system_prompt": "x"},
                "actualizar_config_sandbox",
                servicio.ClaveDelTenantError(["clinical"]),
                400,
            ),
            (
                "put",
                "/agent-config",
                {"name": None},
                "actualizar_config_sandbox",
                servicio.CampoNoAnulableError(["name"]),
                400,
            ),
            (
                "put",
                "/agent-config",
                {"system_prompt": "x"},
                "actualizar_config_sandbox",
                servicio.SinConfiguracionError("sin agente"),
                400,
            ),
            (
                "post",
                "/messages",
                {"text": "hola"},
                "enviar_mensaje_de_prueba",
                servicio.SandboxNoExisteError("x"),
                404,
            ),
            ("post", "/publish", None, "publicar", servicio.SandboxNoExisteError("x"), 404),
            (
                "post",
                "/publish",
                None,
                "publicar",
                servicio.SinConfiguracionError("sin agente"),
                400,
            ),
            ("post", "/rollback", {}, "revertir", servicio.VersionInexistenteError("9"), 404),
        ],
    )
    async def test_cada_error_del_servicio_tiene_su_codigo(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        metodo: str,
        ruta: str,
        cuerpo: dict[str, Any] | None,
        funcion: str,
        excepcion: Exception,
        codigo: int,
    ) -> None:
        _servicio(monkeypatch, **{funcion: AsyncMock(side_effect=excepcion)})
        cliente = authenticated_client_factory(role="admin")

        respuesta = await _llamar(cliente, metodo, ruta, cuerpo)

        assert respuesta.status_code == codigo, respuesta.text

    async def test_las_claves_del_tenant_se_nombran_en_el_error(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _servicio(
            monkeypatch,
            actualizar_config_sandbox=AsyncMock(
                side_effect=servicio.ClaveDelTenantError(["clinical", "marketing"])
            ),
        )
        cliente = authenticated_client_factory(role="admin")

        respuesta = await cliente.put(f"{URL}/agent-config", json={"config": {"clinical": {}}})

        assert "clinical, marketing" in respuesta.json()["message"]

    async def test_si_el_agente_revienta_no_se_filtra_el_error_interno(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _servicio(
            monkeypatch,
            enviar_mensaje_de_prueba=AsyncMock(
                side_effect=RuntimeError("sk-secreto en el traceback")
            ),
        )
        cliente = authenticated_client_factory(role="admin")

        respuesta = await cliente.post(f"{URL}/messages", json={"text": "hola"})

        assert respuesta.status_code == 502
        assert "sk-secreto" not in respuesta.text
        assert "Traceback" not in respuesta.text

    @pytest.mark.parametrize(
        ("ruta", "cuerpo"),
        [
            ("/messages", {"text": ""}),
            ("/messages", {"text": "x" * 4001}),
            ("/messages", {}),
            ("/rollback", {"version": 0}),
            ("/rollback", {"version": "abc"}),
        ],
    )
    async def test_entradas_invalidas_se_rechazan_sin_llegar_al_servicio(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        ruta: str,
        cuerpo: dict[str, Any],
    ) -> None:
        _servicio(monkeypatch)
        cliente = authenticated_client_factory(role="admin")

        respuesta = await cliente.post(f"{URL}{ruta}", json=cuerpo)

        assert respuesta.status_code in (400, 422)
