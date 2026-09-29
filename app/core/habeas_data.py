"""Cumplimiento de Habeas Data (Ley 1581 de 2012, Colombia) para datos de salud.

Contrato: `specs/sprint-13-advanced-modules.md` §10. Todas las operaciones
reciben una `AsyncSession` con el contexto de tenant ya aplicado
(`tenant_session()`); el llamante es dueno de la transaccion.

Principios de la Ley 1581 (art. 4) que este modulo hace cumplir en codigo
—no en el prompt del agente—: el dato de salud es *sensible* (art. 5) y solo se
trata con autorizacion previa y expresa del titular (art. 6); el titular puede
conocerlo (art. 8, a-b y art. 14), revocar la autorizacion o pedir su
supresion (art. 8, e).

Desviaciones sobre el pseudocodigo del spec (ADR-071):

- **El titular se identifica por documento, no por `contact_id`.** Quien habla
  con el agente es el profesional; el paciente puede no ser un contacto.
  El documento se normaliza y se convierte en un indice ciego por tenant
  (`hash_paciente()`), igual que los identificadores de contacto.
- **El consentimiento vive en `patient_consents`**, no en
  `clinical_records.data_processing_authorized`: en el spec el registro clinico
  es lo unico que guarda el consentimiento, y ese registro no puede crearse sin
  consentimiento previo — la verificacion nunca podria pasar.
- **La supresion no alcanza a la historia clinica firmada.** El spec anonimiza
  todo registro del paciente. Pero un registro firmado o enviado ya es parte de
  la historia clinica, que la normativa colombiana obliga a conservar (Ley 23
  de 1981 y Resolucion 839 de 2017, 20 anos desde la ultima atencion): el
  derecho de supresion cede ante un deber legal de conservacion (Ley 1581,
  art. 15 y Decreto 1377 de 2013, art. 10). Solo se anonimizan los borradores y
  los revisados sin firmar; el resto se informa como retenido. **Esta lectura
  de la norma debe validarla el asesor legal del tenant** antes de produccion.
- **`anonymize_patient_data` no hace `commit()`**: lo hace `tenant_session()`
  al cerrar el contexto. Un commit aca partiria la transaccion del llamante.
"""

import logging
import re
from datetime import datetime, timezone
from importlib import import_module
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import blind_index
from app.models.clinical_record import (
    CONSENT_TYPES,
    DATA_CATEGORY_HEALTH,
    RECORD_RETAINED_STATUSES,
    ClinicalRecord,
    PatientConsent,
)

logger = logging.getLogger(__name__)

#: Tipos de documento de identidad admitidos en RIPS.
TIPOS_DOCUMENTO: tuple[str, ...] = ("CC", "TI", "CE", "PA", "RC", "MS", "AS", "NU")

#: Categorias de dato sensible (Ley 1581, art. 5).
SENSITIVE_DATA_CATEGORIES: tuple[str, ...] = (
    "health",
    "biometric",
    "sexual",
    "racial",
    "political",
    "religious",
    "union_membership",
)

ANONIMIZADO = "ANONIMIZADO"

_DOCUMENTO_PATTERN = re.compile(r"^[A-Z0-9]{4,20}$")


class HabeasDataError(ValueError):
    """Dato de entrada invalido para una operacion de Habeas Data."""


def normalizar_documento(tipo: str, numero: str) -> tuple[str, str]:
    """Normaliza el documento de un paciente.

    Args:
        tipo: Tipo de documento (`cc`, `CC`, ...).
        numero: Numero, con o sin puntos, guiones o espacios.

    Returns:
        `(tipo, numero)` en mayusculas y sin separadores.

    Raises:
        HabeasDataError: Si el tipo no es uno de `TIPOS_DOCUMENTO` o el numero
            no es alfanumerico de 4 a 20 caracteres.
    """
    tipo_limpio = (tipo or "").strip().upper()
    if tipo_limpio not in TIPOS_DOCUMENTO:
        raise HabeasDataError(f"Tipo de documento invalido. Validos: {list(TIPOS_DOCUMENTO)}")
    numero_limpio = re.sub(r"[\s.\-]", "", numero or "").upper()
    if not _DOCUMENTO_PATTERN.match(numero_limpio):
        raise HabeasDataError("El numero de documento debe ser alfanumerico de 4 a 20 caracteres")
    return tipo_limpio, numero_limpio


