"""Verifica que la migracion 009 dejo todos los `identifier_hash` con la formula por tenant.

Es la comprobacion del runbook de ADR-052, en un script para poder correrla igual antes
y despues del despliegue. Solo LEE: no modifica nada.

Uso (desde la raiz del repo, con el entorno del despliegue cargado):

    python scripts/verify_migration_009.py

Variables de entorno:
    DATABASE_URL_DIRECT   conexion directa (no el pooler), la misma que usa Alembic.
    ENCRYPTION_KEY        la MISMA clave con la que se cifro la columna.

Codigos de salida:
    0  todas las filas coinciden (0 filas desalineadas)
    1  hay filas desalineadas: NO arrancar la aplicacion
    2  no se pudo verificar (falta una variable, no hay conexion, clave incorrecta)
"""

import os
import sys

# La clave viaja como parametro, nunca incrustada en el SQL ni en el log. Es la misma
# expresion que la migracion y que `app.core.encryption.blind_index()`.
CONSULTA_DESALINEADAS = """
SELECT count(*) FROM contact_identifiers
WHERE identifier_hash <> encode(hmac(
    client_id::text || ':' || lower(trim(pgp_sym_decrypt(identifier_value, %(clave)s))),
    %(clave)s, 'sha256'), 'hex')
"""

CONSULTA_TOTAL = "SELECT count(*) FROM contact_identifiers"


def main() -> int:
    """Ejecuta la verificacion.

    Returns:
        Codigo de salida (ver el docstring del modulo).
    """
    url = os.getenv("DATABASE_URL_DIRECT", "").replace("postgresql+asyncpg://", "postgresql://")
    clave = os.getenv("ENCRYPTION_KEY", "")
    if not url or not clave:
        print("ERROR: faltan DATABASE_URL_DIRECT y/o ENCRYPTION_KEY en el entorno", file=sys.stderr)
        return 2

    try:
        import psycopg2

        with psycopg2.connect(url) as conexion, conexion.cursor() as cursor:
            cursor.execute(CONSULTA_TOTAL)
            total = int(cursor.fetchone()[0])
            cursor.execute(CONSULTA_DESALINEADAS, {"clave": clave})
            desalineadas = int(cursor.fetchone()[0])
    except Exception as exc:
        # Solo el tipo: el mensaje de un error de conexion puede traer la URL con la clave.
        print(f"ERROR: no se pudo verificar ({type(exc).__name__})", file=sys.stderr)
        return 2

    print(f"contact_identifiers: {total} filas, {desalineadas} desalineadas")
    if desalineadas:
        print("FALLO: hay hashes que no siguen la formula por tenant. NO arrancar la aplicacion.")
        return 1
    print("OK: todos los hashes siguen la formula por tenant.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
