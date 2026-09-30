"""PIN de voz contra PostgreSQL (RLS) y Redis reales (ADR-073)."""

import json
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.test_clinical_records import _url_admin

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

TELEFONO = "+573001234567"
PIN = "482913"


@pytest_asyncio.fixture
async def tenant() -> AsyncGenerator[uuid.UUID, None]:
    """Tenant commiteado; borra lo que siembren los tests."""
    from app.core.database import engine, tenant_session

    await engine.dispose()
    client_id = uuid.uuid4()
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, 'Tenant PIN', :slug, 'free', true)"
            ),
            {"id": str(client_id), "slug": f"pin-{client_id.hex[:8]}"},
        )
    yield client_id

    admin = create_async_engine(_url_admin())
    try:
        async with admin.begin() as conexion:
            await conexion.execute(text("SET session_replication_role = replica"))
            for tabla in ("voice_pins", "audit_logs", "agent_configs", "contacts"):
                await conexion.execute(
                    text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                    {"cid": str(client_id)},
                )
            await conexion.execute(
                text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)}
            )
    finally:
        await admin.dispose()


async def _contacto(tenant: uuid.UUID, *, profesional: bool = True) -> uuid.UUID:
    from app.core.database import tenant_session

    contacto = uuid.uuid4()
    async with tenant_session(tenant) as session:
        await session.execute(
            text("INSERT INTO contacts (id, client_id, display_name) VALUES (:id, :cid, 'Dra.')"),
            {"id": str(contacto), "cid": str(tenant)},
        )
        if profesional:
            await session.execute(
                text(
                    "INSERT INTO agent_configs (client_id, name, config) "
                    "VALUES (:cid, 'clinico', CAST(:config AS jsonb))"
                ),
                {
                    "cid": str(tenant),
                    "config": json.dumps(
                        {
                            "enabled_agents": ["rag", "clinical"],
                            "clinical": {"professional_contact_ids": [str(contacto)]},
                        }
                    ),
                },
            )
    return contacto


async def _fijar(tenant: uuid.UUID, contacto: uuid.UUID, telefono: str = TELEFONO) -> None:
    from app.core.database import tenant_session
    from app.services.voice.pin_auth import fijar_pin

    async with tenant_session(tenant) as session:
        await fijar_pin(session, tenant, contacto, telefono, PIN)


async def _fila(tenant: uuid.UUID) -> Any:
    from app.core.database import tenant_session

    async with tenant_session(tenant) as session:
        return (
            await session.execute(
                text(
                    "SELECT pin_hash, phone_hash, failed_attempts, locked_until "
                    "FROM voice_pins WHERE client_id = :cid"
                ),
                {"cid": str(tenant)},
            )
        ).one()


