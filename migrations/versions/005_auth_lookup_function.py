"""auth_lookup_function — BUG-017: el login no funciona bajo RLS.

El problema
-----------
`app/api/v1/auth.py::login` tiene que encontrar al usuario por email **sin**
contexto de tenant, y no puede tenerlo: el tenant se deduce del usuario, y el
usuario es justo lo que todavia no se conoce. Pero la politica de `users`
(migracion 002) filtra por `client_id = current_setting('app.current_client_id')`,
asi que contra un rol con `NOBYPASSRLS` esa consulta no devuelve nada — revienta.
Lo mismo le pasa a la comprobacion de `clients` y al `UPDATE` de `last_login_at`.

La solucion
-----------
Una funcion `SECURITY DEFINER` que es el **unico** punto del sistema con acceso
sin contexto de tenant, y que solo puede hacer una cosa: dado un email exacto,
devolver los datos que el login necesita de ese usuario. No lista usuarios, no
acepta patrones, no devuelve nada de otras tablas.

El resto de la RLS se queda intacta: nada de `NO FORCE`, ninguna politica nueva
sobre `users`, ningun rol con `BYPASSRLS` para la aplicacion. La excepcion queda
acotada a una firma concreta y auditable, que es la razon de elegir esta salida
frente a las otras dos que planteaba BUG-017.

Por que hace falta que el dueno tenga BYPASSRLS
-----------------------------------------------
`SECURITY DEFINER` hace que el cuerpo corra con los privilegios del dueno de la
funcion, pero `FORCE ROW LEVEL SECURITY` (CLAUDE.md, regla 1) aplica las
politicas **tambien al dueno de la tabla**. Asi que la funcion solo esquiva la
RLS si su dueno tiene el atributo `BYPASSRLS` (o es superusuario): el rol con el
que corren las migraciones, no el de la aplicacion.

Eso es una precondicion implicita, y una precondicion implicita que falla en
silencio es justo lo que produjo BUG-017: la funcion devolveria cero filas y el
login diria "credenciales invalidas" para todo el mundo, sin un solo error en
los logs. Por eso la migracion la comprueba y **aborta el despliegue** si no se
cumple, en vez de dejar que se descubra en el primer login.

Revision ID: 005_auth_lookup_function
Revises: 004_audit_gdpr_quick_replies
Create Date: 2026-09-17
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005_auth_lookup_function"
down_revision: Union[str, None] = "004_audit_gdpr_quick_replies"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── Precondicion: el dueno de la funcion tiene que poder saltarse la RLS ──
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles
                WHERE rolname = current_user
                  AND (rolbypassrls OR rolsuper)
            ) THEN
                RAISE EXCEPTION
                    'El rol % no tiene BYPASSRLS ni es superusuario. auth_lookup_user() '
                    'quedaria sujeta a la politica de RLS de users y el login devolveria '
                    '"credenciales invalidas" para todos los usuarios, sin ningun error '
                    'visible. Ejecuta las migraciones con el rol propietario del schema '
                    '(DATABASE_URL_DIRECT), no con el rol de la aplicacion.',
                    current_user;
            END IF;
        END $$;
    """)

    # ─── La funcion ───────────────────────────────────────────────────────────
    # - STABLE: solo lee.
    # - SET search_path: obligatorio en SECURITY DEFINER. Sin el, quien llama
    #   puede anteponer un schema propio con una tabla `users` falsa y hacer que
    #   la funcion privilegiada lea lo que el atacante quiera. `pg_temp` va al
    #   final justamente para que los objetos temporales no puedan tapar nada.
    # - Los nombres van calificados con `public.` ademas del search_path fijo.
    # - El WHERE es por igualdad exacta sobre `email`, que es UNIQUE: la funcion
    #   no puede usarse para enumerar usuarios ni para buscar por patron.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.auth_lookup_user(p_email text)
        RETURNS TABLE (
            id uuid,
            client_id uuid,
            email varchar,
            password_hash varchar,
            first_name varchar,
            last_name varchar,
            role text,
            is_active boolean,
            client_is_active boolean
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public, pg_temp
        AS $$
            SELECT
                u.id,
                u.client_id,
                u.email,
                u.password_hash,
                u.first_name,
                u.last_name,
                u.role::text,
                u.is_active,
                c.is_active
            FROM public.users u
            JOIN public.clients c ON c.id = u.client_id
            WHERE u.email = p_email
        $$;
    """)

    op.execute("""
        COMMENT ON FUNCTION public.auth_lookup_user(text) IS
        'BUG-017: unico acceso a users/clients sin contexto de tenant. Lo usa '
        'exclusivamente POST /api/v1/auth/login, que no puede conocer el tenant '
        'antes de identificar al usuario. Devuelve solo la fila del email exacto.';
    """)

    # EXECUTE se queda en el default de PostgreSQL (PUBLIC). No se restringe a un
    # rol concreto porque el provisioning crea el rol de la aplicacion DESPUES de
    # correr las migraciones (en CI y en Supabase), asi que un GRANT nominal aqui
    # fallaria. Acotarlo al rol de la aplicacion es una mejora del script que crea
    # el rol — misma nota que el REVOKE de audit_logs, anotada en PROGRESS.md.
    #
    # El alcance que PUBLIC concede es estrecho a proposito: quien pueda conectar
    # ya tiene SELECT sobre `users`, y lo unico que suma la funcion es resolver un
    # email exacto. No permite listar ni recorrer la tabla.


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.auth_lookup_user(text)")
