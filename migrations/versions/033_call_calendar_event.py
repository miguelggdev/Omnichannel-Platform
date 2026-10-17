"""`scheduled_calls.calendar_event_id`: el evento de Google Calendar de una llamada (Sprint 19).

Al agendar una llamada humana se crea un evento en el calendario del tenant; para moverlo o
borrarlo al reprogramar o cancelar hay que saber cual es. El id lo asigna Google y no es un dato
personal. `NULL` = la llamada no tiene evento (calendario sin configurar, o Google no respondio).

Revision ID: 033_call_calendar_event
Revises: 032_lead_automation_config
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "033_call_calendar_event"
down_revision: str | None = "032_lead_automation_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Anade la columna (`NULL` para las llamadas existentes)."""
    op.add_column(
        "scheduled_calls", sa.Column("calendar_event_id", sa.String(200), nullable=True)
    )


def downgrade() -> None:
    """Quita la columna."""
    op.drop_column("scheduled_calls", "calendar_event_id")
