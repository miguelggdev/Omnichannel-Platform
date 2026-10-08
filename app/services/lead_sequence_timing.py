"""Cuando ejecutar el siguiente paso de una secuencia (Sprint 18: "smart timing").

Tres reglas, en este orden, sobre `ahora + espera`:

1. **Smart timing** (si el paso lo pide): mover el envio a la hora local a la que el lead suele
   contestar, si hay historia suficiente (`MIN_RESPUESTAS` respuestas); nunca antes de lo que
   pide la espera.
2. **Horario de atencion** (si el paso lo pide): si el momento cae fuera del horario del negocio,
   pasar a la siguiente apertura. Un mensaje a las 3 a. m. no se contesta y, en WhatsApp, molesta.
3. La zona horaria y el horario salen del **perfil del negocio** (`clients.settings
   ["business_profile"]`, Sprint 15); sin perfil, Bogota de lunes a viernes de 08:00 a 18:00,
   que es el valor por defecto del propio perfil.

Funciones puras salvo `ventana_del_tenant()`, que lee el perfil.
"""

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.schemas.business_profile import DIAS, DaySchedule
from app.schemas.lead_sequence import WaitStep
from app.services.calendar import DEFAULT_TIMEZONE

logger = logging.getLogger(__name__)

#: Respuestas del lead necesarias para fiarse de "su" hora.
MIN_RESPUESTAS = 3
#: Hasta donde se busca la siguiente apertura (un negocio cerrado toda la semana no cuelga nada).
_HORIZONTE_DIAS = 14


@dataclass(frozen=True)
class VentanaEnvio:
    """Zona horaria y horario de atencion del negocio.

    Attributes:
        tz: Zona horaria IANA.
        horario: Dia de la semana (0 = lunes) -> `(apertura, cierre)`, o `None` si cierra.
    """

    tz: ZoneInfo
    horario: Mapping[int, tuple[time, time] | None]

    def abierto(self, momento: datetime) -> bool:
        """Si el negocio atiende en ese momento (en su zona horaria).

        Args:
            momento: Instante aware.

        Returns:
            `True` si cae dentro del horario de ese dia.
        """
        local = momento.astimezone(self.tz)
        tramo = self.horario.get(local.weekday())
        return tramo is not None and tramo[0] <= local.time() < tramo[1]


def _hora(valor: str | None, defecto: str) -> time:
    """`"08:30"` a `time`."""
    horas, minutos = (valor or defecto).split(":")
    return time(int(horas), int(minutos))


def ventana_desde_perfil(
    perfil: Mapping[str, Any] | None, client_id: UUID | None = None
) -> VentanaEnvio:
    """Construye la ventana a partir del perfil del negocio.

    Lo invalido se ignora en vez de romper la programacion: zona desconocida -> Bogota; un dia
    mal guardado (hora no valida, cierre antes de la apertura) -> el horario por defecto de ese
    dia (`DaySchedule` lo rechaza al validar).

    Args:
        perfil: `clients.settings["business_profile"]`.
        client_id: Tenant, solo para el log (saber de quien es el perfil roto).

    Returns:
        La ventana.
    """
    datos = perfil or {}
    try:
        tz = ZoneInfo(str(datos.get("timezone") or DEFAULT_TIMEZONE))
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "Zona horaria del perfil desconocida (tenant %s); se usa %s",
            client_id,
            DEFAULT_TIMEZONE,
        )
        tz = ZoneInfo(DEFAULT_TIMEZONE)

    guardado = datos.get("operating_hours") or {}
    horario: dict[int, tuple[time, time] | None] = {}
    for indice, dia in enumerate(DIAS):
        try:
            crudo = guardado.get(dia) if isinstance(guardado, Mapping) else None
            dia_cfg = (
                DaySchedule.model_validate(crudo)
                if crudo
                else DaySchedule(is_open=dia not in ("saturday", "sunday"))
            )
        except ValueError:
            logger.warning(
                "Horario del %s mal guardado (tenant %s); se usa el de defecto", dia, client_id
            )
            dia_cfg = DaySchedule(is_open=dia not in ("saturday", "sunday"))
        if not dia_cfg.is_open:
            horario[indice] = None
            continue
        apertura = _hora(dia_cfg.open_time, "08:00")
        cierre = _hora(dia_cfg.close_time, "18:00")
        horario[indice] = (apertura, cierre) if apertura < cierre else None
    return VentanaEnvio(tz=tz, horario=horario)


async def ventana_del_tenant(session: AsyncSession, client_id: UUID) -> VentanaEnvio:
    """La ventana de envio del tenant, leida de su perfil.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.

    Returns:
        La ventana.
    """
    ajustes = (
        await session.execute(select(Client.settings).where(Client.id == client_id))
    ).scalar_one_or_none()
    perfil = (ajustes or {}).get("business_profile") if isinstance(ajustes, Mapping) else None
    return ventana_desde_perfil(perfil if isinstance(perfil, Mapping) else None, client_id)


def siguiente_apertura(desde: datetime, ventana: VentanaEnvio) -> datetime:
    """El primer instante >= `desde` en que el negocio atiende.

    Args:
        desde: Instante aware.
        ventana: Horario del negocio.

    Returns:
        `desde` si ya esta abierto; si no, la siguiente apertura. Si el negocio no abre ningun
        dia en `_HORIZONTE_DIAS`, `desde` sin tocar (mejor enviar que no enviar nunca).
    """
    if ventana.abierto(desde):
        return desde
    local = desde.astimezone(ventana.tz)
    for dias in range(_HORIZONTE_DIAS + 1):
        fecha = (local + timedelta(days=dias)).date()
        tramo = ventana.horario.get(fecha.weekday())
        if tramo is None:
            continue
        apertura = datetime.combine(fecha, tramo[0], tzinfo=ventana.tz)
        if apertura >= desde:
            return apertura
    return desde


def hora_preferida(horas_locales: Sequence[int]) -> int | None:
    """La hora local (0-23) a la que el lead mas contesta, si hay historia suficiente.

    Args:
        horas_locales: Hora local de cada respuesta del lead.

    Returns:
        La hora mas frecuente (la mas temprana si empatan), o `None` con menos de
        `MIN_RESPUESTAS` respuestas.
    """
    validas = [h for h in horas_locales if 0 <= h <= 23]
    if len(validas) < MIN_RESPUESTAS:
        return None
    conteo = Counter(validas)
    maximo = max(conteo.values())
    return min(h for h, n in conteo.items() if n == maximo)


def programar_siguiente(
    ahora: datetime,
    espera: WaitStep,
    ventana: VentanaEnvio,
    horas_respuesta: Sequence[int] = (),
) -> datetime:
    """Cuando ejecutar el paso que sigue a una espera.

    Args:
        ahora: Instante actual (aware).
        espera: El paso `wait`.
        ventana: Horario del negocio.
        horas_respuesta: Horas locales a las que el lead contesto antes (para smart timing).

    Returns:
        El instante (aware, en UTC).
    """
    momento = ahora + espera.delta
    if espera.smart_timing:
        hora = hora_preferida(horas_respuesta)
        if hora is not None:
            local = momento.astimezone(ventana.tz)
            candidato = local.replace(hour=hora, minute=0, second=0, microsecond=0)
            if candidato < local:
                candidato += timedelta(days=1)
            momento = candidato
    if espera.business_hours_only:
        momento = siguiente_apertura(momento, ventana)
    return momento.astimezone(ZoneInfo("UTC"))