class TestVerificarPin:
    async def test_el_pin_correcto_devuelve_al_contacto(self, tenant: uuid.UUID) -> None:
        from app.services.voice.pin_auth import verificar_pin

        contacto = await _contacto(tenant)
        await _fijar(tenant, contacto)

        assert await verificar_pin(tenant, TELEFONO, PIN) == contacto

    async def test_ni_el_pin_ni_el_telefono_quedan_en_claro(self, tenant: uuid.UUID) -> None:
        contacto = await _contacto(tenant)
        await _fijar(tenant, contacto)

        pin_hash, phone_hash, *_ = await _fila(tenant)

        assert PIN not in pin_hash
        assert pin_hash.startswith("$2"), "bcrypt"
        assert "3001234567" not in phone_hash
        assert len(phone_hash) == 64

    async def test_el_telefono_se_normaliza(self, tenant: uuid.UUID) -> None:
        from app.services.voice.pin_auth import verificar_pin

        contacto = await _contacto(tenant)
        await _fijar(tenant, contacto, "+57 300 123-4567")

        assert await verificar_pin(tenant, "573001234567", PIN) == contacto

    async def test_un_pin_incorrecto_no_autentica_y_cuenta_el_fallo(
        self, tenant: uuid.UUID
    ) -> None:
        from app.services.voice.pin_auth import verificar_pin

        await _fijar(tenant, await _contacto(tenant))

        assert await verificar_pin(tenant, TELEFONO, "111222") is None
        assert (await _fila(tenant)).failed_attempts == 1

    async def test_un_numero_sin_pin_es_indistinguible_de_un_pin_equivocado(
        self, tenant: uuid.UUID
    ) -> None:
        from app.services.voice.pin_auth import verificar_pin

        await _fijar(tenant, await _contacto(tenant))

        assert await verificar_pin(tenant, "+573009999999", PIN) is None
        assert await verificar_pin(tenant, "no-es-un-telefono", PIN) is None

    async def test_el_acierto_reinicia_el_contador(self, tenant: uuid.UUID) -> None:
        from app.services.voice.pin_auth import verificar_pin

        contacto = await _contacto(tenant)
        await _fijar(tenant, contacto)
        await verificar_pin(tenant, TELEFONO, "111222")
        await verificar_pin(tenant, TELEFONO, "111222")

        assert await verificar_pin(tenant, TELEFONO, PIN) == contacto
        assert (await _fila(tenant)).failed_attempts == 0

    async def test_cinco_fallos_seguidos_bloquean_hasta_con_el_pin_correcto(
        self, tenant: uuid.UUID
    ) -> None:
        from app.services.voice.pin_auth import BLOQUEO, MAX_FALLOS_SEGUIDOS, verificar_pin

        await _fijar(tenant, await _contacto(tenant))
        for _ in range(MAX_FALLOS_SEGUIDOS):
            assert await verificar_pin(tenant, TELEFONO, "111222") is None

        fila = await _fila(tenant)
        assert fila.locked_until is not None
        assert fila.locked_until > datetime.now(timezone.utc) + BLOQUEO - timedelta(minutes=1)
        assert await verificar_pin(tenant, TELEFONO, PIN) is None, "bloqueado: ni el correcto"

    async def test_pasado_el_bloqueo_vuelve_a_aceptar(self, tenant: uuid.UUID) -> None:
        from app.core.database import tenant_session
        from app.services.voice.pin_auth import MAX_FALLOS_SEGUIDOS, verificar_pin

        contacto = await _contacto(tenant)
        await _fijar(tenant, contacto)
        for _ in range(MAX_FALLOS_SEGUIDOS):
            await verificar_pin(tenant, TELEFONO, "111222")
        async with tenant_session(tenant) as session:
            await session.execute(
                text("UPDATE voice_pins SET locked_until = now() - interval '1 minute'")
            )

        assert await verificar_pin(tenant, TELEFONO, PIN) == contacto

    async def test_reemplazar_el_pin_levanta_el_bloqueo(self, tenant: uuid.UUID) -> None:
        from app.services.voice.pin_auth import MAX_FALLOS_SEGUIDOS, verificar_pin

        contacto = await _contacto(tenant)
        await _fijar(tenant, contacto)
        for _ in range(MAX_FALLOS_SEGUIDOS):
            await verificar_pin(tenant, TELEFONO, "111222")

        await _fijar(tenant, contacto)

        assert await verificar_pin(tenant, TELEFONO, PIN) == contacto

    async def test_un_tenant_no_verifica_contra_el_pin_de_otro(self, tenant: uuid.UUID) -> None:
        from app.core.database import tenant_session
        from app.services.voice.pin_auth import verificar_pin

        await _fijar(tenant, await _contacto(tenant))
        otro = uuid.uuid4()
        async with tenant_session(otro) as session:
            await session.execute(
                text(
                    "INSERT INTO clients (id, name, slug, plan, is_active) "
                    "VALUES (:id, 'Otro', :slug, 'free', true)"
                ),
                {"id": str(otro), "slug": f"otro-{otro.hex[:8]}"},
            )
        try:
            assert await verificar_pin(otro, TELEFONO, PIN) is None
        finally:
            admin = create_async_engine(_url_admin())
            async with admin.begin() as conexion:
                await conexion.execute(
                    text("DELETE FROM clients WHERE id = :id"), {"id": str(otro)}
                )
            await admin.dispose()


