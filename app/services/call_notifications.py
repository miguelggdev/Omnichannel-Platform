"""Avisos de las llamadas agendadas: recordatorios al lead y aviso al agente (Sprint 19, Dev B).

**Recordatorios al lead** (24 h y 1 h antes; las reglas de cuando tocan son de Dev A,
`call_scheduling.recordatorio_pendiente`):

- Cada recordatorio va en su propia transaccion, con la fila bloqueada (`FOR UPDATE SKIP LOCKED`):
  dos workers no avisan dos veces. Se envia y se marca (`marcar_recordatorio`) en la misma
  transaccion.
- **Canales: email primero, WhatsApp despues.** Un recordatorio sale casi siempre fuera de la
  ventana de 24 h de WhatsApp, donde el texto libre lo rechaza Meta; por eso WhatsApp usa la
  plantilla aprobada del tenant (`call_reminders.whatsapp_template`) si la configuro.
- Fallo **temporal** en todos los canales: no se marca y se reintenta en la siguiente pasada (el
  recordatorio sigue valido hasta la hora de la llamada). Fallo **permanente** en todos: se marca
  (para no reintentar eternamente) y queda `call_reminder_failed` con el codigo.
- Un lead borrado, suprimido o que pidio no recibir mas mensajes **no recibe nada** (se marca).
- La hora va en la zona de la llamada y en el idioma del tenant.

**Aviso al agente** (llamadas humanas o mixtas): un email a la persona asignada al agendar y otro
cuando falta 1 h. El agente es del equipo: el aviso lleva el nombre de pila y la empresa del lead,
no su telefono ni su email.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.models.client import Client
from app.models.lead import Lead
from app.models.lead_activity import ACTIVITY_CALL_REMINDER_FAILED, ACTIVITY_CALL_REMINDER_SENT
from app.models.scheduled_call import UPCOMING_STATUSES, ScheduledCall
from app.models.user import User
from app.services.call_scheduling import (
    RECORDATORIO_1H,
    marcar_recordatorio,
    recordatorio_pendiente,
)
from app.services.lead_activity import registrar_actividad
from app.services.lead_outreach import (
    CanalNoDisponibleError,
    EnvioPermanenteError,
    EnvioTemporalError,
    datos_del_tenant,
    destino_del_lead,
    enviar_texto,
)
from app.services.lead_privacy import lead_esta_anonimizado
from app.services.lead_qualification import cargar_automatizacion
from app.services.lead_sequences import se_dio_de_baja
from app.services.prompt_safety import limpiar
from app.services.scheduled_calls import recordatorios_pendientes

logger = logging.getLogger(__name__)

#: Canales de recordatorio, en orden.
CANALES_RECORDATORIO: tuple[str, ...] = ("email", "whatsapp")

RES_ENVIADO = "sent"
RES_OMITIDO = "skipped"
RES_REINTENTO = "retry"
RES_FALLIDO = "failed"

_PLANTILLAS: dict[str, dict[str, str]] = {
    "es": {
        "asunto": "Recordatorio de tu llamada con {negocio}",
        "24h": "Hola{nombre}, te recordamos que manana tienes una llamada con {negocio}: {cuando}.",
        "1h": "Hola{nombre}, tu llamada con {negocio} es en una hora: {cuando}.",
    },
    "en": {
        "asunto": "Reminder of your call with {negocio}",
        "24h": "Hi{nombre}, a reminder that you have a call with {negocio} tomorrow: {cuando}.",
        "1h": "Hi{nombre}, your call with {negocio} starts in one hour: {cuando}.",
    },
    "pt": {
        "asunto": "Lembrete da sua ligacao com {negocio}",
        "24h": "Ola{nombre}, lembramos que voce tem uma ligacao com {negocio} amanha: {cuando}.",
        "1h": "Ola{nombre}, sua ligacao com {negocio} e em uma hora: {cuando}.",
    },
}
_AGENTE: dict[str, dict[str, str]] = {
    "es": {
        "asunto_nueva": "Nueva llamada agendada: {lead}",
        "nueva": "Tienes una llamada agendada con {lead}: {cuando}.",
        "asunto_1h": "Tu llamada con {lead} es en una hora",
        "1h": "Recuerda: tu llamada con {lead} es en una hora ({cuando}).",
    },
    "en": {
        "asunto_nueva": "New call scheduled: {lead}",
        "nueva": "You have a call scheduled with {lead}: {cuando}.",
        "asunto_1h": "Your call with {lead} starts in one hour",
        "1h": "Reminder: your call with {lead} starts in one hour ({cuando}).",
    },
    "pt": {
        "asunto_nueva": "Nova ligacao agendada: {lead}",
        "nueva": "Voce tem uma ligacao agendada com {lead}: {cuando}.",
        "asunto_1h": "Sua ligacao com {lead} e em uma hora",
        "1h": "Lembrete: sua ligacao com {lead} e em uma hora ({cuando}).",
    },
}


def formatear_cuando(inicio: datetime, zona: str) -> str:
    """`14/10/2026 10:00 (America/Bogota)`: la hora en la zona de la llamada.

    Args:
        inicio: Inicio (aware).
        zona: Zona IANA de la llamada.

    Returns:
        El texto. Una zona desconocida cae a UTC.
    """
    try:
        tz = ZoneInfo(zona)
        etiqueta = zona
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
        etiqueta = "UTC"
    return f"{inicio.astimezone(tz):%d/%m/%Y %H:%M} ({etiqueta})"


def texto_recordatorio(
    cual: str,
    *,
    idioma: str,
    nombre: str | None,
    negocio: str | None,
    inicio: datetime,
    zona: str,
) -> tuple[str, str]:
    """Asunto y cuerpo del recordatorio.

    Args:
        cual: `24h` o `1h`.
        idioma: `es`, `en` o `pt`.
        nombre: Nombre de pila del lead (sin el, el saludo queda sin nombre).
        negocio: Nombre del negocio.
        inicio: Inicio de la llamada.
        zona: Zona de la llamada.

    Returns:
        `(asunto, cuerpo)`.
    """
    t = _PLANTILLAS.get(idioma, _PLANTILLAS["es"])
    datos = {
        "negocio": limpiar(negocio, 100, una_linea=True) or "nuestro equipo",
        "nombre": f" {limpiar(nombre, 60, una_linea=True)}" if nombre else "",
        "cuando": formatear_cuando(inicio, zona),
    }
    return t["asunto"].format(**datos), t[cual].format(**datos)


@dataclass
class ResumenRecordatorios:
    """Lo que hizo una pasada de recordatorios (sin datos personales)."""

    procesados: int = 0
    por_resultado: dict[str, int] = field(default_factory=dict)

    def anotar(self, resultado: str) -> None:
        """Cuenta un recordatorio con su resultado."""
        self.procesados += 1
        self.por_resultado[resultado] = self.por_resultado.get(resultado, 0) + 1


async def _entregar(
    session: AsyncSession,
    llamada: ScheduledCall,
    lead: Lead,
    cual: str,
    *,
    idioma: str,
    negocio: str | None,
    plantilla_whatsapp: str | None,
) -> tuple[str | None, str | None, bool]:
    """Prueba los canales en orden hasta que uno entregue.

    Returns:
        `(canal, ultimo_codigo_de_fallo, hubo_fallo_temporal)`. `canal` es `None` si ninguno
        entrego.
    """
    asunto, cuerpo = texto_recordatorio(
        cual,
        idioma=idioma,
        nombre=lead.first_name,
        negocio=negocio,
        inicio=llamada.scheduled_at,
        zona=llamada.timezone,
    )
    ultimo: str | None = None
    temporal = False
    for canal in CANALES_RECORDATORIO:
        destino = await destino_del_lead(session, lead, canal)
        if destino is None:
            continue
        try:
            await enviar_texto(
                canal=canal,
                destino=destino,
                texto=cuerpo,
                asunto=asunto if canal == "email" else None,
                plantilla_whatsapp=plantilla_whatsapp if canal == "whatsapp" else None,
                idioma=idioma,
            )
        except EnvioTemporalError as exc:
            ultimo, temporal = exc.code, True
        except (EnvioPermanenteError, CanalNoDisponibleError) as exc:
            ultimo = exc.code
        else:
            return canal, None, temporal
    return None, ultimo or "no_destination", temporal


async def _recordatorio(
    client_id: UUID,
    call_id: UUID,
    ahora: datetime,
    idioma: str,
    negocio: str | None,
    plantilla: str | None,
) -> str:
    """Un recordatorio en su transaccion: bloquea, envia y marca."""
    async with tenant_session(client_id) as session:
        llamada = (
            await session.execute(
                select(ScheduledCall)
                .where(ScheduledCall.id == call_id, ScheduledCall.client_id == client_id)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if llamada is None:
            return RES_OMITIDO
        cual = recordatorio_pendiente(llamada, ahora)
        if cual is None:
            return RES_OMITIDO
        lead = (
            await session.execute(
                select(Lead).where(Lead.id == llamada.lead_id, Lead.client_id == client_id)
            )
        ).scalar_one()
        if (
            lead.deleted_at is not None
            or lead_esta_anonimizado(lead)
            or await se_dio_de_baja(session, lead)
        ):
            marcar_recordatorio(llamada, cual, ahora)
            return RES_OMITIDO
        canal, fallo, temporal = await _entregar(
            session,
            llamada,
            lead,
            cual,
            idioma=idioma,
            negocio=negocio,
            plantilla_whatsapp=plantilla,
        )
        if canal is not None:
            marcar_recordatorio(llamada, cual, ahora)
            registrar_actividad(
                session,
                client_id=client_id,
                lead_id=lead.id,
                tipo=ACTIVITY_CALL_REMINDER_SENT,
                call_id=llamada.id,
                reminder=cual,
                channel=canal,
            )
            if cual == RECORDATORIO_1H and llamada.assigned_user_id is not None:
                await _avisar_al_agente(session, llamada, lead, idioma, "1h")
            return RES_ENVIADO
        if temporal:
            return RES_REINTENTO  # sin marcar: la siguiente pasada lo vuelve a intentar
        marcar_recordatorio(llamada, cual, ahora)
        registrar_actividad(
            session,
            client_id=client_id,
            lead_id=lead.id,
            tipo=ACTIVITY_CALL_REMINDER_FAILED,
            call_id=llamada.id,
            reminder=cual,
            code=fallo,
        )
        return RES_FALLIDO


async def procesar_recordatorios(
    client_id: UUID, ahora: datetime, *, limite: int = 200
) -> ResumenRecordatorios:
    """Envia los recordatorios que tocan a un tenant.

    Args:
        client_id: Tenant.
        ahora: Instante actual.
        limite: Maximo de recordatorios por pasada.

    Returns:
        El resumen. Un fallo en uno se registra y no detiene a los demas.
    """
    resumen = ResumenRecordatorios()
    async with tenant_session(client_id) as session:
        pendientes = [
            llamada.id
            for llamada, _ in await recordatorios_pendientes(
                session, client_id, ahora, limite=limite
            )
        ]
        if not pendientes:
            return resumen
        negocio, idioma = await datos_del_tenant(session, client_id)
        bruto = (
            await session.execute(
                select(Client.lead_automation_config).where(Client.id == client_id)
            )
        ).scalar_one_or_none()
        plantilla = cargar_automatizacion(
            bruto, client_id=client_id
        ).call_reminders.whatsapp_template
    for call_id in pendientes:
        try:
            resumen.anotar(
                await _recordatorio(client_id, call_id, ahora, idioma, negocio, plantilla)
            )
        except Exception:
            logger.exception(
                "Fallo enviando un recordatorio (call_id=%s, client_id=%s)", call_id, client_id
            )
            resumen.anotar("error")
    return resumen


async def _avisar_al_agente(
    session: AsyncSession, llamada: ScheduledCall, lead: Lead, idioma: str, cual: str
) -> bool:
    """Email al agente asignado (`nueva` al agendar, `1h` cuando falta una hora).

    Nunca propaga: un correo que no sale no puede costar el agendamiento ni el recordatorio.
    """
    if llamada.assigned_user_id is None:
        return False
    try:
        correo = (
            await session.execute(
                select(User.email).where(
                    User.id == llamada.assigned_user_id,
                    User.client_id == llamada.client_id,
                    User.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if not correo:
            return False
        t = _AGENTE.get(idioma, _AGENTE["es"])
        quien = (
            limpiar(
                " ".join(
                    p
                    for p in (lead.first_name, lead.company_name and f"({lead.company_name})")
                    if p
                ),
                120,
                una_linea=True,
            )
            or "un lead"
        )
        datos = {"lead": quien, "cuando": formatear_cuando(llamada.scheduled_at, llamada.timezone)}
        asunto = t["asunto_nueva" if cual == "nueva" else "asunto_1h"].format(**datos)
        await enviar_texto(
            canal="email", destino=str(correo), texto=t[cual].format(**datos), asunto=asunto
        )
    except Exception as exc:
        logger.warning(
            "No se pudo avisar al agente de la llamada %s: %s", llamada.id, type(exc).__name__
        )
        return False
    return True


async def avisar_agente_de_llamada_nueva(client_id: UUID, call_id: UUID) -> bool:
    """Avisa al agente asignado de una llamada recien agendada (humana o mixta).

    Se llama despues del commit del agendamiento.

    Args:
        client_id: Tenant.
        call_id: Llamada.

    Returns:
        `True` si el email salio.
    """
    try:
        async with tenant_session(client_id) as session:
            llamada = (
                await session.execute(
                    select(ScheduledCall).where(
                        ScheduledCall.id == call_id,
                        ScheduledCall.client_id == client_id,
                        ScheduledCall.status.in_(UPCOMING_STATUSES),
                    )
                )
            ).scalar_one_or_none()
            if llamada is None or llamada.call_type == "ai_voice":
                return False
            lead = (
                await session.execute(
                    select(Lead).where(Lead.id == llamada.lead_id, Lead.client_id == client_id)
                )
            ).scalar_one()
            _, idioma = await datos_del_tenant(session, client_id)
            return await _avisar_al_agente(session, llamada, lead, idioma, "nueva")
    except Exception as exc:
        logger.warning("No se pudo avisar al agente (call_id=%s): %s", call_id, type(exc).__name__)
        return False
