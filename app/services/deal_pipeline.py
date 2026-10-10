"""Reglas de un deal: etapas, probabilidad, cierre, efecto en el lead y resumen (Sprint 19).

Funciones puras (salvo que modifican el `Deal` que reciben): el servicio de BD
(`app/services/deals.py`) las aplica y guarda. Reglas:

- **Etapas abiertas** (`new_contact` -> `qualified` -> `proposal` -> `negotiation`): se puede ir
  adelante y atras libremente; un deal real no avanza en linea recta.
- **Cerrar** (`closed_won` / `closed_lost`) desde cualquier etapa abierta. Ganado fija
  probabilidad 100, `won_at` y `actual_close_date`; perdido, 0, `lost_at` y su motivo.
- **De cerrado solo se sale reabriendo** a una etapa abierta (borra fechas y motivo de cierre).
  Ganado -> perdido directo no: obliga a reabrir, para que el historial cuente lo que paso.
- **Probabilidad:** la de la etapa (`PROBABILIDAD_POR_ETAPA`) salvo que se indique otra.
- **Efecto en el lead:** ganar un deal convierte al lead (`won`, `converted_at`); reabrir el unico
  deal ganado lo devuelve a `active`. Perder un deal **no** toca al lead: puede tener otros o
  seguir en seguimiento.
- **Importes:** nunca se suman monedas distintas; el resumen agrupa por etapa **y** moneda.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from app.models.deal import (
    CLOSED_STAGES,
    DEAL_LOST,
    DEAL_NEGOTIATION,
    DEAL_NEW_CONTACT,
    DEAL_PROPOSAL,
    DEAL_QUALIFIED,
    DEAL_STAGES,
    DEAL_WON,
    OPEN_STAGES,
    Deal,
)
from app.models.lead import Lead

#: Probabilidad por defecto de cada etapa (%).
PROBABILIDAD_POR_ETAPA: dict[str, int] = {
    DEAL_NEW_CONTACT: 10,
    DEAL_QUALIFIED: 25,
    DEAL_PROPOSAL: 50,
    DEAL_NEGOTIATION: 75,
    DEAL_WON: 100,
    DEAL_LOST: 0,
}

_CENTIMOS = Decimal("0.01")


class TransicionInvalidaError(ValueError):
    """El cambio de etapa no esta permitido.

    Attributes:
        code: Codigo estable para la API.
    """

    def __init__(self, code: str, message: str) -> None:
        """Crea el error.

        Args:
            code: Codigo estable (`same_stage`, `closed_to_closed`, `unknown_stage`).
            message: Descripcion.
        """
        super().__init__(message)
        self.code = code


def validar_transicion(actual: str, nueva: str) -> None:
    """Comprueba que un deal pueda pasar de `actual` a `nueva`.

    Args:
        actual: Etapa actual.
        nueva: Etapa pedida.

    Raises:
        TransicionInvalidaError: Si no se puede.
    """
    if nueva not in DEAL_STAGES:
        raise TransicionInvalidaError("unknown_stage", f"Etapa desconocida: {nueva!r}")
    if nueva == actual:
        raise TransicionInvalidaError("same_stage", "El deal ya esta en esa etapa")
    if actual in CLOSED_STAGES and nueva in CLOSED_STAGES:
        raise TransicionInvalidaError(
            "closed_to_closed", "Un deal cerrado se reabre antes de cerrarlo de otra forma"
        )


def dia_local(instante: datetime, zona: str) -> date:
    """El dia de calendario de un instante en una zona (el cierre "es" del dia local).

    Args:
        instante: Instante aware.
        zona: Zona IANA del negocio.

    Returns:
        La fecha local.
    """
    return instante.astimezone(ZoneInfo(zona)).date()


def aplicar_etapa(
    deal: Deal,
    nueva: str,
    *,
    ahora: datetime,
    zona: str,
    probabilidad: int | None = None,
    motivo_perdida: str | None = None,
) -> str:
    """Mueve el deal de etapa y ajusta lo que depende de ella.

    Args:
        deal: Deal (se modifica en sitio).
        nueva: Etapa nueva.
        ahora: Instante del cambio.
        zona: Zona del negocio, para `actual_close_date`.
        probabilidad: Probabilidad explicita (solo en etapas abiertas).
        motivo_perdida: Motivo, al perder.

    Returns:
        La etapa anterior.

    Raises:
        TransicionInvalidaError: Si la transicion no esta permitida.
    """
    anterior = deal.stage
    validar_transicion(anterior, nueva)
    deal.stage = nueva
    deal.stage_changed_at = ahora
    if nueva == DEAL_WON:
        deal.probability = 100
        deal.won_at = ahora
        deal.lost_at = None
        deal.lost_reason = None
        deal.actual_close_date = dia_local(ahora, zona)
    elif nueva == DEAL_LOST:
        deal.probability = 0
        deal.lost_at = ahora
        deal.won_at = None
        deal.lost_reason = motivo_perdida
        deal.actual_close_date = dia_local(ahora, zona)
    else:
        # Abierta (tambien al reabrir): sin rastro de cierre.
        deal.probability = (
            probabilidad if probabilidad is not None else PROBABILIDAD_POR_ETAPA[nueva]
        )
        deal.won_at = None
        deal.lost_at = None
        deal.lost_reason = None
        deal.actual_close_date = None
    return anterior


@dataclass(frozen=True)
class EfectoEnLead:
    """Lo que un cambio de etapa de un deal hace a su lead.

    Attributes:
        status: Estado nuevo del lead, o `None` si no cambia.
        converted_at: Nuevo `converted_at` (solo si `status` cambia).
    """

    status: str | None = None
    converted_at: datetime | None = None


def efecto_en_lead(
    lead: Lead, anterior: str, nueva: str, *, ahora: datetime, otros_ganados: int
) -> EfectoEnLead:
    """Sincronizacion deal -> lead tras un cambio de etapa.

    Args:
        lead: Lead del deal.
        anterior: Etapa anterior del deal.
        nueva: Etapa nueva.
        ahora: Instante del cambio.
        otros_ganados: Otros deals ganados del mismo lead (sin contar este).

    Returns:
        El efecto (vacio si no cambia nada).
    """
    if nueva == DEAL_WON and lead.status == "active":
        return EfectoEnLead(status="won", converted_at=lead.converted_at or ahora)
    if (
        anterior == DEAL_WON
        and nueva in OPEN_STAGES
        and otros_ganados == 0
        and lead.status == "won"
    ):
        return EfectoEnLead(status="active", converted_at=None)
    return EfectoEnLead()


def aplicar_efecto(lead: Lead, efecto: EfectoEnLead) -> bool:
    """Aplica al lead el efecto calculado.

    Args:
        lead: Lead (se modifica en sitio).
        efecto: Lo devuelto por `efecto_en_lead`.

    Returns:
        Si cambio algo.
    """
    if efecto.status is None:
        return False
    lead.status = efecto.status
    lead.converted_at = efecto.converted_at
    return True


# ─── Resumen del pipeline ───────────────────────────────────────────────────────────────────


def valor_ponderado(valor: Decimal, probabilidad: int) -> Decimal:
    """Valor x probabilidad, redondeado al centimo (la mitad hacia arriba).

    Args:
        valor: Importe.
        probabilidad: 0-100.

    Returns:
        El valor esperado.
    """
    return (valor * probabilidad / 100).quantize(_CENTIMOS, rounding=ROUND_HALF_UP)


@dataclass
class ResumenEtapa:
    """Una etapa del pipeline en una moneda (ver `PipelineStageSummary`)."""

    stage: str
    currency: str
    count: int = 0
    total_value: Decimal = Decimal("0.00")
    weighted_value: Decimal = Decimal("0.00")
    dias_acumulados: float = 0.0

    @property
    def avg_days_in_stage(self) -> float:
        """Dias medios en la etapa, con un decimal."""
        return round(self.dias_acumulados / self.count, 1) if self.count else 0.0


def resumir_pipeline(deals: Iterable[Deal], *, ahora: datetime) -> list[ResumenEtapa]:
    """Agrupa deals por etapa y moneda (orden: etapas del embudo, luego moneda).

    Args:
        deals: Deals del tenant.
        ahora: Instante de referencia para los dias en etapa.

    Returns:
        Una fila por (etapa, moneda) con al menos un deal.
    """
    filas: dict[tuple[str, str], ResumenEtapa] = {}
    for deal in deals:
        clave = (deal.stage, deal.currency)
        fila = filas.setdefault(clave, ResumenEtapa(stage=deal.stage, currency=deal.currency))
        fila.count += 1
        fila.total_value += deal.value
        fila.weighted_value += valor_ponderado(deal.value, deal.probability)
        fila.dias_acumulados += max((ahora - deal.stage_changed_at).total_seconds(), 0) / 86400
    orden = {etapa: i for i, etapa in enumerate(DEAL_STAGES)}
    return sorted(filas.values(), key=lambda f: (orden[f.stage], f.currency))
