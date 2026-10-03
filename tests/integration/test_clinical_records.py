"""Agente clinico contra PostgreSQL real (Sprint 13, Dev B).

Lo que los dobles de sesion no pueden probar: que el documento del paciente
queda cifrado en disco, que el consentimiento se exige de verdad en la misma
transaccion que inserta, que el trigger de auditoria no copia contenido clinico
a `audit_logs`, y que la anonimizacion respeta la historia clinica firmada.
"""

import json
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

DOCUMENTO = "1234567890"


def _url_admin() -> str:
    """URL del superusuario de la base de tests (el del servicio de Postgres del CI)."""
    return os.environ.get(
        "DATABASE_URL_ADMIN",
        "postgresql+asyncpg://test_user:test_password@localhost:5432/test_omnichannel",
    )


@pytest_asyncio.fixture
async def tenant() -> AsyncGenerator[uuid.UUID, None]:
    """Crea un tenant commiteado y borra lo que siembren los tests."""
    from app.core.database import engine, tenant_session

    await engine.dispose()

    client_id = uuid.uuid4()
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, 'Tenant Clinico', :slug, 'free', true)"
            ),
            {"id": str(client_id), "slug": f"clinico-{client_id.hex[:8]}"},
        )

    yield client_id

    # La historia clinica firmada no se puede borrar (trigger de retencion): la
    # limpieza va con el superusuario del CI y los triggers desactivados en su sesion.
    admin = create_async_engine(_url_admin())
    try:
        async with admin.begin() as conexion:
            await conexion.execute(text("SET session_replication_role = replica"))
            for tabla in (
                "messages",
                "call_records",
                "clinical_records",
                "patient_consents",
                "audit_logs",
                "conversations",
                "users",
                "agent_configs",
                "contacts",
            ):
                await conexion.execute(
                    text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                    {"cid": str(client_id)},
                )
            await conexion.execute(
                text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)}
            )
    finally:
        await admin.dispose()


@pytest_asyncio.fixture
async def profesional(tenant: uuid.UUID) -> uuid.UUID:
    """Un contacto que actua como profesional que dicta."""
    from app.core.database import tenant_session

    contacto = uuid.uuid4()
    async with tenant_session(tenant) as session:
        await session.execute(
            text(
                "INSERT INTO contacts (id, client_id, display_name) "
                "VALUES (:id, :cid, 'Dra. Prueba')"
            ),
            {"id": str(contacto), "cid": str(tenant)},
        )
        # El tenant lo declara profesional: sin esto las tools no lo dejan operar.
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


def _config(client_id: uuid.UUID, contacto: uuid.UUID) -> dict[str, Any]:
    return {
        "configurable": {
            "client_id": str(client_id),
            "contact_id": str(contacto),
            "channel": "whatsapp",
        }
    }


def _rips(**over: Any) -> dict[str, Any]:
    base = {
        "patient_document_type": "CC",
        "patient_document_number": DOCUMENTO,
        "patient_name": "Ana Perez",
        "service_date": "2025-01-15",
        "service_type": "consulta",
        "rips_type": "AC",
        "diagnosis_codes": [{"code": "I10", "type": "principal"}],
        "purpose_code": "05",
        "notes": {"subjective": "cefalea intensa"},
    }
    base.update(over)
    return base


async def _consentir(tenant: uuid.UUID, profesional: uuid.UUID, documento: str = DOCUMENTO) -> None:
    from app.agents.tools import clinical_tools as ct

    resultado = await ct.register_patient_consent.ainvoke(
        {"document_type": "CC", "document_number": documento, "consent_type": "verbal"},
        config=_config(tenant, profesional),
    )
    assert resultado["registered"] is True, resultado


async def _crear(tenant: uuid.UUID, profesional: uuid.UUID, **over: Any) -> dict[str, Any]:
    from app.agents.tools import clinical_tools as ct

    return await ct.create_rips_record.ainvoke(_rips(**over), config=_config(tenant, profesional))


