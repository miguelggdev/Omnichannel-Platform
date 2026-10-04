"""Schemas de la analitica del dashboard (Sprint 15, fase 2)."""

from pydantic import BaseModel


class DashboardMetrics(BaseModel):
    """Cifras del inicio del panel, calculadas sobre los datos reales del tenant.

    Attributes:
        active_conversations: Conversaciones abiertas (cualquier estado menos
            `resolved` y `archived`).
        waiting_human: De ellas, las que esperan a una persona.
        conversations_last_7_days: Conversaciones creadas en los ultimos 7 dias.
        conversations_trend: Variacion en % frente a los 7 dias anteriores; `None` si
            no habia ninguna entonces (no se puede dividir por cero).
        messages_last_24h: Mensajes (entrantes y salientes) de las ultimas 24 horas.
        messages_trend: Variacion en % frente a las 24 horas anteriores; `None` si no
            habia ninguno.
        token_usage_percentage: % del presupuesto de tokens del mes en curso; `None` si
            el tenant no tiene presupuesto o es ilimitado.
        avg_response_time_seconds: Tiempo medio hasta la primera respuesta (del bot o de
            una persona) a lo que escribe el contacto, en los ultimos 7 dias.
        median_response_time_seconds: Mediana de lo mismo: el bot contesta en segundos y
            unas pocas esperas largas sesgan la media.
        csat_score: Valoracion media (1-5) de las encuestas respondidas en 30 dias.
        csat_responses: Cuantas respuestas hay detras de `csat_score`.
        total_contacts: Contactos del tenant, sin contar los fusionados.
    """

    active_conversations: int
    waiting_human: int
    conversations_last_7_days: int
    conversations_trend: float | None
    messages_last_24h: int
    messages_trend: float | None
    token_usage_percentage: float | None
    avg_response_time_seconds: float | None
    median_response_time_seconds: float | None
    csat_score: float | None
    csat_responses: int
    total_contacts: int


class ChannelCount(BaseModel):
    """Conversaciones creadas por un canal en el periodo."""

    channel: str
    count: int


class DailyMessages(BaseModel):
    """Mensajes de un dia (UTC), separados por direccion."""

    date: str
    inbound: int
    outbound: int
