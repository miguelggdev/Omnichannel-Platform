"""Schemas de la API de llamadas agendadas que no estan en `app/schemas/deal.py` (Sprint 19, Dev B).

El contrato de crear, reprogramar y completar lo dejo Dev A (`ScheduledCallCreate`,
`ScheduledCallReschedule`, `ScheduledCallComplete`); aqui estan los huecos libres y la edicion de
notas.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ScheduledCallNotes(BaseModel):
    """Cuerpo de `PATCH /scheduled-calls/{id}`: solo las notas.

    El resto (hora, persona, tipo) se cambia reprogramando o agendando otra: asi los
    recordatorios y el calendario no se descuadran.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    notes: str | None = Field(default=None, max_length=5000)


class SlotsResponse(BaseModel):
    """Huecos libres para agendar.

    Attributes:
        slots: Inicios posibles, en UTC y de menor a mayor.
        timezone: Zona del negocio (para mostrarlos en horario local).
        duration_minutes: Duracion con la que se calcularon.
        calendar_checked: Si ademas se miro el calendario de Google. `false` = sin calendario
            configurado o Google no respondio: los huecos solo descuentan las llamadas de la base.
    """

    slots: list[datetime]
    timezone: str
    duration_minutes: int
    calendar_checked: bool
