"""Canales de Sprint 9 (Telegram y email) contra PostgreSQL real, con RLS activo.

`_process_message()` es lo que hace el worker con lo que el endpoint encola; aqui
se comprueba lo que queda escrito en la base con el contexto de tenant aplicado:
contacto (con el nombre publico del remitente), identificador cifrado,
conversacion por canal y mensaje. Y, para email, que la respuesta saliente se
cuelga del hilo del ultimo mensaje entrante.

Tambien la unificacion de un mismo contacto entre canales (criterio 5 del spec):
solo por telefono **verificado por el propio canal** y solo si la coincidencia es
inequivoca (`app/services/phone_unification.py`). Cada test de "no une" protege
una fuga entre personas o entre tenants.

Requiere base de datos: `pytest tests/ --run-db`.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.agents.nodes import _delivery as delivery_module
from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.encryption import blind_index
from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
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
            delivery_module, "get_channel_config", lambda canal: ("email", {"from_email": "a@b.c"})
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


TELEFONO = "573001112233"


async def _filas(client_id: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    """Ejecuta una consulta con el contexto de tenant aplicado.

    Args:
        client_id: Tenant.
        sql: Consulta con `:cid` para el tenant.
        **params: Otros parametros.

    Returns:
        Todas las filas.
    """
    async with tenant_session(client_id) as session:
        return list((await session.execute(text(sql), {"cid": str(client_id), **params})).all())


async def _activos(client_id: uuid.UUID) -> list[uuid.UUID]:
    """Ids de los contactos vigentes (no fusionados) del tenant."""
    filas = await _filas(
        client_id, "SELECT id FROM contacts WHERE client_id = :cid AND merged_into_id IS NULL"
    )
    return [uuid.UUID(str(f.id)) for f in filas]


async def _canales_de(client_id: uuid.UUID, contact_id: uuid.UUID) -> set[str]:
    """Canales de los identificadores que tiene un contacto."""
    filas = await _filas(
        client_id,
        "SELECT channel FROM contact_identifiers WHERE client_id = :cid AND contact_id = :ct",
        ct=str(contact_id),
    )
    return {f.channel for f in filas}


async def _sembrar(
    client_id: uuid.UUID, identificadores: dict[str, str], *, borrado_gdpr: bool = False
) -> uuid.UUID:
    """Crea un contacto ya existente, con los identificadores dados.

    Args:
        client_id: Tenant.
        identificadores: `canal -> valor`.
        borrado_gdpr: Si el contacto ya fue anonimizado por GDPR.

    Returns:
        Id del contacto.
    """
    contact_id = uuid.uuid4()
    async with tenant_session(client_id) as session:
        session.add(
            Contact(
                id=contact_id,
                client_id=client_id,
                display_name="Sembrado",
                is_gdpr_deleted=borrado_gdpr,
            )
        )
        await session.flush()
        for canal, valor in identificadores.items():
            session.add(
                ContactIdentifier(
                    client_id=client_id,
                    contact_id=contact_id,
                    channel=canal,
                    identifier_value=valor,
                )
            )
    return contact_id


def _telegram(chat_id: str, external_id: str, telefono: str | None) -> dict[str, Any]:
    """Mensaje de Telegram; con `telefono`, como si el usuario compartiera el suyo."""
    return _normalized(
        "telegram",
        chat_id,
        external_id,
        "Contacto compartido" if telefono else "Hola",
        verified_phone=telefono,
    )


async def _whatsapp(external_id: str, telefono: str = TELEFONO) -> None:
    await _process_message("ycloud", "whatsapp", _normalized("whatsapp", telefono, external_id))


@pytest_asyncio.fixture
async def otro_tenant() -> AsyncGenerator[uuid.UUID, None]:
    """Un segundo tenant commiteado, con su limpieza."""
    client_id = uuid.uuid4()
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, 'Otro Tenant', :slug, 'free', true)"
            ),
            {"id": str(client_id), "slug": f"otro-tenant-{client_id.hex[:8]}"},
        )

    yield client_id

    async with tenant_session(client_id) as session:
        for tabla in (
            "messages",
            "conversations",
            "contact_identifiers",
            "contacts",
            "webhook_dedup",
        ):
            await session.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                {"cid": str(client_id)},
            )
        await session.execute(text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)})


class TestUnificacionPorTelefonoVerificado:
    """La misma persona en dos canales es un solo contacto, con prueba."""

    async def test_telegram_con_su_propio_telefono_se_une_al_contacto_de_whatsapp(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        await _whatsapp("w1")

        await _process_message("telegram", "telegram", _telegram("789", "t1", TELEFONO))

        activos = await _activos(webhook_tenant)
        assert len(activos) == 1
        assert await _canales_de(webhook_tenant, activos[0]) == {"whatsapp", "telegram"}

    async def test_las_conversaciones_de_los_dos_canales_cuelgan_del_mismo_contacto(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        await _whatsapp("w1")
        await _process_message("telegram", "telegram", _telegram("789", "t1", TELEFONO))

        filas = await _filas(
            webhook_tenant, "SELECT channel, contact_id FROM conversations WHERE client_id = :cid"
        )

        assert {f.channel for f in filas} == {"whatsapp", "telegram"}
        assert len({f.contact_id for f in filas}) == 1

    async def test_un_mensaje_posterior_de_telegram_sigue_en_el_contacto_unido(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        """El identificador de Telegram ahora apunta al superviviente."""
        await _whatsapp("w1")
        await _process_message("telegram", "telegram", _telegram("789", "t1", TELEFONO))

        await _process_message("telegram", "telegram", _telegram("789", "t2", None))

        assert len(await _activos(webhook_tenant)) == 1
        assert await _contar(webhook_tenant, "conversations") == 2

    async def test_whatsapp_posterior_se_une_al_contacto_de_telegram(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        """Orden inverso: el telefono verificado de Telegram quedo registrado."""
        await _process_message("telegram", "telegram", _telegram("789", "t1", TELEFONO))

        await _whatsapp("w1")

        activos = await _activos(webhook_tenant)
        assert len(activos) == 1
        assert await _canales_de(webhook_tenant, activos[0]) == {
            "telegram",
            "verified_phone",
            "whatsapp",
        }

    async def test_el_telefono_verificado_no_es_un_canal_para_responder(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        """Ninguna conversacion puede usar `verified_phone` como canal de envio."""
        from app.agents.nodes._tenant import ChannelNotConfiguredError, get_channel_config

        with pytest.raises(ChannelNotConfiguredError):
            get_channel_config("verified_phone")

    async def test_el_telefono_verificado_se_guarda_cifrado(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        await _process_message("telegram", "telegram", _telegram("789", "t1", TELEFONO))

        (fila,) = await _filas(
            webhook_tenant,
            "SELECT identifier_value FROM contact_identifiers "
            "WHERE client_id = :cid AND channel = 'verified_phone'",
        )

        assert TELEFONO.encode() not in bytes(fila.identifier_value)

    async def test_el_superviviente_deja_traza_sin_datos_personales(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        await _whatsapp("w1")
        await _process_message("telegram", "telegram", _telegram("789", "t1", TELEFONO))
        (contacto,) = await _activos(webhook_tenant)

        (fila,) = await _filas(
            webhook_tenant,
            "SELECT jsonb_array_length(metadata->'unifications') AS n, "
            "metadata->'unifications'->0->>'reason' AS motivo, "
            "(metadata->'unifications')::text AS crudo "
            "FROM contacts WHERE id = :ct",
            ct=str(contacto),
        )

        assert fila.n == 1
        assert fila.motivo == "verified_phone"
        assert TELEFONO not in fila.crudo


class TestNoUneSinCoincidenciaInequivoca:
    """Cada uno de estos casos evita mezclar a dos personas."""

    async def test_el_mismo_texto_en_dos_canales_son_dos_contactos(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        """Un id de Telegram con forma de telefono no se confunde con un telefono."""
        await _process_message("telegram", "telegram", _telegram(TELEFONO, "t1", None))
        await _whatsapp("w1")

        assert len(await _activos(webhook_tenant)) == 2
        assert await _contar(webhook_tenant, "conversations") == 2

    async def test_telegram_sin_telefono_verificado_no_une(self, webhook_tenant: uuid.UUID) -> None:
        """Un contacto ajeno o reenviado llega sin `verified_phone` (lo filtra el provider)."""
        await _whatsapp("w1")

        await _process_message("telegram", "telegram", _telegram("789", "t1", None))

        assert len(await _activos(webhook_tenant)) == 2

    async def test_instagram_y_email_nunca_unen(self, webhook_tenant: uuid.UUID) -> None:
        await _whatsapp("w1")

        await _process_message(
            "meta",
            "instagram",
            _normalized("instagram", "6789000000000001", "ig1", verified_phone=TELEFONO),
        )
        await _process_message(
            "email",
            "email",
            _normalized("email", "juan@example.com", "e1", verified_phone=TELEFONO),
        )

        assert len(await _activos(webhook_tenant)) == 3

    async def test_dos_titulares_previos_es_ambiguo(self, webhook_tenant: uuid.UUID) -> None:
        """No se sabe cual es el correcto: queda para la fusion manual."""
        await _sembrar(webhook_tenant, {"whatsapp": f"+{TELEFONO}"})
        await _sembrar(webhook_tenant, {"verified_phone": TELEFONO})

        await _process_message("telegram", "telegram", _telegram("789", "t1", TELEFONO))

        assert len(await _activos(webhook_tenant)) == 3

    async def test_otro_telefono_ya_registrado_no_se_pisa(self, webhook_tenant: uuid.UUID) -> None:
        await _process_message("telegram", "telegram", _telegram("789", "t1", TELEFONO))

        await _process_message("telegram", "telegram", _telegram("789", "t2", "573109998877"))

        filas = await _filas(
            webhook_tenant,
            "SELECT id FROM contact_identifiers "
            "WHERE client_id = :cid AND channel = 'verified_phone'",
        )
        assert len(filas) == 1
        assert len(await _activos(webhook_tenant)) == 1

    async def test_dos_cuentas_de_telegram_no_se_unen(self, webhook_tenant: uuid.UUID) -> None:
        """Un contacto de WhatsApp que ya tiene OTRA cuenta de Telegram."""
        await _sembrar(webhook_tenant, {"whatsapp": TELEFONO, "telegram": "111"})

        await _process_message("telegram", "telegram", _telegram("222", "t1", TELEFONO))

        assert len(await _activos(webhook_tenant)) == 2

    async def test_un_contacto_borrado_por_gdpr_no_se_une(self, webhook_tenant: uuid.UUID) -> None:
        await _sembrar(webhook_tenant, {"whatsapp": TELEFONO}, borrado_gdpr=True)

        await _process_message("telegram", "telegram", _telegram("789", "t1", TELEFONO))

        assert len(await _activos(webhook_tenant)) == 2


class TestAislamientoEntreTenants:
    """El mismo telefono en dos tenants son dos personas distintas para cada uno."""

    async def test_el_mismo_telefono_en_otro_tenant_no_se_une(
        self,
        webhook_tenant: uuid.UUID,
        otro_tenant: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _whatsapp("w1")
        contacto_a = (await _activos(webhook_tenant))[0]
        monkeypatch.setattr(get_settings(), "DEFAULT_CLIENT_ID", str(otro_tenant))

        await _process_message("telegram", "telegram", _telegram("789", "t1", TELEFONO))

        # El tenant A no cambia: un contacto, un solo canal, sin fusiones.
        assert await _activos(webhook_tenant) == [contacto_a]
        assert await _canales_de(webhook_tenant, contacto_a) == {"whatsapp"}
        # El tenant B tiene su propio contacto, sin nada del A.
        activos_b = await _activos(otro_tenant)
        assert len(activos_b) == 1
        assert activos_b[0] != contacto_a
        assert await _canales_de(otro_tenant, activos_b[0]) == {"telegram", "verified_phone"}

    async def test_los_hashes_del_mismo_telefono_difieren_por_tenant(
        self, webhook_tenant: uuid.UUID, otro_tenant: uuid.UUID
    ) -> None:
        """La barrera de fondo: aunque un filtro fallara, el indice ciego no coincide."""
        await _whatsapp("w1")
        (fila_a,) = await _filas(
            webhook_tenant,
            "SELECT identifier_hash FROM contact_identifiers WHERE client_id = :cid",
        )
        await _sembrar(otro_tenant, {"whatsapp": TELEFONO})
        (fila_b,) = await _filas(
            otro_tenant, "SELECT identifier_hash FROM contact_identifiers WHERE client_id = :cid"
        )

        assert fila_a.identifier_hash != fila_b.identifier_hash


class TestFlujoDeTelefonoTelegram:
    """Pedir y agradecer el telefono, de punta a punta contra Postgres real."""

    @pytest.fixture
    def respuestas(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        """Captura las respuestas fijas que el webhook encola."""
        from app.tasks import webhook_processor

        capturadas: list[dict[str, Any]] = []
        monkeypatch.setattr(
            webhook_processor, "_enqueue_channel_reply", lambda **kw: capturadas.append(kw)
        )
        return capturadas

    async def test_vincular_ofrece_el_boton_y_no_llama_a_la_ia(
        self,
        webhook_tenant: uuid.UUID,
        respuestas: list[dict[str, Any]],
        ia_encolada: list[dict[str, Any]],
    ) -> None:
        mensaje = _normalized("telegram", "789", "t1", "/vincular")

        await _process_message("telegram", "telegram", mensaje)

        assert [r["metadata"] for r in respuestas] == [
            {"request_contact": "📱 Compartir mi numero"}
        ]
        assert ia_encolada == []
        # El mensaje del usuario si queda en el historial.
        assert await _contar(webhook_tenant, "messages") == 1

    async def test_compartir_el_numero_agradece_une_y_no_guarda_el_numero_en_claro(
        self,
        webhook_tenant: uuid.UUID,
        respuestas: list[dict[str, Any]],
        ia_encolada: list[dict[str, Any]],
    ) -> None:
        await _whatsapp("w1")
        ia_encolada.clear()
        compartido = _normalized(
            "telegram",
            "789",
            "t1",
            "Compartio su numero de telefono",
            verified_phone=TELEFONO,
            raw_payload={"message": {"contact": {"phone_number": "********2233", "user_id": 789}}},
        )

        await _process_message("telegram", "telegram", compartido)

        assert [r["metadata"] for r in respuestas] == [{"remove_keyboard": True}]
        assert ia_encolada == []
        assert len(await _activos(webhook_tenant)) == 1
        filas = await _filas(
            webhook_tenant,
            "SELECT content, metadata::text AS crudo FROM messages "
            "WHERE client_id = :cid AND external_message_id = 't1'",
        )
        assert TELEFONO not in filas[0].content
        assert TELEFONO not in filas[0].crudo


VISITANTE = "a1" * 16


def _webchat(visitor: str, external: str, texto: str = "hola", **extra: Any) -> dict[str, Any]:
    """Mensaje de Webchat ya normalizado, como lo deja el endpoint del WebSocket."""
    return _normalized(
        "webchat",
        visitor,
        f"{visitor}:{external}",
        texto,
        raw_payload={"visitor_id": visitor, "message_id": external},
        **extra,
    )


async def _guardar_salientes(
    client_id: uuid.UUID, visitor: str, textos: list[str], *, desde: int = 0
) -> list[str]:
    """Guarda mensajes salientes de la conversacion de Webchat del visitante.

    Args:
        client_id: Tenant.
        visitor: Visitante (su conversacion debe existir).
        textos: Contenidos, en orden cronologico.
        desde: Segundo (de un dia fijo) del primer mensaje, para ordenar entre llamadas.

    Returns:
        Los `message_id` (`external_message_id`) asignados.
    """
    from datetime import datetime, timedelta, timezone

    (fila,) = await _filas(
        client_id,
        "SELECT cv.id FROM conversations cv "
        "JOIN contact_identifiers ci ON ci.contact_id = cv.contact_id "
        "WHERE cv.client_id = :cid AND cv.channel = 'webchat' AND ci.channel = 'webchat' "
        "AND ci.identifier_hash = :h",
        h=blind_index(visitor, client_id),
    )
    base = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    ids: list[str] = []
    async with tenant_session(client_id) as session:
        for i, texto in enumerate(textos):
            externo = f"out-{visitor[:4]}-{desde + i}"
            ids.append(externo)
            session.add(
                Message(
                    client_id=client_id,
                    conversation_id=fila.id,
                    direction="outbound",
                    message_type="text",
                    content=texto,
                    external_message_id=externo,
                    sender_type="bot",
                    created_at=base + timedelta(seconds=desde + i),
                )
            )
    return ids


class TestWebchat:
    """Un visitante de Webchat recorre el mismo camino que cualquier otro canal."""

    async def test_el_primer_mensaje_crea_contacto_identificador_y_conversacion(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        await _process_message(
            "webchat", "webchat", _webchat(VISITANTE, "m1", sender_name="Ada Visitante")
        )

        fila = await _una_fila(
            webhook_tenant,
            "SELECT c.display_name, cv.channel, m.content, m.direction "
            "FROM contacts c JOIN conversations cv ON cv.contact_id = c.id "
            "JOIN messages m ON m.conversation_id = cv.id WHERE c.client_id = :cid",
        )
        assert fila.display_name == "Ada Visitante"
        assert fila.channel == "webchat"
        assert (fila.content, fila.direction) == ("hola", "inbound")

    async def test_el_visitor_id_queda_cifrado_y_el_visitante_se_reencuentra(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        await _process_message("webchat", "webchat", _webchat(VISITANTE, "m1"))
        await _process_message("webchat", "webchat", _webchat(VISITANTE, "m2", "otra vez"))

        assert len(await _activos(webhook_tenant)) == 1
        assert await _contar(webhook_tenant, "conversations") == 1
        (fila,) = await _filas(
            webhook_tenant,
            "SELECT identifier_value FROM contact_identifiers WHERE client_id = :cid",
        )
        assert VISITANTE.encode() not in bytes(fila.identifier_value)

    async def test_dos_visitantes_son_dos_contactos(self, webhook_tenant: uuid.UUID) -> None:
        await _process_message("webchat", "webchat", _webchat("1" * 32, "m1"))
        await _process_message("webchat", "webchat", _webchat("2" * 32, "m1"))

        assert len(await _activos(webhook_tenant)) == 2

    async def test_un_visitante_no_se_une_a_otro_canal_aunque_el_id_se_parezca(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        await _whatsapp("w1", TELEFONO)
        await _process_message("webchat", "webchat", _webchat(TELEFONO, "m1"))

        assert len(await _activos(webhook_tenant)) == 2


class TestWebchatRecuperacion:
    """Lo que un visitante se perdio mientras estuvo desconectado."""

    async def _preparar(self, tenant: uuid.UUID, textos: list[str]) -> list[str]:
        await _process_message("webchat", "webchat", _webchat(VISITANTE, "m1"))
        return await _guardar_salientes(tenant, VISITANTE, textos)

    async def test_un_visitante_nuevo_recibe_lo_ultimo_en_orden(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        from app.services.webchat_history import mensajes_perdidos

        ids = await self._preparar(webhook_tenant, ["uno", "dos", "tres"])

        frames = await mensajes_perdidos(webhook_tenant, VISITANTE, None, 50)

        assert [f["text"] for f in frames] == ["uno", "dos", "tres"]
        assert [f["message_id"] for f in frames] == ids
        assert all(f["type"] == "message" and f["timestamp"] for f in frames)

    async def test_con_el_ultimo_visto_solo_llega_lo_posterior(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        from app.services.webchat_history import mensajes_perdidos

        ids = await self._preparar(webhook_tenant, ["uno", "dos", "tres", "cuatro"])

        frames = await mensajes_perdidos(webhook_tenant, VISITANTE, ids[1], 50)

        assert [f["text"] for f in frames] == ["tres", "cuatro"]

    async def test_un_last_message_id_desconocido_cae_en_los_ultimos(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        from app.services.webchat_history import mensajes_perdidos

        await self._preparar(webhook_tenant, ["uno", "dos"])

        frames = await mensajes_perdidos(webhook_tenant, VISITANTE, "no-existe", 50)

        assert [f["text"] for f in frames] == ["uno", "dos"]

    async def test_el_limite_devuelve_los_ultimos_y_en_orden(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        from app.services.webchat_history import mensajes_perdidos

        await self._preparar(webhook_tenant, ["uno", "dos", "tres", "cuatro"])

        frames = await mensajes_perdidos(webhook_tenant, VISITANTE, None, 2)

        assert [f["text"] for f in frames] == ["tres", "cuatro"]

    async def test_no_incluye_los_mensajes_del_propio_visitante(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        from app.services.webchat_history import mensajes_perdidos

        await self._preparar(webhook_tenant, ["respuesta"])

        frames = await mensajes_perdidos(webhook_tenant, VISITANTE, None, 50)

        assert [f["text"] for f in frames] == ["respuesta"]

    async def test_un_visitante_sin_historial_recibe_una_lista_vacia(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        from app.services.webchat_history import mensajes_perdidos

        assert await mensajes_perdidos(webhook_tenant, "9" * 32, None, 50) == []

    async def test_no_se_ve_la_conversacion_de_otro_visitante(
        self, webhook_tenant: uuid.UUID
    ) -> None:
        """Ni con su `visitor_id` ni con el `last_message_id` de un mensaje ajeno."""
        from app.services.webchat_history import mensajes_perdidos

        ids = await self._preparar(webhook_tenant, ["secreto de A"])
        await _process_message("webchat", "webchat", _webchat("b" * 32, "m1"))

        # B no tiene salientes; con el id de un mensaje de A no obtiene nada de A.
        frames = await mensajes_perdidos(webhook_tenant, "b" * 32, ids[0], 50)

        assert frames == []

    async def test_otro_tenant_no_encuentra_al_visitante(
        self, webhook_tenant: uuid.UUID, otro_tenant: uuid.UUID
    ) -> None:
        from app.services.webchat_history import mensajes_perdidos

        await self._preparar(webhook_tenant, ["secreto"])

        assert await mensajes_perdidos(otro_tenant, VISITANTE, None, 50) == []

    async def test_lo_entregado_en_vivo_y_lo_recuperado_comparten_message_id(
        self, webhook_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Es lo que permite al cliente deduplicar lo que le llega por las dos vias."""
        import json

        from app.services.messaging import webchat as wc
        from app.services.webchat_history import mensajes_perdidos

        publicados: list[tuple[str, str]] = []

        class RedisFalso:
            async def publish(self, canal: str, mensaje: str) -> int:
                publicados.append((canal, mensaje))
                return 0

        monkeypatch.setattr(wc, "get_redis", lambda: RedisFalso())
        await _process_message("webchat", "webchat", _webchat(VISITANTE, "m1"))
        (fila,) = await _filas(
            webhook_tenant, "SELECT id, contact_id FROM conversations WHERE client_id = :cid"
        )

        await delivery_module.deliver_message(
            client_id=webhook_tenant,
            conversation_id=uuid.UUID(str(fila.id)),
            contact_id=uuid.UUID(str(fila.contact_id)),
            channel="webchat",
            text="Hola, ¿en que te ayudo?",
        )

        (canal, cuerpo) = publicados[0]
        en_vivo = json.loads(cuerpo)
        assert canal.startswith(f"webchat:out:{webhook_tenant}:")
        assert canal.endswith(VISITANTE)
        recuperado = await mensajes_perdidos(webhook_tenant, VISITANTE, None, 50)
        assert [f["message_id"] for f in recuperado] == [en_vivo["message_id"]]
