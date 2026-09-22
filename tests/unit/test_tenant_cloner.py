"""Tests unitarios de TenantCloner (Sprint 10).

Sesion falsa con una cola de resultados: cada `create_snapshot()` hace varias
consultas en orden fijo (agent_configs, quick_replies, tags, documents,
token_budget, client), asi que `_SesionFalsa` se arma con un resultado por
cada `execute()` esperado, en ese mismo orden.
"""

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only-minimum-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters-long")

from app.core.security import verify_password
from app.services.storage import StorageError
from app.services.tenant_cloner import TenantCloner, _slugify


class _Resultado:
    """Devuelve lo mismo por `.scalars()`, `.scalar_one_or_none()` o `.scalar_one()`."""

    def __init__(self, filas: list) -> None:
        self._filas = filas

    def scalars(self):
        return list(self._filas)

    def scalar_one_or_none(self):
        return self._filas[0] if self._filas else None

    def scalar_one(self):
        return self._filas[0]


class _SesionFalsa:
    """Sesion falsa: una cola de resultados y lo que se le agrego con `.add()`."""

    def __init__(self, resultados: list[_Resultado] | None = None) -> None:
        self._resultados = list(resultados or [])
        self.added: list[object] = []
        self.ejecutadas: list[object] = []

    async def __aenter__(self) -> "_SesionFalsa":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return

    async def execute(self, stmt: object = None, params: object = None) -> _Resultado:
        self.ejecutadas.append(stmt)
        if self._resultados:
            return self._resultados.pop(0)
        return _Resultado([])

    def add(self, obj: object) -> None:
        self.added.append(obj)


def _tenant_session_falsa(sesion: _SesionFalsa):
    @asynccontextmanager
    async def _cm(client_id, user_id=None):
        yield sesion

    return _cm


