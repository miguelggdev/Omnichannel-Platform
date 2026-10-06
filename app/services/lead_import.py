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
    for numero, registro in enumerate(lector, start=2):
        if not any(c.strip() for c in registro):
            continue  # fila en blanco
        if len(filas) >= MAX_FILAS:
            raise CsvError(f"El archivo supera {MAX_FILAS} filas")
        crudo: dict[str, str] = {}
        for i, campo in mapa.items():
            valor = registro[i].strip() if i < len(registro) else ""
            if valor:
                crudo[campo] = valor
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
            filas.append(FilaImportada(numero=numero, error=_mensaje_de_error(exc)))
            continue
        # Solo lo que venia en el archivo (el modelo anade valores por defecto que no son del CSV);
        # ya validado y normalizado por el schema (p. ej. el telefono recortado).
        datos = {k: v for k, v in lead.model_dump(exclude_none=True).items() if k in crudo}
        if "email" in datos:
            datos["email"] = str(datos["email"])
        filas.append(FilaImportada(numero=numero, datos=datos))
    if not filas:
        raise CsvError("El archivo no tiene filas de datos")
    return CsvParseado(filas=filas, columnas_ignoradas=ignoradas)
