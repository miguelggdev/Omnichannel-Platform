"""Schemas del enriquecimiento y el scoring de leads (Sprint 17, slice Dev A).

Tres contratos:

- `IcpConfig`: el perfil de cliente ideal del tenant (`clients.icp_config`). Es lo que validara
  `PUT /api/v1/clients/icp` (Dev B) y lo que lee el calculador de FIT.
- `CompanyData` / `PersonData` / `EnrichmentResult`: la forma **normalizada** que devuelve
  cualquier proveedor de enriquecimiento (Apollo, Hunter, Clearbit...). Cada integracion traduce
  su respuesta a esto; el resto del modulo nunca ve el JSON de un proveedor.
- `ScoreResult` y las respuestas de la API de scores.
"""

import re
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.text_normalization import normalizar_texto

#: Tramos de tamano de empresa, de menor a mayor. El orden importa: el FIT da credito parcial a un
#: tramo vecino del ICP.
COMPANY_SIZE_BUCKETS: tuple[str, ...] = (
    "1-10",
    "11-50",
    "51-200",
    "201-500",
    "501-1000",
    "1001-5000",
    "5001+",
)
CompanySize = Literal["1-10", "11-50", "51-200", "201-500", "501-1000", "1001-5000", "5001+"]
Seniority = Literal["c_level", "vp", "director", "manager", "senior", "entry"]
EmailStatus = Literal["valid", "invalid", "risky", "unknown"]

_PAIS = re.compile(r"^[A-Z]{2}$")
_DOMINIO = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_MAX_TERMINOS = 50
_MAX_LARGO_TERMINO = 100


def _terminos(valores: list[str]) -> list[str]:
    """Limpia una lista de terminos del ICP: sin vacios, sin repetidos (ignorando acentos).

    Conserva la grafia del primero de cada grupo de repetidos: es lo que el tenant escribio y lo
    que vera de vuelta.
    """
    vistos: set[str] = set()
    resultado: list[str] = []
    for valor in valores:
        limpio = valor.strip()
        if not limpio:
            continue
        if len(limpio) > _MAX_LARGO_TERMINO:
            raise ValueError(f"Cada termino admite como mucho {_MAX_LARGO_TERMINO} caracteres")
        clave = normalizar_texto(limpio)
        if not clave:
            raise ValueError("Un termino debe tener al menos una letra o un digito")
        if clave not in vistos:
            vistos.add(clave)
            resultado.append(limpio)
    return resultado


def _paises(valores: list[str]) -> list[str]:
    """Codigos ISO 3166-1 alfa-2 en mayusculas, sin repetidos."""
    resultado: list[str] = []
    for valor in valores:
        codigo = valor.strip().upper()
        if not _PAIS.match(codigo):
            raise ValueError(
                f"Pais no valido: usa el codigo ISO de dos letras (p. ej. CO), no {valor!r}"
            )
        if codigo not in resultado:
            resultado.append(codigo)
    return resultado


def normalizar_dominio(valor: str | None) -> str | None:
    """Dominio en minusculas, sin `www.` ni punto final; `None` si no tiene forma de dominio.

    Args:
        valor: Dominio tal como llego (`WWW.Acme.com.`).

    Returns:
        `acme.com`, o `None` si no es un dominio (una URL, una IP, texto suelto).
    """
    if valor is None:
        return None
    limpio = valor.strip().lower().rstrip(".").removeprefix("www.")
    return limpio if _DOMINIO.match(limpio) else None


def _pais_o_none(valor: str | None) -> str | None:
    """Codigo ISO alfa-2 en mayusculas, o `None` si el valor no lo es."""
    if valor is None:
        return None
    codigo = valor.strip().upper()
    return codigo if _PAIS.match(codigo) else None


class IcpWeights(BaseModel):
    """Peso de cada dimension del FIT. Se normalizan por su suma: no tienen que sumar 100."""

    model_config = ConfigDict(extra="forbid")

    industry: int = Field(default=30, ge=0, le=100)
    company_size: int = Field(default=25, ge=0, le=100)
    job_title: int = Field(default=30, ge=0, le=100)
    region: int = Field(default=15, ge=0, le=100)

    @model_validator(mode="after")
    def _no_todo_cero(self) -> Self:
        """Pesos que suman 0 dejarian el FIT sin denominador."""
        if self.industry + self.company_size + self.job_title + self.region == 0:
            raise ValueError("Los pesos del ICP no pueden sumar 0")
        return self