def _agent_config(**over):
    base = {
        "name": "general",
        "system_prompt": "Sos un asistente",
        "welcome_message": "Hola",
        "model": "gpt-4o",
        "temperature": 0.7,
        "max_tokens": 1024,
        "training_mode": False,
        "similarity_threshold": 0.80,
        "handoff_message": "Te derivo con un humano",
        "config": {},
        "is_active": True,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _quick_reply(**over):
    base = {
        "shortcut": "/saludo",
        "title": "Saludo",
        "content": "Hola, bienvenido",
        "category": "saludo",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _tag(**over):
    base = {"name": "vip", "color": "#FF5733"}
    base.update(over)
    return SimpleNamespace(**base)


def _document(**over):
    base = {
        "title": "Manual",
        "file_url": "client/doc/manual.pdf",
        "file_type": "application/pdf",
        "file_size": 1024,
        "metadata_": {},
        "status": "completed",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _token_budget(**over):
    base = {"total_budget": 100_000, "model_default": "gpt-4o"}
    base.update(over)
    return SimpleNamespace(**base)


def _client(**over):
    base = {"settings": {"idioma": "es"}, "theme_config": {"color": "#000"}}
    base.update(over)
    return SimpleNamespace(**base)


def _template(**over):
    base = {
        "id": uuid4(),
        "config": {
            "source_client_id": str(uuid4()),
            "agent_configs": [
                {
                    "name": "general",
                    "system_prompt": "Sos un asistente",
                    "welcome_message": "Hola",
                    "model": "gpt-4o",
                    "temperature": 0.7,
                    "max_tokens": 1024,
                    "training_mode": False,
                    "similarity_threshold": 0.80,
                    "handoff_message": "Te derivo con un humano",
                    "config": {},
                    "is_active": True,
                }
            ],
            "quick_replies": [
                {"shortcut": "/saludo", "title": "Saludo", "content": "Hola", "category": "saludo"}
            ],
            "tags": [{"name": "vip", "color": "#FF5733"}],
            "documents": [],
            "token_budget": {"total_budget": 100_000, "model_default": "gpt-4o"},
            "client_settings": {"settings": {"idioma": "es"}, "theme_config": {"color": "#000"}},
        },
    }
    base.update(over)
    return SimpleNamespace(**base)


class TestSlugify:
    def test_minusculas_y_guiones(self) -> None:
        assert _slugify("Acme Corp S.A.") == "acme-corp-s-a"

    def test_nunca_vacio(self) -> None:
        assert _slugify("!!!") == "tenant"

    def test_recorta_a_la_longitud_maxima(self) -> None:
        assert len(_slugify("a" * 200)) <= 80


class TestCreateSnapshot:
    @pytest.mark.asyncio
    async def test_incluye_todas_las_secciones_esperadas(self) -> None:
        sesion = _SesionFalsa(
            [
                _Resultado([_agent_config()]),
                _Resultado([_quick_reply()]),
                _Resultado([_tag()]),
                _Resultado([_document()]),
                _Resultado([_token_budget()]),
                _Resultado([_client()]),
            ]
        )
        cloner = TenantCloner()
        client_id = uuid4()

        with patch("app.services.tenant_cloner.tenant_session", _tenant_session_falsa(sesion)):
            snapshot = await cloner.create_snapshot(client_id)

        assert snapshot["source_client_id"] == str(client_id)
        assert len(snapshot["agent_configs"]) == 1
        assert snapshot["agent_configs"][0]["name"] == "general"
        assert len(snapshot["quick_replies"]) == 1
        assert len(snapshot["tags"]) == 1
        assert len(snapshot["documents"]) == 1
        assert snapshot["token_budget"] == {"total_budget": 100_000, "model_default": "gpt-4o"}
        assert snapshot["client_settings"] == {
            "settings": {"idioma": "es"},
            "theme_config": {"color": "#000"},
        }

    @pytest.mark.asyncio
    async def test_no_incluye_contactos_conversaciones_ni_mensajes(self) -> None:
        sesion = _SesionFalsa(
            [
                _Resultado([]),
                _Resultado([]),
                _Resultado([]),
                _Resultado([]),
                _Resultado([]),
                _Resultado([_client()]),
            ]
        )
        cloner = TenantCloner()

        with patch("app.services.tenant_cloner.tenant_session", _tenant_session_falsa(sesion)):
            snapshot = await cloner.create_snapshot(uuid4())

        assert "contacts" not in snapshot
        assert "conversations" not in snapshot
        assert "messages" not in snapshot

    @pytest.mark.asyncio
    async def test_no_incluye_channel_configs_porque_la_tabla_no_existe_todavia(self) -> None:
        sesion = _SesionFalsa(
            [
                _Resultado([]),
                _Resultado([]),
                _Resultado([]),
                _Resultado([]),
                _Resultado([]),
                _Resultado([_client()]),
            ]
        )
        cloner = TenantCloner()

        with patch("app.services.tenant_cloner.tenant_session", _tenant_session_falsa(sesion)):
            snapshot = await cloner.create_snapshot(uuid4())

        assert "channel_configs" not in snapshot

    @pytest.mark.asyncio
    async def test_documento_sin_file_url_se_excluye(self) -> None:
        sesion = _SesionFalsa(
            [
                _Resultado([]),
                _Resultado([]),
                _Resultado([]),
                _Resultado([_document(file_url=None), _document(title="Con archivo")]),
                _Resultado([]),
                _Resultado([_client()]),
            ]
        )
        cloner = TenantCloner()

        with patch("app.services.tenant_cloner.tenant_session", _tenant_session_falsa(sesion)):
            snapshot = await cloner.create_snapshot(uuid4())

        assert len(snapshot["documents"]) == 1
        assert snapshot["documents"][0]["title"] == "Con archivo"

    @pytest.mark.asyncio
    async def test_solo_pide_documentos_completados(self) -> None:
        """No se clonan documentos `pending`/`processing`/`failed`.

        Un documento a medio procesar no tiene contenido extraido que valga
        la pena copiar, y uno fallido tampoco. La sesion falsa no filtra por
        si sola (siempre devuelve lo que se le precargo), asi que esto
        inspecciona el WHERE compilado en vez del resultado.
        """
        sesion = _SesionFalsa(
            [
                _Resultado([]),
                _Resultado([]),
                _Resultado([]),
                _Resultado([]),
                _Resultado([]),
                _Resultado([_client()]),
            ]
        )
        cloner = TenantCloner()

        with patch("app.services.tenant_cloner.tenant_session", _tenant_session_falsa(sesion)):
            await cloner.create_snapshot(uuid4())

        consulta_documentos = sesion.ejecutadas[3]
        compilada = consulta_documentos.compile(compile_kwargs={"literal_binds": True})
        assert "documents.status = 'completed'" in str(compilada)

    @pytest.mark.asyncio
    async def test_sin_presupuesto_del_mes_da_none(self) -> None:
        sesion = _SesionFalsa(
            [
                _Resultado([]),
                _Resultado([]),
                _Resultado([]),
                _Resultado([]),
                _Resultado([]),
                _Resultado([_client()]),
            ]
        )
        cloner = TenantCloner()

        with patch("app.services.tenant_cloner.tenant_session", _tenant_session_falsa(sesion)):
            snapshot = await cloner.create_snapshot(uuid4())

        assert snapshot["token_budget"] is None


class TestInstantiate:
    @pytest.mark.asyncio
    async def test_el_client_nuevo_usa_el_mismo_id_que_abre_tenant_session(self) -> None:
        """El WITH CHECK de la RLS de `clients` exige id == current_client_id.

        Si `Client.id` y el `client_id` con el que se abrio `tenant_session()`
        no coincidieran, el INSERT lo rechazaria la propia base.
        """
        sesion = _SesionFalsa()
        contextos: list = []

        @asynccontextmanager
        async def _cm(client_id, user_id=None):
            contextos.append(client_id)
            yield sesion

        cloner = TenantCloner()
        with patch("app.services.tenant_cloner.tenant_session", _cm):
            new_client_id, _ = await cloner.instantiate(
                _template(), "Nuevo Tenant", "admin@nuevo.com"
            )

        cliente_creado = next(o for o in sesion.added if type(o).__name__ == "Client")
        assert cliente_creado.id == new_client_id
        assert contextos == [new_client_id]

    @pytest.mark.asyncio
    async def test_el_password_devuelto_verifica_contra_el_hash_guardado(self) -> None:
        sesion = _SesionFalsa()
        cloner = TenantCloner()

        with patch("app.services.tenant_cloner.tenant_session", _tenant_session_falsa(sesion)):
            _, temp_password = await cloner.instantiate(
                _template(), "Nuevo Tenant", "admin@nuevo.com"
            )

        usuario_creado = next(o for o in sesion.added if type(o).__name__ == "User")
        assert usuario_creado.password_hash != temp_password
        assert verify_password(temp_password, usuario_creado.password_hash)
        assert usuario_creado.role == "admin"
        assert usuario_creado.email == "admin@nuevo.com"

    @pytest.mark.asyncio
    async def test_copia_agent_configs_quick_replies_y_tags(self) -> None:
        sesion = _SesionFalsa()
        cloner = TenantCloner()

        with patch("app.services.tenant_cloner.tenant_session", _tenant_session_falsa(sesion)):
            await cloner.instantiate(_template(), "Nuevo Tenant", "admin@nuevo.com")

        tipos = [type(o).__name__ for o in sesion.added]
        assert tipos.count("AgentConfig") == 1
        assert tipos.count("QuickReply") == 1
        assert tipos.count("Tag") == 1
        assert tipos.count("TokenBudget") == 1

    @pytest.mark.asyncio
    async def test_overrides_sobrescriben_un_campo_por_nombre(self) -> None:
        sesion = _SesionFalsa()
        cloner = TenantCloner()
        overrides = {"agent_configs": {"general": {"system_prompt": "Prompt personalizado"}}}

        with patch("app.services.tenant_cloner.tenant_session", _tenant_session_falsa(sesion)):
            await cloner.instantiate(_template(), "Nuevo Tenant", "admin@nuevo.com", overrides)

        agent_config = next(o for o in sesion.added if type(o).__name__ == "AgentConfig")
        assert agent_config.system_prompt == "Prompt personalizado"
        # Los campos que no se sobrescriben se conservan.
        assert agent_config.model == "gpt-4o"

    @pytest.mark.asyncio
    async def test_override_de_un_agent_config_inexistente_no_rompe(self) -> None:
        sesion = _SesionFalsa()
        cloner = TenantCloner()
        overrides = {"agent_configs": {"soporte": {"system_prompt": "No deberia aplicar"}}}

        with patch("app.services.tenant_cloner.tenant_session", _tenant_session_falsa(sesion)):
            await cloner.instantiate(_template(), "Nuevo Tenant", "admin@nuevo.com", overrides)

        agent_config = next(o for o in sesion.added if type(o).__name__ == "AgentConfig")
        assert agent_config.system_prompt == "Sos un asistente"

    @pytest.mark.asyncio
    async def test_sin_presupuesto_en_el_snapshot_no_crea_token_budget(self) -> None:
        sesion = _SesionFalsa()
        cloner = TenantCloner()
        template = _template()
        template.config["token_budget"] = None

        with patch("app.services.tenant_cloner.tenant_session", _tenant_session_falsa(sesion)):
            await cloner.instantiate(template, "Nuevo Tenant", "admin@nuevo.com")

        tipos = [type(o).__name__ for o in sesion.added]
        assert "TokenBudget" not in tipos


class TestCloneDocuments:
    @pytest.mark.asyncio
    async def test_copia_el_archivo_y_crea_el_documento_en_pending(self) -> None:
        sesion = _SesionFalsa()
        cloner = TenantCloner()
        new_client_id = uuid4()
        template = _template()
        template.config["documents"] = [
            {
                "title": "Manual",
                "file_url": "origen/doc-1/manual.pdf",
                "file_type": "application/pdf",
                "file_size": 2048,
            }
        ]

        with (
            patch("app.services.tenant_cloner.tenant_session", _tenant_session_falsa(sesion)),
            patch(
                "app.services.tenant_cloner.download_from_storage",
                AsyncMock(return_value=b"contenido"),
            ) as mock_download,
            patch(
                "app.services.tenant_cloner.upload_to_storage", AsyncMock(return_value="nueva/ruta")
            ) as mock_upload,
        ):
            document_ids = await cloner.clone_documents(template, new_client_id)

        mock_download.assert_awaited_once_with("origen/doc-1/manual.pdf")
        assert mock_upload.await_count == 1
        assert len(document_ids) == 1

        documento_creado = next(o for o in sesion.added if type(o).__name__ == "Document")
        assert documento_creado.status == "pending"
        assert documento_creado.client_id == new_client_id
        assert str(new_client_id) in documento_creado.file_url

    @pytest.mark.asyncio
    async def test_un_archivo_que_ya_no_existe_se_omite_sin_bloquear_el_resto(self) -> None:
        sesion = _SesionFalsa()
        cloner = TenantCloner()
        new_client_id = uuid4()
        template = _template()
        template.config["documents"] = [
            {"title": "Roto", "file_url": "origen/doc-1/roto.pdf", "file_type": "application/pdf"},
            {"title": "Sano", "file_url": "origen/doc-2/sano.pdf", "file_type": "application/pdf"},
        ]

        async def _download(path: str) -> bytes:
            if "doc-1" in path:
                raise StorageError("no existe")
            return b"contenido"

        with (
            patch("app.services.tenant_cloner.tenant_session", _tenant_session_falsa(sesion)),
            patch("app.services.tenant_cloner.download_from_storage", _download),
            patch(
                "app.services.tenant_cloner.upload_to_storage", AsyncMock(return_value="nueva/ruta")
            ),
        ):
            document_ids = await cloner.clone_documents(template, new_client_id)

        assert len(document_ids) == 1
        documentos_creados = [o for o in sesion.added if type(o).__name__ == "Document"]
        assert len(documentos_creados) == 1
        assert documentos_creados[0].title == "Sano"

    @pytest.mark.asyncio
    async def test_sin_documentos_en_el_template_no_hace_nada(self) -> None:
        sesion = _SesionFalsa()
        cloner = TenantCloner()
        template = _template()
        template.config["documents"] = []

        with patch("app.services.tenant_cloner.tenant_session", _tenant_session_falsa(sesion)):
            document_ids = await cloner.clone_documents(template, uuid4())

        assert document_ids == []
        assert sesion.added == []
