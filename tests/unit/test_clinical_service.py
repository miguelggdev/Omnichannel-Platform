"""Tests del catalogo y de las reglas RIPS del servicio clinico (Sprint 13, Dev B)."""

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only-minimum-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters-long")

from app.models.clinical_record import RECORD_DRAFT, ClinicalRecord
from app.services import clinical as svc
from app.services.clinical import ClinicalValidationError, validar_registro_rips
from app.services.clinical_catalog import buscar_cie10, buscar_cups, normalizar_texto
from tests.unit.agent_doubles import FakeSession

CLIENT_ID = uuid.uuid4()
PROFESIONAL = uuid.uuid4()


def _ok(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "rips_type": "AC",
        "service_type": "consulta",
        "service_date": "2025-01-15",
        "diagnosis_codes": [{"code": "I10", "description": "HTA", "type": "principal"}],
        "procedure_codes": None,
        "purpose_code": "05",
        "external_cause": None,
        "diagnosis_type": None,
        "notes": None,
    }
    base.update(over)
    return base


class TestCatalogo:
    def test_sin_tildes_ni_mayusculas(self) -> None:
        """El spec comparaba `lower()` a secas: 'Hipertensión' no encontraba nada."""
        assert buscar_cie10("Hipertensión esencial")[0]["code"] == "I10"
        assert buscar_cie10("hipertension ESENCIAL")[0]["code"] == "I10"

    def test_codigo_exacto_en_cualquier_formato(self) -> None:
        assert buscar_cie10(" j06.9 ")[0]["code"] == "J06.9"
        assert buscar_cups("890201")[0]["description"].startswith("Consulta de primera vez")

    def test_todas_las_palabras_tienen_que_estar(self) -> None:
        assert buscar_cie10("infeccion urinarias")[0]["code"] == "N39.0"
        assert buscar_cie10("hipertension migrana") == []

    def test_sin_coincidencia_no_inventa(self) -> None:
        """Criterio del ADR-072: nada de caer a un LLM para 'encontrar' un codigo."""
        assert buscar_cie10("sindrome inexistente xyz") == []
        assert buscar_cups("cirugia rara") == []

    def test_consulta_vacia(self) -> None:
        assert buscar_cie10("   ") == []

    def test_normalizar_texto(self) -> None:
        assert normalizar_texto("  Hospitalización ") == "hospitalizacion"


