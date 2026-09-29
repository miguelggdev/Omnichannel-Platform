"""cie10_catalog y cups_catalog — Sprint 13: catalogos oficiales de codificacion.

El spec (§Notas tecnicas) pide que CIE-10 y CUPS vivan en tablas de referencia
"no por tenant — son catalogos universales". Por eso, a diferencia del resto
de las tablas, **no llevan `client_id` ni RLS**: son datos publicos y
compartidos (misma clase de excepcion a la Regla 1 que `tenant_templates`,
ADR-064). La aplicacion solo las lee; las carga
`scripts/load_clinical_catalogs.py` con el dataset oficial (CIE-10 de la
OMS/MinSalud, CUPS de la Resolucion 5171 de 2017), que no se versiona aca.

Mientras esten vacias, el agente usa el subconjunto de referencia en memoria
(`app/services/clinical_catalog.py`); en cuanto tienen filas, son la fuente de
verdad y un codigo que no este en ellas se rechaza.

`description_norm` es la descripcion en minusculas y sin tildes, calculada por
el cargador: la busqueda por palabras es un `ILIKE` sobre ~15 mil filas, sin
necesidad de `unaccent` ni de `tsvector`.

Revision ID: 017_clinical_catalogs
Revises: 016_clinical_records
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "017_clinical_catalogs"
down_revision: str | None = "016_clinical_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for tabla in ("cie10_catalog", "cups_catalog"):
        op.create_table(
            tabla,
            sa.Column("code", sa.String(length=10), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("description_norm", sa.Text(), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("code"),
        )


def downgrade() -> None:
    op.drop_table("cups_catalog")
    op.drop_table("cie10_catalog")
