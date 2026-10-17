"""Calificacion automatica, asignacion y respuestas de los endpoints de scoring (Sprint 17).

`QualificationConfig` y `AssignmentConfig` son lo que se guarda en
`clients.lead_automation_config` (migracion 032) y lo que valida la API de configuracion. Ambos
con `extra="forbid"`: una clave mal escrita es un error, no una regla que no hace nada.
"""

import re
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.lead_scoring import IcpConfig

SLUG = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")]
MAX_REGLAS = 10
MAX_USUARIOS = 100


class QualificationRule(BaseModel):
    """Si el score total llega a `min_score`, el lead pasa a la etapa `stage_slug`.

    Attributes:
        min_score: Score total minimo (1-100) para aplicar la regla.
        stage_slug: Etapa de destino (debe existir en el tenant y no ser terminal).
    """

    model_config = ConfigDict(extra="forbid")

    min_score: int = Field(ge=1, le=100)
    stage_slug: SLUG


class QualificationConfig(BaseModel):
    """Calificacion automatica por umbrales de score.

    Reglas de aplicacion (`app.services.lead_qualification`): solo mueve hacia delante, solo
    leads activos, solo a etapas no terminales y nunca si una persona movio la etapa a mano.

    Attributes:
        enabled: Si esta activa (apagada por defecto).
        rules: Umbrales; se aplica el mas alto que el lead alcance.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    rules: list[QualificationRule] = Field(default_factory=list, max_length=MAX_REGLAS)

    @model_validator(mode="after")
    def _sin_repetidos(self) -> "QualificationConfig":
        """Dos reglas con el mismo umbral serian ambiguas."""
        umbrales = [r.min_score for r in self.rules]
        if len(set(umbrales)) != len(umbrales):
            raise ValueError("Hay dos reglas con el mismo umbral")
        if self.enabled and not self.rules:
            raise ValueError("Activa la calificacion automatica solo si hay al menos una regla")
        return self


class AssignmentConfig(BaseModel):
    """Asignacion automatica round-robin de leads nuevos.

    Attributes:
        auto_assign: Si los leads nuevos sin responsable se asignan solos (apagado por defecto).
        user_ids: Usuarios entre los que rotar; `None` = todos los agentes y supervisores activos.
    """

    model_config = ConfigDict(extra="forbid")

    auto_assign: bool = False
    user_ids: list[UUID] | None = Field(default=None, max_length=MAX_USUARIOS)

    @field_validator("user_ids")
    @classmethod
    def _sin_repetidos(cls, v: list[UUID] | None) -> list[UUID] | None:
        """Un usuario repetido recibiria el doble de leads."""
        if v is not None and len(set(v)) != len(v):
            raise ValueError("Hay usuarios repetidos")
        return v


_CHAT_TELEGRAM = re.compile(r"^(-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")
_MONEDA = re.compile(r"^[A-Z]{3}$")
MAX_UMBRALES = 10
MAX_UMBRAL = Decimal("9999999999.99")


class DealAlertsConfig(BaseModel):
    """Alerta por Telegram cuando se gana un deal grande.

    El umbral es **por moneda**: 5.000 USD y 5.000 COP no se comparan. Una moneda sin umbral no
    alerta. El aviso sale por el bot de canal de la plataforma (`TELEGRAM_CHANNEL_BOT_TOKEN`) al
    chat del tenant; el bot debe estar en ese chat.

    Attributes:
        enabled: Si esta activa (apagada por defecto).
        telegram_chat_id: Id numerico del chat o grupo (`-100123...`) o `@canal`.
        thresholds: Moneda ISO -> valor minimo (inclusive) del deal ganado.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    telegram_chat_id: str | None = Field(default=None, max_length=40)
    thresholds: dict[str, Decimal] = Field(default_factory=dict, max_length=MAX_UMBRALES)

    @field_validator("telegram_chat_id")
    @classmethod
    def _chat_valido(cls, v: str | None) -> str | None:
        """Un id numerico o un `@canal`."""
        if v is not None and not _CHAT_TELEGRAM.match(v):
            raise ValueError("telegram_chat_id debe ser un id numerico o un @canal")
        return v

    @field_validator("thresholds")
    @classmethod
    def _umbrales_validos(cls, v: dict[str, Decimal]) -> dict[str, Decimal]:
        """Monedas ISO en mayusculas y umbrales positivos."""
        for moneda, umbral in v.items():
            if not _MONEDA.match(moneda):
                raise ValueError(f"Moneda no valida: {moneda!r} (ISO 4217 en mayusculas)")
            if not Decimal(0) < umbral <= MAX_UMBRAL:
                raise ValueError("Los umbrales deben ser mayores que 0")
        return v

    @model_validator(mode="after")
    def _completa(self) -> "DealAlertsConfig":
        """Activarla exige chat y al menos un umbral."""
        if self.enabled and (not self.telegram_chat_id or not self.thresholds):
            raise ValueError("Para activar la alerta indica telegram_chat_id y algun umbral")
        return self


