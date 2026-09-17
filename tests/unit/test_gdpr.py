"""Tests de los endpoints de RGPD (export y anonimizacion).

La base se sustituye por `CrmSession`: corren sin `--run-db`. El recorrido con
Postgres real, incluida la fila que el trigger de auditoria deja al anonimizar,
vive en `tests/integration/test_audit_gdpr.py`.
"""

import uuid
from typing import Any

import pytest

from app.api.v1 import admin as admin_module
from app.api.v1.admin import ANONIMIZADO, CONTENIDO_ANONIMIZADO
from tests.unit.agent_doubles import fake_tenant_session
from tests.unit.crm_doubles import (
    AHORA,
    CrmSession,
    FakeContact,
    FakeConversation,
    FakeMessage,
    FakeNote,
)

URL = "/api/v1/admin/contacts"


class FakeIdentifier:
    """Sustituto de ContactIdentifier."""

    def __init__(self, **kwargs: Any) -> None:
        """Construye el identificador con valores por defecto razonables."""
        self.id = kwargs.get("id") or uuid.uuid4()
        self.channel = kwargs.get("channel", "whatsapp")
        self.identifier_value = kwargs.get("identifier_value", "+5215500000001")
        self.created_at = kwargs.get("created_at", AHORA)


class _FilaTag:
    """Fila (name,) tal como la devuelve el join contra tags."""

    def __init__(self, name: str) -> None:
        """Guarda el nombre de la etiqueta."""
        self.name = name


def _usa_sesion(monkeypatch: pytest.MonkeyPatch, session: CrmSession) -> CrmSession:
    """Hace que los endpoints de admin usen la sesion falsa indicada."""
    monkeypatch.setattr(admin_module, "tenant_session", fake_tenant_session(session))
    return session


# ─── GET /admin/contacts/{id}/export ─────────────────────────────────────────


class TestExport:
    """Derecho de acceso y portabilidad."""

    async def test_devuelve_todo_lo_asociado_al_contacto(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contacto, identificadores, etiquetas, notas y conversaciones con mensajes."""
        contacto = FakeContact(metadata_={"origen": "webhook"})
        conv = FakeConversation(contact_id=contacto.id)
        mensaje = FakeMessage(content="quiero una cita", conversation_id=conv.id)
        _usa_sesion(
            monkeypatch,
            CrmSession(
                resultados=[
                    contacto,
                    [FakeIdentifier()],
                    [_FilaTag("vip")],
                    [FakeNote(content="llamar el lunes")],
                    [conv],
                    [mensaje],
                ]
            ),
        )

        response = await authenticated_client.get(f"{URL}/{contacto.id}/export")

        assert response.status_code == 200
        cuerpo = response.json()
        assert cuerpo["contact"]["metadata"] == {"origen": "webhook"}
        assert cuerpo["identifiers"][0]["identifier_value"] == "+5215500000001"
        assert cuerpo["tags"] == ["vip"]
        assert cuerpo["notes"][0]["content"] == "llamar el lunes"
        assert cuerpo["conversations"][0]["messages"][0]["content"] == "quiero una cita"

    async def test_lleva_la_fecha_del_export(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El JSON entregado al interesado dice cuando se genero."""
        contacto = FakeContact()
        _usa_sesion(monkeypatch, CrmSession(resultados=[contacto, [], [], [], []]))

        response = await authenticated_client.get(f"{URL}/{contacto.id}/export")

        assert response.json()["export_date"].endswith("+00:00")

    async def test_un_contacto_sin_conversaciones_no_consulta_mensajes(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin conversaciones no hace falta el SELECT de mensajes."""
        contacto = FakeContact()
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[contacto, [], [], [], []]))

        response = await authenticated_client.get(f"{URL}/{contacto.id}/export")

        assert response.status_code == 200
        assert response.json()["conversations"] == []
        sentencias = [str(s).lower() for s in session.executed]
        assert not any("from messages" in s for s in sentencias)

    async def test_los_mensajes_salen_en_una_sola_consulta(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una consulta por conversacion seria N+1 sobre la tabla mas grande."""
        contacto = FakeContact()
        convs = [FakeConversation(contact_id=contacto.id) for _ in range(3)]
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[contacto, [], [], [], convs, []]))

        await authenticated_client.get(f"{URL}/{contacto.id}/export")

        sentencias = [str(s).lower() for s in session.executed]
        assert sum(1 for s in sentencias if "from messages" in s) == 1

    async def test_contacto_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No se exporta lo que no existe en este tenant."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.get(f"{URL}/{uuid.uuid4()}/export")

        assert response.status_code == 404

    @pytest.mark.parametrize("role", ["agent", "supervisor"])
    async def test_solo_admin_puede_exportar(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch, role: str
    ) -> None:
        """Un export vuelca todos los datos personales: no es para cualquiera."""
        _usa_sesion(monkeypatch, CrmSession())
        client = authenticated_client_factory(role=role)

        response = await client.get(f"{URL}/{uuid.uuid4()}/export")

        assert response.status_code == 403