class TestValidacionRips:
    def test_consulta_valida(self) -> None:
        datos = validar_registro_rips(**_ok())

        assert datos["rips_type"] == "AC"
        assert datos["service_date"] == date(2025, 1, 15)
        assert datos["purpose_code"] == "05"
        assert datos["diagnosis_codes"][0]["catalog_verified"] is True

    def test_el_codigo_dictado_se_normaliza(self) -> None:
        datos = validar_registro_rips(
            **_ok(diagnosis_codes=[{"code": " j06.9 ", "type": "principal"}])
        )

        assert datos["diagnosis_codes"][0]["code"] == "J06.9"
        # La descripcion sale del catalogo si el profesional no la dicto.
        assert datos["diagnosis_codes"][0]["description"].startswith("Infeccion aguda")

    def test_un_codigo_fuera_del_catalogo_con_formato_valido_se_acepta_sin_verificar(self) -> None:
        datos = validar_registro_rips(
            **_ok(
                diagnosis_codes=[{"code": "S72.00", "description": "Fractura", "type": "principal"}]
            )
        )

        assert datos["diagnosis_codes"][0]["catalog_verified"] is False

    @pytest.mark.parametrize("rips", ["", "XX", "am"])
    def test_tipo_rips_invalido(self, rips: str) -> None:
        with pytest.raises(ClinicalValidationError, match="Tipo RIPS"):
            validar_registro_rips(**_ok(rips_type=rips))

    def test_tipo_rips_en_minusculas_se_acepta(self) -> None:
        assert validar_registro_rips(**_ok(rips_type="ac"))["rips_type"] == "AC"

    def test_el_servicio_tiene_que_corresponder_al_tipo_rips(self) -> None:
        with pytest.raises(ClinicalValidationError, match="corresponde a 'consulta'"):
            validar_registro_rips(**_ok(service_type="urgencia"))

    def test_hospitalizacion_con_tilde(self) -> None:
        datos = validar_registro_rips(
            **_ok(rips_type="AH", service_type="hospitalización", external_cause="13")
        )
        assert datos["service_type"] == "hospitalizacion"

    def test_fecha_futura(self) -> None:
        manana = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
        with pytest.raises(ClinicalValidationError, match="futura"):
            validar_registro_rips(**_ok(service_date=manana))

    @pytest.mark.parametrize("fecha", ["15/01/2025", "ayer", ""])
    def test_fecha_con_formato_invalido(self, fecha: str) -> None:
        with pytest.raises(ClinicalValidationError, match="YYYY-MM-DD"):
            validar_registro_rips(**_ok(service_date=fecha))

    def test_sin_diagnostico_principal(self) -> None:
        with pytest.raises(ClinicalValidationError, match="exactamente un diagnostico principal"):
            validar_registro_rips(**_ok(diagnosis_codes=[{"code": "J06.9", "type": "relacionado"}]))

    def test_dos_diagnosticos_principales(self) -> None:
        with pytest.raises(ClinicalValidationError, match="exactamente un diagnostico principal"):
            validar_registro_rips(
                **_ok(
                    diagnosis_codes=[
                        {"code": "J06.9", "type": "principal"},
                        {"code": "I10", "type": "principal"},
                    ]
                )
            )

    def test_mas_de_tres_relacionados(self) -> None:
        relacionados = [
            {"code": c, "type": "relacionado"} for c in ("J06.9", "A09", "R51", "R50.9")
        ]
        with pytest.raises(ClinicalValidationError, match="3 diagnosticos relacionados"):
            validar_registro_rips(
                **_ok(diagnosis_codes=[{"code": "I10", "type": "principal"}, *relacionados])
            )

    def test_tres_relacionados_es_el_tope_valido(self) -> None:
        relacionados = [{"code": c, "type": "relacionado"} for c in ("J06.9", "A09", "R51")]
        datos = validar_registro_rips(
            **_ok(diagnosis_codes=[{"code": "I10", "type": "principal"}, *relacionados])
        )
        assert len(datos["diagnosis_codes"]) == 4

    @pytest.mark.parametrize("codigo", ["", "6", "JJ06", "J06.999", "ABC", "12345"])
    def test_cie10_con_formato_invalido(self, codigo: str) -> None:
        with pytest.raises(ClinicalValidationError, match="CIE-10 invalido"):
            validar_registro_rips(**_ok(diagnosis_codes=[{"code": codigo, "type": "principal"}]))

    def test_diagnostico_repetido(self) -> None:
        with pytest.raises(ClinicalValidationError, match="repetido"):
            validar_registro_rips(
                **_ok(
                    diagnosis_codes=[
                        {"code": "I10", "type": "principal"},
                        {"code": "i10", "type": "relacionado"},
                    ]
                )
            )

    def test_tipo_de_diagnostico_desconocido(self) -> None:
        with pytest.raises(ClinicalValidationError, match="principal' o 'relacionado"):
            validar_registro_rips(**_ok(diagnosis_codes=[{"code": "I10", "type": "otro"}]))

    def test_ap_exige_procedimiento(self) -> None:
        with pytest.raises(ClinicalValidationError, match="al menos un procedimiento"):
            validar_registro_rips(**_ok(rips_type="AP", service_type="procedimiento"))

    def test_ap_con_procedimiento(self) -> None:
        datos = validar_registro_rips(
            **_ok(
                rips_type="AP",
                service_type="procedimiento",
                procedure_codes=[{"code": "890201"}],
            )
        )
        assert datos["procedure_codes"][0]["catalog_verified"] is True

    @pytest.mark.parametrize("codigo", ["8902", "89020A", "8902011", ""])
    def test_cups_con_formato_invalido(self, codigo: str) -> None:
        with pytest.raises(ClinicalValidationError, match="CUPS invalido"):
            validar_registro_rips(**_ok(procedure_codes=[{"code": codigo}]))

    @pytest.mark.parametrize("finalidad", ["", "0", "00", "11", "1", "abc"])
    def test_finalidad_fuera_de_01_a_10(self, finalidad: str) -> None:
        with pytest.raises(ClinicalValidationError, match="finalidad"):
            validar_registro_rips(**_ok(purpose_code=finalidad))

    def test_urgencias_exige_causa_externa(self) -> None:
        with pytest.raises(ClinicalValidationError, match="causa externa"):
            validar_registro_rips(**_ok(rips_type="AU", service_type="urgencia"))

    def test_causa_externa_fuera_de_rango(self) -> None:
        with pytest.raises(ClinicalValidationError, match="causa externa"):
            validar_registro_rips(**_ok(external_cause="99"))

    def test_tipo_de_diagnostico_del_registro(self) -> None:
        assert validar_registro_rips(**_ok(diagnosis_type="Impresión"))["diagnosis_type"] == (
            "impresion"
        )
        with pytest.raises(ClinicalValidationError, match="Tipo de diagnostico"):
            validar_registro_rips(**_ok(diagnosis_type="quizas"))

    def test_notas_soap(self) -> None:
        datos = validar_registro_rips(**_ok(notes={"subjective": " dolor ", "plan": ""}))
        assert datos["structured_notes"] == {"subjective": "dolor"}

    def test_seccion_soap_desconocida(self) -> None:
        with pytest.raises(ClinicalValidationError, match="Seccion SOAP desconocida"):
            validar_registro_rips(**_ok(notes={"otra": "x"}))


