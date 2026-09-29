"""Catalogos de referencia CIE-10 y CUPS (Sprint 13).

Tablas globales, sin `client_id` ni RLS: son catalogos publicos compartidos
por todos los tenants (ver la migracion 018). Solo la aplicacion las lee; las
carga `scripts/load_clinical_catalogs.py`.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Cie10Catalog(Base):
    """Diagnosticos CIE-10.

    Attributes:
        code: Codigo (`J06.9`).
        description: Descripcion oficial.
        description_norm: Descripcion en minusculas y sin tildes, para buscar.
        updated_at: Ultima carga de la fila.
    """

    __tablename__ = "cie10_catalog"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    description_norm: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CupsCatalog(Base):
    """Procedimientos CUPS.

    Attributes:
        code: Codigo de seis digitos.
        description: Descripcion oficial.
        description_norm: Descripcion en minusculas y sin tildes, para buscar.
        updated_at: Ultima carga de la fila.
    """

    __tablename__ = "cups_catalog"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    description_norm: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
