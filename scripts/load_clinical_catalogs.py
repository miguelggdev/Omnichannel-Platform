"""Carga los catalogos oficiales CIE-10 y CUPS en la base (Sprint 13).

Uso:
    python scripts/load_clinical_catalogs.py --cie10 cie10.csv --cups cups.csv

Cada CSV lleva una fila por codigo con dos columnas, en cualquier orden y con
cabecera: `codigo`/`code` y `descripcion`/`description`/`nombre`. Acepta coma o
punto y coma y UTF-8 (con o sin BOM). El dataset oficial no se versiona en el
repositorio: CIE-10 de la OMS/MinSalud y CUPS de la Resolucion 5171 de 2017.

Los codigos CIE-10 pueden venir con o sin punto: los datasets oficiales y los RIPS
los traen sin el (`E119`) y se guardan como `E11.9`, igual que como los dicta el
profesional. Los CUPS son seis digitos.

Es idempotente: recargar actualiza las descripciones existentes y agrega los
codigos nuevos; nunca borra. Las filas cuyo codigo no cumple el formato se
descartan y se cuentan en el resumen.

Se conecta con `DATABASE_URL_DIRECT` si existe (escribir catalogos no es una
operacion del pooler de transacciones), y si no con `DATABASE_URL`. Las tablas
no tienen RLS (son catalogos publicos), pero solo este script las escribe: el
rol de la aplicacion deberia tener solo SELECT sobre ellas.
"""

import argparse
import asyncio
import csv
import sys
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings
from app.services.clinical_catalog import cargar_catalogo

_COLUMNAS_CODIGO = ("codigo", "código", "code")
_COLUMNAS_DESCRIPCION = ("descripcion", "descripción", "description", "nombre")


def leer_filas(ruta: Path) -> Iterator[tuple[str, str]]:
    """Lee `(codigo, descripcion)` de un CSV con cabecera.

    Args:
        ruta: Archivo CSV.

    Yields:
        Una tupla por fila.

    Raises:
        SystemExit: Si la cabecera no tiene las dos columnas.
    """
    with ruta.open(encoding="utf-8-sig", newline="") as archivo:
        muestra = archivo.read(4096)
        archivo.seek(0)
        dialecto = csv.Sniffer().sniff(muestra, delimiters=",;")
        lector = csv.DictReader(archivo, dialect=dialecto)
        campos = {(c or "").strip().lower(): c for c in (lector.fieldnames or [])}
        col_codigo = next((campos[c] for c in _COLUMNAS_CODIGO if c in campos), None)
        col_desc = next((campos[c] for c in _COLUMNAS_DESCRIPCION if c in campos), None)
        if col_codigo is None or col_desc is None:
            raise SystemExit(
                f"{ruta}: la cabecera necesita una columna de codigo y una de descripcion"
            )
        for fila in lector:
            yield fila[col_codigo] or "", fila[col_desc] or ""


async def main(cie10: Path | None, cups: Path | None) -> None:
    """Carga los archivos indicados en una sola transaccion por catalogo.

    Args:
        cie10: CSV del CIE-10, si se quiere cargar.
        cups: CSV del CUPS, si se quiere cargar.
    """
    ajustes = get_settings()
    engine = create_async_engine(ajustes.DATABASE_URL_DIRECT or ajustes.DATABASE_URL)
    fabrica = async_sessionmaker(engine, expire_on_commit=False)
    try:
        for tipo, ruta in (("cie10", cie10), ("cups", cups)):
            if ruta is None:
                continue
            async with fabrica() as session, session.begin():
                resumen = await cargar_catalogo(
                    session,
                    tipo,  # type: ignore[arg-type]
                    leer_filas(ruta),
                )
            print(f"{tipo}: {resumen['loaded']} codigos cargados, {resumen['invalid']} descartados")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    analizador = argparse.ArgumentParser(description=__doc__.splitlines()[0])  # type: ignore[union-attr]
    analizador.add_argument("--cie10", type=Path, help="CSV del catalogo CIE-10")
    analizador.add_argument("--cups", type=Path, help="CSV del catalogo CUPS")
    args = analizador.parse_args()
    if not (args.cie10 or args.cups):
        analizador.error("indica al menos --cie10 o --cups")
    asyncio.run(main(args.cie10, args.cups))
