"""Importacion de leads desde CSV: parseo, mapeo de columnas y validacion por fila (Sprint 16).

Solo CSV. Un `.xlsx` es un zip con formulas y macros posibles (bombas de descompresion,
inyeccion de formulas) y la mayoria de hojas de calculo exportan CSV; si hace falta, es trabajo
aparte con sus propios limites.

Lo que hace este modulo es **puro** (bytes -> filas validadas, sin base de datos) para poder
probarlo a fondo. La API (`POST /leads/import`) lo usa y se ocupa de duplicados contra la base.

Decisiones:

- **Codificacion:** UTF-8 (con o sin BOM) y, si no decodifica, `latin-1`: Excel en espanol guarda
  CSV en cp1252. **Delimitador:** se detecta entre `,` `;` y tabulador (Excel con configuracion
  regional espanola usa `;`).
- **Cabeceras tolerantes:** sin acentos, mayusculas ni signos, con alias en espanol e ingles y los
  nombres de los exportadores de LinkedIn/Sales Navigator/Phantombuster (`profileUrl`,
  `fullName`, `title`, `company`...). Las columnas desconocidas se ignoran y se informan.
- **Errores por fila, sin eco de datos:** el mensaje dice *que campo* falla, no repite el valor
  (que puede ser un dato personal) para que el informe no lo propague a logs ni capturas.
- **Limites** de tamano y de filas, comprobados antes de procesar.
"""

import csv
import io
import re
import unicodedata
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.schemas.lead import LeadCreate

MAX_BYTES = 2 * 1024 * 1024
MAX_FILAS = 5000
MAX_ERRORES_INFORMADOS = 100

#: Cabecera normalizada -> campo canonico del lead. `full_name` se divide en nombre y apellido.
_ALIAS: dict[str, str] = {}
for _campo, _nombres in {
    "first_name": ("firstname", "nombre", "nombres", "givenname", "primernombre"),
    "last_name": ("lastname", "apellido", "apellidos", "surname", "familyname"),
    "full_name": ("fullname", "name", "nombrecompleto", "nombreyapellido", "contactname"),
    "email": ("email", "correo", "correoelectronico", "mail", "emailaddress", "workemail"),
    "phone": ("phone", "telefono", "celular", "movil", "mobile", "whatsapp", "phonenumber", "tel"),
    "linkedin_url": (
        "linkedin",
        "linkedinurl",
        "linkedinprofile",
        "profileurl",
        "perfillinkedin",
        "linkedinprofileurl",
    ),
    "company_name": ("company", "companyname", "empresa", "organization", "organizacion"),
    "company_domain": ("website", "domain", "companydomain", "sitioweb", "companywebsite", "web"),
    "company_size": ("companysize", "employees", "tamano", "tamanoempresa", "empleados"),
    "industry": ("industry", "sector", "industria"),
    "job_title": ("jobtitle", "title", "cargo", "puesto", "position", "role"),
}.items():
    for _n in _nombres:
        _ALIAS[_n] = _campo

_PUNTUACION = re.compile(r"[^a-z0-9]")


def normalizar_cabecera(texto: str) -> str:
    """Minusculas, sin acentos ni signos: `Teléfono (móvil)` -> `telefonomovil`.

    Args:
        texto: Cabecera tal como viene en el CSV.

    Returns:
        La cabecera reducida a `[a-z0-9]`.
    """
    sin_acentos = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return _PUNTUACION.sub("", sin_acentos.lower())


class CsvError(ValueError):
    """El archivo no se puede importar en absoluto (vacio, demasiado grande, sin cabeceras utiles)."""


@dataclass
class FilaImportada:
    """Una fila ya validada (o con su error).

    Attributes:
        numero: Numero de fila en el archivo, contando la cabecera como 1.
        datos: Campos del lead listos para `Lead(...)`; vacio si hubo error.
        error: Que campo fallo, sin repetir su valor.
    """

    numero: int
    datos: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class CsvParseado:
    """Resultado de leer un CSV.

    Attributes:
        filas: Una entrada por fila de datos.
        columnas_ignoradas: Cabeceras que no corresponden a ningun campo del lead.
    """

    filas: list[FilaImportada]
    columnas_ignoradas: list[str]


