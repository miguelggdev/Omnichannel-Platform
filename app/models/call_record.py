"""Modelo CallRecord — una llamada del canal de voz (Sprint 13, Dev A).

Una fila por llamada de Twilio, identificada por su `CallSid`. La escriben dos
fuentes que no se esperan entre si, y por eso se hace siempre con un upsert
sobre `(client_id, call_sid)` (`app/tasks/voice_tasks.py`):

- el **status callback** de Twilio, que trae la verdad del operador: estado
  final, duracion facturada y, si la grabacion esta activa, su URL;
- el **cierre del stream de audio**, que trae lo que solo sabe la plataforma:
  la transcripcion de los dos lados y la conversacion en la que acabo.

Desviaciones sobre el spec (§8.1)
---------------------------------
- `contact_id` es **nullable**. La API no resuelve contactos: eso ocurre en el
  worker cuando llega el primer mensaje transcrito. Una llamada que se corta
  antes de que el cliente diga nada no tiene contacto, y tiene que quedar
  registrada igual.
- Los telefonos (`phone_from`, `phone_to`) van cifrados con pgcrypto, como
  cualquier otro telefono de la plataforma (CLAUDE.md, regla 3).
- `status` guarda el valor de Twilio tal cual (`in-progress`, `no-answer`...),
  con un CHECK en la base, en vez de una traduccion propia que habria que
  mantener sincronizada.
"""

from datetime import datetime
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString
from app.models.base import TenantBaseModel

#: Estados de una llamada, tal como los reporta Twilio (`CallStatus`).
CALL_STATUSES: tuple[str, ...] = (
    # `initiated` no aparece en la lista de `CallStatus` de la documentacion,
    # pero es lo que trae el callback del evento del mismo nombre al que se
    # suscribe `TwilioVoiceProvider.start_call`. Sin el, toda llamada saliente
    # perdia su primer estado y dejaba un error en el log.
    "initiated",
    "queued",
    "ringing",
    "in-progress",
    "completed",
    "busy",
    "failed",
    "no-answer",
    "canceled",
)

#: Estados que ya no cambian: una llamada en uno de estos termino.
CALL_FINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "busy", "failed", "no-answer", "canceled"}
)

CALL_DIRECTIONS: tuple[str, ...] = ("inbound", "outbound")


def _en(valores: tuple[str, ...], columna: str) -> str:
    """Arma el `IN (...)` de un CHECK a partir de los valores permitidos.

    Args:
        valores: Valores admitidos.
        columna: Columna sobre la que aplica.

    Returns:
        La expresion SQL del CHECK.
    """
    return f"{columna} IN ({', '.join(repr(v) for v in valores)})"


class CallRecord(TenantBaseModel):
    """Registro de una llamada telefonica.

    Attributes:
        call_sid: `CallSid` de Twilio. Unico por tenant.
        contact_id: Contacto que llamo o al que se llamo, si se llego a resolver.
        conversation_id: Conversacion en la que quedaron los mensajes de la llamada.
        direction: `inbound` u `outbound`.
        status: Ultimo estado reportado por Twilio (uno de `CALL_STATUSES`).
        phone_from: Numero de origen, cifrado.
        phone_to: Numero de destino, cifrado.
        started_at: Inicio de la llamada.
        ended_at: Fin de la llamada, si ya termino.
        duration_seconds: Duracion que reporta Twilio (`CallDuration`).
        transcript: Turnos de la llamada, `[{role, text, timestamp}]`.
        recording_url: URL de la grabacion en Twilio, si se grabo.
        recording_duration: Duracion de la grabacion en segundos.
        metadata_: Datos extra del proveedor (columna `metadata`).
        updated_at: Ultima modificacion.
    """

    __tablename__ = "call_records"
    __table_args__ = (
        Index("uq_call_records_call_sid", "client_id", "call_sid", unique=True),
        Index("idx_call_records_client_started", "client_id", "started_at"),
        Index("idx_call_records_contact", "client_id", "contact_id"),
        # Derivados de las tuplas de arriba y no escritos a mano: son los
        # mismos valores que la migracion 016, y copiarlos dejaba las dos listas
        # libres de separarse sin que nada avisara.
        CheckConstraint(_en(CALL_STATUSES, "status"), name="ck_call_records_status"),
        CheckConstraint(_en(CALL_DIRECTIONS, "direction"), name="ck_call_records_direction"),
    )

    call_sid: Mapped[str] = mapped_column(String(64), nullable=False)
    contact_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=True
    )
    conversation_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=True
    )
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    phone_from: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    phone_to: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    transcript: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default="[]", nullable=False
    )
    recording_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    recording_duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default="{}", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
