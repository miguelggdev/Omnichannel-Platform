"""Schemas de Campaign — CRUD de campanas de marketing (Sprint 12, Dev B)."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.campaign import CAMPAIGN_STATUSES
from app.services.campaigns import CANALES_VALIDOS

__all__ = ["CANALES_VALIDOS"]


class CampaignCreate(BaseModel):
    """Alta de una campana, siempre en estado `draft`.

    Attributes:
        name: Nombre con el que el tenant la reconoce.
        channel: Canal de envio.
        segment_criteria: Criterios de segmentacion (ver `services/segmentation.py`).
        message_template: Texto con variables `{{contact_name}}`, `{{first_name}}`.
        scheduled_for: Momento para el que se programa, opcional.
    """

    name: str = Field(min_length=1, max_length=200)
    channel: str = Field(min_length=1, max_length=50)
    segment_criteria: dict[str, Any]
    message_template: str = Field(min_length=1)
    scheduled_for: datetime | None = None

    @field_validator("scheduled_for")
    @classmethod
    def _en_utc(cls, valor: datetime | None) -> datetime | None:
        """Una fecha sin zona horaria se interpreta en UTC.

        Mismo criterio que `marketing_tools.create_campaign()`. Sin esto, una
        fecha naive no se puede comparar con `datetime.now(timezone.utc)` en
        `debe_salir_ya()` (TypeError) y su significado dependeria del driver.

        Args:
            valor: Fecha recibida, opcional.

        Returns:
            La misma fecha, con zona horaria.
        """
        if valor is not None and valor.tzinfo is None:
            return valor.replace(tzinfo=timezone.utc)
        return valor


class CampaignResponse(BaseModel):
    """Campana con sus contadores.

    Attributes:
        id: Identificador de la campana.
        name: Nombre de la campana.
        channel: Canal de envio.
        segment_criteria: Criterios de segmentacion.
        message_template: Plantilla del mensaje.
        status: Estado actual.
        target_count: Contactos que entraban en el segmento al crearla.
        delivered_count: Mensajes entregados.
        read_count: Mensajes leidos.
        replied_count: Contactos que respondieron.
        failed_count: Envios fallidos.
        scheduled_for: Momento programado, si lo tiene.
        started_at: Cuando arranco el envio.
        completed_at: Cuando termino.
        error_log: Errores acotados del envio.
        created_at: Alta de la campana.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    channel: str
    segment_criteria: dict[str, Any]
    message_template: str
    status: str
    target_count: int
    delivered_count: int
    read_count: int
    replied_count: int
    failed_count: int
    scheduled_for: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error_log: list[dict[str, Any]]
    created_at: datetime


class CampaignListResponse(BaseModel):
    """Pagina de campanas.

    Attributes:
        items: Campanas de la pagina.
        total: Total de campanas que cumplen el filtro.
        page: Pagina actual, desde 1.
        page_size: Tamano de pagina.
    """

    items: list[CampaignResponse]
    total: int
    page: int
    page_size: int


class CampaignSendResponse(BaseModel):
    """Acuse de que la campana quedo confirmada.

    Attributes:
        campaign_id: Campana confirmada.
        status: Estado en el que quedo.
        target_count: Contactos a los que se le va a enviar.
        scheduled_for: Cuando sale. Si ya paso, quedo encolada en el acto; si
            es futura, la encola Beat al llegar la hora.
    """

    campaign_id: UUID
    status: str
    target_count: int
    scheduled_for: datetime


def estado_invalido(status: str) -> bool:
    """Si `status` no es uno de los estados que conoce el modelo.

    Args:
        status: Estado recibido como filtro.

    Returns:
        `True` si no se puede usar para filtrar.
    """
    return status not in CAMPAIGN_STATUSES
