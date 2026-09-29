"""Schemas del canal de voz (Sprint 13, Dev A)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

#: Numero E.164: `+`, codigo de pais y hasta 15 digitos en total.
E164 = r"^\+[1-9]\d{7,14}$"


class VoiceCallCreate(BaseModel):
    """Pedido de llamada saliente.

    Attributes:
        to: Numero a llamar, en E.164 (`+573001234567`).
    """

    to: str = Field(pattern=E164, description="Numero en E.164, con codigo de pais")


class VoiceCallAccepted(BaseModel):
    """Llamada saliente creada en Twilio.

    Attributes:
        call_sid: `CallSid` asignado por Twilio.
        status: Estado inicial (`queued`).
    """

    call_sid: str
    status: str


class CallRecordResponse(BaseModel):
    """Una llamada, para el CRM.

    Los telefonos van enmascarados (`mask_identifier`), como el `display_name`
    de un contacto: el agente reconoce el numero por los ultimos digitos sin
    que el valor completo salga de la base cifrada.

    Attributes:
        id: UUID del registro.
        call_sid: `CallSid` de Twilio.
        contact_id: Contacto, si se resolvio.
        conversation_id: Conversacion de la llamada, si la hubo.
        direction: `inbound` u `outbound`.
        status: Ultimo estado reportado por Twilio.
        phone_from: Origen, enmascarado.
        phone_to: Destino, enmascarado.
        started_at: Inicio.
        ended_at: Fin.
        duration_seconds: Duracion reportada por Twilio.
        transcript: Turnos de la llamada.
        recording_url: Grabacion, si la hay.
    """

    id: UUID
    call_sid: str
    contact_id: UUID | None
    conversation_id: UUID | None
    direction: str
    status: str
    phone_from: str | None
    phone_to: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int
    transcript: list[dict[str, Any]]
    recording_url: str | None


class CallRecordListResponse(BaseModel):
    """Pagina de llamadas.

    Attributes:
        items: Llamadas de la pagina, sin la transcripcion.
        total: Total del tenant (con el filtro aplicado).
        page: Pagina actual, desde 1.
        page_size: Tamano de pagina.
    """

    items: list[CallRecordResponse]
    total: int
    page: int
    page_size: int
