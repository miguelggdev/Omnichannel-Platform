"""Tests de cumplimiento Habeas Data (Ley 1581 de 2012) — Sprint 13, Dev B."""

import os
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only-minimum-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters-long")

from app.core.habeas_data import (
    ANONIMIZADO,
    HabeasDataCompliance,
    HabeasDataError,
    hash_paciente,
    normalizar_documento,
)
from app.models.clinical_record import PatientConsent
from tests.unit.agent_doubles import FakeSession

CLIENT_ID = uuid.uuid4()


def _consentimiento(**over: Any) -> SimpleNamespace:
    base = {
        "granted_at": datetime(2025, 1, 10, tzinfo=timezone.utc),
        "consent_type": "verbal",
        "data_category": "health",
        "revoked_at": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _registro(estado: str, **over: Any) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "status": estado,
        "patient_document_number": "1234567890",
        "patient_name": "Ana",
        "raw_transcription": "dictado",
        "structured_notes": {"subjective": "dolor"},
        "medical_entities": [{"text": "x"}],
        "contact_id": uuid.uuid4(),
        "patient_document_hash": "h",
        "anonymized_at": None,
        "service_date": date(2025, 1, 15),
        "service_type": "consulta",
        "rips_type": "AC",
        "specialty": None,
        "diagnosis_codes": [{"code": "I10", "type": "principal"}],
        "procedure_codes": [],
        "call_record_id": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


class TestDocumento:
    def test_normaliza_tipo_y_numero(self) -> None:
        assert normalizar_documento(" cc ", "1.234-567 890") == ("CC", "1234567890")

    @pytest.mark.parametrize("tipo", ["", "XX", "cedula"])
    def test_tipo_invalido(self, tipo: str) -> None:
        with pytest.raises(HabeasDataError, match="Tipo de documento"):
            normalizar_documento(tipo, "12345678")

    @pytest.mark.parametrize("numero", ["", "123", "12 3", "a" * 21, "1234$5678"])
    def test_numero_invalido(self, numero: str) -> None:
        with pytest.raises(HabeasDataError, match="numero de documento"):
            normalizar_documento("CC", numero)

    def test_el_hash_es_estable_ante_el_formato(self) -> None:
        assert hash_paciente(CLIENT_ID, "CC", "1.234.567.890") == hash_paciente(
            CLIENT_ID, "cc", "1234567890"
        )

    def test_el_hash_es_por_tenant(self) -> None:
        """Igual que los identificadores de contacto (ADR-052): no se puede cruzar tenants."""
        assert hash_paciente(uuid.uuid4(), "CC", "1234567890") != hash_paciente(
            uuid.uuid4(), "CC", "1234567890"
        )

    def test_el_tipo_de_documento_entra_en_el_hash(self) -> None:
        assert hash_paciente(CLIENT_ID, "CC", "1234567890") != hash_paciente(
            CLIENT_ID, "TI", "1234567890"
        )

    def test_el_hash_no_contiene_el_documento(self) -> None:
        assert "1234567890" not in hash_paciente(CLIENT_ID, "CC", "1234567890")


class TestConsentimiento:
    async def test_sin_consentimiento(self) -> None:
        resultado = await HabeasDataCompliance.verify_consent(
            FakeSession(resultados=[None]),  # type: ignore[arg-type]
            CLIENT_ID,
            "CC",
            "1234567890",
        )

        assert resultado["has_consent"] is False
        assert "Ley 1581" in resultado["required_action"]

    async def test_con_consentimiento(self) -> None:
        resultado = await HabeasDataCompliance.verify_consent(
            FakeSession(resultados=[_consentimiento()]),  # type: ignore[arg-type]
            CLIENT_ID,
            "CC",
            "1234567890",
        )

        assert resultado == {
            "has_consent": True,
            "consent_date": "2025-01-10T00:00:00+00:00",
            "consent_type": "verbal",
        }

    async def test_la_consulta_es_del_tenant_y_excluye_revocados(self) -> None:
        sesion = FakeSession(resultados=[None])

        await HabeasDataCompliance.verify_consent(sesion, CLIENT_ID, "CC", "1234567890")  # type: ignore[arg-type]

        sql = str(sesion.executed[0]).lower()
        assert "patient_consents.client_id" in sql
        assert "revoked_at is null" in sql

    async def test_registrar_crea_la_autorizacion(self) -> None:
        sesion = FakeSession(resultados=[None])
        profesional = uuid.uuid4()

        resultado = await HabeasDataCompliance.register_consent(
            sesion,  # type: ignore[arg-type]
            CLIENT_ID,
            "cc",
            "1234567890",
            "verbal",
            registered_by_contact_id=profesional,
        )

        (consentimiento,) = sesion.agregados_de(PatientConsent)
        assert resultado["registered"] is True
        assert resultado["already_registered"] is False
        assert consentimiento.patient_document_type == "CC"
        assert consentimiento.registered_by_contact_id == profesional
        # Ni el documento ni el nombre se guardan en la tabla de consentimientos.
        assert "1234567890" not in consentimiento.patient_document_hash

    async def test_registrar_dos_veces_es_idempotente(self) -> None:
        """Un reintento del agente no duplica ni mueve la fecha real de la autorizacion."""
        sesion = FakeSession(resultados=[_consentimiento()])

        resultado = await HabeasDataCompliance.register_consent(
            sesion,  # type: ignore[arg-type]
            CLIENT_ID,
            "CC",
            "1234567890",
            "digital",
        )

        assert resultado["already_registered"] is True
        assert resultado["consent_type"] == "verbal"
        assert sesion.added == []

    async def test_tipo_de_consentimiento_invalido(self) -> None:
        with pytest.raises(HabeasDataError, match="consentimiento"):
            await HabeasDataCompliance.register_consent(
                FakeSession(),  # type: ignore[arg-type]
                CLIENT_ID,
                "CC",
                "1234567890",
                "telepatico",
            )

    async def test_categoria_invalida(self) -> None:
        with pytest.raises(HabeasDataError, match="Categoria"):
            await HabeasDataCompliance.register_consent(
                FakeSession(),  # type: ignore[arg-type]
                CLIENT_ID,
                "CC",
                "1234567890",
                "verbal",
                data_category="otra",
            )

    async def test_revocar_marca_la_fecha(self) -> None:
        vigente = _consentimiento()

        resultado = await HabeasDataCompliance.revoke_consent(
            FakeSession(resultados=[vigente]),  # type: ignore[arg-type]
            CLIENT_ID,
            "CC",
            "1234567890",
        )

        assert resultado["revoked"] is True
        assert vigente.revoked_at is not None

    async def test_revocar_sin_autorizacion_vigente(self) -> None:
        resultado = await HabeasDataCompliance.revoke_consent(
            FakeSession(resultados=[None]),  # type: ignore[arg-type]
            CLIENT_ID,
            "CC",
            "1234567890",
        )

        assert resultado == {"revoked": False, "revoked_at": None}


class TestExportacion:
    async def test_incluye_registros_y_autorizaciones(self) -> None:
        sesion = FakeSession(resultados=[[_registro("signed")], [_consentimiento()]])

        resultado = await HabeasDataCompliance.export_patient_data(
            sesion,  # type: ignore[arg-type]
            CLIENT_ID,
            "CC",
            "1234567890",
        )

        assert resultado["clinical_records"][0]["diagnosis_codes"][0]["code"] == "I10"
        assert resultado["consents"][0]["consent_type"] == "verbal"
        assert resultado["call_records"] == []
        assert "Ley 1581" in resultado["legal_basis"]

    async def test_no_devuelve_el_documento_ni_el_hash(self) -> None:
        sesion = FakeSession(resultados=[[_registro("draft")], []])

        resultado = await HabeasDataCompliance.export_patient_data(
            sesion,  # type: ignore[arg-type]
            CLIENT_ID,
            "CC",
            "1234567890",
        )

        assert "1234567890" not in str(resultado)

    async def test_la_consulta_va_acotada_al_tenant_y_al_paciente(self) -> None:
        sesion = FakeSession(resultados=[[], []])

        await HabeasDataCompliance.export_patient_data(sesion, CLIENT_ID, "CC", "1234567890")  # type: ignore[arg-type]

        sql = str(sesion.executed[0]).lower()
        assert "clinical_records.client_id" in sql
        assert "patient_document_hash" in sql


class TestAnonimizacion:
    async def test_anonimiza_borradores_y_revisados(self) -> None:
        borrador = _registro("draft")
        revisado = _registro("reviewed")
        hash_original = borrador.patient_document_hash

        resultado = await HabeasDataCompliance.anonymize_patient_data(
            FakeSession(resultados=[[borrador, revisado]]),  # type: ignore[arg-type]
            CLIENT_ID,
            "CC",
            "1234567890",
        )

        assert resultado["anonymized"] is True
        assert resultado["records_anonymized"] == 2
        assert resultado["records_retained"] == 0
        assert borrador.patient_document_number == ANONIMIZADO
        assert borrador.patient_name is None
        assert borrador.raw_transcription is None
        assert borrador.structured_notes == {}
        assert borrador.medical_entities == []
        assert borrador.contact_id is None
        assert borrador.anonymized_at is not None
        # El hash tambien: si no, el registro seguiria ligado al documento.
        assert borrador.patient_document_hash != hash_original
        assert borrador.patient_document_hash != revisado.patient_document_hash
        # Lo epidemiologico se conserva.
        assert borrador.diagnosis_codes[0]["code"] == "I10"

    @pytest.mark.parametrize("estado", ["signed", "submitted"])
    async def test_la_historia_clinica_firmada_se_retiene(self, estado: str) -> None:
        """Deber legal de conservacion: la supresion cede ante el (ver el modulo)."""
        firmado = _registro(estado)

        resultado = await HabeasDataCompliance.anonymize_patient_data(
            FakeSession(resultados=[[firmado]]),  # type: ignore[arg-type]
            CLIENT_ID,
            "CC",
            "1234567890",
        )

        assert resultado["anonymized"] is False
        assert resultado["records_retained"] == 1
        assert "conservacion" in resultado["note"]
        assert firmado.patient_document_number == "1234567890"
        assert firmado.structured_notes == {"subjective": "dolor"}
        assert firmado.anonymized_at is None

    async def test_mezcla_de_estados(self) -> None:
        borrador, firmado = _registro("draft"), _registro("signed")

        resultado = await HabeasDataCompliance.anonymize_patient_data(
            FakeSession(resultados=[[borrador, firmado]]),  # type: ignore[arg-type]
            CLIENT_ID,
            "CC",
            "1234567890",
        )

        assert (resultado["records_anonymized"], resultado["records_retained"]) == (1, 1)

    async def test_paciente_sin_registros(self) -> None:
        resultado = await HabeasDataCompliance.anonymize_patient_data(
            FakeSession(resultados=[[]]),  # type: ignore[arg-type]
            CLIENT_ID,
            "CC",
            "1234567890",
        )

        assert resultado["anonymized"] is False
        assert resultado["records_anonymized"] == 0
