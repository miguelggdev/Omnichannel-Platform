"""Catalogos de referencia CIE-10 y CUPS para el agente clinico (Sprint 13).

Es un subconjunto **de referencia**, no el listado oficial. El spec (§9.2) prevé
que el catalogo completo viva en una tabla de referencia y que los codigos mas
frecuentes esten en memoria; la tabla completa exige cargar el dataset oficial
(CIE-10 de la OMS/MinSalud y CUPS de la Resolucion 5171 de 2017), que no esta en
el repositorio. Mientras tanto:

- **No se inventan codigos.** El spec cae a un LLM ("llm_cie10_lookup") cuando
  el codigo no esta en el catalogo. Un codigo alucinado en un RIPS termina en
  una glosa o en una factura a la EPS por un servicio que no fue; por eso aca la
  busqueda solo devuelve lo que esta en el catalogo, y si no hay coincidencia el
  agente le pide el codigo al profesional (que es quien lo dicta).
- **Solo entran codigos verificados.** Los CUPS del pseudocodigo del spec no se
  copiaron enteros: al menos `903841` (glicemia) y `903856` figuran ahi con
  descripciones que no coinciden con el listado oficial que conozco. Se dejan
  solo los dos de consulta medica general, que son inequivocos. Ampliar esta
  lista es cargar el dataset oficial, no escribirla a mano.
- Un codigo que **no** esta aqui pero tiene formato valido no se rechaza (el
  profesional puede dictar cualquiera del listado oficial); se guarda marcado
  como `catalog_verified: false` para que quien revise lo confirme.
- **Con los catalogos oficiales cargados manda la base.** Las tablas
  `cie10_catalog` y `cups_catalog` (migracion 018, cargadas por
  `scripts/load_clinical_catalogs.py`) reemplazan al subconjunto: se busca ahi,
  todo codigo queda verificado y uno que no exista se rechaza. Mientras esten
  vacias rige lo de arriba.
"""

import re
import unicodedata
from collections.abc import Iterable
from typing import Literal, TypedDict

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clinical_catalog import Cie10Catalog, CupsCatalog

#: Formato de un codigo CIE-10: letra, dos digitos y hasta dos de subcategoria.
CIE10_PATTERN = re.compile(r"^[A-Z][0-9]{2}(\.[0-9]{1,2})?$")

#: Formato de un codigo CUPS: seis digitos.
CUPS_PATTERN = re.compile(r"^[0-9]{6}$")

CIE10_COMMON: dict[str, str] = {
    "A08.4": "Infeccion intestinal viral, sin otra especificacion",
    "A09": "Diarrea y gastroenteritis de presunto origen infeccioso",
    "B34.9": "Infeccion viral, no especificada",
    "D64.9": "Anemia, no especificada",
    "E03.9": "Hipotiroidismo, no especificado",
    "E11.9": "Diabetes mellitus tipo 2 sin complicaciones",
    "E66.9": "Obesidad, no especificada",
    "E78.5": "Hiperlipidemia, no especificada",
    "F41.9": "Trastorno de ansiedad, no especificado",
    "G43.9": "Migrana, no especificada",
    "H10.9": "Conjuntivitis, no especificada",
    "H66.9": "Otitis media, no especificada",
    "I10": "Hipertension esencial (primaria)",
    "I25.9": "Enfermedad isquemica cronica del corazon, no especificada",
    "J00": "Rinofaringitis aguda (resfriado comun)",
    "J02.9": "Faringitis aguda, no especificada",
    "J03.9": "Amigdalitis aguda, no especificada",
    "J06.9": "Infeccion aguda de las vias respiratorias superiores, no especificada",
    "J18.9": "Neumonia, organismo no especificado",
    "J20.9": "Bronquitis aguda, no especificada",
    "J45.9": "Asma, no especificada",
    "K21.9": "Enfermedad del reflujo gastroesofagico sin esofagitis",
    "K29.7": "Gastritis, no especificada",
    "K59.0": "Estrenimiento",
    "L30.9": "Dermatitis, no especificada",
    "M25.5": "Dolor en articulacion",
    "M54.5": "Dolor en la region lumbar",
    "N18.9": "Enfermedad renal cronica, no especificada",
    "N39.0": "Infeccion de vias urinarias, sitio no especificado",
    "R10.4": "Otros dolores abdominales y los no especificados",
    "R11": "Nausea y vomito",
    "R50.9": "Fiebre, no especificada",
    "R51": "Cefalea",
    "Z00.0": "Examen medico general",
    "Z00.1": "Control de salud de rutina del nino",
    "Z34.9": "Supervision de embarazo normal, no especificado",
}

