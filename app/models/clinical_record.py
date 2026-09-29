"""Modelos clinicos: `ClinicalRecord` (RIPS) y `PatientConsent` (Sprint 13, Dev B).

Contrato: `specs/sprint-13-advanced-modules.md` §8.2-8.3. Se aparta del
pseudocodigo en cinco puntos (ADR-071):

1. **El paciente no es el contacto de la conversacion.** El spec pone
   `contact_id NOT NULL` y verifica el consentimiento por contacto, pero quien
   escribe al agente es el *profesional* que dicta; el paciente se identifica
   por tipo y numero de documento. `contact_id` (el paciente, si tambien es un
   contacto) queda opcional y `dictated_by_contact_id` registra al profesional
   ("registra siempre quien dicto", §9.1).
2. **`patient_document_hash`**: el numero de documento va cifrado y, con un
   IV aleatorio, no se puede buscar por igualdad (misma razon que
   `contact_identifiers.identifier_hash`). El indice ciego por tenant permite
   ubicar el historial, deduplicar y ligar el consentimiento sin descifrar.
3. **El consentimiento tiene tabla propia (`patient_consents`).** El spec lo
   busca en `clinical_records.data_processing_authorized`, pero ese registro no
   puede existir antes del consentimiento: la verificacion nunca pasaria. Una
   tabla aparte permite ademas revocarlo (Ley 1581, art. 8) y probarlo.
4. **`call_record_id` sin FK.** `call_records` es de la mitad de Dev A del
   sprint (canal de voz) y no existe todavia; su migracion agrega la FK.
5. **`anonymized_at`** marca los registros anonimizados (derecho de supresion).
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString
from app.models.base import TenantBaseModel

RECORD_DRAFT = "draft"
RECORD_REVIEWED = "reviewed"
RECORD_SIGNED = "signed"
RECORD_SUBMITTED = "submitted"

RECORD_STATUSES: tuple[str, ...] = (
    RECORD_DRAFT,
    RECORD_REVIEWED,
    RECORD_SIGNED,
    RECORD_SUBMITTED,
)

#: Estados en los que el registro ya es parte de la historia clinica y no se
#: puede anonimizar ni modificar (deber de conservacion).
RECORD_RETAINED_STATUSES: tuple[str, ...] = (RECORD_SIGNED, RECORD_SUBMITTED)

CONSENT_TYPES: tuple[str, ...] = ("verbal", "digital", "written")

#: Categoria de dato sensible que cubre el consentimiento (Ley 1581, art. 5).
DATA_CATEGORY_HEALTH = "health"


class ClinicalRecord(TenantBaseModel):
    """Registro clinico RIPS en borrador, revisado, firmado o enviado.

    Attributes:
        contact_id: Paciente, si ademas es un contacto del tenant.
        dictated_by_contact_id: Profesional que dicto el registro.
        conversation_id: Conversacion en la que se dicto.
        call_record_id: Llamada de origen (Dev A); sin FK todavia.
        patient_document_type: CC, TI, CE, PA, RC, MS, AS o NU.
        patient_document_number: Documento del paciente, cifrado.
        patient_document_hash: Indice ciego por tenant del documento.
        patient_name: Nombre del paciente, cifrado.
        service_date: Fecha del servicio.
        service_type: consulta, procedimiento, urgencia u hospitalizacion.
        specialty: Especialidad medica.
        provider_code: Codigo del prestador.
        diagnosis_codes: `[{code, description, type, catalog_verified}]`.
        procedure_codes: `[{code, description, laterality?, catalog_verified}]`.
        diagnosis_type: confirmado, presuntivo o impresion.
        raw_transcription: Dictado crudo, cifrado.
        structured_notes: Notas SOAP.
        medical_entities: Entidades extraidas del dictado.
        rips_type: AC, AP, AU o AH.
        purpose_code: Finalidad (01-10).
        external_cause: Causa externa (01-15).
        discharge_status: Estado de salida.
        consent_given: Cuando se dio el consentimiento informado.
        consent_type: verbal, digital o written.
        data_processing_authorized: Cuando se autorizo el tratamiento de datos.
        status: Uno de `RECORD_STATUSES`.
        reviewed_by: Usuario que reviso.
        signed_at: Cuando se firmo.
        anonymized_at: Cuando se anonimizo, si se hizo.
        updated_at: Ultima modificacion.
    """

    __tablename__ = "clinical_records"
    __table_args__ = (
        Index("idx_clinical_records_client_date", "client_id", "service_date"),
        Index("idx_clinical_records_patient", "client_id", "patient_document_hash"),
        CheckConstraint(
            "status IN ('draft', 'reviewed', 'signed', 'submitted')",
            name="ck_clinical_records_status",
        ),
        CheckConstraint(
            "rips_type IS NULL OR rips_type IN ('AC', 'AP', 'AU', 'AH')",
            name="ck_clinical_records_rips_type",
        ),
    )

    contact_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=True
    )
    dictated_by_contact_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=True
    )
    conversation_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=True
    )
    call_record_id: Mapped[_UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    patient_document_type: Mapped[str | None] = mapped_column(String(5))
    patient_document_number: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    patient_document_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    patient_name: Mapped[str | None] = mapped_column(EncryptedString)

    service_date: Mapped[date] = mapped_column(Date, nullable=False)
    service_type: Mapped[str | None] = mapped_column(String(50))
    specialty: Mapped[str | None] = mapped_column(String(100))
    provider_code: Mapped[str | None] = mapped_column(String(20))

    diagnosis_codes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default="[]", nullable=False
    )
    procedure_codes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default="[]", nullable=False
    )
    diagnosis_type: Mapped[str | None] = mapped_column(String(20))

    raw_transcription: Mapped[str | None] = mapped_column(EncryptedString)
    structured_notes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default="{}", nullable=False
    )
    medical_entities: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default="[]", nullable=False
    )

    rips_type: Mapped[str | None] = mapped_column(String(5))
    purpose_code: Mapped[str | None] = mapped_column(String(5))
    external_cause: Mapped[str | None] = mapped_column(String(5))
    discharge_status: Mapped[str | None] = mapped_column(String(5))

    consent_given: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_type: Mapped[str | None] = mapped_column(String(50))
    data_processing_authorized: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String(20), server_default=RECORD_DRAFT, nullable=False)
    reviewed_by: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    anonymized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PatientConsent(TenantBaseModel):
    """Autorizacion del titular para tratar sus datos de salud (Ley 1581, art. 6).

    Una fila por autorizacion; la vigente es la mas reciente sin `revoked_at`.
    No guarda el documento en claro: solo el indice ciego y el tipo.

    Attributes:
        patient_document_type: Tipo de documento del titular.
        patient_document_hash: Indice ciego por tenant del documento.
        contact_id: Titular, si tambien es un contacto del tenant.
        data_category: Categoria de dato sensible autorizada.
        consent_type: verbal, digital o written.
        granted_at: Cuando se autorizo.
        revoked_at: Cuando se revoco, si se hizo.
        registered_by_contact_id: Profesional que registro la autorizacion
            desde el chat.
        registered_by_user_id: Usuario que la registro desde la API.
    """

    __tablename__ = "patient_consents"
    __table_args__ = (
        Index("idx_patient_consents_patient", "client_id", "patient_document_hash"),
        CheckConstraint(
            "consent_type IN ('verbal', 'digital', 'written')",
            name="ck_patient_consents_type",
        ),
    )

    patient_document_type: Mapped[str] = mapped_column(String(5), nullable=False)
    patient_document_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    contact_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=True
    )
    data_category: Mapped[str] = mapped_column(
        String(30), server_default=DATA_CATEGORY_HEALTH, nullable=False
    )
    consent_type: Mapped[str] = mapped_column(String(20), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registered_by_contact_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=True
    )
    registered_by_user_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