class IcpConfig(BaseModel):
    """Perfil de cliente ideal de un tenant (`clients.icp_config`).

    Una lista vacia significa "esta dimension no me importa": no suma ni resta y sale del
    denominador del FIT. Las listas de exclusion mandan sobre todo lo demas: un lead de un sector
    o con un cargo excluido tiene FIT 0.

    Attributes:
        version: Version del formato, para poder migrarlo sin adivinar.
        industries: Sectores objetivo (texto libre: "software", "salud").
        excluded_industries: Sectores que descalifican.
        company_sizes: Tramos de empleados objetivo.
        job_titles: Palabras clave de cargo ("cto", "gerente de compras").
        excluded_job_titles: Palabras clave de cargo que descalifican ("practicante").
        countries: Paises objetivo, ISO 3166-1 alfa-2.
        weights: Peso de cada dimension.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    industries: list[str] = Field(default_factory=list, max_length=_MAX_TERMINOS)
    excluded_industries: list[str] = Field(default_factory=list, max_length=_MAX_TERMINOS)
    company_sizes: list[CompanySize] = Field(default_factory=list)
    job_titles: list[str] = Field(default_factory=list, max_length=_MAX_TERMINOS)
    excluded_job_titles: list[str] = Field(default_factory=list, max_length=_MAX_TERMINOS)
    countries: list[str] = Field(default_factory=list, max_length=250)
    weights: IcpWeights = Field(default_factory=IcpWeights)

    @field_validator("industries", "excluded_industries", "job_titles", "excluded_job_titles")
    @classmethod
    def _limpiar_terminos(cls, valores: list[str]) -> list[str]:
        """Sin vacios ni repetidos."""
        return _terminos(valores)

    @field_validator("countries")
    @classmethod
    def _limpiar_paises(cls, valores: list[str]) -> list[str]:
        """Codigos ISO en mayusculas, sin repetidos."""
        return _paises(valores)

    @field_validator("company_sizes")
    @classmethod
    def _tramos_sin_repetir(cls, valores: list[str]) -> list[str]:
        """Sin repetidos y en el orden natural de los tramos."""
        return [t for t in COMPANY_SIZE_BUCKETS if t in set(valores)]

    @model_validator(mode="after")
    def _sin_contradicciones(self) -> Self:
        """Un sector o cargo no puede ser objetivo y excluido a la vez."""
        for incluidos, excluidos, nombre in (
            (self.industries, self.excluded_industries, "sector"),
            (self.job_titles, self.excluded_job_titles, "cargo"),
        ):
            choque = {normalizar_texto(t) for t in incluidos} & {
                normalizar_texto(t) for t in excluidos
            }
            if choque:
                raise ValueError(f"Un {nombre} no puede estar a la vez incluido y excluido")
        return self

    @property
    def configurado(self) -> bool:
        """Si el tenant definio al menos una dimension con peso; sin eso no hay FIT que calcular."""
        dimensiones = (
            (self.industries, self.weights.industry),
            (self.company_sizes, self.weights.company_size),
            (self.job_titles, self.weights.job_title),
            (self.countries, self.weights.region),
        )
        return any(valores and peso > 0 for valores, peso in dimensiones)


# ─── Datos normalizados de enriquecimiento ──────────────────────────────────────────────────


class CompanyData(BaseModel):
    """Lo que un proveedor sabe de una empresa, en forma comun a todos los proveedores.

    No son datos personales: describen a la empresa. Por eso, y solo por eso, se pueden cachear
    (`app/services/enrichment/cache.py`).
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    domain: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, max_length=200)
    industry: str | None = Field(default=None, max_length=100)
    employees: int | None = Field(default=None, ge=0, le=10_000_000)
    employee_range: CompanySize | None = None
    country: str | None = None
    city: str | None = Field(default=None, max_length=100)
    linkedin_url: str | None = Field(default=None, max_length=500)
    founded_year: int | None = Field(default=None, ge=1600, le=2100)
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("domain")
    @classmethod
    def _dominio(cls, valor: str | None) -> str | None:
        """Dominio en minusculas y con forma de dominio; uno raro se descarta, no se adivina."""
        return normalizar_dominio(valor)

    @field_validator("country")
    @classmethod
    def _pais(cls, valor: str | None) -> str | None:
        """Codigo ISO alfa-2; si el proveedor manda otra cosa ("Colombia"), se descarta."""
        return _pais_o_none(valor)


class PersonData(BaseModel):
    """Lo que un proveedor sabe de la persona del lead. **Son datos personales: no se cachean.**"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    job_title: str | None = Field(default=None, max_length=150)
    seniority: Seniority | None = None
    country: str | None = None
    linkedin_url: str | None = Field(default=None, max_length=500)
    email_status: EmailStatus | None = None

    @field_validator("country")
    @classmethod
    def _pais(cls, valor: str | None) -> str | None:
        """Codigo ISO alfa-2 o nada."""
        return _pais_o_none(valor)


class EnrichmentResult(BaseModel):
    """Respuesta de un proveedor para un lead: empresa, persona o ambas (o nada)."""

    provider: str = Field(min_length=1, max_length=50)
    company: CompanyData | None = None
    person: PersonData | None = None

    @property
    def vacio(self) -> bool:
        """Si el proveedor no supo nada."""
        return self.company is None and self.person is None


# ─── Resultado de un calculo y respuestas de la API ────────────────────────────────────────


class ScoreResult(BaseModel):
    """Resultado de un calculador de score.

    Attributes:
        score: 0-100.
        factors: Explicacion: dimensiones, puntos y codigos de motivo. **Sin datos del lead.**
        aplicable: `False` si no hay base para calcular (p. ej. el tenant no tiene ICP): quien lo
            aplica no debe tocar el score vigente.
    """

    score: int = Field(ge=0, le=100)
    factors: dict[str, Any] = Field(default_factory=dict)
    aplicable: bool = True


class LeadScoreEntry(BaseModel):
    """Una fila del historial de scores, tal como la devuelve la API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    score_type: str
    score: int
    previous_score: int | None
    trigger: str
    factors: dict[str, Any]
    user_id: UUID | None
    created_at: datetime


class LeadScoresDetail(BaseModel):
    """Respuesta de `GET /leads/{id}/score`: valores vigentes y el ultimo calculo de cada tipo."""

    lead_id: UUID
    fit_score: int
    behavioral_score: int
    ai_score: int
    total_score: int
    weights: dict[str, int]
    latest: dict[str, LeadScoreEntry]
