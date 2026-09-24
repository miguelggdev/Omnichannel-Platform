"""Modelo SatisfactionSurvey — encuestas CSAT post-resolucion (Sprint 11, Dev B).

Una fila por conversacion resuelta (`conversation_id` es `UNIQUE`): el
constraint es la garantia de fondo de "una encuesta por conversacion" (criterio
7 del spec), y la comprobacion en `CSATService.schedule_survey()` es una
proteccion adicional que evita el viaje a la base cuando ya se sabe la
respuesta, no la unica barrera.
"""

from datetime import datetime
from uuid import UUID as _UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel

# Estados de `status`. `sent` -> `responded` (con `rating`) o `sent` -> `expired`
# (`app.tasks.notification_expire_csat_surveys`, transcurrido `SURVEY_EXPIRY_HOURS`).
STATUS_SENT = "sent"
STATUS_RESPONDED = "responded"
STATUS_EXPIRED = "expired"


class SatisfactionSurvey(TenantBaseModel):
    """Encuesta CSAT (1-5) enviada tras resolver una conversacion.

    Attributes:
        conversation_id: Conversacion que origino la encuesta. Unica: no puede
            haber dos encuestas para la misma conversacion.
        contact_id: Contacto al que se le envio.
        channel: Canal por el que se envio (determina el formato del mensaje).
        rating: Calificacion 1-5. NULL mientras no responda.
        comment: Comentario opcional que acompana al rating.
        sent_at: Cuando se envio la encuesta.
        responded_at: Cuando respondio, si respondio.
        survey_message_id: Id externo del mensaje de encuesta (para email, el
            link de respuesta lleva el id de esta fila, no del mensaje).
        status: `sent`, `responded` o `expired`.
    """

    __tablename__ = "satisfaction_surveys"
    __table_args__ = (
        CheckConstraint("rating IS NULL OR (rating BETWEEN 1 AND 5)", name="ck_csat_rating_range"),
        Index("idx_csat_client_sent_at", "client_id", "sent_at"),
    )

    conversation_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    contact_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    survey_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), server_default=STATUS_SENT, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
