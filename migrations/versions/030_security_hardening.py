"""Endurecimiento de seguridad tras la auditoria (ADR-088).

1. **Funciones `SECURITY DEFINER` sin `EXECUTE` para `PUBLIC`.** `auth_lookup_user` (devuelve el
   hash de la contrasena), `admin_list_clients`, `list_active_client_ids` y
   `capture_lookup_source` se crearon en `public` con `EXECUTE` para `PUBLIC`. En Supabase Cloud,
   `public` lo expone la API REST (PostgREST) y los roles `anon`/`authenticated` heredan ese
   permiso: con la clave publica del proyecto se podia llamar
   `POST /rest/v1/rpc/auth_lookup_user {"p_email": ...}` y leer el hash bcrypt de cualquier
   usuario. Ahora solo las ejecuta su dueno (el rol de las migraciones) y el rol de la app
   (`app_user`, o el que diga `APP_DB_ROLE`), si existe.
2. **`audit_trigger_function()` con `search_path` fijo.** Es `SECURITY DEFINER` e inserta en
   `audit_logs` sin calificar: un rol que pudiera crear objetos en un esquema anterior del
   `search_path` (o una tabla temporal) la desviaria. Se fija `pg_catalog, public, pg_temp`.
3. **`UNIQUE (client_id, month)` en `token_budgets`.** El presupuesto del mes se crea ahora bajo
   demanda (antes, sin fila, el consumo era ILIMITADO desde el segundo mes del tenant); el
   indice hace segura la carrera de dos mensajes creandola a la vez (`ON CONFLICT DO NOTHING`).
   Si hubiera filas repetidas, se conserva la de mas consumo.

Revision ID: 030_security_hardening
Revises: 029_deals_scheduled_calls
Create Date: 2026-10-10
"""

import os
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "030_security_hardening"
down_revision: str | None = "029_deals_scheduled_calls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Funciones `SECURITY DEFINER` que no deben poder llamarse desde fuera de la app.
FUNCIONES = (
    "public.auth_lookup_user(text)",
    "public.list_active_client_ids()",
    "public.admin_list_clients(text, boolean, integer, integer)",
    "public.capture_lookup_source(text)",
)
#: Roles de Supabase que heredan permisos de `PUBLIC` via PostgREST.
ROLES_API = ("anon", "authenticated")


def _rol_app() -> str:
    """El rol con el que se conecta la app (por defecto `app_user`, como en CI)."""
    rol = os.environ.get("APP_DB_ROLE", "app_user")
    if not rol.replace("_", "").isalnum():
        raise ValueError(f"APP_DB_ROLE no valido: {rol!r}")
    return rol


def upgrade() -> None:
    """Quita `EXECUTE` publico, fija el `search_path` del trigger de auditoria y el UNIQUE."""
    rol_app = _rol_app()
    for funcion in FUNCIONES:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {funcion} FROM PUBLIC")
        # Solo se interpolan constantes de este modulo y un rol validado por `_rol_app()`.
        for rol in ROLES_API:
            op.execute(f"""
                DO $$ BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{rol}') THEN
                        EXECUTE 'REVOKE EXECUTE ON FUNCTION {funcion} FROM {rol}';
                    END IF;
                END $$
            """)  # noqa: S608
        # Sin el rol, solo el dueno puede ejecutarlas: si la app conecta con otro rol, el login
        # deja de funcionar. Se avisa en vez de fallar (conectar como el dueno sigue valiendo).
        op.execute(f"""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{rol_app}') THEN
                    EXECUTE 'GRANT EXECUTE ON FUNCTION {funcion} TO {rol_app}';
                ELSE
                    RAISE WARNING 'El rol {rol_app} no existe: nadie salvo el dueno puede ejecutar {funcion}. Crea el rol y repite el GRANT EXECUTE (ADR-088).';
                END IF;
            END $$
        """)  # noqa: S608

    op.execute(
        "ALTER FUNCTION public.audit_trigger_function() "
        "SET search_path = pg_catalog, public, pg_temp"
    )

    op.execute("""
        DELETE FROM token_budgets t
        USING token_budgets o
        WHERE t.client_id = o.client_id AND t.month = o.month
          AND (t.used_tokens, t.id::text) < (o.used_tokens, o.id::text)
    """)
    op.create_index("uq_token_budget_month", "token_budgets", ["client_id", "month"], unique=True)


def downgrade() -> None:
    """Devuelve `EXECUTE` a `PUBLIC` y quita el `search_path` y el UNIQUE."""
    op.drop_index("uq_token_budget_month", table_name="token_budgets")
    op.execute("ALTER FUNCTION public.audit_trigger_function() RESET search_path")
    for funcion in FUNCIONES:
        op.execute(f"GRANT EXECUTE ON FUNCTION {funcion} TO PUBLIC")
