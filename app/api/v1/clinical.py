"""API del modulo clinico: configuracion y derechos del titular (Sprint 13, Dev B).

PUT/GET /api/v1/clinical/settings           profesionales que operan el agente
POST    /api/v1/clinical/consents           registrar la autorizacion de un paciente
POST    /api/v1/clinical/consents/revoke    revocarla
POST    /api/v1/clinical/patients/export    derecho de acceso (Ley 1581, art. 8 y 14)
POST    /api/v1/clinical/patients/anonymize derecho de supresion (Ley 1581, art. 8, e)

Solo `super_admin` y `admin`: son datos de salud. El spec propone un rol
`medical` con una politica RLS propia, pero el enum `user_role` no lo tiene y
nada fija `app.current_user_role` (ver la migracion 016); un rol clinico
dedicado queda como decision de producto pendiente (ADR-071).

Los endpoints de consulta son POST, con el documento en el cuerpo: un GET lo
dejaria en la URL, y por tanto en los logs de acceso (ver `schemas/clinical.py`).

La configuracion se escribe con `UPDATE` + concatenacion JSONB sobre la clave
`clinical`, no con read-modify-write del `config` completo (misma leccion que
BUG-022 y `marketing_settings.py`).
"""

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import VALIDATION_ERROR, AppException
from app.core.habeas_data import HabeasDataCompliance, HabeasDataError
from app.models.agent_config import AgentConfig
from app.models.contact import Contact
from app.schemas.clinical import (
    ClinicalSettings,
    ClinicalSettingsUpdate,
    ConsentCreate,
    PatientRef,
)
from app.services.clinical import leer_config_clinica

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("super_admin", "admin")

#: Mezcla el parche dentro de `config.clinical` sin tocar el resto de `config`.
_ACTUALIZAR_CLINICA = text(
    """
    UPDATE agent_configs
    SET config = COALESCE(config, '{}'::jsonb) || jsonb_build_object(
        'clinical',
        COALESCE(config -> 'clinical', '{}'::jsonb) || CAST(:parche AS jsonb)
    )
    WHERE id = :id AND client_id = :client_id
    """
)


def _a_respuesta(clinica: dict[str, Any]) -> ClinicalSettings:
    """Normaliza lo guardado en el JSONB a la respuesta de la API.

    Lo que no tenga forma valida (escrito a mano en la base) se descarta en vez
    de romper el GET.

    Args:
        clinica: `agent_configs.config.clinical`.

    Returns:
        La configuracion vigente.
    """
    profesionales: list[UUID] = []
    crudos = clinica.get("professional_contact_ids")
    for valor in crudos if isinstance(crudos, list) else []:
        try:
            profesionales.append(UUID(str(valor)))
        except ValueError:
            continue
    return ClinicalSettings(professional_contact_ids=profesionales)


def _error_de_documento(exc: HabeasDataError) -> AppException:
    """Traduce un dato invalido de Habeas Data a un 400.

    Args:
        exc: Error de validacion.

    Returns:
        La excepcion HTTP para el cliente.
    """
    return AppException(status_code=400, error_code=VALIDATION_ERROR, message=str(exc))