class TestApiDePines:
    async def test_un_admin_registra_el_pin_de_un_profesional(
        self, tenant: uuid.UUID, authenticated_client_factory: Any
    ) -> None:
        from app.services.voice.pin_auth import verificar_pin

        contacto = await _contacto(tenant)
        cliente = authenticated_client_factory(role="admin", client_id=tenant)

        respuesta = await cliente.put(
            "/api/v1/voice/pins",
            json={"contact_id": str(contacto), "phone": TELEFONO, "pin": PIN},
        )

        assert respuesta.status_code == 204
        assert respuesta.content == b""
        assert await verificar_pin(tenant, TELEFONO, PIN) == contacto

    async def test_un_contacto_que_no_es_profesional_ni_operador_se_rechaza(
        self, tenant: uuid.UUID, authenticated_client_factory: Any
    ) -> None:
        contacto = await _contacto(tenant, profesional=False)
        cliente = authenticated_client_factory(role="admin", client_id=tenant)

        respuesta = await cliente.put(
            "/api/v1/voice/pins",
            json={"contact_id": str(contacto), "phone": TELEFONO, "pin": PIN},
        )

        assert respuesta.status_code == 400

    @pytest.mark.parametrize("pin", ["123456", "111111", "1234", "abcdef"])
    async def test_un_pin_debil_o_mal_formado_se_rechaza(
        self, tenant: uuid.UUID, authenticated_client_factory: Any, pin: str
    ) -> None:
        contacto = await _contacto(tenant)
        cliente = authenticated_client_factory(role="admin", client_id=tenant)

        respuesta = await cliente.put(
            "/api/v1/voice/pins",
            json={"contact_id": str(contacto), "phone": TELEFONO, "pin": pin},
        )

        assert respuesta.status_code == 400
        assert pin not in respuesta.text

    async def test_un_error_de_validacion_no_devuelve_el_pin(
        self, tenant: uuid.UUID, authenticated_client_factory: Any
    ) -> None:
        contacto = await _contacto(tenant)
        cliente = authenticated_client_factory(role="admin", client_id=tenant)

        respuesta = await cliente.put(
            "/api/v1/voice/pins",
            json={"contact_id": str(contacto), "phone": "no-es-e164", "pin": "482913"},
        )

        assert respuesta.status_code == 422
        assert "482913" not in respuesta.text

    async def test_un_agente_no_puede_fijar_pines(
        self, tenant: uuid.UUID, authenticated_client_factory: Any
    ) -> None:
        contacto = await _contacto(tenant)
        cliente = authenticated_client_factory(role="agent", client_id=tenant)

        respuesta = await cliente.put(
            "/api/v1/voice/pins",
            json={"contact_id": str(contacto), "phone": TELEFONO, "pin": PIN},
        )

        assert respuesta.status_code == 403

    async def test_borrar_los_pines_de_un_contacto(
        self, tenant: uuid.UUID, authenticated_client_factory: Any
    ) -> None:
        from app.services.voice.pin_auth import verificar_pin

        contacto = await _contacto(tenant)
        await _fijar(tenant, contacto)
        cliente = authenticated_client_factory(role="admin", client_id=tenant)

        respuesta = await cliente.delete(f"/api/v1/voice/pins/{contacto}")

        assert respuesta.status_code == 204
        assert await verificar_pin(tenant, TELEFONO, PIN) is None


class TestDeLaLlamadaAlAgente:
    """Recorrido completo: teclas -> PIN -> Redis -> nodo clinico y tools."""

    async def test_una_llamada_autenticada_opera_y_una_sin_pin_no(self, tenant: uuid.UUID) -> None:
        from app.agents.tools import clinical_tools as ct
        from app.services.channel_identity import contacto_autenticado
        from app.services.dedup import get_redis
        from app.services.voice.pin_auth import (
            marcar_llamada_autenticada,
            profesional_de_la_llamada,
            verificar_pin,
        )

        profesional = await _contacto(tenant)
        de_voz = await _contacto(tenant, profesional=False)  # el contacto del caller ID
        await _fijar(tenant, profesional)
        call_sid = f"CA{uuid.uuid4().hex}"
        config = {
            "configurable": {
                "client_id": str(tenant),
                "contact_id": str(de_voz),
                "channel": "voice",
                "external_message_id": f"{call_sid}:1",
            }
        }
        redis = get_redis()
        try:
            # Sin PIN, la llamada no opera aunque el numero sea el del profesional.
            assert await ct._profesional(config) != profesional

            autenticado = await verificar_pin(tenant, TELEFONO, PIN)
            assert autenticado == profesional
            await marcar_llamada_autenticada(tenant, call_sid, autenticado)

            assert await profesional_de_la_llamada(tenant, call_sid) == profesional
            assert await contacto_autenticado(
                channel="voice",
                client_id=tenant,
                contact_id=de_voz,
                external_message_id=f"{call_sid}:1",
            ) == str(profesional)
            assert await ct._profesional(config) == profesional
            # Otra llamada del mismo numero no hereda la autenticacion.
            otra = {"configurable": {**config["configurable"], "external_message_id": "CA-otra:1"}}
            assert await ct._profesional(otra) != profesional
        finally:
            await redis.delete(f"voice:auth:{str(tenant).lower()}:{call_sid}")
