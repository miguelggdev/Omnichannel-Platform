"""Modelo Invoice — facturas electronicas emitidas por el tenant (Sprint 12).

La tabla no esta en el spec: su §1.2 escribe "Guardar en DB # ..." y hace que
`get_invoice_status()`/`list_invoices()` devuelvan datos inventados. Sin
persistencia, tres de las cuatro tools del agente financiero son una
maqueta — de ahi que esta entrega agregue la tabla (migracion 012).

Los importes van en centavos (`Integer`), no en `Float`: una factura es dinero
y el binario flotante no representa exactamente ni 0.1; con IVA del 19% sobre
varios items, el redondeo se ve en el total. El calculo se hace entero y la
presentacion divide por 100.

El identificador fiscal del comprador (`buyer_nit`) se guarda cifrado con
pgcrypto, igual que los identificadores de contacto desde la migracion 008: es
un dato personal de un tercero que la plataforma custodia por cuenta del
tenant.
"""

from datetime import datetime
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString
from app.models.base import TenantBaseModel

# Estados por los que pasa una factura.
INVOICE_DRAFT = "draft"
INVOICE_PENDING_DIAN = "pending_dian"
INVOICE_APPROVED = "approved"
INVOICE_REJECTED = "rejected"

INVOICE_STATUSES: tuple[str, ...] = (
    INVOICE_DRAFT,
    INVOICE_PENDING_DIAN,
    INVOICE_APPROVED,
    INVOICE_REJECTED,
)


class Invoice(TenantBaseModel):
    """Factura electronica emitida a un contacto.

    Attributes:
        contact_id: Comprador. NULL si el contacto se dio de baja: la factura
            es un documento fiscal y no se borra con el.
        conversation_id: Conversacion en la que se emitio, si nacio de un chat.
        invoice_number: Consecutivo del tenant (`FE-000001`), unico por tenant.
        buyer_nit: NIT o cedula del comprador, cifrado.
        buyer_name: Razon social o nombre del comprador.
        items: Lineas de la factura tal como se calcularon.
        subtotal_cents: Suma de las lineas, sin impuestos.
        tax_total_cents: Total de IVA.
        total_cents: `subtotal_cents + tax_total_cents`.
        currency: Moneda ISO-4217; COP por defecto.
        status: Uno de `INVOICE_STATUSES`.
        dian_cufe: CUFE que devuelve la DIAN al aprobarla.
        dian_response: Respuesta cruda de la DIAN, para auditoria.
        error_message: Motivo del rechazo o del fallo de envio.
        issued_at: Cuando se emitio.
        updated_at: Ultima modificacion.
    """

    __tablename__ = "invoices"
    __table_args__ = (
        Index("uq_invoices_number", "client_id", "invoice_number", unique=True),
        Index("idx_invoices_contact_status", "client_id", "contact_id", "status"),
    )

    contact_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=True, index=True
    )
    conversation_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=True
    )
    invoice_number: Mapped[str] = mapped_column(String(50), nullable=False)
    buyer_nit: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    buyer_name: Mapped[str] = mapped_column(String(300), nullable=False)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    subtotal_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    tax_total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), server_default="COP", nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default=INVOICE_DRAFT, nullable=False)
    dian_cufe: Mapped[str | None] = mapped_column(String(200), nullable=True)
    dian_response: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