@router.get("/settings", response_model=ClinicalSettings)
async def get_clinical_settings(
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> ClinicalSettings:
    """Devuelve los profesionales autorizados a operar el agente clinico.

    Args:
        user: Usuario autenticado.

    Returns:
        La configuracion vigente.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        return _a_respuesta(await leer_config_clinica(session, client_id))


@router.put("/settings", response_model=ClinicalSettings)
async def update_clinical_settings(
    data: ClinicalSettingsUpdate,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> ClinicalSettings:
    """Reemplaza la lista de profesionales autorizados.

    Args:
        data: Lista nueva; si no viene, queda como estaba.
        user: Usuario autenticado.

    Returns:
        La configuracion resultante.

    Raises:
        AppException: 400 si el tenant no tiene agente activo o si algun
            profesional no es un contacto vigente del tenant.
    """
    client_id: UUID = user["client_id"]
    parche: dict[str, Any] = {}
    if data.professional_contact_ids is not None:
        parche["professional_contact_ids"] = [str(c) for c in data.professional_contact_ids]

    async with tenant_session(client_id) as session:
        config_id = (
            await session.execute(
                select(AgentConfig.id)
                .where(AgentConfig.client_id == client_id, AgentConfig.is_active.is_(True))
                .order_by(AgentConfig.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if config_id is None:
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message="El tenant no tiene un agente activo que configurar",
            )

        if data.professional_contact_ids:
            # Un profesional tiene que ser un contacto vigente de ESTE tenant:
            # un id de otro tenant, uno fusionado o uno borrado por RGPD nunca
            # podria escribir, y dejarlo en la lista solo confunde.
            vigentes = set(
                (
                    await session.execute(
                        select(Contact.id).where(
                            Contact.client_id == client_id,
                            Contact.id.in_(data.professional_contact_ids),
                            Contact.merged_into_id.is_(None),
                            Contact.is_gdpr_deleted.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            desconocidos = [str(c) for c in data.professional_contact_ids if c not in vigentes]
            if desconocidos:
                raise AppException(
                    status_code=400,
                    error_code=VALIDATION_ERROR,
                    message=f"Contactos inexistentes en este tenant: {', '.join(desconocidos)}",
                )

        if parche:
            await session.execute(
                _ACTUALIZAR_CLINICA,
                {"parche": json.dumps(parche), "id": str(config_id), "client_id": str(client_id)},
            )
        respuesta = _a_respuesta(await leer_config_clinica(session, client_id))

    logger.info(
        "Configuracion clinica del tenant %s actualizada por %s (%s)",
        client_id,
        user.get("user_id"),
        ", ".join(sorted(parche)) or "sin cambios",
    )
    return respuesta


@router.post("/consents")
async def register_consent(
    data: ConsentCreate,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> dict[str, Any]:
    """Registra la autorizacion de un paciente para tratar sus datos de salud.

    Args:
        data: Documento del paciente y como se otorgo la autorizacion.
        user: Usuario autenticado.

    Returns:
        Resultado del registro; `already_registered` si ya habia una vigente.

    Raises:
        AppException: 400 si el documento no es valido.
    """
    client_id: UUID = user["client_id"]
    try:
        async with tenant_session(client_id) as session:
            return await HabeasDataCompliance.register_consent(
                session,
                client_id,
                data.document_type,
                data.document_number,
                data.consent_type,
                contact_id=data.contact_id,
                registered_by_user_id=user["user_id"],
            )
    except HabeasDataError as exc:
        raise _error_de_documento(exc) from exc


@router.post("/consents/revoke")
async def revoke_consent(
    data: PatientRef,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> dict[str, Any]:
    """Revoca la autorizacion vigente de un paciente.

    Args:
        data: Documento del paciente.
        user: Usuario autenticado.

    Returns:
        `{"revoked": bool, "revoked_at"}`.

    Raises:
        AppException: 400 si el documento no es valido.
    """
    client_id: UUID = user["client_id"]
    try:
        async with tenant_session(client_id) as session:
            return await HabeasDataCompliance.revoke_consent(
                session, client_id, data.document_type, data.document_number
            )
    except HabeasDataError as exc:
        raise _error_de_documento(exc) from exc


@router.post("/patients/export")
async def export_patient_data(
    data: PatientRef,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> dict[str, Any]:
    """Entrega todos los datos clinicos de un paciente (derecho de acceso).

    Args:
        data: Documento del paciente.
        user: Usuario autenticado.

    Returns:
        Registros clinicos, autorizaciones y llamadas del paciente.

    Raises:
        AppException: 400 si el documento no es valido.
    """
    client_id: UUID = user["client_id"]
    try:
        async with tenant_session(client_id) as session:
            exportacion = await HabeasDataCompliance.export_patient_data(
                session, client_id, data.document_type, data.document_number
            )
    except HabeasDataError as exc:
        raise _error_de_documento(exc) from exc

    # El acceso a datos de salud deja huella, sin el documento en el log.
    logger.info(
        "Habeas Data: exportacion de %d registros clinicos por %s (tenant %s)",
        len(exportacion["clinical_records"]),
        user.get("user_id"),
        client_id,
    )
    return exportacion


@router.post("/patients/anonymize")
async def anonymize_patient_data(
    data: PatientRef,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> dict[str, Any]:
    """Anonimiza los registros clinicos aun no firmados de un paciente.

    Los firmados o enviados se retienen (ver `app/core/habeas_data.py`).

    Args:
        data: Documento del paciente.
        user: Usuario autenticado.

    Returns:
        Cuantos registros se anonimizaron y cuantos se retuvieron.

    Raises:
        AppException: 400 si el documento no es valido.
    """
    client_id: UUID = user["client_id"]
    try:
        async with tenant_session(client_id) as session:
            resultado = await HabeasDataCompliance.anonymize_patient_data(
                session, client_id, data.document_type, data.document_number
            )
    except HabeasDataError as exc:
        raise _error_de_documento(exc) from exc

    logger.warning(
        "Habeas Data: supresion solicitada por %s (tenant %s): %d anonimizados, %d retenidos",
        user.get("user_id"),
        client_id,
        resultado["records_anonymized"],
        resultado["records_retained"],
    )
    return resultado
