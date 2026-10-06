"""Captura publica de leads: `POST /api/v1/capture/{source_token}` (Sprint 16, ADR-083).

Es el unico endpoint de leads sin JWT: lo llama el formulario web de un cliente. La
autenticacion es el token de la fuente (en la URL). Para que no sea un grifo abierto:

1. **Limite por IP primero**, antes de tocar la base: tambien frena a quien prueba tokens al azar.
   Falla cerrado (503) si Redis no responde.
2. **Limite por fuente** (un formulario con mucho trafico no debe poder agotar a otro tenant, ni
   uno atacado inundar de leads a su dueno).
3. **Respuesta uniforme.** Token desconocido, fuente apagada, tenant suspendido y modulo
   deshabilitado devuelven el mismo `404`; un lead repetido, un honeypot relleno y un alta nueva
   devuelven el mismo `202`. Nada de esto sirve de oraculo para enumerar fuentes ni emails.
4. **Honeypot** (`website`) y **consentimiento** (Ley 1581): la fuente lo exige salvo que
   `config["require_consent"]` sea `false`.
5. Cuerpo de hasta 16 KB; los campos ya tienen longitud maxima.

Un lead repetido (mismo email o telefono entre los no borrados) no se duplica: solo se anota la
nueva actividad en `last_activity_at`. Los UTM de la visita (o los que defina la fuente) y el
consentimiento quedan en `enrichment_data["capture"]`, que la anonimizacion RGPD vacia.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, tenant_session
from app.core.encryption import blind_index
from app.core.exceptions import NOT_FOUND, VALIDATION_ERROR, AppException
from app.core.rate_limit import client_ip, hit
from app.models.lead import Lead, canonicalizar_linkedin, normalizar_telefono_de_lead
from app.models.lead_activity import ACTIVITY_CAPTURED, ACTIVITY_RECAPTURED
from app.models.lead_pipeline_stage import LeadPipelineStage
from app.models.lead_source import LeadSource
from app.schemas.lead import LeadCapture
from app.services.lead_activity import registrar_actividad
from app.services.lead_pipeline import FIRST_STAGE_SLUG, hash_capture_token

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_BODY_BYTES = 16 * 1024
_RESPUESTA = {"status": "received"}
_UTM = ("utm_source", "utm_medium", "utm_campaign")

_BUSCAR_FUENTE = text("SELECT * FROM public.capture_lookup_source(:h)")


def _no_encontrada() -> AppException:
    """El unico error que ve un llamador sin fuente valida (ver el docstring del modulo)."""
    return AppException(status_code=404, error_code=NOT_FOUND, message="No encontrado")


async def _resolver_fuente(token_hash: str) -> Any:
    """La fuente del token, o `None` si no existe, esta apagada o su tenant no puede recibir leads.

    Args:
        token_hash: SHA-256 del token de la URL.

    Returns:
        La fila de `capture_lookup_source()` o `None`.
    """
    async with AsyncSessionLocal() as session, session.begin():
        fila = (await session.execute(_BUSCAR_FUENTE, {"h": token_hash})).one_or_none()
    if fila is None or not (fila.source_active and fila.client_active and fila.leads_enabled):
        return None
    return fila


@router.post("/{source_token}", status_code=status.HTTP_202_ACCEPTED)
async def capture_lead(source_token: str, data: LeadCapture, request: Request) -> dict[str, str]:
    """Recibe un lead de un formulario publico.

    Args:
        source_token: Token de la fuente (va en la URL).
        data: Datos del formulario.
        request: Peticion, para limitar por IP y acotar el cuerpo.

    Returns:
        Siempre `{"status": "received"}` si la fuente es valida, haya o no creado un lead nuevo.

    Raises:
        AppException: 413 si el cuerpo supera 16 KB; 429 si se supero un limite; 503 si Redis no
            responde; 404 si el token no corresponde a una fuente utilizable; 422 si la fuente
            exige consentimiento y no llega.
    """
    declarado = request.headers.get("content-length")
    if declarado is not None and declarado.isdigit() and int(declarado) > MAX_BODY_BYTES:
        raise AppException(
            status_code=413, error_code=VALIDATION_ERROR, message="Cuerpo demasiado grande"
        )

    ajustes = get_settings()
    await hit(f"capture-ip:{client_ip(request)}", ajustes.CAPTURE_MAX_PER_IP_PER_MINUTE, 60)

    token_hash = hash_capture_token(source_token)
    await hit(f"capture-src:{token_hash[:16]}", ajustes.CAPTURE_MAX_PER_SOURCE_PER_MINUTE, 60)

    fuente = await _resolver_fuente(token_hash)
    if fuente is None:
        raise _no_encontrada()
    client_id: UUID = fuente.client_id

    if data.website:
        # Honeypot: un bot rellena el campo oculto. Se responde igual que a un alta real.
        logger.info("Captura descartada por honeypot (fuente %s)", fuente.source_id)
        return _RESPUESTA

    try:
        async with tenant_session(client_id) as session:
            origen: LeadSource | None = (
                await session.execute(
                    select(LeadSource).where(
                        LeadSource.id == fuente.source_id, LeadSource.client_id == client_id
                    )
                )
            ).scalar_one_or_none()
            if origen is None or not origen.is_active:
                raise _no_encontrada()
            if origen.config.get("require_consent", True) and not data.consent:
                raise AppException(
                    status_code=422,
                    error_code=VALIDATION_ERROR,
                    message="Hace falta la autorizacion de tratamiento de datos (consent)",
                )

            existente = await _lead_existente(session, client_id, data)
            ahora = datetime.now(timezone.utc)
            if existente is not None:
                existente.last_activity_at = ahora
                registrar_actividad(
                    session,
                    client_id=client_id,
                    lead_id=existente.id,
                    tipo=ACTIVITY_RECAPTURED,
                    source_id=origen.id,
                )
                return _RESPUESTA

            etapa = await _etapa_inicial(session, client_id)
            utm = {**origen.utm_tracking, **{k: getattr(data, k) for k in _UTM if getattr(data, k)}}
            lead = Lead(
                client_id=client_id,
                source_id=origen.id,
                pipeline_stage_id=etapa,
                first_name=data.first_name,
                last_name=data.last_name,
                email=data.email,
                phone=data.phone,
                linkedin_url=data.linkedin_url,
                company_name=data.company_name,
                company_domain=data.company_domain,
                company_size=data.company_size,
                industry=data.industry,
                job_title=data.job_title,
                last_activity_at=ahora,
                enrichment_data={
                    "capture": {
                        "captured_at": ahora.isoformat(),
                        "consent": data.consent,
                        "utm": utm,
                    }
                },
            )
            session.add(lead)
            await session.flush()
            registrar_actividad(
                session,
                client_id=client_id,
                lead_id=lead.id,
                tipo=ACTIVITY_CAPTURED,
                source_id=origen.id,
            )
    except IntegrityError:
        # Dos capturas del mismo email a la vez: la segunda es un duplicado y se trata como tal.
        logger.info("Captura duplicada por carrera (fuente %s)", fuente.source_id)
    return _RESPUESTA


async def _lead_existente(session: Any, client_id: UUID, data: LeadCapture) -> Lead | None:
    """Un lead no borrado del tenant con el mismo email, telefono o LinkedIn, si lo hay."""
    condiciones = []
    if data.email:
        condiciones.append(Lead.email_hash == blind_index(str(data.email), client_id))
    digitos = normalizar_telefono_de_lead(data.phone)
    if digitos:
        condiciones.append(Lead.phone_hash == blind_index(digitos, client_id))
    url = canonicalizar_linkedin(data.linkedin_url)
    if url:
        condiciones.append(Lead.linkedin_url == url)
    for condicion in condiciones:
        lead: Lead | None = (
            await session.execute(
                select(Lead)
                .where(Lead.client_id == client_id, Lead.deleted_at.is_(None), condicion)
                .limit(1)
            )
        ).scalar_one_or_none()
        if lead is not None:
            return lead
    return None


async def _etapa_inicial(session: Any, client_id: UUID) -> UUID | None:
    """La etapa `new` o la primera; `None` si el tenant borro todo su pipeline.

    Un lead sin etapa no aparece en el Kanban pero **no se pierde**: el listado lo muestra.
    """
    etapas = (
        (
            await session.execute(
                select(LeadPipelineStage)
                .where(LeadPipelineStage.client_id == client_id)
                .order_by(LeadPipelineStage.position)
            )
        )
        .scalars()
        .all()
    )
    if not etapas:
        logger.warning("El tenant %s recibe leads pero no tiene pipeline", client_id)
        return None
    elegida = next((e for e in etapas if e.slug == FIRST_STAGE_SLUG), etapas[0])
    return UUID(str(elegida.id))
