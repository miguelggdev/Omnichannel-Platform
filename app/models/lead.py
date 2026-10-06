"""Modelo Lead — entidad principal del modulo de leads (Sprint 16, ADR-082).

`email` y `phone` estan cifrados en la base con pgcrypto (columnas `BYTEA`), como los
identificadores de contacto (migraciones 008/009), y `email_hash`/`phone_hash` son sus
indices ciegos por tenant: lo que permite buscar por igualdad y deduplicar sobre un valor
cifrado. Los hashes no los escribe nadie a mano: los deriva un listener antes de cada INSERT y
UPDATE (mismo motivo que en `ContactIdentifier`: un valor cambiado sin recalcular el hash no
falla, solo deja una fila que ya no se encuentra).

`total_score` es una columna normal y no `GENERATED`: los pesos de FIT/comportamiento/IA son
configurables por tenant (`clients.lead_scoring_weights`) y una columna generada no puede leer
otra tabla. La calcula `app.services.lead_scoring.compute_total_score()`.
"""

import re
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    desc,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString, blind_index
from app.models.base import TenantBaseModel

LEAD_STATUSES: tuple[str, ...] = ("active", "won", "lost", "disqualified")
LEAD_TEMPERATURES: tuple[str, ...] = ("cold", "warm", "hot")

_NO_DIGITOS = re.compile(r"\D")
_ACTIVO = "status = 'active' AND deleted_at IS NULL"


def normalizar_telefono_de_lead(valor: str | None) -> str | None:
    """Telefono canonico para el indice ciego: solo digitos.

    `+57 300 111-2233` y `573001112233` son el mismo numero. Es mas laxo que
    `phone_unification.normalizar_telefono` (que devuelve `None` ante la duda porque fusionar
    contactos erroneamente es peor que no fusionar): aqui un formulario web trae numeros
    locales y lo peor que pasa con un falso duplicado es avisar de que ya existe.

    Args:
        valor: Telefono tal como lo escribio quien lo capturo.

    Returns:
        Los digitos, o el valor recortado si no tiene ninguno (el hash no puede ser vacio), o
        `None` si no hay valor.
    """
    if valor is None or not valor.strip():
        return None
    return _NO_DIGITOS.sub("", valor) or valor.strip().lower()


class Lead(TenantBaseModel):
    """Un lead del tenant.

    Attributes:
        contact_id: Contacto vinculado (sincronizacion lead <-> contacto).
        source_id: Fuente por la que llego.
        assigned_user_id: Usuario responsable.
        pipeline_stage_id: Etapa actual del pipeline.
        email: Email, cifrado en la base.
        email_hash: Indice ciego del email (por tenant).
        phone: Telefono, cifrado en la base.
        phone_hash: Indice ciego del telefono (digitos, por tenant).
        fit_score: Encaje con el ICP, 0-100.
        behavioral_score: Interaccion, 0-100.
        ai_score: Valoracion de la IA, 0-100.
        total_score: Combinacion ponderada de las tres, 0-100.
        status: `active`, `won`, `lost` o `disqualified`.
        temperature: `cold`, `warm` o `hot`.
        deleted_at: Soft delete; un lead borrado no cuenta para la deduplicacion.
    """

    __tablename__ = "leads"
    __table_args__ = (
        CheckConstraint("fit_score BETWEEN 0 AND 100", name="ck_leads_fit_score"),
        CheckConstraint("behavioral_score BETWEEN 0 AND 100", name="ck_leads_behavioral_score"),
        CheckConstraint("ai_score BETWEEN 0 AND 100", name="ck_leads_ai_score"),
        CheckConstraint("total_score BETWEEN 0 AND 100", name="ck_leads_total_score"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in LEAD_STATUSES) + ")", name="ck_leads_status"
        ),
        CheckConstraint(
            "temperature IN (" + ", ".join(f"'{t}'" for t in LEAD_TEMPERATURES) + ")",
            name="ck_leads_temperature",
        ),
        CheckConstraint("estimated_value IS NULL OR estimated_value >= 0", name="ck_leads_value"),
        Index("ix_leads_client_stage", "client_id", "pipeline_stage_id"),
        Index("ix_leads_client_source", "client_id", "source_id"),
        Index("ix_leads_client_score", "client_id", desc("total_score")),
        Index(
            "ix_leads_client_next_followup",
            "client_id",
            "next_follow_up_at",
            postgresql_where=text(_ACTIVO),
        ),
        Index("ix_leads_assigned", "assigned_user_id", "client_id", postgresql_where=text(_ACTIVO)),
        Index(
            "uq_leads_client_email_hash",
            "client_id",
            "email_hash",
            unique=True,
            postgresql_where=text("email_hash IS NOT NULL AND deleted_at IS NULL"),
        ),
        Index(
            "uq_leads_client_phone_hash",
            "client_id",
            "phone_hash",
            unique=True,
            postgresql_where=text("phone_hash IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    contact_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lead_sources.id", ondelete="SET NULL"), nullable=True
    )
    assigned_user_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    pipeline_stage_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lead_pipeline_stages.id"), nullable=True
    )

    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    email: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    email_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    company_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_size: Mapped[str | None] = mapped_column(String(50), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)
    job_title: Mapped[str | None] = mapped_column(String(150), nullable=True)

    fit_score: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    behavioral_score: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    ai_score: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    total_score: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)

    status: Mapped[str] = mapped_column(String(20), server_default="active", nullable=False)
    temperature: Mapped[str] = mapped_column(String(10), server_default="cold", nullable=False)
    estimated_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), server_default="USD", nullable=False)

    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disqualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disqualified_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    enrichment_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default="{}", nullable=False
    )
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


@event.listens_for(Lead, "before_insert")
@event.listens_for(Lead, "before_update")
def _sincronizar_indices_ciegos(_mapper: Any, _connection: Any, target: Lead) -> None:
    """Recalcula `email_hash` y `phone_hash` a partir de los valores antes de escribir.

    Cubre el ORM, que es por donde pasa todo el codigo de la aplicacion. Un `UPDATE` masivo con
    `sqlalchemy.update()` **no** dispara este evento: si alguna vez hace falta uno sobre estas
    columnas, tiene que escribir el hash en el mismo `.values()`.

    Args:
        _mapper: Mapper de SQLAlchemy (no se usa).
        _connection: Conexion en curso (no se usa).
        target: Fila que esta a punto de escribirse.
    """
    target.email_hash = blind_index(target.email, target.client_id) if target.email else None
    telefono = normalizar_telefono_de_lead(target.phone)
    target.phone_hash = blind_index(telefono, target.client_id) if telefono else None