# ─── DELETE /admin/contacts/{id}/gdpr-delete ─────────────────────────────────


class TestAnonimizacion:
    """Derecho de supresion."""

    async def test_anonimiza_contacto_identificadores_mensajes_y_notas(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Los datos personales se reemplazan; las filas siguen ahi."""
        contacto = FakeContact(first_name="Ada", last_name="Lovelace", metadata_={"tel": "x"})
        identificador = FakeIdentifier()
        conv = FakeConversation(contact_id=contacto.id)
        entrante = FakeMessage(direction="inbound", content="mi telefono es 600...")
        nota = FakeNote(content="cliente de Ada")
        _usa_sesion(
            monkeypatch,
            CrmSession(resultados=[contacto, [identificador], [conv.id], [entrante], [nota]]),
        )

        response = await authenticated_client.delete(f"{URL}/{contacto.id}/gdpr-delete")

        assert response.status_code == 200
        assert contacto.first_name == ANONIMIZADO
        assert contacto.last_name == ANONIMIZADO
        assert contacto.display_name == ANONIMIZADO
        assert contacto.metadata_ == {}
        assert contacto.is_gdpr_deleted is True
        assert contacto.gdpr_deleted_at is not None
        assert identificador.identifier_value.startswith("[ELIMINADO-")
        assert entrante.content == CONTENIDO_ANONIMIZADO
        assert entrante.media_url is None
        assert nota.content == CONTENIDO_ANONIMIZADO

    async def test_el_identificador_anonimizado_sigue_siendo_unico(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La tabla tiene unique(client_id, channel, identifier_value).

        Dos identificadores del mismo canal anonimizados al mismo texto
        chocarian con ese unique; el sufijo con el id lo evita.
        """
        contacto = FakeContact()
        uno, dos = FakeIdentifier(), FakeIdentifier()
        _usa_sesion(monkeypatch, CrmSession(resultados=[contacto, [uno, dos], [], []]))

        await authenticated_client.delete(f"{URL}/{contacto.id}/gdpr-delete")

        assert uno.identifier_value != dos.identifier_value

    async def test_solo_anonimiza_los_mensajes_entrantes(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El WHERE filtra direction='inbound'.

        Los salientes son el registro de lo que respondio la empresa, no datos
        aportados por el contacto; borrarlos destruiria la trazabilidad.
        """
        contacto = FakeContact()
        conv = FakeConversation()
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[contacto, [], [conv.id], [], []]))

        await authenticated_client.delete(f"{URL}/{contacto.id}/gdpr-delete")

        sql_mensajes = next(s for s in session.executed if "FROM messages" in str(s))
        assert "messages.direction =" in str(sql_mensajes)

    async def test_reporta_cuantas_filas_toco(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El que ejecuta la supresion necesita constancia de su alcance."""
        contacto = FakeContact()
        conv = FakeConversation()
        _usa_sesion(
            monkeypatch,
            CrmSession(
                resultados=[
                    contacto,
                    [FakeIdentifier(), FakeIdentifier()],
                    [conv.id],
                    [FakeMessage(), FakeMessage(), FakeMessage()],
                    [FakeNote()],
                ]
            ),
        )

        response = await authenticated_client.delete(f"{URL}/{contacto.id}/gdpr-delete")

        cuerpo = response.json()
        assert cuerpo["identifiers_anonymized"] == 2
        assert cuerpo["messages_anonymized"] == 3
        assert cuerpo["notes_anonymized"] == 1

    async def test_repetir_la_supresion_es_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un doble clic no debe reescribir la fecha real de la supresion."""
        contacto = FakeContact(is_gdpr_deleted=True, gdpr_deleted_at=AHORA)
        _usa_sesion(monkeypatch, CrmSession(resultados=[contacto]))

        response = await authenticated_client.delete(f"{URL}/{contacto.id}/gdpr-delete")

        assert response.status_code == 400
        assert contacto.gdpr_deleted_at == AHORA

    async def test_contacto_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No se anonimiza lo que no existe en este tenant."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.delete(f"{URL}/{uuid.uuid4()}/gdpr-delete")

        assert response.status_code == 404

    @pytest.mark.parametrize("role", ["agent", "supervisor"])
    async def test_solo_admin_puede_anonimizar(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch, role: str
    ) -> None:
        """La supresion es irreversible: solo admin y super_admin."""
        _usa_sesion(monkeypatch, CrmSession())
        client = authenticated_client_factory(role=role)

        response = await client.delete(f"{URL}/{uuid.uuid4()}/gdpr-delete")

        assert response.status_code == 403

    async def test_sin_token_es_401(self, api_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ni exportar ni suprimir sin identificarse."""
        _usa_sesion(monkeypatch, CrmSession())

        response = await api_client.delete(f"{URL}/{uuid.uuid4()}/gdpr-delete")

        assert response.status_code == 401
