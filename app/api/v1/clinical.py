"""API del modulo clinico: configuracion y derechos del titular (Sprint 13, Dev B).

PUT/GET /api/v1/clinical/settings           profesionales que operan el agente
POST    /api/v1/clinical/consents           registrar la autorizacion de un paciente
POST    /api/v1/clinical/consents/revoke    revocarla
POST    /api/v1/clinical/patients/export    derecho de acceso (Ley 1581, art. 8 y 14)
POST    /api/v1/clinical/patients/anonymize derecho de supresion (Ley 1581, art. 8, e)
GET     /api/v1/clinical/records            registros para revision (sin notas ni documento)
GET     /api/v1/clinical/records/{id}       detalle de un registro, documento enmascarado
POST    /api/v1/clinical/records/{id}/review|sign|submit   draft -> reviewed -> signed -> submitted

Un registro firmado no se modifica ni se borra durante 20 anos desde la ultima
atencion del paciente (Resolucion 839 de 2017); lo garantiza el trigger de la
migracion 017, no solo esta API.

Control de acceso (criterio 14 del spec, ADR-073)
-------------------------------------------------
Dos grupos de roles, porque no son el mismo tipo de acto:

- **`super_admin`, `admin`** — actos del responsable del tratamiento: decidir
  quien puede dictar (`/settings`) y atender los derechos del titular
  (`/patients/export`, `/patients/anonymize`, `/consents/revoke`).
- **`super_admin`, `admin`, `medical`** — actos clinicos: leer la historia
  (`/records`), firmarla (`review`/`sign`/`submit`) y dejar constancia de la
  autorizacion que el paciente otorgo en la consulta (`/consents`).

El rol `medical` lo agrega la migracion 020 al enum `user_role`. Antes, firmar
un registro clinico exigia ser administrador del tenant, que es la decision de
producto que ADR-072 dejo abierta. `supervisor` y `agent` siguen fuera de todo
el router: un dato de salud es categoria especial (Ley 1581, art. 5) y no forma
parte de la atencion al cliente.

Sigue sin crearse la politica RLS `clinical_records_medical_access` del spec:
las politicas permisivas de PostgreSQL se combinan con OR con la de aislamiento
por tenant, asi que no restringiria nada, y nada fija `app.current_user_role`
(ADR-071, ADR-072). El control es de aplicacion, y estos endpoints son el unico
camino.

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

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import NOT_FOUND, VALIDATION_ERROR, AppException
from app.core.habeas_data import HabeasDataCompliance, HabeasDataError
from app.models.agent_config import AgentConfig
from app.models.clinical_record import RECORD_STATUSES
from app.models.contact import Contact
from app.schemas.clinical import (
    ClinicalSettings,
    ClinicalSettingsUpdate,
    ConsentCreate,
    PatientRef,
)
from app.services.clinical import (
    ClinicalValidationError,
    RegistroNoEncontradoError,
    TransicionInvalidaError,
    avanzar_registro,
    leer_config_clinica,
    listar_registros,
    obtener_registro,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: No hay un codigo de error estandar para un conflicto de estado.
CONFLICT = "CONFLICT"

#: Actos del responsable del tratamiento: decidir quien puede dictar, y atender
#: los derechos del titular sobre sus datos (exportacion, supresion, revocacion
#: de la autorizacion). No son actos clinicos, asi que un `medical` no los hace.
_ROLES = ("super_admin", "admin")

#: Actos clinicos: leer la historia, firmarla y dejar constancia de la
#: autorizacion que el paciente otorgo en la consulta. El rol `medical`
#: (migracion 020) existe justamente para esto: hasta ahora firmar un registro
#: clinico exigia ser administrador del tenant, que es la decision de producto
#: que ADR-072 dejo abierta y el criterio 14 del spec pedia cerrar.
_ROLES_CLINICOS = ("super_admin", "admin", "medical")

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
    user: dict[str, Any] = Depends(require_role(*_ROLES_CLINICOS)),
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


@router.get("/records")
async def list_records(
    status: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: dict[str, Any] = Depends(require_role(*_ROLES_CLINICOS)),
) -> list[dict[str, Any]]:
    """Lista los registros clinicos del tenant para su revision.

    Args:
        status: Filtra por estado (`draft`, `reviewed`, `signed`, `submitted`).
        limit: Maximo de filas.
        offset: Filas a saltar.
        user: Usuario autenticado.

    Returns:
        Resumen por registro, sin notas ni documento.

    Raises:
        AppException: 400 si el estado no existe.
    """
    client_id: UUID = user["client_id"]
    if status is not None and status not in RECORD_STATUSES:
        # Antes de abrir la sesion: un filtro invalido no gasta una conexion.
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"Estado invalido. Validos: {list(RECORD_STATUSES)}",
        )
    try:
        async with tenant_session(client_id) as session:
            return await listar_registros(
                session, client_id=client_id, estado=status, limite=limit, desplazamiento=offset
            )
    except ClinicalValidationError as exc:
        raise AppException(status_code=400, error_code=VALIDATION_ERROR, message=str(exc)) from exc


@router.get("/records/{record_id}")
async def get_record(
    record_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_ROLES_CLINICOS)),
) -> dict[str, Any]:
    """Devuelve el detalle de un registro clinico para revisarlo.

    Args:
        record_id: Registro pedido.
        user: Usuario autenticado.

    Returns:
        Detalle con el documento enmascarado y la fecha hasta la que se conserva.

    Raises:
        AppException: 404 si no existe en este tenant.
    """
    client_id: UUID = user["client_id"]
    try:
        async with tenant_session(client_id) as session:
            detalle = await obtener_registro(session, client_id=client_id, record_id=record_id)
    except RegistroNoEncontradoError as exc:
        raise AppException(
            status_code=404, error_code=NOT_FOUND, message="Registro clinico no encontrado"
        ) from exc
    logger.info(
        "Registro clinico %s consultado por %s (tenant %s)",
        record_id,
        user.get("user_id"),
        client_id,
    )
    return detalle


async def _avanzar(record_id: UUID, destino: str, user: dict[str, Any]) -> dict[str, Any]:
    """Avanza un registro un estado y traduce los errores a HTTP.

    Args:
        record_id: Registro a avanzar.
        destino: Estado al que se avanza.
        user: Usuario autenticado.

    Returns:
        El detalle del registro ya avanzado.

    Raises:
        AppException: 404 si no existe; 409 si no esta en el estado previo.
    """
    client_id: UUID = user["client_id"]
    try:
        async with tenant_session(client_id) as session:
            detalle = await avanzar_registro(
                session,
                client_id=client_id,
                record_id=record_id,
                destino=destino,
                user_id=user["user_id"],
            )
    except RegistroNoEncontradoError as exc:
        raise AppException(
            status_code=404, error_code=NOT_FOUND, message="Registro clinico no encontrado"
        ) from exc
    except TransicionInvalidaError as exc:
        raise AppException(status_code=409, error_code=CONFLICT, message=str(exc)) from exc
    logger.info(
        "Registro clinico %s -> %s por %s (tenant %s)",
        record_id,
        destino,
        user["user_id"],
        client_id,
    )
    return detalle


@router.post("/records/{record_id}/review")
async def review_record(
    record_id: UUID, user: dict[str, Any] = Depends(require_role(*_ROLES_CLINICOS))
) -> dict[str, Any]:
    """Marca un borrador como revisado; el usuario queda como revisor.

    Args:
        record_id: Registro a revisar.
        user: Usuario autenticado.

    Returns:
        El registro revisado.
    """
    return await _avanzar(record_id, "reviewed", user)


@router.post("/records/{record_id}/sign")
async def sign_record(
    record_id: UUID, user: dict[str, Any] = Depends(require_role(*_ROLES_CLINICOS))
) -> dict[str, Any]:
    """Firma un registro revisado. Desde aqui no se modifica ni se borra durante 20 anos.

    Args:
        record_id: Registro a firmar.
        user: Usuario autenticado.

    Returns:
        El registro firmado.
    """
    return await _avanzar(record_id, "signed", user)


@router.post("/records/{record_id}/submit")
async def submit_record(
    record_id: UUID, user: dict[str, Any] = Depends(require_role(*_ROLES_CLINICOS))
) -> dict[str, Any]:
    """Marca un registro firmado como enviado (RIPS presentado).

    Args:
        record_id: Registro a marcar.
        user: Usuario autenticado.

    Returns:
        El registro enviado.
    """
    return await _avanzar(record_id, "submitted", user)