def _decodificar(contenido: bytes) -> str:
    try:
        return contenido.decode("utf-8-sig")
    except UnicodeDecodeError:
        return contenido.decode("latin-1")


def _detectar_delimitador(muestra: str) -> str:
    """El delimitador mas probable de la primera linea; la coma si no hay duda."""
    primera = muestra.splitlines()[0] if muestra.splitlines() else ""
    cuentas = {d: primera.count(d) for d in (",", ";", "\t")}
    mejor = max(cuentas, key=lambda d: cuentas[d])
    return mejor if cuentas[mejor] > 0 else ","


def normalizar_linkedin(valor: str) -> str:
    """Lleva una URL de LinkedIn a `https://`.

    Los exportadores a veces entregan `http://` o `www.linkedin.com/in/...`.

    Args:
        valor: URL tal como viene.

    Returns:
        La URL con esquema `https`; vacio si no habia nada.
    """
    valor = valor.strip()
    if not valor:
        return ""
    if valor.lower().startswith("http://"):
        return "https://" + valor[7:]
    if not valor.lower().startswith("https://"):
        return "https://" + valor.lstrip("/")
    return valor


def _dividir_nombre(completo: str) -> tuple[str | None, str | None]:
    partes = completo.split(None, 1)
    if not partes:
        return None, None
    return partes[0], (partes[1] if len(partes) > 1 else None)


def _mensaje_de_error(exc: ValidationError) -> str:
    """Los campos que fallan, sin sus valores (que pueden ser datos personales)."""
    campos = []
    for e in exc.errors():
        nombre = str(e["loc"][-1]) if e["loc"] else "fila"
        if nombre == "fila" or nombre == "__root__":
            campos.append("falta email, telefono o LinkedIn")
        else:
            campos.append(f"{nombre} no es valido")
    return "; ".join(dict.fromkeys(campos))


def parsear_csv(contenido: bytes) -> CsvParseado:
    """Lee un CSV de leads y valida cada fila con las mismas reglas que `POST /leads`.

    Args:
        contenido: Bytes del archivo.

    Returns:
        Las filas (validas o con error) y las columnas ignoradas.

    Raises:
        CsvError: Si esta vacio, pesa mas de `MAX_BYTES`, tiene mas de `MAX_FILAS` filas, no trae
            ninguna cabecera reconocible o ninguna columna de contacto (email, telefono, LinkedIn).
    """
    if not contenido.strip():
        raise CsvError("El archivo esta vacio")
    if len(contenido) > MAX_BYTES:
        raise CsvError(f"El archivo supera {MAX_BYTES // (1024 * 1024)} MB")
    texto = _decodificar(contenido)
    lector = csv.reader(io.StringIO(texto, newline=""), delimiter=_detectar_delimitador(texto))
    try:
        cabeceras = next(lector)
    except StopIteration as exc:
        raise CsvError("El archivo esta vacio") from exc

    return _procesar(cabeceras, lector)


def validar_campos(crudo: dict[str, str], numero: int) -> FilaImportada:
    """Normaliza y valida los campos de un registro con las reglas de `POST /leads`.

    Args:
        crudo: Campos canonicos (`email`, `first_name`, `full_name`...) como texto.
        numero: Numero de fila o de elemento, para el informe de errores.

    Returns:
        La fila validada, o con el error (que nombra el campo y no repite el dato).
    """
    crudo = dict(crudo)
    completo = crudo.pop("full_name", None)
    if completo and "first_name" not in crudo:
        nombre, apellido = _dividir_nombre(completo)
        if nombre:
            crudo["first_name"] = nombre
        if apellido and "last_name" not in crudo:
            crudo["last_name"] = apellido
    if "linkedin_url" in crudo:
        crudo["linkedin_url"] = normalizar_linkedin(crudo["linkedin_url"])
    try:
        lead = LeadCreate.model_validate(crudo)
    except ValidationError as exc:
        return FilaImportada(numero=numero, error=_mensaje_de_error(exc))
    # Solo lo que venia en el registro (el modelo anade valores por defecto que no son suyos);
    # ya validado y normalizado por el schema (p. ej. el telefono recortado).
    datos = {k: v for k, v in lead.model_dump(exclude_none=True).items() if k in crudo}
    if "email" in datos:
        datos["email"] = str(datos["email"])
    return FilaImportada(numero=numero, datos=datos)


