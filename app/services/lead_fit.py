"""FIT score: cuanto se parece un lead al cliente ideal del tenant (Sprint 17, ADR-022).

Funcion pura: recibe los datos del lead y el `IcpConfig`, devuelve un `ScoreResult`. No toca la
base; `lead_score_service` la aplica y guarda el historial.

Como se calcula
---------------
Cuatro dimensiones (sector, tamano de empresa, cargo y pais), cada una con un peso del ICP. Una
dimension sin terminos en el ICP no cuenta (ni suma ni sale en el denominador). Cada dimension
configurada da un credito:

- `match` = 1: el lead encaja (sector o cargo contienen un termino del ICP; tramo o pais exactos).
- `partial` = 1/2: tramo de tamano vecino, o el sector del lead es mas generico que el termino
  ("salud" frente a "salud digital").
- `no_match` = 0.
- `missing` = 0: el lead no tiene el dato. **No se reparte su peso entre las demas**: un lead del
  que solo se sabe el sector no puede sacar 100. Enriquecerlo es lo que sube su FIT.

`score = redondeo(100 * suma(peso * credito) / suma(pesos configurados))`, con aritmetica exacta
(la mitad sube). Las exclusiones (sector o cargo excluido) mandan: FIT 0.

Los `factors` guardan dimensiones, pesos y codigos de resultado, **nunca el valor del lead**: el
cargo o el sector de una persona no se copian al historial de scores.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from pydantic import ValidationError

from app.schemas.lead_scoring import COMPANY_SIZE_BUCKETS, IcpConfig, ScoreResult
from app.services.text_normalization import tokens

logger = logging.getLogger(__name__)

FIT_VERSION = 1

MATCH = "match"
PARTIAL = "partial"
NO_MATCH = "no_match"
MISSING = "missing"
EXCLUDED = "excluded"
NOT_CONFIGURED = "not_configured"

_CREDITO: dict[str, Fraction] = {
    MATCH: Fraction(1),
    PARTIAL: Fraction(1, 2),
    NO_MATCH: Fraction(0),
    MISSING: Fraction(0),
    EXCLUDED: Fraction(0),
}

#: Una palabra del ICP de al menos este largo tambien encaja como prefijo ("director" encaja con
#: "directora" y "directores"). Las cortas ("ceo", "cto", "ti") solo encajan completas.
_PREFIJO_MINIMO = 4


@dataclass(frozen=True)
class FitInput:
    """Lo que el calculador necesita saber del lead.

    Attributes:
        industry: Sector, texto libre.
        company_size: Tramo de `COMPANY_SIZE_BUCKETS`, ya resuelto.
        job_title: Cargo, texto libre.
        country: Pais, ISO 3166-1 alfa-2.
    """

    industry: str | None = None
    company_size: str | None = None
    job_title: str | None = None
    country: str | None = None


# ─── Tamano de empresa ──────────────────────────────────────────────────────────────────────

_LIMITES_TRAMOS: tuple[tuple[int, str], ...] = (
    (10, "1-10"),
    (50, "11-50"),
    (200, "51-200"),
    (500, "201-500"),
    (1000, "501-1000"),
    (5000, "1001-5000"),
)
_NUMERO = re.compile(r"(\d+(?:[.,]\d{3})*)(?:\s*([kKmM])\b)?")
_ABIERTO = re.compile(r"\+|\bmas de\b|\bmás de\b|\bover\b|\bmore than\b", re.IGNORECASE)


def tramo_por_empleados(empleados: int) -> str:
    """El tramo de `COMPANY_SIZE_BUCKETS` que corresponde a un numero de empleados.

    Args:
        empleados: Numero de empleados (0 cuenta como el tramo mas pequeno).

    Returns:
        El tramo, p. ej. `"51-200"`.
    """
    for limite, tramo in _LIMITES_TRAMOS:
        if empleados <= limite:
            return tramo
    return "5001+"


def _a_entero(cifra: str, sufijo: str | None) -> int:
    """`"1.000"`, `"10,001"` y `"5"`+`"k"` a entero."""
    valor = int(cifra.replace(".", "").replace(",", ""))
    if sufijo and sufijo.lower() == "k":
        valor *= 1_000
    elif sufijo and sufijo.lower() == "m":
        valor *= 1_000_000
    return valor


def parsear_tamano_empresa(texto: str | None) -> str | None:
    """Resuelve el tamano de empresa escrito a mano a un tramo de `COMPANY_SIZE_BUCKETS`.

    Acepta los tramos tal cual (`"11-50"`), rangos con texto (`"201 - 500 empleados"`,
    `"1K-5K"`, `"1.001-5.000"`), cotas abiertas (`"5000+"`, `"mas de 1000"`) y numeros sueltos
    (`"120"`). Un rango se resuelve por su punto medio; una cota abierta, por la cota.

    Args:
        texto: El valor de `leads.company_size`.

    Returns:
        El tramo, o `None` si no hay ningun numero (no se adivina a partir de "pyme").
    """
    if texto is None:
        return None
    limpio = texto.strip()
    if limpio in COMPANY_SIZE_BUCKETS:
        return limpio
    numeros = [_a_entero(cifra, sufijo) for cifra, sufijo in _NUMERO.findall(limpio)]
    if not numeros:
        return None
    if _ABIERTO.search(limpio):
        # "5000+" y "mas de 1000" son *mas* que la cota: 5000+ no es una empresa de 1001-5000.
        return tramo_por_empleados(max(numeros) + 1)
    if len(numeros) >= 2:
        bajo, alto = sorted(numeros[:2])
        return tramo_por_empleados((bajo + alto) // 2)
    return tramo_por_empleados(numeros[0])


# ─── Datos del lead ─────────────────────────────────────────────────────────────────────────


def _de(datos: Mapping[str, Any], seccion: str, campo: str) -> Any:
    """`enrichment_data[seccion][campo]` sin romperse si la forma no es la esperada."""
    bloque = datos.get(seccion)
    return bloque.get(campo) if isinstance(bloque, Mapping) else None


def fit_input_de_lead(
    *,
    industry: str | None,
    company_size: str | None,
    job_title: str | None,
    enrichment_data: Mapping[str, Any] | None,
) -> FitInput:
    """Arma la entrada del FIT con las columnas del lead y, si faltan, lo enriquecido.

    Las columnas mandan: son lo que escribio una persona o el formulario. Lo enriquecido
    (`enrichment_data["company"]` y `["person"]`, ver `CompanyData`/`PersonData`) solo cubre
    huecos. El pais sale de la empresa y, si no, de la persona.

    Args:
        industry: `leads.industry`.
        company_size: `leads.company_size` (texto libre).
        job_title: `leads.job_title`.
        enrichment_data: `leads.enrichment_data`.

    Returns:
        La entrada para `calcular_fit()`.
    """
    datos = enrichment_data or {}
    tramo = parsear_tamano_empresa(company_size)
    if tramo is None:
        rango = _de(datos, "company", "employee_range")
        if isinstance(rango, str) and rango in COMPANY_SIZE_BUCKETS:
            tramo = rango
    if tramo is None:
        empleados = _de(datos, "company", "employees")
        if isinstance(empleados, int) and not isinstance(empleados, bool) and empleados >= 0:
            tramo = tramo_por_empleados(empleados)

    def texto(valor: str | None, seccion: str, campo: str) -> str | None:
        if valor and valor.strip():
            return valor
        otro = _de(datos, seccion, campo)
        return otro if isinstance(otro, str) and otro.strip() else None

    pais = _de(datos, "company", "country") or _de(datos, "person", "country")
    return FitInput(
        industry=texto(industry, "company", "industry"),
        company_size=tramo,
        job_title=texto(job_title, "person", "job_title"),
        country=pais.upper() if isinstance(pais, str) and len(pais) == 2 else None,
    )


# ─── Comparaciones ──────────────────────────────────────────────────────────────────────────


def _palabra_encaja(termino: str, palabra: str) -> bool:
    """Una palabra del ICP encaja con una del lead: igual o, si es larga, como prefijo."""
    if len(termino) >= _PREFIJO_MINIMO:
        return palabra.startswith(termino)
    return palabra == termino


def contiene_termino(texto: str, termino: str) -> bool:
    """Si todas las palabras del termino aparecen en el texto (en cualquier orden).

    Args:
        texto: Valor del lead ("Gerente General de Compras").
        termino: Termino del ICP ("gerente de compras").

    Returns:
        `True` si cada palabra del termino encaja con alguna del texto.
    """
    palabras = tokens(texto)
    buscadas = tokens(termino)
    return bool(buscadas) and all(any(_palabra_encaja(b, p) for p in palabras) for b in buscadas)


def _resultado_texto(valor: str | None, objetivos: list[str], excluidos: list[str]) -> str:
    """Resultado de sector o cargo frente a sus listas del ICP."""
    if valor is None:
        return MISSING
    if any(contiene_termino(valor, t) for t in excluidos):
        return EXCLUDED
    if any(contiene_termino(valor, t) for t in objetivos):
        return MATCH
    # El lead es mas generico que un termino ("salud" frente a "salud digital"): la mitad.
    if tokens(valor) and any(contiene_termino(t, valor) for t in objetivos):
        return PARTIAL
    return NO_MATCH


def _resultado_tamano(tramo: str | None, objetivos: Sequence[str]) -> str:
    """Tramo exacto, vecino o ninguno."""
    if tramo is None:
        return MISSING
    if tramo in objetivos:
        return MATCH
    indice = COMPANY_SIZE_BUCKETS.index(tramo)
    vecinos = {
        COMPANY_SIZE_BUCKETS[i]
        for i in (indice - 1, indice + 1)
        if 0 <= i < len(COMPANY_SIZE_BUCKETS)
    }
    return PARTIAL if vecinos & set(objetivos) else NO_MATCH


# ─── Calculo ────────────────────────────────────────────────────────────────────────────────


def cargar_icp(raw: Mapping[str, Any] | None) -> IcpConfig | None:
    """Lee `clients.icp_config` sin romper un calculo en segundo plano.

    Lo que se guarda pasa por `IcpConfig` en la API (Dev B), asi que un valor invalido solo
    puede venir de una escritura directa en la base: se registra y se trata como "sin ICP".

    Args:
        raw: El JSON guardado.

    Returns:
        El ICP, o `None` si esta vacio, no configura nada o no es valido.
    """
    if not raw:
        return None
    try:
        icp = IcpConfig.model_validate(raw)
    except ValidationError:
        logger.warning("icp_config invalido; se trata como sin ICP")
        return None
    return icp if icp.configurado else None


def calcular_fit(datos: FitInput, icp: IcpConfig | None) -> ScoreResult:
    """FIT score de un lead frente al ICP del tenant.

    Args:
        datos: Lo que se sabe del lead (ver `fit_input_de_lead()`).
        icp: ICP del tenant; `None` si no tiene.

    Returns:
        El score con sus factores. `aplicable=False` si el tenant no tiene ICP: no hay contra
        que medir y el FIT vigente no debe tocarse.
    """
    if icp is None or not icp.configurado:
        return ScoreResult(
            score=0,
            aplicable=False,
            factors={"version": FIT_VERSION, "reason": "icp_not_configured"},
        )

    w = icp.weights
    dimensiones: dict[str, tuple[int, str]] = {}
    if icp.industries or icp.excluded_industries:
        dimensiones["industry"] = (
            w.industry,
            _resultado_texto(datos.industry, icp.industries, icp.excluded_industries),
        )
    if icp.company_sizes:
        dimensiones["company_size"] = (
            w.company_size,
            _resultado_tamano(datos.company_size, icp.company_sizes),
        )
    if icp.job_titles or icp.excluded_job_titles:
        dimensiones["job_title"] = (
            w.job_title,
            _resultado_texto(datos.job_title, icp.job_titles, icp.excluded_job_titles),
        )
    if icp.countries:
        dimensiones["region"] = (
            w.region,
            MISSING
            if datos.country is None
            else (MATCH if datos.country in icp.countries else NO_MATCH),
        )

    excluido_por = next((d for d, (_, r) in dimensiones.items() if r == EXCLUDED), None)
    # Solo cuentan para el denominador las dimensiones con lista objetivo y peso; una que solo
    # tiene exclusiones filtra pero no puntua.
    puntuables = {
        d: (peso, r)
        for d, (peso, r) in dimensiones.items()
        if peso > 0 and _tiene_objetivos(d, icp)
    }
    total_pesos = sum(peso for peso, _ in puntuables.values())
    if excluido_por is not None or total_pesos == 0:
        score = 0
    else:
        obtenido = sum(peso * _CREDITO[r] for peso, r in puntuables.values())
        exacto = Fraction(100) * obtenido / total_pesos
        score = int(exacto + Fraction(1, 2))  # la mitad sube; sin flotantes

    conocidas = sum(1 for _, r in puntuables.values() if r != MISSING)
    factores: dict[str, Any] = {
        "version": FIT_VERSION,
        "dimensions": {
            d: {
                "weight": peso,
                "result": r,
                "points": float(round(Fraction(100) * peso * _CREDITO[r] / total_pesos, 2))
                if d in puntuables and total_pesos and excluido_por is None
                else 0.0,
            }
            for d, (peso, r) in dimensiones.items()
        },
        "completeness": f"{conocidas}/{len(puntuables)}",
    }
    for d in ("industry", "company_size", "job_title", "region"):
        if d not in dimensiones:
            factores["dimensions"][d] = {"weight": 0, "result": NOT_CONFIGURED, "points": 0.0}
    if excluido_por is not None:
        factores["excluded_by"] = excluido_por
    return ScoreResult(score=score, factors=factores)


def _tiene_objetivos(dimension: str, icp: IcpConfig) -> bool:
    """Si la dimension tiene lista objetivo (no solo exclusiones)."""
    return bool(
        {
            "industry": icp.industries,
            "company_size": icp.company_sizes,
            "job_title": icp.job_titles,
            "region": icp.countries,
        }[dimension]
    )
