"""invoices: importes en BIGINT — Sprint 12 (BUG-044).

La migracion 013 dejo `subtotal_cents`, `tax_total_cents` y `total_cents`
como `INTEGER`. En centavos, el maximo de un `INTEGER` (2.147.483.647) son
unos 21,5 millones de pesos: una factura B2B por encima de eso fallaba al
insertarse con `NumericValueOutOfRange`. `BIGINT` llega a ~92 billones de
pesos, que ninguna factura real alcanza.

Solo ensancha el tipo: los valores existentes caben y la `CHECK` de que el
total cuadra (`ck_invoices_total`) sigue igual.

Revision ID: 014_invoice_amounts_bigint
Revises: 013_invoices_campaigns
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "014_invoice_amounts_bigint"
down_revision: str | None = "013_invoices_campaigns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNAS: tuple[str, ...] = ("subtotal_cents", "tax_total_cents", "total_cents")


def upgrade() -> None:
    for columna in COLUMNAS:
        op.alter_column(
            "invoices",
            columna,
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )


def downgrade() -> None:
    # Falla si alguna factura ya supera el rango de INTEGER: es preferible a
    # truncar importes en silencio.
    for columna in COLUMNAS:
        op.alter_column(
            "invoices",
            columna,
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