def campos_de_objeto(objeto: dict[str, Any]) -> dict[str, str]:
    """Mapea las claves de un objeto JSON (p. ej. un perfil de Phantombuster) a campos del lead.

    Usa los mismos alias que las cabeceras del CSV (`profileUrl`, `fullName`, `title`,
    `company`...). Solo se toman valores escalares (texto o numero): un objeto anidado se ignora.
    Si dos claves apuntan al mismo campo gana la primera.

    Args:
        objeto: Un registro JSON.

    Returns:
        Campos canonicos con su valor como texto.
    """
    campos: dict[str, str] = {}
    for clave, valor in objeto.items():
        campo = _ALIAS.get(normalizar_cabecera(str(clave)))
        if campo is None or campo in campos or isinstance(valor, bool):
            continue
        if isinstance(valor, (str, int, float)):
            texto = _texto_de_celda(valor)
            if texto:
                campos[campo] = texto
    return campos


def _procesar(cabeceras: list[str], registros: Iterable[list[str]]) -> CsvParseado:
    """Mapea las cabeceras y valida cada registro. Comun a CSV y a Excel.

    Args:
        cabeceras: Primera fila del archivo.
        registros: Resto de filas, ya como listas de texto.

    Returns:
        Las filas (validas o con error) y las columnas ignoradas.

    Raises:
        CsvError: Si no hay cabeceras reconocibles ni columna de contacto, hay demasiadas filas
            o ninguna fila de datos.
    """
    mapa: dict[int, str] = {}
    ignoradas: list[str] = []
    for i, cab in enumerate(cabeceras):
        campo = _ALIAS.get(normalizar_cabecera(cab))
        if campo is None or campo in mapa.values():
            if cab.strip():
                ignoradas.append(cab.strip())
        else:
            mapa[i] = campo
    if not mapa:
        raise CsvError("No se reconoce ninguna columna: usa cabeceras como email, nombre, empresa")
    if not {"email", "phone", "linkedin_url"} & set(mapa.values()):
        raise CsvError("Falta una columna de contacto: email, telefono o LinkedIn")

    filas: list[FilaImportada] = []
    for numero, registro in enumerate(registros, start=2):
        if not any(c.strip() for c in registro):
            continue  # fila en blanco
        if len(filas) >= MAX_FILAS:
            raise CsvError(f"El archivo supera {MAX_FILAS} filas")
        crudo: dict[str, str] = {}
        for i, campo in mapa.items():
            valor = registro[i].strip() if i < len(registro) else ""
            if valor:
                crudo[campo] = valor
        filas.append(validar_campos(crudo, numero))
    if not filas:
        raise CsvError("El archivo no tiene filas de datos")
    return CsvParseado(filas=filas, columnas_ignoradas=ignoradas)


# ── Excel (.xlsx) ────────────────────────────────────────────────────────────────────────────
#
# Un `.xlsx` es un zip: se vigila lo que un zip puede hacer antes de abrirlo. Limites de
# descompresion (bomba de descompresion), sin macros, solo la primera hoja, y se leen los valores
# **cacheados** (`data_only=True`): una celda con formula devuelve su ultimo resultado, nunca se
# evalua nada. Una celda de texto que empiece por `=` es solo texto.

MAX_DESCOMPRIMIDO = 20 * 1024 * 1024
MAX_ENTRADAS_ZIP = 200
MAX_COLUMNAS = 60
_FIRMA_ZIP = b"PK\x03\x04"


