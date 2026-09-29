"""Agente clinico contra PostgreSQL real (Sprint 13, Dev B).

Lo que los dobles de sesion no pueden probar: que el documento del paciente
queda cifrado en disco, que el consentimiento se exige de verdad en la misma
transaccion que inserta, que el trigger de auditoria no copia contenido clinico
a `audit_logs`, y que la anonimizacion respeta la historia clinica firmada.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

DOCUMENTO = "1234567890"


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

    async with tenant_session(client_id) as session:
        for tabla in (
            "clinical_records",
            "patient_consents",
            "audit_logs",
            "conversations",
            "contacts",
        ):
            await session.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                {"cid": str(client_id)},
            )
        await session.execute(text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)})


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
    return contacto


def _config(client_id: uuid.UUID, contacto: uuid.UUID) -> dict[str, Any]:
    return {"configurable": {"client_id": str(client_id), "contact_id": str(contacto)}}


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
    from app.core.database import tenant_session
    from app.core.habeas_data import HabeasDataCompliance

    await _consentir(tenant, profesional)
    await _crear(tenant, profesional, service_date="2025-01-15")
    await _crear(tenant, profesional, service_date="2025-01-16")
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
        "structured_notes::text, contact_id IS NULL "
        "FROM clinical_records WHERE client_id = :cid ORDER BY service_date",
    )
    firmado, borrador = filas
    assert firmado[2] is False, "la historia firmada no se toca"
    assert "cefalea" in firmado[3]
    assert borrador[2] is True
    assert borrador[3] == "{}"

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