def _consentimiento() -> SimpleNamespace:
    return SimpleNamespace(
        granted_at=datetime(2025, 1, 10, tzinfo=timezone.utc), consent_type="verbal"
    )


class TestCrearRegistro:
    async def _crear(self, sesion: FakeSession, conversation_id: uuid.UUID | None = None) -> Any:
        return await svc.crear_registro_rips(
            sesion,  # type: ignore[arg-type]
            client_id=CLIENT_ID,
            dictated_by_contact_id=PROFESIONAL,
            conversation_id=conversation_id,
            document_type="cc",
            document_number="1.234.567.890",
            patient_name="  Ana Perez ",
            specialty=None,
            datos=validar_registro_rips(**_ok()),
        )

    async def test_sin_consentimiento_no_se_guarda_nada(self) -> None:
        """Criterio 11: el spec lo dejaba solo en el prompt."""
        sesion = FakeSession(resultados=[None, None])

        resultado = await self._crear(sesion)

        assert resultado["success"] is False
        assert resultado["error"] == "consent_required"
        assert "Ley 1581" in resultado["message"]
        assert sesion.added == []

    async def test_con_consentimiento_crea_un_borrador(self) -> None:
        # consentimiento, advisory lock, busqueda de duplicado
        sesion = FakeSession(resultados=[None, _consentimiento(), None, None])

        resultado = await self._crear(sesion, conversation_id=uuid.uuid4())

        (registro,) = sesion.agregados_de(ClinicalRecord)
        assert resultado["success"] is True
        assert resultado["duplicate"] is False
        assert registro.status == RECORD_DRAFT
        assert registro.dictated_by_contact_id == PROFESIONAL
        assert registro.patient_document_number == "1234567890"
        assert registro.patient_name == "Ana Perez"
        assert len(registro.patient_document_hash) == 64
        assert registro.consent_type == "verbal"
        # El documento completo no vuelve al LLM.
        assert "1234567890" not in str(resultado)
        assert resultado["patient"].endswith("7890")

    async def test_un_reintento_no_duplica_el_borrador(self) -> None:
        previo = SimpleNamespace(
            id=uuid.uuid4(),
            status=RECORD_DRAFT,
            patient_document_type="CC",
            service_date=date(2025, 1, 15),
            rips_type="AC",
            diagnosis_codes=[{"code": "I10", "type": "principal"}],
            procedure_codes=[],
        )
        sesion = FakeSession(resultados=[None, _consentimiento(), None, previo])

        resultado = await self._crear(sesion, conversation_id=uuid.uuid4())

        assert resultado["duplicate"] is True
        assert resultado["record_id"] == str(previo.id)
        assert sesion.added == []

    async def test_el_lock_es_por_paciente(self) -> None:
        sesion = FakeSession(resultados=[None, _consentimiento(), None, None])

        await self._crear(sesion, conversation_id=uuid.uuid4())

        assert "pg_advisory_xact_lock" in str(sesion.executed[2])


class TestAutorizacion:
    async def test_sin_lista_nadie_opera(self) -> None:
        assert (
            await svc.es_profesional_clinico(FakeSession(resultados=[None]), CLIENT_ID, PROFESIONAL)
            is False
        )  # type: ignore[arg-type]

    async def test_solo_los_declarados(self) -> None:
        config = SimpleNamespace(
            config={"clinical": {"professional_contact_ids": [str(PROFESIONAL)]}}
        )

        assert (
            await svc.es_profesional_clinico(
                FakeSession(resultados=[config]), CLIENT_ID, PROFESIONAL
            )
            is True
        )  # type: ignore[arg-type]
        assert (
            await svc.es_profesional_clinico(
                FakeSession(resultados=[config]), CLIENT_ID, uuid.uuid4()
            )
            is False
        )  # type: ignore[arg-type]

    async def test_sin_contacto_no_hay_acceso(self) -> None:
        assert await svc.es_profesional_clinico(FakeSession(), CLIENT_ID, None) is False  # type: ignore[arg-type]

    async def test_lista_con_forma_invalida(self) -> None:
        config = SimpleNamespace(config={"clinical": {"professional_contact_ids": "todos"}})

        assert (
            await svc.es_profesional_clinico(
                FakeSession(resultados=[config]), CLIENT_ID, PROFESIONAL
            )
            is False
        )  # type: ignore[arg-type]

    async def test_la_config_no_es_la_de_marketing(self) -> None:
        """Ser operador de marketing no da acceso a datos de salud."""
        config = SimpleNamespace(config={"marketing": {"operator_contact_ids": [str(PROFESIONAL)]}})

        assert (
            await svc.es_profesional_clinico(
                FakeSession(resultados=[config]), CLIENT_ID, PROFESIONAL
            )
            is False
        )  # type: ignore[arg-type]
