"""Modelo VoicePin — PIN por DTMF que autentica una llamada (Sprint 13, ADR-073).

El caller ID de una llamada se puede falsificar, asi que por si solo no
autoriza a los agentes que operan sobre datos de otras personas (ADR-072). El
profesional demuestra quien es tecleando un PIN durante la llamada: dos
factores, el numero registrado (algo que tiene) y el PIN (algo que sabe).

Cada fila liga **un numero** a **un contacto autorizado** (el que el tenant
declaro profesional u operador de marketing, normalmente el de WhatsApp: el
contacto de voz es otro, porque el caller ID no unifica contactos). Guarda:

- `phone_hash`: indice ciego del numero normalizado. No el numero, que no hace
  falta para buscar la fila.
- `pin_hash`: bcrypt del PIN **ya mezclado** con un HMAC de `ENCRYPTION_KEY`
  (`pin_auth.preparar_pin`). Un PIN tiene 10^6 combinaciones: un volcado de la
  base sin la clave del servidor no basta para probarlas offline.
- `failed_attempts` y `locked_until`: bloqueo tras cinco fallos seguidos.

No lleva trigger de auditoria: `to_jsonb(NEW)` copiaria el hash a `audit_logs`.
"""

from datetime import datetime
from uuid import UUID as _UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel


class VoicePin(TenantBaseModel):
    """PIN de voz de un contacto autorizado, ligado a un numero de telefono.

    Attributes:
        contact_id: Contacto al que autentica el PIN.
        phone_hash: Indice ciego (por tenant) del numero registrado.
        pin_hash: bcrypt del PIN mezclado con el HMAC del servidor.
        failed_attempts: Fallos seguidos desde el ultimo acierto.
        locked_until: Hasta cuando no se aceptan intentos, si esta bloqueado.
        updated_at: Ultima modificacion.
    """

    __tablename__ = "voice_pins"
    __table_args__ = (
        Index("uq_voice_pins_phone", "client_id", "phone_hash", unique=True),
        Index("idx_voice_pins_contact", "client_id", "contact_id"),
    )

    contact_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pin_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