CUPS_COMMON: dict[str, str] = {
    "890201": "Consulta de primera vez por medicina general",
    "890301": "Consulta de control o de seguimiento por medicina general",
}


class CodigoCatalogo(TypedDict):
    """Un codigo del catalogo con su descripcion.

    Attributes:
        code: Codigo (CIE-10 o CUPS).
        description: Descripcion en espanol, sin tildes.
    """

    code: str
    description: str


def normalizar_texto(texto: str) -> str:
    """Pasa a minusculas y quita tildes, para comparar lo dictado con el catalogo.

    El spec compara `diagnosis.lower() in description.lower()`, que no
    encuentra "hipertension" si el dictado (o el catalogo) lleva tilde en un
    lado y no en el otro.

    Args:
        texto: Texto libre.

    Returns:
        El texto en minusculas y sin marcas diacriticas.
    """
    descompuesto = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in descompuesto if not unicodedata.combining(c)).strip()


def normalizar_codigo(codigo: str) -> str:
    """Normaliza un codigo dictado: sin espacios y en mayusculas.

    Args:
        codigo: Codigo tal como llego (`" j06.9 "`).

    Returns:
        El codigo limpio (`"J06.9"`).
    """
    return "".join(codigo.split()).upper()


def _buscar(catalogo: dict[str, str], consulta: str, limite: int) -> list[CodigoCatalogo]:
    """Busca en un catalogo por codigo exacto o por las palabras de la consulta.

    Args:
        catalogo: `codigo -> descripcion`.
        consulta: Codigo o texto libre.
        limite: Maximo de resultados.

    Returns:
        Coincidencias: primero el codigo exacto y luego las descripciones que
        contienen **todas** las palabras de la consulta.
    """
    codigo = normalizar_codigo(consulta)
    if codigo in catalogo:
        return [CodigoCatalogo(code=codigo, description=catalogo[codigo])]

    palabras = normalizar_texto(consulta).split()
    if not palabras:
        return []
    return [
        CodigoCatalogo(code=cod, description=desc)
        for cod, desc in catalogo.items()
        if all(p in normalizar_texto(desc) for p in palabras)
    ][:limite]


def buscar_cie10(consulta: str, limite: int = 10) -> list[CodigoCatalogo]:
    """Busca diagnosticos en el catalogo CIE-10 local.

    Args:
        consulta: Codigo o texto libre.
        limite: Maximo de resultados.

    Returns:
        Coincidencias del catalogo; vacio si no hay ninguna.
    """
    return _buscar(CIE10_COMMON, consulta, limite)


def buscar_cups(consulta: str, limite: int = 10) -> list[CodigoCatalogo]:
    """Busca procedimientos en el catalogo CUPS local.

    Args:
        consulta: Codigo o texto libre.
        limite: Maximo de resultados.

    Returns:
        Coincidencias del catalogo; vacio si no hay ninguna.
    """
    return _buscar(CUPS_COMMON, consulta, limite)


TipoCatalogo = Literal["cie10", "cups"]

_MODELOS: dict[str, type[Cie10Catalog] | type[CupsCatalog]] = {
    "cie10": Cie10Catalog,
    "cups": CupsCatalog,
}
_PATRONES = {"cie10": CIE10_PATTERN, "cups": CUPS_PATTERN}
_MEMORIA = {"cie10": CIE10_COMMON, "cups": CUPS_COMMON}
_LOTE = 1000


async def catalogo_cargado(session: AsyncSession, tipo: TipoCatalogo) -> bool:
    """Si el catalogo oficial ya se cargo en la base.

    Args:
        session: Sesion de base de datos (las tablas no tienen RLS).
        tipo: `cie10` o `cups`.

    Returns:
        `True` si la tabla tiene al menos una fila.
    """
    modelo = _MODELOS[tipo]
    return (await session.execute(select(modelo.code).limit(1))).scalar_one_or_none() is not None


