"""Modelos clinicos: `ClinicalRecord` (RIPS) y `PatientConsent` (Sprint 13).

`ClinicalRecord` lo creo Dev A (migracion 016); Dev B lo extiende en la
migracion 017 (ADR-072): el profesional que dicta (`dictated_by_contact_id`),
`anonymized_at`, y `contact_id` pasa a opcional. `PatientConsent` es de Dev B.

Que va cifrado y por que
------------------------
Un registro clinico es dato sensible (Ley 1581 de 2012, art. 5) y CLAUDE.md
exige cifrar los datos medicos. Van cifrados con pgcrypto:

- la identidad del paciente (`patient_document_number`, `patient_name`);
- todo lo que describe su salud: la transcripcion del dictado, las notas SOAP,
  las entidades medicas extraidas **y los codigos de diagnostico y
  procedimiento**. El spec los deja en `JSONB`, pero un CIE-10 asociado a un
  paciente dice de que esta enfermo; en claro quedaria en disco, en los backups
  y en `audit_logs` (el trigger guarda `to_jsonb(NEW)`).

Quedan en claro los campos administrativos del RIPS (tipo de registro,
finalidad, causa externa, fechas, estado): no identifican a nadie y hacen
falta para filtrar y generar los reportes.

Buscar un paciente por documento
--------------------------------
El spec indexa `patient_document_number`, pero sobre una columna cifrada con IV
aleatorio ese indice no encuentra nada. Se agrega `patient_document_hash`, el
indice ciego del tipo y numero de documento (`blind_index()`), que es por
tenant como los de `contact_identifiers`.

Acceso por rol
--------------
El spec propone una segunda politica RLS que exige un rol medico leyendo
`app.current_user_role`. No se incluye: las politicas permisivas de PostgreSQL
se combinan con OR, asi que junto a `tenant_isolation` no restringiria nada, y
esa variable no la fija nadie (`tenant_session()` solo fija el tenant, y los
workers de Celery no tienen usuario). El control por rol queda en la API y en
el nodo del agente clinico (Dev B).
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedJSON, EncryptedString
from app.models.base import TenantBaseModel

#: Estados de un registro clinico.
CLINICAL_STATUSES: tuple[str, ...] = ("draft", "reviewed", "signed", "submitted")

#: Tipos de archivo RIPS: consulta, procedimiento, urgencia, hospitalizacion.
RIPS_TYPES: tuple[str, ...] = ("AC", "AP", "AU", "AH")

RECORD_DRAFT = "draft"
RECORD_REVIEWED = "reviewed"
RECORD_SIGNED = "signed"
RECORD_SUBMITTED = "submitted"

#: Alias de `CLINICAL_STATUSES` con el nombre que usa el servicio clinico.
RECORD_STATUSES: tuple[str, ...] = CLINICAL_STATUSES

#: Estados en los que el registro ya es parte de la historia clinica y no se
#: puede anonimizar ni modificar (deber de conservacion).
RECORD_RETAINED_STATUSES: tuple[str, ...] = (RECORD_SIGNED, RECORD_SUBMITTED)

#: Anos que se conserva la historia clinica desde la ultima atencion del
#: paciente: 5 en el archivo de gestion y 15 en el archivo central (Resolucion
#: 839 de 2017 del Ministerio de Salud, que reemplaza a la 1995 de 1999).
RETENTION_YEARS = 20

#: Transiciones validas: cada estado solo puede pasar al siguiente.
RECORD_TRANSITIONS: dict[str, str] = {
    RECORD_DRAFT: RECORD_REVIEWED,
    RECORD_REVIEWED: RECORD_SIGNED,
    RECORD_SIGNED: RECORD_SUBMITTED,
}

CONSENT_TYPES: tuple[str, ...] = ("verbal", "digital", "written")

#: Categoria de dato sensible que cubre el consentimiento (Ley 1581, art. 5).
DATA_CATEGORY_HEALTH = "health"


def fin_de_retencion(ultima_atencion: date) -> date:
    """Fecha hasta la que hay que conservar la historia clinica de un paciente.

    Args:
        ultima_atencion: Fecha de la ultima atencion registrada del paciente.

    Returns:
        `ultima_atencion` mas `RETENTION_YEARS` anos (el 29 de febrero cae el 28).
    """
    try:
        return ultima_atencion.replace(year=ultima_atencion.year + RETENTION_YEARS)
    except ValueError:
        return ultima_atencion.replace(year=ultima_atencion.year + RETENTION_YEARS, day=28)


class ClinicalRecord(TenantBaseModel):
    """Registro individual de prestacion de un servicio de salud.

    La retencion de 20 anos la garantiza el trigger
    `clinical_records_protect_trigger` (migracion 017): un registro firmado no
    se borra ni se modifica mientras corra, salvo `signed -> submitted`.

    Attributes:
        contact_id: Paciente, si ademas es un contacto del tenant. Opcional
            desde la migracion 017: quien escribe al agente es el profesional y
            el paciente se identifica por su documento.
        dictated_by_contact_id: Profesional que dicto el registro (017).
        anonymized_at: Cuando se anonimizo, si se hizo (017).
        conversation_id: Conversacion en la que se dicto, si la hay.
        call_record_id: Llamada en la que se dicto, si fue por voz.
        patient_document_type: Tipo de documento (CC, TI, CE, PA, RC...).
        patient_document_number: Numero de documento, cifrado.
        patient_document_hash: Indice ciego de tipo + numero, para buscar.
        patient_name: Nombre del paciente, cifrado.
        service_date: Fecha de la atencion.
        service_type: consulta, procedimiento, urgencia u hospitalizacion.
        specialty: Especialidad medica.
        provider_code: Codigo de habilitacion del prestador.
        diagnosis_codes: CIE-10, cifrado: `[{code, description, type}]`.
        procedure_codes: CUPS, cifrado: `[{code, description, laterality}]`.
        diagnosis_type: confirmado, presuntivo o impresion diagnostica.
        raw_transcription: Transcripcion del dictado, cifrada.
        structured_notes: Notas SOAP, cifradas.
        medical_entities: Entidades medicas extraidas, cifradas.
        rips_type: Uno de `RIPS_TYPES`.
        purpose_code: Finalidad de la consulta.
        external_cause: Causa externa.
        discharge_status: Estado a la salida.
        consent_given: Cuando dio el paciente el consentimiento informado.
        consent_type: verbal, digital o escrito.
        data_processing_authorized: Cuando autorizo el tratamiento de datos.
        status: Uno de `CLINICAL_STATUSES`.
        reviewed_by: Usuario (profesional) que lo reviso.
        signed_at: Cuando se firmo.
        updated_at: Ultima modificacion.
    """

    __tablename__ = "clinical_records"
    __table_args__ = (
        Index("idx_clinical_records_client_date", "client_id", "service_date"),
        Index("idx_clinical_records_patient", "client_id", "patient_document_hash"),
        Index("idx_clinical_records_contact", "client_id", "contact_id"),
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
    call_record_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("call_records.id"), nullable=True
    )

    patient_document_type: Mapped[str | None] = mapped_column(String(5), nullable=True)
    patient_document_number: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    patient_document_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    patient_name: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    service_date: Mapped[date] = mapped_column(Date, nullable=False)
    service_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    specialty: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_code: Mapped[str | None] = mapped_column(String(20), nullable=True)

    diagnosis_codes: Mapped[list[dict[str, Any]] | None] = mapped_column(
        EncryptedJSON, nullable=True
    )
    procedure_codes: Mapped[list[dict[str, Any]] | None] = mapped_column(
        EncryptedJSON, nullable=True
    )
    diagnosis_type: Mapped[str | None] = mapped_column(String(20), nullable=True)

    raw_transcription: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    structured_notes: Mapped[dict[str, Any] | None] = mapped_column(EncryptedJSON, nullable=True)
    medical_entities: Mapped[list[dict[str, Any]] | None] = mapped_column(
        EncryptedJSON, nullable=True
    )

    rips_type: Mapped[str | None] = mapped_column(String(5), nullable=True)
    purpose_code: Mapped[str | None] = mapped_column(String(5), nullable=True)
    external_cause: Mapped[str | None] = mapped_column(String(5), nullable=True)
    discharge_status: Mapped[str | None] = mapped_column(String(5), nullable=True)

    consent_given: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    data_processing_authorized: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    status: Mapped[str] = mapped_column(String(20), server_default="draft", nullable=False)
    reviewed_by: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    anonymized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