class CallRemindersConfig(BaseModel):
    """Recordatorios de llamadas agendadas.

    Attributes:
        whatsapp_template: Plantilla de WhatsApp aprobada por Meta para avisar a un lead que no
            escribio en las ultimas 24 h (sin ella, el recordatorio sale por email).
    """

    model_config = ConfigDict(extra="forbid")

    whatsapp_template: str | None = Field(default=None, min_length=1, max_length=512)


class LeadAutomationConfig(BaseModel):
    """Configuracion completa de automatizacion de leads del tenant."""

    model_config = ConfigDict(extra="forbid")

    qualification: QualificationConfig = Field(default_factory=QualificationConfig)
    assignment: AssignmentConfig = Field(default_factory=AssignmentConfig)
    deal_alerts: DealAlertsConfig = Field(default_factory=DealAlertsConfig)
    call_reminders: CallRemindersConfig = Field(default_factory=CallRemindersConfig)


class EnrichResponse(BaseModel):
    """Respuesta de `POST /leads/{id}/enrich` (el trabajo se encola)."""

    lead_id: UUID
    status: Literal["queued"] = "queued"


class ScoreRecalcRequest(BaseModel):
    """Cuerpo de `POST /leads/{id}/score/recalc`.

    Attributes:
        include_ai: Calcular tambien el AI score (gasta tokens del tenant).
    """

    model_config = ConfigDict(extra="forbid")

    include_ai: bool = False


class ScoreRecalcResponse(BaseModel):
    """Que scores se recalcularon y como quedo el lead.

    Attributes:
        recalculated: Tipos calculados (`fit`, `behavioral`, `ai`).
        skipped: Tipo -> motivo, para los que no se pudieron calcular.
        fit_score: Valores vigentes tras el recalculo.
        behavioral_score: Idem.
        ai_score: Idem.
        total_score: Idem.
    """

    lead_id: UUID
    recalculated: list[str]
    skipped: dict[str, str]
    fit_score: int | None
    behavioral_score: int | None
    ai_score: int | None
    total_score: int | None


class IcpUpdateResponse(BaseModel):
    """Respuesta de `PUT /clients/icp`.

    Attributes:
        icp: El ICP guardado.
        recalculation: `queued` si se encolo el recalculo del FIT de los leads activos;
            `not_needed` si el ICP no cambio; `unavailable` si el ICP se guardo pero no se pudo
            encolar (los scores se actualizaran con el siguiente recalculo de cada lead).
    """

    icp: IcpConfig
    recalculation: Literal["queued", "not_needed", "unavailable"]


class AssignRequest(BaseModel):
    """Cuerpo de `POST /leads/{id}/assign`: un usuario concreto o `round_robin`.

    Attributes:
        user_id: Usuario al que asignar; `None` con `round_robin`.
        strategy: `user` (el de `user_id`) o `round_robin`.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: UUID | None = None
    strategy: Literal["user", "round_robin"] = "user"

    @model_validator(mode="after")
    def _coherente(self) -> "AssignRequest":
        """`user` exige `user_id`; `round_robin` no lo admite."""
        if self.strategy == "user" and self.user_id is None:
            raise ValueError("Indica user_id o usa strategy=round_robin")
        if self.strategy == "round_robin" and self.user_id is not None:
            raise ValueError("round_robin no admite user_id")
        return self
