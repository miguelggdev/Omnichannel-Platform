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
"""

import re
import unicodedata
from typing import TypedDict

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