def es_xlsx(contenido: bytes) -> bool:
    """Si el archivo es un zip (firma `PK\\x03\\x04`), que es como empieza un `.xlsx`.

    Se decide por los bytes y no por el nombre ni el `Content-Type`, que declara el cliente.

    Args:
        contenido: Bytes del archivo.

    Returns:
        `True` si empieza como un zip.
    """
    return contenido.startswith(_FIRMA_ZIP)


def _texto_de_celda(valor: object) -> str:
    """Una celda de Excel como texto: `573001112233.0` pasa a `573001112233`."""
    if valor is None:
        return ""
    if isinstance(valor, bool):
        return str(valor)
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    if hasattr(valor, "isoformat"):
        return str(valor.isoformat())
    return str(valor).strip()


def _revisar_zip(contenido: bytes) -> None:
    """Rechaza un zip que se descomprimiria demasiado, trae macros o tiene demasiadas entradas."""
    try:
        with zipfile.ZipFile(io.BytesIO(contenido)) as z:
            entradas = z.infolist()
    except zipfile.BadZipFile as exc:
        raise CsvError("El archivo no es un Excel (.xlsx) valido") from exc
    if len(entradas) > MAX_ENTRADAS_ZIP:
        raise CsvError("El archivo Excel tiene demasiadas partes")
    if sum(e.file_size for e in entradas) > MAX_DESCOMPRIMIDO:
        raise CsvError("El archivo Excel ocupa demasiado al descomprimirse")
    if any(e.filename.lower().endswith("vbaproject.bin") for e in entradas):
        raise CsvError("El archivo Excel contiene macros: guardalo como .xlsx sin macros")
    if not any(e.filename == "xl/workbook.xml" for e in entradas):
        raise CsvError("El archivo no es un Excel (.xlsx) valido")


def parsear_xlsx(contenido: bytes) -> CsvParseado:
    """Lee la primera hoja de un `.xlsx` con las mismas reglas que un CSV.

    Args:
        contenido: Bytes del archivo.

    Returns:
        Las filas (validas o con error) y las columnas ignoradas.

    Raises:
        CsvError: Si no es un Excel valido, trae macros, se descomprime demasiado, esta vacio o
            incumple los limites de filas y columnas.
    """
    if len(contenido) > MAX_BYTES:
        raise CsvError(f"El archivo supera {MAX_BYTES // (1024 * 1024)} MB")
    _revisar_zip(contenido)
    from openpyxl import load_workbook

    try:
        libro = load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl lanza tipos muy variados ante un archivo malformado
        raise CsvError("No se pudo leer el archivo Excel") from exc
    try:
        hoja = libro.worksheets[0] if libro.worksheets else None
        if hoja is None:
            raise CsvError("El archivo Excel no tiene hojas")
        filas = hoja.iter_rows(values_only=True, max_col=MAX_COLUMNAS)
        try:
            cabeceras = [_texto_de_celda(c) for c in next(filas)]
        except StopIteration as exc:
            raise CsvError("El archivo esta vacio") from exc

        def registros() -> Iterable[list[str]]:
            consecutivas_en_blanco = 0
            for fila in filas:
                valores = [_texto_de_celda(c) for c in fila]
                # Una hoja con "formato hasta la fila 1.048.576" se lee entera; se corta en
                # cuanto hay mas filas vacias seguidas de las que cabria en un archivo real.
                consecutivas_en_blanco = 0 if any(valores) else consecutivas_en_blanco + 1
                if consecutivas_en_blanco > 1000:
                    return
                yield valores

        return _procesar(cabeceras, registros())
    finally:
        libro.close()


def parsear_archivo(contenido: bytes) -> CsvParseado:
    """Lee un CSV o un `.xlsx`, decidiendo por los bytes del archivo.

    Args:
        contenido: Bytes del archivo subido.

    Returns:
        Las filas (validas o con error) y las columnas ignoradas.

    Raises:
        CsvError: Si el archivo no se puede importar (ver `parsear_csv` y `parsear_xlsx`).
    """
    return parsear_xlsx(contenido) if es_xlsx(contenido) else parsear_csv(contenido)
