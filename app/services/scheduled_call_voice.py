"""Llamadas agendadas con voz IA: iniciar, y enlazar la llamada real al terminar (Sprint 19, Dev B).

Iniciar en tres pasos, porque llamar a un proveedor puede tardar y no se debe tener la fila
bloqueada mientras tanto:

1. **Reservar** (transaccion corta con la fila bloqueada): se valida y se deja una marca
   `ai_voice_config["runtime"] = {"state": "starting", "at": ...}`. Una segunda peticion a la vez
   ve la marca y recibe `already_starting`; una marca de mas de `VIDA_MARCA` se considera
   abandonada (el worker murio entre medias) y se puede reintentar.
2. **Llamar** al proveedor, sin transaccion abierta.
3. **Anotar**: con exito, el estado pasa a `in_progress` y la marca guarda el id de la llamada en
   el proveedor; con fallo, se quita la marca y la llamada queda como estaba (se puede reintentar
   o cancelar).

La clave `runtime` es del servidor: la API rechaza que el cliente la envie en `ai_voice_config`.

Enlazar: cuando el status callback del canal de voz guarda la fila de `call_records`
(`voice_tasks.guardar_llamada`), `enlazar_llamada()` busca la llamada agendada por el id del
proveedor, le pone `call_record_id` y, si la llamada termino, la cierra: `completed` si hablaron,
`no_show` con resultado `no_answer` si no contesto, comunicaba o fallo.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.models.call_record import CALL_FINAL_STATUSES
from app.models.lead import Lead
from app.models.scheduled_call import (
    CALL_AI_VOICE,
    CALL_CONFIRMED,
    CALL_HYBRID,
    CALL_IN_PROGRESS,
    CALL_NO_SHOW,
    CALL_PENDING,
    ScheduledCall,
)
from app.services import voice_ai_twilio  # noqa: F401  (registra el proveedor "twilio")
from app.services.lead_privacy import lead_esta_anonimizado
from app.services.lead_sequences import se_dio_de_baja
from app.services.phone_unification import normalizar_telefono
from app.services.scheduled_calls import CallError, cambiar_estado
from app.services.voice_call_provider import (
    OutboundCallRequest,
    ProviderCallRef,
    UnknownVoiceProviderError,
    VoiceProviderError,
    VoiceProviderNotConfiguredError,
    VoiceProviderTemporaryError,
    build_voice_provider,
)

logger = logging.getLogger(__name__)

#: Clave de `ai_voice_config` que escribe el servidor (la API rechaza que la envie el cliente).
RUNTIME = "runtime"
#: Cuanto vale la marca "starting" antes de darla por abandonada.
VIDA_MARCA = timedelta(minutes=2)
#: Resultado de una llamada que no se llego a hablar.
SIN_RESPUESTA = "no_answer"


@dataclass(frozen=True)
class InicioReservado:
    """Lo necesario para llamar al proveedor, ya validado y con la fila marcada.

    Attributes:
        peticion: La peticion al proveedor (el telefono no sale en su `repr`).
        proveedor: Nombre del proveedor.
        estado_previo: Estado de la llamada antes de iniciarla.
    """

    peticion: OutboundCallRequest
    proveedor: str
    estado_previo: str


def a_e164(telefono: str | None) -> str | None:
    """El telefono del lead en E.164 con `+`, o `None` si no parece internacional.

    Args:
        telefono: Telefono tal como esta guardado.

    Returns:
        `+573001112233`, o `None`.
    """
    digitos = normalizar_telefono(telefono)
    return f"+{digitos}" if digitos else None


def _marca_vigente(config: dict[str, Any], ahora: datetime) -> bool:
    """Si hay una marca `starting` reciente."""
    runtime = config.get(RUNTIME)
    if not isinstance(runtime, dict) or runtime.get("state") != "starting":
        return False
    try:
        desde = datetime.fromisoformat(str(runtime.get("at")))
    except ValueError:
        return False
    return ahora - desde < VIDA_MARCA


async def reservar_inicio(
    session: AsyncSession, llamada: ScheduledCall, lead: Lead, *, ahora: datetime, idioma: str
) -> InicioReservado:
    """Valida que la llamada pueda iniciarse y deja la marca `starting`.

    Args:
        session: Sesion del tenant; la fila de `llamada` ya esta bloqueada por quien llama.
        llamada: Llamada agendada.
        lead: Su lead.
        ahora: Instante actual.
        idioma: Idioma de la conversacion.

    Returns:
        Lo necesario para llamar al proveedor.

    Raises:
        CallError: `not_ai_call`, `invalid_status`, `already_starting`, `lead_unavailable`,
            `opted_out`, `no_phone` o `provider_unknown`.
    """
    if llamada.call_type not in (CALL_AI_VOICE, CALL_HYBRID) or not llamada.ai_voice_provider:
        raise CallError("not_ai_call", "Esta llamada no usa voz IA")
    if llamada.status not in (CALL_PENDING, CALL_CONFIRMED):
        raise CallError("invalid_status", f"Una llamada en '{llamada.status}' no se puede iniciar")
    config = dict(llamada.ai_voice_config or {})
    if _marca_vigente(config, ahora):
        raise CallError("already_starting", "La llamada ya se esta iniciando")
    if lead.deleted_at is not None or lead_esta_anonimizado(lead):
        raise CallError("lead_unavailable", "El lead esta borrado o suprimido")
    if await se_dio_de_baja(session, lead):
        raise CallError("opted_out", "El lead pidio no recibir mas contacto")
    telefono = a_e164(str(lead.phone) if lead.phone else None)
    if telefono is None:
        raise CallError("no_phone", "El lead no tiene un telefono internacional valido")
    try:
        build_voice_provider(llamada.ai_voice_provider)
    except UnknownVoiceProviderError as exc:
        raise CallError("provider_unknown", "Proveedor de voz desconocido") from exc
    except VoiceProviderNotConfiguredError as exc:
        raise CallError("provider_not_configured", "Proveedor de voz sin configurar") from exc
    config[RUNTIME] = {"state": "starting", "at": ahora.isoformat()}
    llamada.ai_voice_config = config
    await session.flush()
    visibles = {k: v for k, v in config.items() if k != RUNTIME}
    return InicioReservado(
        peticion=OutboundCallRequest(
            scheduled_call_id=llamada.id,
            client_id=llamada.client_id,
            to_phone=telefono,
            language=idioma,
            timezone=llamada.timezone,
            max_duration_minutes=llamada.duration_minutes,
            context={"call_type": llamada.call_type},
            config=visibles,
        ),
        proveedor=llamada.ai_voice_provider,
        estado_previo=llamada.status,
    )


def _sin_marca(config: dict[str, Any]) -> dict[str, Any]:
    """La configuracion sin la clave del servidor."""
    return {k: v for k, v in config.items() if k != RUNTIME}


async def iniciar_llamada_ia(
    client_id: UUID,
    call_id: UUID,
    *,
    ahora: datetime,
    idioma: str,
    user_id: UUID | None,
) -> ProviderCallRef:
    """Inicia una llamada agendada con voz IA (reservar, llamar, anotar).

    Args:
        client_id: Tenant.
        call_id: Llamada agendada.
        ahora: Instante actual.
        idioma: Idioma de la conversacion.
        user_id: Quien la inicia; `None` si la inicio el sistema.

    Returns:
        La referencia en el proveedor.

    Raises:
        CallError: Ver `reservar_inicio`; ademas `not_found` si no existe, `provider_down` si el
            proveedor fallo de forma pasajera y `provider_rejected` si la rechazo.
    """
    async with tenant_session(client_id, user_id=user_id) as session:
        llamada = (
            await session.execute(
                select(ScheduledCall)
                .where(ScheduledCall.id == call_id, ScheduledCall.client_id == client_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if llamada is None:
            raise CallError("not_found", "Llamada no encontrada")
        lead = (
            await session.execute(
                select(Lead).where(Lead.id == llamada.lead_id, Lead.client_id == client_id)
            )
        ).scalar_one()
        reservado = await reservar_inicio(session, llamada, lead, ahora=ahora, idioma=idioma)

    error: CallError | None = None
    ref: ProviderCallRef | None = None
    try:
        ref = await build_voice_provider(reservado.proveedor).start_call(reservado.peticion)
    except VoiceProviderNotConfiguredError as exc:
        error = CallError("provider_not_configured", "Proveedor de voz sin configurar")
        error.__cause__ = exc
    except VoiceProviderTemporaryError as exc:
        error = CallError("provider_down", "El proveedor de voz no responde; reintenta")
        error.__cause__ = exc
    except VoiceProviderError as exc:
        error = CallError("provider_rejected", "El proveedor de voz rechazo la llamada")
        error.__cause__ = exc

    async with tenant_session(client_id, user_id=user_id) as session:
        llamada = (
            await session.execute(
                select(ScheduledCall)
                .where(ScheduledCall.id == call_id, ScheduledCall.client_id == client_id)
                .with_for_update()
            )
        ).scalar_one()
        config = _sin_marca(dict(llamada.ai_voice_config or {}))
        if ref is None:
            llamada.ai_voice_config = config  # sigue como estaba: se puede reintentar o cancelar
        else:
            config[RUNTIME] = {
                "state": "started",
                "external_id": ref.external_id,
                "at": ahora.isoformat(),
            }
            llamada.ai_voice_config = config
            cambiar_estado(session, llamada, CALL_IN_PROGRESS, ahora=ahora, user_id=user_id)
        await session.flush()
    if error is not None:
        raise error
    if ref is None:  # inalcanzable: sin error siempre hay referencia
        raise CallError("provider_rejected", "El proveedor de voz no devolvio la llamada")
    return ref


async def enlazar_llamada(
    session: AsyncSession,
    *,
    client_id: UUID,
    call_sid: str,
    call_record_id: UUID,
    estado: str,
    ahora: datetime,
) -> bool:
    """Enlaza la fila de `call_records` con la llamada agendada que la origino y la cierra.

    Args:
        session: Sesion del tenant (la de `guardar_llamada`).
        client_id: Tenant.
        call_sid: `CallSid` de Twilio (el `external_id` guardado al iniciarla).
        call_record_id: Fila de `call_records`.
        estado: Estado de Twilio de la llamada (`completed`, `no-answer`, `in-progress`...).
        ahora: Instante actual.

    Returns:
        `True` si habia una llamada agendada con ese id de proveedor.
    """
    llamada = (
        await session.execute(
            select(ScheduledCall)
            .where(
                ScheduledCall.client_id == client_id,
                ScheduledCall.ai_voice_config[RUNTIME]["external_id"].astext == call_sid,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if llamada is None:
        return False
    llamada.call_record_id = call_record_id
    if llamada.status == CALL_IN_PROGRESS and estado in CALL_FINAL_STATUSES:
        if estado == "completed":
            cambiar_estado(session, llamada, "completed", ahora=ahora)
        else:
            cambiar_estado(session, llamada, CALL_NO_SHOW, ahora=ahora, outcome=SIN_RESPUESTA)
    await session.flush()
    return True
