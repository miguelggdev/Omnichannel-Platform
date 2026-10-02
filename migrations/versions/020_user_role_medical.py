"""Rol de usuario `medical` — Sprint 13: quien firma la historia clinica.

El criterio 14 del sprint pide que solo el personal medico acceda a los datos
clinicos. El spec lo resuelve con una segunda politica RLS sobre
`clinical_records` que lee `app.current_user_role`; esa politica no se creo
(ADR-071, ADR-072) y no se crea aqui: las politicas permisivas de PostgreSQL se
combinan con OR, asi que junto a `tenant_isolation` no restringiria nada, y esa
variable de sesion no la fija nadie.

El control vive entonces en la API (`app/api/v1/clinical.py`), que es donde hay
un usuario autenticado con un rol. Para eso hace falta el rol: el enum
`user_role` de la migracion 001 solo tiene `super_admin`, `admin`, `supervisor`
y `agent`, y ninguno sirve. Mientras no exista, **firmar un registro clinico
exige ser administrador del tenant** — ADR-072 lo dejo anotado como decision de
producto pendiente. Las dos alternativas sin este rol son peores: dar los datos
de salud a `supervisor` se los da tambien a quien supervisa la atencion al
cliente, y reservarlos a `admin` obliga a que cada medico administre el tenant.

`ALTER TYPE ... ADD VALUE` **no se puede revertir**: PostgreSQL no admite quitar
un valor de un enum. El `downgrade()` comprueba que no quede ningun usuario con
el rol y deja el valor en el tipo; recrear el enum sin el valor exigiria
reescribir la columna de `users` y todo lo que dependa de ella, que es mucho mas
riesgoso que dejar un valor sin usar.

Revision ID: 020_user_role_medical
Revises: 019_encrypt_call_transcript
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "020_user_role_medical"
down_revision: str | None = "019_encrypt_call_transcript"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Agrega el valor `medical` al enum `user_role`.

    `IF NOT EXISTS` hace la migracion idempotente. Desde PostgreSQL 12 esta
    sentencia corre dentro de una transaccion mientras el valor nuevo no se use
    en la misma transaccion, que es el caso: aqui solo se declara.
    """
    op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'medical'")


def downgrade() -> None:
    """Comprueba que el rol no este en uso; el valor del enum no se quita.

    Raises:
        RuntimeError: Si algun usuario todavia tiene el rol `medical`. Bajar la
            migracion con usuarios asignados los dejaria con un rol que la
            aplicacion ya no reconoce, y sin acceso a nada.
    """
    en_uso = (
        op.get_bind().exec_driver_sql("SELECT count(*) FROM users WHERE role = 'medical'").scalar()
    )
    if en_uso:
        raise RuntimeError(
            f"Hay {en_uso} usuario(s) con rol 'medical'. Cambieles el rol antes de bajar "
            "esta migracion."
        )