def _escapar_like(palabra: str) -> str:
    """Escapa `%`, `_` y `\\` para usar una palabra dentro de un `LIKE`.

    Args:
        palabra: Palabra de la consulta.

    Returns:
        La palabra, con los comodines neutralizados.
    """
    return palabra.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def buscar_en_catalogo(
    session: AsyncSession, tipo: TipoCatalogo, consulta: str, limite: int = 10
) -> list[CodigoCatalogo]:
    """Busca un codigo o texto en el catalogo oficial o, si no esta cargado, en el de referencia.

    Args:
        session: Sesion de base de datos.
        tipo: `cie10` o `cups`.
        consulta: Codigo o texto libre.
        limite: Maximo de resultados.

    Returns:
        Coincidencias; vacio si no hay ninguna.
    """
    if not await catalogo_cargado(session, tipo):
        return (buscar_cie10 if tipo == "cie10" else buscar_cups)(consulta, limite)

    modelo = _MODELOS[tipo]
    codigo = normalizar_codigo(consulta)
    exacta = (
        await session.execute(select(modelo.code, modelo.description).where(modelo.code == codigo))
    ).first()
    if exacta is not None:
        return [CodigoCatalogo(code=exacta[0], description=exacta[1])]

    palabras = normalizar_texto(consulta).split()
    if not palabras:
        return []
    filas = (
        await session.execute(
            select(modelo.code, modelo.description)
            .where(
                *(
                    modelo.description_norm.ilike(f"%{_escapar_like(p)}%", escape="\\")
                    for p in palabras
                )
            )
            .order_by(modelo.code)
            .limit(limite)
        )
    ).all()
    return [CodigoCatalogo(code=f[0], description=f[1]) for f in filas]


async def verificar_codigos(
    session: AsyncSession, tipo: TipoCatalogo, codigos: list[str]
) -> tuple[bool, dict[str, str]]:
    """Comprueba codigos contra el catalogo oficial.

    Args:
        session: Sesion de base de datos.
        tipo: `cie10` o `cups`.
        codigos: Codigos ya normalizados.

    Returns:
        `(cargado, encontrados)`: si hay catalogo oficial y, en ese caso, el
        `codigo -> descripcion` de los que existen.
    """
    if not codigos or not await catalogo_cargado(session, tipo):
        return False, {}
    modelo = _MODELOS[tipo]
    filas = (
        await session.execute(
            select(modelo.code, modelo.description).where(modelo.code.in_(codigos))
        )
    ).all()
    return True, {f[0]: f[1] for f in filas}


async def cargar_catalogo(
    session: AsyncSession, tipo: TipoCatalogo, filas: Iterable[tuple[str, str]]
) -> dict[str, int]:
    """Carga (o actualiza) un catalogo oficial. Idempotente.

    Args:
        session: Sesion con permiso de escritura sobre las tablas de catalogo.
        tipo: `cie10` o `cups`.
        filas: `(codigo, descripcion)`.

    Returns:
        `{"loaded": n, "invalid": m}`: las filas con un codigo que no cumple el
        formato del catalogo (o sin descripcion) se descartan y se cuentan.
    """
    modelo = _MODELOS[tipo]
    patron = _PATRONES[tipo]
    cargadas = 0
    invalidas = 0
    lote: list[dict[str, str]] = []

    async def _volcar() -> None:
        if not lote:
            return
        stmt = pg_insert(modelo).values(lote)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["code"],
                set_={
                    "description": stmt.excluded.description,
                    "description_norm": stmt.excluded.description_norm,
                },
            )
        )
        lote.clear()

    vistos: set[str] = set()
    for codigo, descripcion in filas:
        limpio = normalizar_codigo(codigo)
        texto = (descripcion or "").strip()
        if not patron.match(limpio) or not texto:
            invalidas += 1
            continue
        if limpio in vistos:  # ON CONFLICT no admite dos filas iguales en un mismo INSERT
            continue
        vistos.add(limpio)
        lote.append(
            {"code": limpio, "description": texto, "description_norm": normalizar_texto(texto)}
        )
        cargadas += 1
        if len(lote) >= _LOTE:
            await _volcar()
            vistos.clear()
    await _volcar()
    return {"loaded": cargadas, "invalid": invalidas}
