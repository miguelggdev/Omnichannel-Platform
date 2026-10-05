"""Vision general de seguridad de la plataforma — solo `super_admin` (Sprint 15, cierre).

GET /api/v1/platform/security

Todo se mide, nada se declara: RLS sale del catalogo de PostgreSQL (no de una lista a mano, asi
una tabla nueva sin politica aparece sola) y la configuracion de `Settings`.

Reglas:

- **Nunca se devuelve un secreto ni su longitud**: solo si cumple o no. Este panel lo ve un
  `super_admin`, pero sus respuestas acaban en capturas, tickets y logs del navegador.
- **El rol de base de datos importa tanto como las politicas.** Un rol `superuser` o `BYPASSRLS`
  se salta RLS entera aunque cada tabla la tenga forzada; es el fallo que RLS no protege.
- Las tablas sin `client_id` (checkpoints de LangGraph, `alembic_version`) no entran: no son
  datos de un tenant.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.database import AsyncSessionLocal
from app.core.dependencies import require_role
from app.schemas.platform import RlsTableStatus, SecurityCheck, SecurityOverview

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("super_admin",)

OK, WARN, FAIL = "ok", "warn", "fail"
_JWT_MAX_MINUTES = 60

_TABLAS_CON_TENANT = text(
    """
    SELECT c.relname AS name, c.relrowsecurity AS rls, c.relforcerowsecurity AS forced
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r'
      AND (c.relname = 'clients' OR EXISTS (
            SELECT 1 FROM information_schema.columns col
            WHERE col.table_schema = 'public' AND col.table_name = c.relname
              AND col.column_name = 'client_id'))
    ORDER BY c.relname
    """
)
_ROL_ACTUAL = text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")


def comprobar_configuracion(ajustes: Settings) -> list[SecurityCheck]:
    """Evalua la postura de seguridad de la configuracion.

    Args:
        ajustes: Configuracion de la aplicacion.

    Returns:
        Una comprobacion por tema; ninguna lleva valores secretos.
    """
    produccion = ajustes.APP_ENV == "production"
    cors = [o.strip() for o in ajustes.CORS_ORIGINS]
    cors_mal = "*" in cors
    cors_local = any("localhost" in o or "127.0.0.1" in o for o in cors)
    secretos_webhook = {
        "YCLOUD_WEBHOOK_SECRET": ajustes.YCLOUD_WEBHOOK_SECRET,
        "META_APP_SECRET": ajustes.META_APP_SECRET,
        "TELEGRAM_WEBHOOK_SECRET": ajustes.TELEGRAM_WEBHOOK_SECRET,
        "EMAIL_INBOUND_WEBHOOK_SECRET": ajustes.EMAIL_INBOUND_WEBHOOK_SECRET,
    }
    sin_secreto = sorted(k for k, v in secretos_webhook.items() if not v)
    smtp_ok = bool(ajustes.EMAIL_SMTP_HOST and ajustes.EMAIL_FROM_ADDRESS)

    checks = [
        SecurityCheck(id="environment", status=OK if produccion else WARN, detail=ajustes.APP_ENV),
        SecurityCheck(
            id="jwt_lifetime",
            status=OK if ajustes.JWT_EXPIRATION_MINUTES <= _JWT_MAX_MINUTES else WARN,
            detail=f"{ajustes.JWT_EXPIRATION_MINUTES} min",
        ),
        SecurityCheck(
            id="cors",
            status=FAIL if cors_mal else (WARN if produccion and cors_local else OK),
            detail=f"{len(cors)} origen(es)",
        ),
        SecurityCheck(
            id="webhook_secrets",
            status=OK if not sin_secreto else WARN,
            # Los nombres de las variables no son secretos y dicen que falta configurar.
            detail=", ".join(sin_secreto) if sin_secreto else None,
        ),
    ]
    if ajustes.ONBOARDING_ENABLED:
        checks.append(
            SecurityCheck(
                id="onboarding_proxy_hops",
                # Detras de un proxy, 0 hace que todos compartan la IP del proxy y el
                # limite por IP corte a todo el mundo o a nadie.
                status=WARN if ajustes.TRUSTED_PROXY_HOPS == 0 else OK,
                detail=f"TRUSTED_PROXY_HOPS={ajustes.TRUSTED_PROXY_HOPS}",
            )
        )
        checks.append(
            SecurityCheck(id="onboarding_smtp", status=OK if smtp_ok else WARN, detail=None)
        )
    else:
        checks.append(SecurityCheck(id="onboarding_disabled", status=OK, detail=None))
    return checks


def comprobar_rol(fila: Any) -> SecurityCheck:
    """Evalua si el rol de la aplicacion queda sujeto a RLS.

    Args:
        fila: `rolsuper` y `rolbypassrls` del rol actual, o `None` si no se pudo leer.

    Returns:
        `fail` si el rol salta RLS; `warn` si no se pudo comprobar.
    """
    if fila is None:
        return SecurityCheck(id="db_role_rls", status=WARN, detail="no se pudo leer el rol")
    if fila.rolsuper or fila.rolbypassrls:
        motivo = "superuser" if fila.rolsuper else "BYPASSRLS"
        return SecurityCheck(id="db_role_rls", status=FAIL, detail=motivo)
    return SecurityCheck(id="db_role_rls", status=OK, detail=None)


@router.get("/security", response_model=SecurityOverview)
async def security_overview(
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> SecurityOverview:
    """Postura de seguridad: RLS por tabla, rol de la base y configuracion.

    Args:
        user: Usuario autenticado; solo super_admin.

    Returns:
        Las comprobaciones, el estado de RLS de cada tabla con `client_id` y las que no
        estan protegidas.
    """
    async with AsyncSessionLocal() as session:
        tablas = (await session.execute(_TABLAS_CON_TENANT)).all()
        rol = (await session.execute(_ROL_ACTUAL)).one_or_none()

    rls_tables = [
        RlsTableStatus(name=t.name, rls_enabled=t.rls, rls_forced=t.forced) for t in tablas
    ]
    sin_proteger = [t.name for t in rls_tables if not (t.rls_enabled and t.rls_forced)]
    checks = [
        SecurityCheck(
            id="rls_tables",
            status=FAIL if sin_proteger else OK,
            detail=f"{len(rls_tables) - len(sin_proteger)}/{len(rls_tables)}",
        ),
        comprobar_rol(rol),
        *comprobar_configuracion(get_settings()),
    ]
    return SecurityOverview(
        environment=get_settings().APP_ENV,
        checks=checks,
        rls_tables=rls_tables,
        rls_unprotected=sin_proteger,
    )
