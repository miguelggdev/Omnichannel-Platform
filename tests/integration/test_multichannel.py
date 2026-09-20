"""Canales de Sprint 9 (Telegram y email) contra PostgreSQL real, con RLS activo.

`_process_message()` es lo que hace el worker con lo que el endpoint encola; aqui
se comprueba lo que queda escrito en la base con el contexto de tenant aplicado:
contacto (con el nombre publico del remitente), identificador cifrado,
conversacion por canal y mensaje. Y, para email, que la respuesta saliente se
cuelga del hilo del ultimo mensaje entrante.

Lo que **no** cubre, a proposito: la unificacion de un mismo contacto entre
canales (criterio 5 del spec). Telegram no expone el telefono del usuario en sus
mensajes, y unificar por telefono exige un flujo propio de resolucion que no
existe todavia; aqui se fija el comportamiento actual (un contacto por
canal e identificador) para que el dia que se implemente, el cambio sea
deliberado y no una regresion silenciosa.

Requiere base de datos: `pytest tests/ --run-db`.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from app.agents.nodes import _delivery as delivery_module
from app.core.config import get_settings
from app.core.database import tenant_session
from app.models.message import Message
from app.tasks.webhook_processor import _process_message
from tests.integration.identifiers import SQL_SELECT_IDENTIFICADOR
from tests.integration.test_webhook_flow import _contar, _normalized

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("ia_encolada")]


def _email(external_id: str, **extra: Any) -> dict[str, Any]:
    """Un email ya normalizado, como lo encola el endpoint.

    Args:
        external_id: Message-ID del email.
        **extra: Campos que reemplazan a los de base (o van dentro de `raw_payload`
            si se pasan como `raw_payload`).

    Returns:
        El `NormalizedMessage` serializado.
    """
    raw = {
        "subject": "Consulta de precios",
        "message_id": external_id,
        "in_reply_to": "",
        "references": "",
        "from": "juan@example.com",
        "to": "soporte@empresa.com",
    }
    raw.update(extra.pop("raw_payload", {}))
    return _normalized(
        "email",
        "juan@example.com",
        external_id,
        "Asunto: Consulta de precios\n\nHola, quiero el precio del plan Pro.",
        sender_name="Juan Perez",
        raw_payload=raw,
        **extra,
    )


async def _una_fila(client_id: uuid.UUID, sql: str, **params: Any) -> Any:
    """Ejecuta una consulta de una fila con el contexto de tenant aplicado.

    Args:
        client_id: Tenant.
        sql: Consulta con `:cid` para el tenant.
        **params: Otros parametros.

    Returns:
        La unica fila, o falla si no hay exactamente una.
    """
    async with tenant_session(client_id) as session:
        return (await session.execute(text(sql), {"cid": str(client_id), **params})).one()


class TestTelegram:
    """Un mensaje de Telegram recorre el mismo camino que uno de WhatsApp."""

    async def test_crea_contacto_conversacion_y_mensaje(self, webhook_tenant: uuid.UUID) -> None:
        """El contacto lleva el nombre publico de Telegram, no un id enmascarado."""
        mensaje = _normalized(
            "telegram",
            "789",
            "4242",
            "Hola",
            sender_name="Ada Lovelace",
            raw_payload={"update_id": 4242},
        )

        await _process_message("telegram", "telegram", mensaje)

        fila = await _una_fila(
            webhook_tenant,
            "SELECT c.display_name, cv.channel, m.external_message_id, m.direction "
            "FROM contacts c JOIN conversations cv ON cv.contact_id = c.id "
            "JOIN messages m ON m.conversation_id = cv.id WHERE c.client_id = :cid",
        )
        assert fila.display_name == "Ada Lovelace"
        assert fila.channel == "telegram"
        assert fila.external_message_id == "4242"
        assert fila.direction == "inbound"

    async def test_el_identificador_queda_cifrado_y_se_reencuentra(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        """Un segundo mensaje del mismo chat reusa contacto y conversacion.

        Es la prueba de que la busqueda por el indice ciego (por tenant) encuentra
        el identificador que el listener del modelo acaba de escribir.
        """
        await _process_message(
            "telegram", "telegram", _normalized("telegram", "789", "1", "Primero")
        )
        await _process_message(
            "telegram", "telegram", _normalized("telegram", "789", "2", "Segundo")
        )

        assert await _contar(webhook_tenant, "contacts") == 1
        assert await _contar(webhook_tenant, "conversations") == 1
        assert await _contar(webhook_tenant, "messages") == 2
        contacto = await _una_fila(webhook_tenant, "SELECT id FROM contacts WHERE client_id = :cid")
        async with tenant_session(webhook_tenant) as session:
            identificador = await session.scalar(
                text(SQL_SELECT_IDENTIFICADOR),
                {"id": str(contacto.id), "clave": get_settings().ENCRYPTION_KEY},
            )
        assert identificador == "789"

    async def test_sin_nombre_publico_cae_al_identificador_enmascarado(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        """Sin `sender_name` se mantiene lo de siempre: el numero nunca en claro."""
        await _process_message("telegram", "telegram", _normalized("telegram", "789456123", "3"))

        fila = await _una_fila(
            webhook_tenant, "SELECT display_name FROM contacts WHERE client_id = :cid"
        )
        assert fila.display_name.startswith("*****6123 #")
        assert "789456" not in fila.display_name


class TestEmail:
    """Un email llega como contacto por direccion y conserva lo necesario del hilo."""

    async def test_crea_contacto_y_guarda_el_hilo_en_el_metadata(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        """`messages.metadata` lleva el Message-ID y el asunto: de ahi sale el `In-Reply-To`."""
        await _process_message("email", "email", _email("<abc@mail.example.com>"))

        fila = await _una_fila(
            webhook_tenant,
            "SELECT c.display_name, m.metadata, m.external_message_id FROM contacts c "
            "JOIN conversations cv ON cv.contact_id = c.id "
            "JOIN messages m ON m.conversation_id = cv.id WHERE c.client_id = :cid",
        )
        assert fila.display_name == "Juan Perez"
        assert fila.external_message_id == "<abc@mail.example.com>"
        assert fila.metadata["subject"] == "Consulta de precios"
        assert fila.metadata["message_id"] == "<abc@mail.example.com>"

    async def test_la_direccion_es_un_identificador_cifrado(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        """La direccion de correo es dato personal: va cifrada como el telefono."""
        await _process_message("email", "email", _email("<a@x>"))

        contacto = await _una_fila(webhook_tenant, "SELECT id FROM contacts WHERE client_id = :cid")
        async with tenant_session(webhook_tenant) as session:
            guardado = await session.scalar(
                text(SQL_SELECT_IDENTIFICADOR),
                {"id": str(contacto.id), "clave": get_settings().ENCRYPTION_KEY},
            )
            en_claro = await session.scalar(
                text(
                    "SELECT count(*) FROM contact_identifiers "
                    "WHERE client_id = :cid AND position('juan@example.com'::bytea IN identifier_value) > 0"
                ),
                {"cid": str(webhook_tenant)},
            )
        assert guardado == "juan@example.com"
        assert en_claro == 0

    async def test_dos_emails_de_la_misma_direccion_comparten_conversacion(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        """El hilo es por (contacto, canal): el cliente escribe dos veces, una sola conversacion."""
        await _process_message("email", "email", _email("<uno@x>"))
        await _process_message(
            "email", "email", _email("<dos@x>", raw_payload={"in_reply_to": "<uno@x>"})
        )

        assert await _contar(webhook_tenant, "contacts") == 1
        assert await _contar(webhook_tenant, "conversations") == 1
        assert await _contar(webhook_tenant, "messages") == 2

    async def test_la_direccion_en_mayusculas_es_el_mismo_contacto(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        """El indice ciego normaliza: `Juan@X.com` y `juan@x.com` son la misma persona."""
        await _process_message("email", "email", _email("<uno@x>"))
        otro = _email("<dos@x>")
        otro["sender_identifier"] = "JUAN@EXAMPLE.COM"

        await _process_message("email", "email", otro)

        assert await _contar(webhook_tenant, "contacts") == 1


class TestRespuestaEnElHilo:
    """`deliver_message()` cuelga la respuesta del ultimo email entrante, desde la base."""

    async def _conversacion(self, client_id: uuid.UUID) -> uuid.UUID:
        """Id de la unica conversacion del tenant."""
        fila = await _una_fila(
            client_id, "SELECT id, contact_id FROM conversations WHERE client_id = :cid"
        )
        return uuid.UUID(str(fila.id))

    async def test_usa_el_ultimo_mensaje_entrante(self, webhook_tenant: uuid.UUID) -> None:
        """Con dos emails entrantes, la respuesta cuelga del segundo."""
        await _process_message(
            "email",
            "email",
            _email("<uno@x>", raw_payload={"references": ""}),
        )
        await _process_message(
            "email",
            "email",
            _email(
                "<dos@x>",
                raw_payload={
                    "in_reply_to": "<uno@x>",
                    "references": "<uno@x>",
                    "subject": "Re: precios",
                },
            ),
        )
        conversacion = await self._conversacion(webhook_tenant)

        contexto = await delivery_module._contexto_de_respuesta(
            webhook_tenant, conversacion, "email"
        )

        assert contexto == {
            "subject": "Re: precios",
            "in_reply_to": "<dos@x>",
            "references": "<uno@x> <dos@x>",
        }

    async def test_no_toma_mensajes_salientes(self, webhook_tenant: uuid.UUID) -> None:
        """Un saliente posterior no es el mensaje al que responde el cliente."""
        await _process_message("email", "email", _email("<uno@x>"))
        conversacion = await self._conversacion(webhook_tenant)
        async with tenant_session(webhook_tenant) as session:
            session.add(
                Message(
                    client_id=webhook_tenant,
                    conversation_id=conversacion,
                    direction="outbound",
                    message_type="text",
                    content="Respuesta del bot",
                    sender_type="bot",
                    metadata_={"message_id": "<bot@empresa.com>", "subject": "Otro"},
                )
            )

        contexto = await delivery_module._contexto_de_respuesta(
            webhook_tenant, conversacion, "email"
        )

        assert contexto is not None
        assert contexto["in_reply_to"] == "<uno@x>"

    async def test_otro_tenant_no_ve_el_hilo(self, webhook_tenant: uuid.UUID) -> None:
        """RLS y el `client_id` explicito: la conversacion de un tenant no es de otro."""
        await _process_message("email", "email", _email("<uno@x>"))
        conversacion = await self._conversacion(webhook_tenant)

        contexto = await delivery_module._contexto_de_respuesta(uuid.uuid4(), conversacion, "email")

        assert contexto is None

    async def test_entrega_completa_guarda_el_message_id_propio(
        self, webhook_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El saliente queda con su Message-ID: el `In-Reply-To` del cliente lo resolvera."""
        await _process_message("email", "email", _email("<uno@x>"))
        fila = await _una_fila(
            webhook_tenant, "SELECT id, contact_id FROM conversations WHERE client_id = :cid"
        )
        recibidos: list[Any] = []

        class ProviderFalso:
            async def send_message(
                self, to: str, content: Any, channel_config: dict[str, Any]
            ) -> str:
                recibidos.append((to, content))
                return "<bot-1@empresa.com>"

        monkeypatch.setattr(
            delivery_module,
            "get_channel_config",
            lambda canal, client_id=None: ("email", {"from_email": "a@b.c"}),
        )
        monkeypatch.setattr(delivery_module, "get_messaging_provider", lambda n, c: ProviderFalso())

        externo = await delivery_module.deliver_message(
            client_id=webhook_tenant,
            conversation_id=uuid.UUID(str(fila.id)),
            contact_id=uuid.UUID(str(fila.contact_id)),
            channel="email",
            text="Claro, el plan Pro cuesta 20 USD.",
        )

        assert externo == "<bot-1@empresa.com>"
        destino, contenido = recibidos[0]
        assert destino == "juan@example.com"
        assert contenido.metadata["in_reply_to"] == "<uno@x>"
        saliente = await _una_fila(
            webhook_tenant,
            "SELECT external_message_id, direction FROM messages "
            "WHERE client_id = :cid AND direction = 'outbound'",
        )
        assert saliente.external_message_id == "<bot-1@empresa.com>"


class TestSinUnificacionEntreCanales:
    """Comportamiento actual: un contacto por (canal, identificador)."""

    async def test_el_mismo_texto_en_dos_canales_son_dos_contactos(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        """Un id de Telegram y un numero de WhatsApp iguales no se confunden.

        Tampoco se unifican por telefono: eso (criterio 5 del spec) necesita un
        flujo de resolucion que no existe. Este test fija lo que hay hoy.
        """
        await _process_message(
            "telegram", "telegram", _normalized("telegram", "573001112233", "t1")
        )
        await _process_message("ycloud", "whatsapp", _normalized("whatsapp", "573001112233", "w1"))

        assert await _contar(webhook_tenant, "contacts") == 2
        assert await _contar(webhook_tenant, "conversations") == 2