def hash_paciente(client_id: UUID | str, tipo: str, numero: str) -> str:
    """Indice ciego por tenant del documento de un paciente.

    Args:
        client_id: Tenant dueno del registro.
        tipo: Tipo de documento.
        numero: Numero de documento.

    Returns:
        HMAC-SHA256 en hexadecimal (64 caracteres).

    Raises:
        HabeasDataError: Si el documento no es valido.
    """
    tipo_limpio, numero_limpio = normalizar_documento(tipo, numero)
    # `blind_index()` solo devuelve None si el valor es None, y aca nunca lo es.
    return cast("str", blind_index(f"{tipo_limpio}:{numero_limpio}", client_id))


async def _consentimiento_vigente(
    session: AsyncSession, client_id: UUID, hash_documento: str, categoria: str
) -> PatientConsent | None:
    """Lee la autorizacion vigente (la mas reciente sin revocar) de un titular.

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant dueno.
        hash_documento: Indice ciego del documento.
        categoria: Categoria de dato sensible.

    Returns:
        La autorizacion, o `None` si no hay una vigente.
    """
    return (
        await session.execute(
            select(PatientConsent)
            .where(
                PatientConsent.client_id == client_id,
                PatientConsent.patient_document_hash == hash_documento,
                PatientConsent.data_category == categoria,
                PatientConsent.revoked_at.is_(None),
            )
            .order_by(PatientConsent.granted_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


class HabeasDataCompliance:
    """Verificaciones y derechos del titular sobre sus datos de salud."""

    @staticmethod
    async def verify_consent(
        session: AsyncSession,
        client_id: UUID,
        document_type: str,
        document_number: str,
        data_category: str = DATA_CATEGORY_HEALTH,
    ) -> dict[str, Any]:
        """Verifica que el titular autorizo el tratamiento de su dato sensible.

        Args:
            session: Sesion con el contexto de tenant aplicado.
            client_id: Tenant dueno.
            document_type: Tipo de documento del paciente.
            document_number: Numero de documento del paciente.
            data_category: Categoria de dato sensible.

        Returns:
            `{"has_consent": True, "consent_date", "consent_type"}` si hay una
            autorizacion vigente; si no, `{"has_consent": False,
            "required_action"}`.

        Raises:
            HabeasDataError: Si el documento no es valido.
        """
        hash_documento = hash_paciente(client_id, document_type, document_number)
        vigente = await _consentimiento_vigente(session, client_id, hash_documento, data_category)
        if vigente is not None:
            return {
                "has_consent": True,
                "consent_date": vigente.granted_at.isoformat(),
                "consent_type": vigente.consent_type,
            }
        return {
            "has_consent": False,
            "required_action": (
                "Se requiere autorizacion previa y expresa del titular para el tratamiento "
                f"de datos personales sensibles (categoria: {data_category}). "
                "Ley 1581 de 2012, art. 6."
            ),
        }

    @staticmethod
    async def register_consent(
        session: AsyncSession,
        client_id: UUID,
        document_type: str,
        document_number: str,
        consent_type: str,
        *,
        data_category: str = DATA_CATEGORY_HEALTH,
        contact_id: UUID | None = None,
        registered_by_contact_id: UUID | None = None,
        registered_by_user_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Registra la autorizacion del titular. Es idempotente.

        Si ya hay una vigente para la misma categoria no se crea otra: un
        reintento del agente no debe multiplicar las autorizaciones ni mover la
        fecha en que realmente se otorgo.

        Args:
            session: Sesion con el contexto de tenant aplicado.
            client_id: Tenant dueno.
            document_type: Tipo de documento del paciente.
            document_number: Numero de documento del paciente.
            consent_type: `verbal`, `digital` o `written`.
            data_category: Categoria de dato sensible.
            contact_id: El titular, si tambien es un contacto del tenant.
            registered_by_contact_id: Profesional que la registra desde el chat.
            registered_by_user_id: Usuario que la registra desde la API.

        Returns:
            `{"registered": True, "already_registered": bool, "timestamp",
            "consent_type"}`.

        Raises:
            HabeasDataError: Si el documento o el tipo de consentimiento no
                son validos, o la categoria no es una de las sensibles.
        """
        if consent_type not in CONSENT_TYPES:
            raise HabeasDataError(
                f"Tipo de consentimiento invalido. Validos: {list(CONSENT_TYPES)}"
            )
        if data_category not in SENSITIVE_DATA_CATEGORIES:
            raise HabeasDataError(f"Categoria invalida. Validas: {list(SENSITIVE_DATA_CATEGORIES)}")
        tipo, _ = normalizar_documento(document_type, document_number)
        hash_documento = hash_paciente(client_id, document_type, document_number)

        vigente = await _consentimiento_vigente(session, client_id, hash_documento, data_category)
        if vigente is not None:
            return {
                "registered": True,
                "already_registered": True,
                "timestamp": vigente.granted_at.isoformat(),
                "consent_type": vigente.consent_type,
            }

        ahora = datetime.now(timezone.utc)
        session.add(
            PatientConsent(
                client_id=client_id,
                patient_document_type=tipo,
                patient_document_hash=hash_documento,
                contact_id=contact_id,
                data_category=data_category,
                consent_type=consent_type,
                granted_at=ahora,
                registered_by_contact_id=registered_by_contact_id,
                registered_by_user_id=registered_by_user_id,
            )
        )
        await session.flush()
        # Sin documento ni nombre en el log: es justo el dato que se protege.
        logger.info(
            "Consentimiento Habeas Data registrado en el tenant %s (%s, %s)",
            client_id,
            consent_type,
            data_category,
        )
        return {
            "registered": True,
            "already_registered": False,
            "timestamp": ahora.isoformat(),
            "consent_type": consent_type,
        }

    @staticmethod
    async def revoke_consent(
        session: AsyncSession,
        client_id: UUID,
        document_type: str,
        document_number: str,
        data_category: str = DATA_CATEGORY_HEALTH,
    ) -> dict[str, Any]:
        """Revoca la autorizacion vigente del titular (Ley 1581, art. 8, e).

        Desde la revocacion el agente no crea registros nuevos para ese
        paciente. Los ya firmados se conservan por el deber de conservacion de
        la historia clinica (ver el docstring del modulo).

        Args:
            session: Sesion con el contexto de tenant aplicado.
            client_id: Tenant dueno.
            document_type: Tipo de documento del paciente.
            document_number: Numero de documento del paciente.
            data_category: Categoria de dato sensible.

        Returns:
            `{"revoked": bool, "revoked_at"}`; `revoked` es `False` si no habia
            una autorizacion vigente.

        Raises:
            HabeasDataError: Si el documento no es valido.
        """
        hash_documento = hash_paciente(client_id, document_type, document_number)
        vigente = await _consentimiento_vigente(session, client_id, hash_documento, data_category)
        if vigente is None:
            return {"revoked": False, "revoked_at": None}
        ahora = datetime.now(timezone.utc)
        vigente.revoked_at = ahora
        await session.flush()
        return {"revoked": True, "revoked_at": ahora.isoformat()}

    @staticmethod
    async def export_patient_data(
        session: AsyncSession, client_id: UUID, document_type: str, document_number: str
    ) -> dict[str, Any]:
        """Entrega al titular todos sus datos clinicos (derecho de acceso).

        Args:
            session: Sesion con el contexto de tenant aplicado.
            client_id: Tenant dueno.
            document_type: Tipo de documento del paciente.
            document_number: Numero de documento del paciente.

        Returns:
            Registros clinicos, autorizaciones y —si el canal de voz esta
            desplegado— llamadas del paciente.

        Raises:
            HabeasDataError: Si el documento no es valido.
        """
        tipo, _ = normalizar_documento(document_type, document_number)
        hash_documento = hash_paciente(client_id, document_type, document_number)

        registros = (
            (
                await session.execute(
                    select(ClinicalRecord)
                    .where(
                        ClinicalRecord.client_id == client_id,
                        ClinicalRecord.patient_document_hash == hash_documento,
                    )
                    .order_by(ClinicalRecord.service_date.desc(), ClinicalRecord.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        consentimientos = (
            (
                await session.execute(
                    select(PatientConsent)
                    .where(
                        PatientConsent.client_id == client_id,
                        PatientConsent.patient_document_hash == hash_documento,
                    )
                    .order_by(PatientConsent.granted_at.desc())
                )
            )
            .scalars()
            .all()
        )

        return {
            "patient_document_type": tipo,
            "export_date": datetime.now(timezone.utc).isoformat(),
            "clinical_records": [
                {
                    "id": str(r.id),
                    "service_date": r.service_date.isoformat(),
                    "service_type": r.service_type,
                    "rips_type": r.rips_type,
                    "specialty": r.specialty,
                    "diagnosis_codes": r.diagnosis_codes,
                    "procedure_codes": r.procedure_codes,
                    "structured_notes": r.structured_notes,
                    "status": r.status,
                    "anonymized": r.anonymized_at is not None,
                }
                for r in registros
            ],
            "consents": [
                {
                    "consent_type": c.consent_type,
                    "data_category": c.data_category,
                    "granted_at": c.granted_at.isoformat(),
                    "revoked_at": c.revoked_at.isoformat() if c.revoked_at else None,
                }
                for c in consentimientos
            ],
            "call_records": await _llamadas_del_paciente(session, client_id, registros),
            "legal_basis": "Ley 1581 de 2012, art. 8 y 14 — Derecho de acceso",
        }

    @staticmethod
    async def anonymize_patient_data(
        session: AsyncSession, client_id: UUID, document_type: str, document_number: str
    ) -> dict[str, Any]:
        """Anonimiza los registros clinicos aun no firmados de un paciente.

        Reemplaza documento, nombre, dictado, notas y entidades, y desliga el
        registro del contacto; conserva los codigos diagnosticos y de
        procedimiento (uso epidemiologico anonimo). Los registros firmados o
        enviados no se tocan (ver el docstring del modulo).

        Args:
            session: Sesion con el contexto de tenant aplicado.
            client_id: Tenant dueno.
            document_type: Tipo de documento del paciente.
            document_number: Numero de documento del paciente.

        Returns:
            Resumen: cuantos registros se anonimizaron y cuantos quedaron
            retenidos por estar firmados.

        Raises:
            HabeasDataError: Si el documento no es valido.
        """
        hash_documento = hash_paciente(client_id, document_type, document_number)
        registros = (
            (
                await session.execute(
                    select(ClinicalRecord).where(
                        ClinicalRecord.client_id == client_id,
                        ClinicalRecord.patient_document_hash == hash_documento,
                        ClinicalRecord.anonymized_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

        ahora = datetime.now(timezone.utc)
        anonimizados = 0
        retenidos = 0
        for registro in registros:
            if registro.status in RECORD_RETAINED_STATUSES:
                retenidos += 1
                continue
            registro.patient_document_number = ANONIMIZADO
            registro.patient_name = None
            registro.raw_transcription = None
            registro.structured_notes = {}
            registro.medical_entities = []
            registro.contact_id = None
            # El hash tambien: si no, seguiria ligando el registro al documento.
            registro.patient_document_hash = hash_paciente_anonimo(client_id, registro.id)
            registro.anonymized_at = ahora
            anonimizados += 1
        await session.flush()

        logger.warning(
            "Habeas Data: %d registros clinicos anonimizados y %d retenidos en el tenant %s",
            anonimizados,
            retenidos,
            client_id,
        )
        return {
            "anonymized": anonimizados > 0,
            "records_anonymized": anonimizados,
            "records_retained": retenidos,
            "legal_basis": "Ley 1581 de 2012, art. 8, literal e — Derecho de supresion",
            "note": (
                "Se conservan de forma anonima los codigos diagnosticos y de procedimiento. "
                "Los registros firmados o enviados forman parte de la historia clinica y se "
                "retienen por el deber legal de conservacion."
                if retenidos
                else "Se conservan de forma anonima los codigos diagnosticos y de procedimiento."
            ),
        }


def hash_paciente_anonimo(client_id: UUID | str, record_id: UUID) -> str:
    """Hash unico e irreversible para un registro anonimizado.

    Args:
        client_id: Tenant dueno.
        record_id: Registro anonimizado.

    Returns:
        Un indice ciego que ya no corresponde a ningun documento.
    """
    return cast("str", blind_index(f"anonimizado:{record_id}", client_id))


async def _llamadas_del_paciente(
    session: AsyncSession, client_id: UUID, registros: Any
) -> list[dict[str, Any]]:
    """Llamadas ligadas a los registros del paciente, si el canal de voz existe.

    `call_records` es de la mitad de Dev A del sprint. Mientras no este
    desplegado, la lista va vacia en vez de romper la exportacion.

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant dueno.
        registros: Registros clinicos del paciente.

    Returns:
        Las llamadas de origen de los registros, o una lista vacia.
    """
    try:
        modulo = import_module("app.models.call_record")
    except ImportError:
        return []
    ids = [r.call_record_id for r in registros if r.call_record_id is not None]
    if not ids:
        return []
    llamada = modulo.CallRecord
    filas = (
        (
            await session.execute(
                select(llamada).where(llamada.client_id == client_id, llamada.id.in_(ids))
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(f.id),
            "date": f.started_at.isoformat(),
            "duration": f.duration_seconds,
            "direction": f.direction,
        }
        for f in filas
    ]