async def _filas(tenant: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    from app.core.database import tenant_session

    async with tenant_session(tenant) as session:
        return list((await session.execute(text(sql), {"cid": str(tenant), **params})).all())


async def test_sin_consentimiento_no_se_crea_nada(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    resultado = await _crear(tenant, profesional)

    assert resultado["error"] == "consent_required"
    assert await _filas(tenant, "SELECT 1 FROM clinical_records WHERE client_id = :cid") == []


async def test_con_consentimiento_se_guarda_cifrado(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    from app.core.config import get_settings

    await _consentir(tenant, profesional)

    resultado = await _crear(tenant, profesional)

    assert resultado["success"] is True, resultado
    assert DOCUMENTO not in str(resultado)
    (fila,) = await _filas(
        tenant,
        "SELECT patient_document_number::text, patient_name::text, "
        "pgp_sym_decrypt(patient_document_number, :clave), "
        "pgp_sym_decrypt(patient_name, :clave), status, dictated_by_contact_id "
        "FROM clinical_records WHERE client_id = :cid",
        clave=get_settings().ENCRYPTION_KEY,
    )
    crudo_documento, crudo_nombre, documento, nombre, estado, dicto = fila
    # Criterio 12: documento y nombre son BYTEA cifrado, no texto.
    assert DOCUMENTO not in crudo_documento
    assert "Ana" not in crudo_nombre
    assert (documento, nombre) == (DOCUMENTO, "Ana Perez")
    assert estado == "draft"
    assert dicto == profesional


async def test_el_consentimiento_de_un_paciente_no_vale_para_otro(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    await _consentir(tenant, profesional, documento="1111111111")

    resultado = await _crear(tenant, profesional, patient_document_number="2222222222")

    assert resultado["error"] == "consent_required"


async def test_revocar_corta_los_registros_nuevos(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    from app.core.database import tenant_session
    from app.core.habeas_data import HabeasDataCompliance

    await _consentir(tenant, profesional)
    async with tenant_session(tenant) as session:
        revocado = await HabeasDataCompliance.revoke_consent(session, tenant, "CC", DOCUMENTO)
    assert revocado["revoked"] is True

    assert (await _crear(tenant, profesional))["error"] == "consent_required"


async def _conversacion(tenant: uuid.UUID, contacto: uuid.UUID) -> uuid.UUID:
    from app.core.database import tenant_session

    conversacion = uuid.uuid4()
    async with tenant_session(tenant) as session:
        await session.execute(
            text(
                "INSERT INTO conversations (id, client_id, contact_id, channel) "
                "VALUES (:id, :cid, :contacto, 'whatsapp')"
            ),
            {"id": str(conversacion), "cid": str(tenant), "contacto": str(contacto)},
        )
    return conversacion


async def test_un_reintento_no_duplica_el_borrador(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """Una entrega repetida de la tarea de Celery no debe crear dos registros."""
    from app.agents.tools import clinical_tools as ct

    await _consentir(tenant, profesional)
    conversacion = await _conversacion(tenant, profesional)
    config = {
        "configurable": {
            **_config(tenant, profesional)["configurable"],
            "conversation_id": str(conversacion),
        }
    }

    primera = await ct.create_rips_record.ainvoke(_rips(), config=config)
    segunda = await ct.create_rips_record.ainvoke(_rips(), config=config)

    assert primera["duplicate"] is False
    assert segunda["duplicate"] is True
    assert segunda["record_id"] == primera["record_id"]
    assert len(await _filas(tenant, "SELECT 1 FROM clinical_records WHERE client_id = :cid")) == 1


async def test_dos_entregas_simultaneas_crean_un_solo_borrador(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """El advisory lock por paciente serializa las entregas que se pisan."""
    import asyncio

    from app.agents.tools import clinical_tools as ct

    await _consentir(tenant, profesional)
    conversacion = await _conversacion(tenant, profesional)
    config = {
        "configurable": {
            **_config(tenant, profesional)["configurable"],
            "conversation_id": str(conversacion),
        }
    }

    resultados = await asyncio.gather(
        *(ct.create_rips_record.ainvoke(_rips(), config=config) for _ in range(4))
    )

    assert all(r["success"] for r in resultados), resultados
    assert sum(1 for r in resultados if r["duplicate"] is False) == 1
    assert len(await _filas(tenant, "SELECT 1 FROM clinical_records WHERE client_id = :cid")) == 1


async def test_la_auditoria_no_copia_contenido_clinico(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """Criterio 13, sin filtrar diagnosticos ni notas SOAP a `audit_logs`."""
    await _consentir(tenant, profesional)
    await _crear(tenant, profesional)

    filas = await _filas(
        tenant,
        "SELECT action::text, new_values::text FROM audit_logs "
        "WHERE client_id = :cid AND table_name = 'clinical_records'",
    )

    assert [f[0] for f in filas] == ["INSERT"]
    volcado = filas[0][1]
    assert "I10" not in volcado
    assert "cefalea" not in volcado
    assert "Ana" not in volcado
    assert DOCUMENTO not in volcado
    assert '"status": "draft"' in volcado


async def test_un_update_tambien_queda_auditado_sin_contenido(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    from app.core.database import tenant_session

    await _consentir(tenant, profesional)
    await _crear(tenant, profesional)
    async with tenant_session(tenant) as session:
        await session.execute(
            text("UPDATE clinical_records SET status = 'reviewed' WHERE client_id = :cid"),
            {"cid": str(tenant)},
        )

    filas = await _filas(
        tenant,
        "SELECT action::text, old_values::text, new_values::text FROM audit_logs "
        "WHERE client_id = :cid AND table_name = 'clinical_records' ORDER BY created_at",
    )

    assert [f[0] for f in filas] == ["INSERT", "UPDATE"]
    assert '"status": "draft"' in filas[1][1]
    assert '"status": "reviewed"' in filas[1][2]
    assert "I10" not in filas[1][1] + filas[1][2]


async def test_la_anonimizacion_respeta_la_historia_firmada(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    from app.core.config import get_settings
    from app.core.database import tenant_session
    from app.core.habeas_data import HabeasDataCompliance

    await _consentir(tenant, profesional)
    await _crear(tenant, profesional, service_date="2025-01-15")
    await _crear(tenant, profesional, service_date="2025-01-16")
    async with tenant_session(tenant) as session:
        await session.execute(
            text(
                "UPDATE clinical_records SET status = 'reviewed' "
                "WHERE client_id = :cid AND service_date = '2025-01-15'"
            ),
            {"cid": str(tenant)},
        )
    async with tenant_session(tenant) as session:
        await session.execute(
            text(
                "UPDATE clinical_records SET status = 'signed', signed_at = now() "
                "WHERE client_id = :cid AND service_date = '2025-01-15'"
            ),
            {"cid": str(tenant)},
        )

    async with tenant_session(tenant) as session:
        resultado = await HabeasDataCompliance.anonymize_patient_data(
            session, tenant, "CC", DOCUMENTO
        )

    assert (resultado["records_anonymized"], resultado["records_retained"]) == (1, 1)
    filas = await _filas(
        tenant,
        "SELECT service_date::text, status, anonymized_at IS NOT NULL, "
        "structured_notes IS NULL, contact_id IS NULL, pgp_sym_decrypt(structured_notes, :clave) "
        "FROM clinical_records WHERE client_id = :cid ORDER BY service_date",
        clave=get_settings().ENCRYPTION_KEY,
    )
    firmado, borrador = filas
    assert firmado[2] is False, "la historia firmada no se toca"
    assert "cefalea" in firmado[5]
    assert borrador[2] is True
    assert borrador[3] is True, "las notas del anonimizado se borran"

    # El historial del agente ya no ve el anonimizado.
    async with tenant_session(tenant) as session:
        from app.services.clinical import obtener_historial

        historial = await obtener_historial(
            session, client_id=tenant, document_type="CC", document_number=DOCUMENTO
        )
    assert [h["date"] for h in historial] == ["2025-01-15"]


async def test_exportar_devuelve_los_datos_del_paciente(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    from app.core.database import tenant_session
    from app.core.habeas_data import HabeasDataCompliance

    await _consentir(tenant, profesional)
    await _crear(tenant, profesional)

    async with tenant_session(tenant) as session:
        exportacion = await HabeasDataCompliance.export_patient_data(
            session, tenant, "CC", DOCUMENTO
        )

    (registro,) = exportacion["clinical_records"]
    assert registro["diagnosis_codes"][0]["code"] == "I10"
    assert registro["structured_notes"] == {"subjective": "cefalea intensa"}
    assert exportacion["consents"][0]["consent_type"] == "verbal"


async def test_un_tenant_no_ve_el_historial_de_otro(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """El indice ciego es por tenant y la RLS acota la consulta."""
    from app.core.database import tenant_session
    from app.services.clinical import obtener_historial

    await _consentir(tenant, profesional)
    await _crear(tenant, profesional)

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
        async with tenant_session(otro) as session:
            historial = await obtener_historial(
                session, client_id=otro, document_type="CC", document_number=DOCUMENTO
            )
        assert historial == []
    finally:
        async with tenant_session(otro) as session:
            await session.execute(text("DELETE FROM clients WHERE id = :id"), {"id": str(otro)})


async def _sql(tenant: uuid.UUID, sql: str, **params: Any) -> None:
    from app.core.database import tenant_session

    async with tenant_session(tenant) as session:
        await session.execute(text(sql), {"cid": str(tenant), **params})


async def _un_registro(tenant: uuid.UUID, profesional: uuid.UUID, **over: Any) -> str:
    await _consentir(tenant, profesional)
    resultado = await _crear(tenant, profesional, **over)
    assert resultado["success"] is True, resultado
    return str(resultado["record_id"])


async def _firmado(tenant: uuid.UUID, profesional: uuid.UUID, **over: Any) -> str:
    """Crea un registro y lo lleva a `signed` por la via normal."""
    from app.core.database import tenant_session
    from app.services.clinical import avanzar_registro

    record_id = await _un_registro(tenant, profesional, **over)
    usuario = await _usuario(tenant)
    for destino in ("reviewed", "signed"):
        async with tenant_session(tenant) as session:
            await avanzar_registro(
                session,
                client_id=tenant,
                record_id=uuid.UUID(record_id),
                destino=destino,
                user_id=usuario,
            )
    return record_id


async def test_las_notas_soap_quedan_cifradas_en_disco(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    await _un_registro(tenant, profesional)

    (fila,) = await _filas(
        tenant, "SELECT structured_notes::text FROM clinical_records WHERE client_id = :cid"
    )

    assert "cefalea" not in fila[0]


async def test_el_flujo_de_firma_avanza_en_orden(tenant: uuid.UUID, profesional: uuid.UUID) -> None:
    from app.core.database import tenant_session
    from app.services import clinical as svc

    record_id = uuid.UUID(await _un_registro(tenant, profesional))
    usuario = await _usuario(tenant)

    async with tenant_session(tenant) as session:
        with pytest.raises(svc.TransicionInvalidaError):
            await svc.avanzar_registro(
                session, client_id=tenant, record_id=record_id, destino="signed", user_id=usuario
            )
    for destino in ("reviewed", "signed", "submitted"):
        async with tenant_session(tenant) as session:
            detalle = await svc.avanzar_registro(
                session, client_id=tenant, record_id=record_id, destino=destino, user_id=usuario
            )
        assert detalle["status"] == destino
    assert detalle["reviewed_by"] == str(usuario)
    assert detalle["signed_at"] is not None
    assert detalle["retention_until"] == "2045-01-15"


async def _usuario(tenant: uuid.UUID) -> uuid.UUID:
    usuario = uuid.uuid4()
    await _sql(
        tenant,
        "INSERT INTO users (id, client_id, email, password_hash, first_name, last_name, role) "
        "VALUES (:id, :cid, :email, 'x', 'Dr', 'Prueba', 'admin')",
        id=str(usuario),
        email=f"{usuario.hex[:8]}@clinico.test",
    )
    return usuario


async def test_dos_firmas_simultaneas_solo_una_gana(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    import asyncio

    from app.core.database import tenant_session
    from app.services import clinical as svc

    record_id = uuid.UUID(await _un_registro(tenant, profesional))
    usuario = await _usuario(tenant)
    async with tenant_session(tenant) as session:
        await svc.avanzar_registro(
            session, client_id=tenant, record_id=record_id, destino="reviewed", user_id=usuario
        )

    async def _firmar() -> str:
        try:
            async with tenant_session(tenant) as session:
                await svc.avanzar_registro(
                    session,
                    client_id=tenant,
                    record_id=record_id,
                    destino="signed",
                    user_id=usuario,
                )
            return "ok"
        except svc.TransicionInvalidaError:
            return "conflicto"

    resultados = await asyncio.gather(_firmar(), _firmar(), _firmar())

    assert sorted(resultados) == ["conflicto", "conflicto", "ok"]


async def test_un_registro_firmado_no_se_puede_borrar_en_20_anos(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """Retencion de la historia clinica: la garantiza la base, no la aplicacion."""
    await _firmado(tenant, profesional)

    with pytest.raises(DBAPIError, match="no se puede borrar"):
        await _sql(tenant, "DELETE FROM clinical_records WHERE client_id = :cid")

    assert len(await _filas(tenant, "SELECT 1 FROM clinical_records WHERE client_id = :cid")) == 1


async def test_un_registro_firmado_no_se_puede_modificar(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    from app.core.config import get_settings

    await _firmado(tenant, profesional)

    for sql in (
        "UPDATE clinical_records SET diagnosis_codes = NULL WHERE client_id = :cid",
        "UPDATE clinical_records SET patient_name = pgp_sym_encrypt('otro', 'k') WHERE client_id = :cid",
        "UPDATE clinical_records SET status = 'draft' WHERE client_id = :cid",
    ):
        with pytest.raises(DBAPIError, match=r"no se puede modificar|Transicion de estado"):
            await _sql(tenant, sql)

    (fila,) = await _filas(
        tenant,
        "SELECT status, pgp_sym_decrypt(diagnosis_codes, :clave) FROM clinical_records "
        "WHERE client_id = :cid",
        clave=get_settings().ENCRYPTION_KEY,
    )
    assert fila[0] == "signed"
    assert "I10" in fila[1]


async def test_lo_firmado_solo_puede_pasar_a_enviado(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    await _firmado(tenant, profesional)

    await _sql(tenant, "UPDATE clinical_records SET status = 'submitted' WHERE client_id = :cid")

    (fila,) = await _filas(tenant, "SELECT status FROM clinical_records WHERE client_id = :cid")
    assert fila[0] == "submitted"


async def test_no_se_puede_saltar_de_borrador_a_firmado(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    await _un_registro(tenant, profesional)

    with pytest.raises(DBAPIError, match="Transicion de estado invalida"):
        await _sql(tenant, "UPDATE clinical_records SET status = 'signed' WHERE client_id = :cid")


async def test_un_borrador_si_se_puede_borrar(tenant: uuid.UUID, profesional: uuid.UUID) -> None:
    await _un_registro(tenant, profesional)

    await _sql(tenant, "DELETE FROM clinical_records WHERE client_id = :cid")

    assert await _filas(tenant, "SELECT 1 FROM clinical_records WHERE client_id = :cid") == []


async def test_vencidos_los_20_anos_lo_firmado_se_puede_anonimizar_y_borrar(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """La conservacion no es para siempre: 20 anos desde la ultima atencion."""
    from app.core.database import tenant_session
    from app.core.habeas_data import HabeasDataCompliance

    await _firmado(tenant, profesional, service_date="2001-03-01")

    async with tenant_session(tenant) as session:
        resultado = await HabeasDataCompliance.anonymize_patient_data(
            session, tenant, "CC", DOCUMENTO
        )
    assert (resultado["records_anonymized"], resultado["records_retained"]) == (1, 0)

    await _sql(tenant, "DELETE FROM clinical_records WHERE client_id = :cid")
    assert await _filas(tenant, "SELECT 1 FROM clinical_records WHERE client_id = :cid") == []


async def test_una_atencion_reciente_extiende_la_retencion_de_las_antiguas(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """El plazo corre desde la ULTIMA atencion del paciente, no desde cada registro."""
    # La fecha de "hoy" es la de Colombia, la que valida el servicio: en UTC ya
    # es "manana" durante cinco horas cada noche y el registro se rechazaba.
    from app.services.clinical import _hoy_colombia

    await _firmado(tenant, profesional, service_date="2001-03-01")
    await _un_registro(tenant, profesional, service_date=_hoy_colombia().isoformat())

    with pytest.raises(DBAPIError, match="no se puede borrar"):
        await _sql(
            tenant,
            "DELETE FROM clinical_records WHERE client_id = :cid AND service_date = '2001-03-01'",
        )


async def test_catalogo_oficial_cargado_verifica_y_rechaza(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    from app.core.database import tenant_session
    from app.services.clinical_catalog import buscar_en_catalogo

    admin = create_async_engine(_url_admin())
    try:
        async with admin.begin() as conexion:
            await conexion.execute(text("DELETE FROM cie10_catalog"))
        from sqlalchemy.ext.asyncio import AsyncSession

        from app.services.clinical_catalog import cargar_catalogo

        async with AsyncSession(admin) as session, session.begin():
            resumen = await cargar_catalogo(
                session,
                "cie10",
                [
                    ("I10", "Hipertensión esencial (primaria)"),
                    ("S72.00", "Fractura del cuello del fémur"),
                    ("mal", "x"),
                ],
            )
        assert resumen == {"loaded": 2, "invalid": 1}

        await _consentir(tenant, profesional)
        # Un codigo oficial: verificado y con la descripcion del catalogo.
        ok = await _crear(
            tenant, profesional, diagnosis_codes=[{"code": "s72.00", "type": "principal"}]
        )
        assert ok["success"] is True, ok
        assert "unverified_codes" not in ok
        # Uno con formato valido pero fuera del catalogo oficial: se rechaza.
        malo = await _crear(
            tenant,
            profesional,
            service_date="2025-01-16",
            diagnosis_codes=[{"code": "Z99.9", "type": "principal"}],
        )
        assert malo["success"] is False
        assert "catalogo oficial" in malo["error"]

        async with tenant_session(tenant) as session:
            assert (await buscar_en_catalogo(session, "cie10", "hipertension esencial"))[0][
                "code"
            ] == "I10"
            assert await buscar_en_catalogo(session, "cie10", "50%") == []
    finally:
        async with admin.begin() as conexion:
            await conexion.execute(text("DELETE FROM cie10_catalog"))
        await admin.dispose()


async def test_el_mensaje_clinico_entrante_no_queda_en_claro(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    from app.agents.nodes import clinical as nodo

    conversacion = await _conversacion(tenant, profesional)
    await _sql(
        tenant,
        "INSERT INTO messages (id, client_id, conversation_id, direction, message_type, content, "
        "external_message_id, sender_type) "
        "VALUES (gen_random_uuid(), :cid, :conv, 'inbound', 'text', 'paciente Ana Perez con HTA', "
        "'wamid.1', 'contact')",
        conv=str(conversacion),
    )

    await nodo._proteger_mensaje_entrante(
        {
            "client_id": str(tenant),
            "conversation_id": str(conversacion),
            "message": {"text": "x", "external_message_id": "wamid.1"},
        }
    )

    (fila,) = await _filas(tenant, "SELECT content FROM messages WHERE client_id = :cid")
    assert "Ana Perez" not in fila[0]
    assert fila[0] == "[contenido clínico protegido]"


async def _llamada(tenant: uuid.UUID, conversacion: uuid.UUID, transcript: str) -> None:
    """Deja una llamada guardada, ligada a la conversacion por sus mensajes."""
    from app.tasks.voice_tasks import guardar_llamada

    await guardar_llamada(
        tenant,
        "CA-clinica-1",
        {
            "direction": "inbound",
            "status": "completed",
            "started_at": "2026-09-29T15:00:00+00:00",
            "transcript": [
                {"role": "caller", "text": transcript, "timestamp": "2026-09-29T15:00:05+00:00"},
                {"role": "agent", "text": "listo", "timestamp": "2026-09-29T15:00:09+00:00"},
            ],
        },
    )


async def _transcripcion(tenant: uuid.UUID) -> str:
    """Transcripcion descifrada, como texto; la columna es `BYTEA` cifrado (migracion 019)."""
    from app.core.config import get_settings

    (fila,) = await _filas(
        tenant,
        "SELECT pgp_sym_decrypt(transcript, :clave) FROM call_records WHERE client_id = :cid",
        clave=get_settings().ENCRYPTION_KEY,
    )
    return fila[0]


async def _mensaje_de_voz(tenant: uuid.UUID, conversacion: uuid.UUID) -> None:
    await _sql(
        tenant,
        "INSERT INTO messages (id, client_id, conversation_id, direction, message_type, content, "
        "external_message_id, sender_type) "
        "VALUES (gen_random_uuid(), :cid, :conv, 'inbound', 'text', 'paciente Ana Perez', "
        "'CA-clinica-1:0', 'contact')",
        conv=str(conversacion),
    )


async def test_la_transcripcion_ya_guardada_se_redacta_al_volverse_clinica(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """La llamada se guardo al colgar, antes de que el grafo procesara la ultima frase."""
    from app.agents.nodes import clinical as nodo

    conversacion = await _conversacion(tenant, profesional)
    await _mensaje_de_voz(tenant, conversacion)
    await _llamada(tenant, conversacion, "paciente Ana Perez, hipertension")
    assert "Ana Perez" in await _transcripcion(tenant)

    await nodo._proteger_mensaje_entrante(
        {
            "client_id": str(tenant),
            "conversation_id": str(conversacion),
            "message": {"external_message_id": "CA-clinica-1:0"},
        }
    )

    transcripcion = await _transcripcion(tenant)
    assert "Ana Perez" not in transcripcion
    assert "hipertension" not in transcripcion
    assert transcripcion.count("[contenido cl") == 2
    # Rol y hora se conservan: el listado del CRM y la auditoria siguen sirviendo.
    turnos = json.loads(transcripcion)
    assert [t["role"] for t in turnos] == ["caller", "agent"]
    assert turnos[0]["timestamp"] == "2026-09-29T15:00:05+00:00"


async def test_la_transcripcion_de_una_llamada_no_clinica_esta_cifrada_en_disco(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """Sin acceso a la clave, `psql` ve bytes: ni el texto ni el JSON aparecen."""
    conversacion = await _conversacion(tenant, profesional)
    await _mensaje_de_voz(tenant, conversacion)
    await _llamada(tenant, conversacion, "quiero una cita el martes")

    (fila,) = await _filas(
        tenant,
        "SELECT pg_typeof(transcript)::text, position('cita'::bytea IN transcript) "
        "FROM call_records WHERE client_id = :cid",
    )
    assert fila[0] == "bytea"
    assert fila[1] == 0
    assert "quiero una cita el martes" in await _transcripcion(tenant)


async def test_la_transcripcion_que_se_guarda_despues_tambien_sale_redactada(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """Si la conversacion ya es clinica cuando la llamada cuelga, no se guarda en claro."""
    from app.agents.nodes import clinical as nodo

    conversacion = await _conversacion(tenant, profesional)
    await _mensaje_de_voz(tenant, conversacion)
    await nodo._proteger_mensaje_entrante(
        {
            "client_id": str(tenant),
            "conversation_id": str(conversacion),
            "message": {"external_message_id": "CA-clinica-1:0"},
        }
    )

    await _llamada(tenant, conversacion, "paciente Ana Perez, hipertension")

    transcripcion = await _transcripcion(tenant)
    assert "Ana Perez" not in transcripcion
    assert transcripcion.count("[contenido cl") == 2


async def test_una_llamada_no_clinica_conserva_su_transcripcion(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    conversacion = await _conversacion(tenant, profesional)
    await _mensaje_de_voz(tenant, conversacion)

    await _llamada(tenant, conversacion, "quiero una cita el martes")

    assert "quiero una cita el martes" in await _transcripcion(tenant)


async def test_marcar_la_conversacion_no_pisa_el_resto_de_su_metadata(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    from app.core.database import tenant_session
    from app.services.clinical_privacy import (
        conversacion_es_clinica,
        marcar_conversacion_clinica,
    )

    conversacion = await _conversacion(tenant, profesional)
    await _sql(
        tenant,
        'UPDATE conversations SET metadata = \'{"handoff": {"reason": "x"}}\'::jsonb '
        "WHERE id = :conv",
        conv=str(conversacion),
    )

    async with tenant_session(tenant) as session:
        assert await conversacion_es_clinica(session, tenant, conversacion) is False
        await marcar_conversacion_clinica(session, tenant, conversacion)
    async with tenant_session(tenant) as session:
        assert await conversacion_es_clinica(session, tenant, conversacion) is True

    (fila,) = await _filas(
        tenant, "SELECT metadata::text FROM conversations WHERE id = :conv", conv=str(conversacion)
    )
    assert "handoff" in fila[0]


async def test_un_canal_de_voz_no_autoriza_aunque_el_numero_este_en_la_lista(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """El caller ID se falsifica: suplantar el numero de un profesional no da acceso."""
    from app.agents.tools import clinical_tools as ct

    llamada = {
        "configurable": {
            **_config(tenant, profesional)["configurable"],
            "channel": "voice",
        }
    }

    resultado = await ct.get_patient_history.ainvoke(
        {"document_type": "CC", "document_number": DOCUMENTO}, config=llamada
    )
    creado = await ct.create_rips_record.ainvoke(_rips(), config=llamada)

    assert resultado["error"] == ct.NO_AUTORIZADO
    assert creado["error"] == ct.NO_AUTORIZADO
    assert await _filas(tenant, "SELECT 1 FROM clinical_records WHERE client_id = :cid") == []


async def test_un_contacto_que_no_esta_en_la_lista_no_opera_ni_por_whatsapp(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    from app.agents.tools import clinical_tools as ct

    otro = uuid.uuid4()
    await _sql(
        tenant,
        "INSERT INTO contacts (id, client_id, display_name) VALUES (:id, :cid, 'Cliente')",
        id=str(otro),
    )

    resultado = await ct.create_rips_record.ainvoke(_rips(), config=_config(tenant, otro))

    assert resultado["error"] == ct.NO_AUTORIZADO


async def test_una_llamada_corta_guardada_antes_de_ligarse_a_la_conversacion_tambien_se_redacta(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """La transcripcion se guarda al colgar, antes de que la llamada tenga conversation_id."""
    from app.agents.nodes import clinical as nodo
    from app.tasks.voice_tasks import guardar_llamada

    conversacion = await _conversacion(tenant, profesional)
    # Sin mensajes todavia: el guardado no puede ligar la llamada a la conversacion.
    await guardar_llamada(
        tenant,
        "CA-corta-1",
        {
            "direction": "inbound",
            "status": "completed",
            "started_at": "2026-09-29T15:00:00+00:00",
            "transcript": [
                {
                    "role": "caller",
                    "text": "paciente Ana Perez",
                    "timestamp": "2026-09-29T15:00:05+00:00",
                }
            ],
        },
    )
    (fila,) = await _filas(
        tenant, "SELECT conversation_id IS NULL FROM call_records WHERE client_id = :cid"
    )
    assert fila[0] is True
    assert "Ana Perez" in await _transcripcion(tenant)

    await nodo._proteger_mensaje_entrante(
        {
            "client_id": str(tenant),
            "conversation_id": str(conversacion),
            "channel": "voice",
            "message": {"external_message_id": "CA-corta-1:0"},
        }
    )

    transcripcion = await _transcripcion(tenant)
    assert "Ana Perez" not in transcripcion
    assert "[contenido cl" in transcripcion


# ─── Registro clinico <-> llamada (call_record_id) ──────────────────────────
#
# Estos tests prueban el enlace de datos. Hoy la voz no autoriza al agente
# clinico (ADR-072), asi que el registro se crea con un canal verificado sobre
# una conversacion que tiene una llamada: lo que importa aqui es el enlace, no
# quien autoriza.


async def _crear_en(
    tenant: uuid.UUID, profesional: uuid.UUID, conversacion: uuid.UUID
) -> dict[str, Any]:
    from app.agents.tools import clinical_tools as ct

    config = _config(tenant, profesional)
    config["configurable"]["conversation_id"] = str(conversacion)
    return await ct.create_rips_record.ainvoke(_rips(), config=config)


async def _llamada_de(
    tenant: uuid.UUID,
    call_sid: str,
    *,
    empezo_hace: timedelta,
    termino_hace: timedelta | None = None,
) -> None:
    """Guarda una llamada con sus tiempos relativos a ahora."""
    from app.tasks.voice_tasks import guardar_llamada

    ahora = datetime.now(timezone.utc)
    await guardar_llamada(
        tenant,
        call_sid,
        {
            "direction": "inbound",
            "status": "completed" if termino_hace is not None else "in-progress",
            "started_at": (ahora - empezo_hace).isoformat(),
            "ended_at": (ahora - termino_hace).isoformat() if termino_hace is not None else None,
        },
    )


async def _mensaje_de_llamada(tenant: uuid.UUID, conversacion: uuid.UUID, call_sid: str) -> None:
    await _sql(
        tenant,
        "INSERT INTO messages (id, client_id, conversation_id, direction, message_type, content, "
        "external_message_id, sender_type) "
        "VALUES (gen_random_uuid(), :cid, :conv, 'inbound', 'text', 'hola', :ext, 'contact')",
        conv=str(conversacion),
        ext=f"{call_sid}:0",
    )


async def _llamada_del_registro(tenant: uuid.UUID, record_id: str) -> uuid.UUID | None:
    (fila,) = await _filas(
        tenant,
        "SELECT call_record_id FROM clinical_records WHERE client_id = :cid AND id = :id",
        id=record_id,
    )
    return fila[0]


async def _id_de_llamada(tenant: uuid.UUID, call_sid: str) -> uuid.UUID:
    (fila,) = await _filas(
        tenant,
        "SELECT id FROM call_records WHERE client_id = :cid AND call_sid = :sid",
        sid=call_sid,
    )
    return fila[0]


async def test_un_dictado_durante_la_llamada_queda_ligado_a_ella(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    conversacion = await _conversacion(tenant, profesional)
    await _mensaje_de_llamada(tenant, conversacion, "CA-en-curso")
    await _llamada_de(tenant, "CA-en-curso", empezo_hace=timedelta(minutes=2))
    await _consentir(tenant, profesional)

    resultado = await _crear_en(tenant, profesional, conversacion)

    assert resultado["success"] is True, resultado
    assert await _llamada_del_registro(tenant, str(resultado["record_id"])) == (
        await _id_de_llamada(tenant, "CA-en-curso")
    )


async def test_un_registro_sin_llamada_queda_sin_enlace(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """WhatsApp, Telegram...: la conversacion no tiene ninguna llamada."""
    conversacion = await _conversacion(tenant, profesional)
    await _consentir(tenant, profesional)

    resultado = await _crear_en(tenant, profesional, conversacion)

    assert await _llamada_del_registro(tenant, str(resultado["record_id"])) is None


async def test_el_registro_creado_antes_de_que_la_llamada_conozca_su_conversacion_se_liga(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """El `conversation_id` de la llamada solo aparece cuando ya hay mensajes."""
    conversacion = await _conversacion(tenant, profesional)
    await _consentir(tenant, profesional)
    resultado = await _crear_en(tenant, profesional, conversacion)
    record_id = str(resultado["record_id"])
    assert await _llamada_del_registro(tenant, record_id) is None

    await _mensaje_de_llamada(tenant, conversacion, "CA-tarde")
    await _llamada_de(
        tenant, "CA-tarde", empezo_hace=timedelta(minutes=5), termino_hace=timedelta(seconds=1)
    )

    assert await _llamada_del_registro(tenant, record_id) == await _id_de_llamada(
        tenant, "CA-tarde"
    )


async def test_una_llamada_anterior_ya_cerrada_no_se_atribuye_un_dictado_nuevo(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """Una conversacion de voz acumula llamadas: la de ayer no dicto el registro de hoy."""
    conversacion = await _conversacion(tenant, profesional)
    await _mensaje_de_llamada(tenant, conversacion, "CA-ayer")
    await _llamada_de(
        tenant, "CA-ayer", empezo_hace=timedelta(hours=3), termino_hace=timedelta(hours=2)
    )
    await _consentir(tenant, profesional)

    resultado = await _crear_en(tenant, profesional, conversacion)

    assert await _llamada_del_registro(tenant, str(resultado["record_id"])) is None


async def test_el_guardado_tardio_de_una_llamada_vieja_no_se_lleva_un_registro_posterior(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """Un status callback atrasado de la llamada de ayer no reclama el registro de hoy."""
    conversacion = await _conversacion(tenant, profesional)
    await _consentir(tenant, profesional)
    resultado = await _crear_en(tenant, profesional, conversacion)
    await _mensaje_de_llamada(tenant, conversacion, "CA-vieja")

    await _llamada_de(
        tenant, "CA-vieja", empezo_hace=timedelta(hours=3), termino_hace=timedelta(hours=2)
    )

    assert await _llamada_del_registro(tenant, str(resultado["record_id"])) is None


async def test_un_registro_firmado_no_aborta_el_guardado_de_la_llamada(
    tenant: uuid.UUID, profesional: uuid.UUID
) -> None:
    """El trigger de retencion rechaza cambiar un registro firmado.

    Si el enlace lo intentara, el error abortaria la transaccion que guarda la
    llamada y se perderian su transcripcion y su estado.
    """
    from app.core.database import tenant_session
    from app.services import clinical as svc

    conversacion = await _conversacion(tenant, profesional)
    await _consentir(tenant, profesional)
    resultado = await _crear_en(tenant, profesional, conversacion)
    record_id = uuid.UUID(str(resultado["record_id"]))
    usuario = await _usuario(tenant)
    for destino in ("reviewed", "signed"):
        async with tenant_session(tenant) as session:
            await svc.avanzar_registro(
                session, client_id=tenant, record_id=record_id, destino=destino, user_id=usuario
            )

    await _mensaje_de_llamada(tenant, conversacion, "CA-firmado")
    await _llamada_de(
        tenant, "CA-firmado", empezo_hace=timedelta(minutes=5), termino_hace=timedelta(seconds=1)
    )

    # La llamada se guardo, y el registro firmado quedo intacto.
    assert await _id_de_llamada(tenant, "CA-firmado") is not None
    assert await _llamada_del_registro(tenant, str(record_id)) is None
