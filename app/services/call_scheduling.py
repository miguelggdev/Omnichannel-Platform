"""Agenda de llamadas: huecos libres, solapes, estados y recordatorios (Sprint 19).

Funciones puras. Los instantes son siempre aware; se calculan en UTC y la zona solo sirve para
decidir el horario (el del negocio) y para mostrarle la hora al lead (la suya).

- **Huecos:** dentro del horario de atencion del negocio (`VentanaEnvio`, el mismo de las
  secuencias), en pasos fijos, sin pisar lo ocupado y con una antelacion minima. Un hueco cabe
  entero antes del cierre.
- **Solapes:** dos llamadas de la misma persona no pueden coincidir; la base no lo impide (haria
  falta `btree_gist`), lo comprueba el servicio con un bloqueo por usuario.
- **Recordatorios:** uno 24 h antes y otro 1 h antes. Si se agenda con menos de 24 h, el de 24 h
  no se envia nunca (avisaria a destiempo); si se pierde la ventana del de 24 h, no se envia
  tarde. Ninguno sale si la llamada ya empezo o no esta pendiente/confirmada.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models.scheduled_call import (
    CALL_CANCELLED,
    CALL_COMPLETED,
    CALL_CONFIRMED,
    CALL_IN_PROGRESS,
    CALL_NO_SHOW,
    CALL_PENDING,
    CALL_RESCHEDULED,
    UPCOMING_STATUSES,
    ScheduledCall,
)
from app.services.lead_sequence_timing import VentanaEnvio

#: Paso entre huecos ofrecidos.
PASO_HUECOS = timedelta(minutes=30)
#: Antelacion minima para agendar (no se ofrece un hueco que empieza en 5 minutos).
ANTELACION_MINIMA = timedelta(hours=1)
#: Huecos maximos devueltos de una vez.
MAX_HUECOS = 50
#: Dias maximos que se exploran buscando huecos.
MAX_DIAS_BUSQUEDA = 31

RECORDATORIO_24H = "24h"
RECORDATORIO_1H = "1h"
_ANTES = {RECORDATORIO_24H: timedelta(hours=24), RECORDATORIO_1H: timedelta(hours=1)}

#: Transiciones de estado permitidas. Los estados finales no tienen salida.
TRANSICIONES: dict[str, frozenset[str]] = {
    CALL_PENDING: frozenset(
        {CALL_CONFIRMED, CALL_IN_PROGRESS, CALL_CANCELLED, CALL_RESCHEDULED, CALL_NO_SHOW}
    ),
    CALL_CONFIRMED: frozenset(
        {CALL_IN_PROGRESS, CALL_COMPLETED, CALL_CANCELLED, CALL_RESCHEDULED, CALL_NO_SHOW}
    ),
    CALL_IN_PROGRESS: frozenset({CALL_COMPLETED, CALL_NO_SHOW}),
    CALL_COMPLETED: frozenset(),
    CALL_NO_SHOW: frozenset(),
    CALL_CANCELLED: frozenset(),
    CALL_RESCHEDULED: frozenset(),
}


class EstadoInvalidoError(ValueError):
    """La llamada no puede pasar a ese estado.

    Attributes:
        code: Codigo estable para la API.
    """

    def __init__(self, actual: str, nuevo: str) -> None:
        """Crea el error.

        Args:
            actual: Estado actual.
            nuevo: Estado pedido.
        """
        super().__init__(f"Una llamada en '{actual}' no puede pasar a '{nuevo}'")
        self.code = "invalid_status"


def validar_estado(actual: str, nuevo: str) -> None:
    """Comprueba una transicion de estado.

    Args:
        actual: Estado actual.
        nuevo: Estado pedido.

    Raises:
        EstadoInvalidoError: Si no esta permitida.
    """
    if nuevo not in TRANSICIONES.get(actual, frozenset()):
        raise EstadoInvalidoError(actual, nuevo)


@dataclass(frozen=True)
class Intervalo:
    """Un tramo ocupado o propuesto, `[inicio, fin)`."""

    inicio: datetime
    fin: datetime

    @classmethod
    def de_llamada(cls, inicio: datetime, minutos: int) -> "Intervalo":
        """El tramo que ocupa una llamada.

        Args:
            inicio: Inicio (aware).
            minutos: Duracion.

        Returns:
            El intervalo.
        """
        return cls(inicio, inicio + timedelta(minutes=minutos))

    def solapa(self, otro: "Intervalo") -> bool:
        """Si comparten algun instante (tocarse en el borde no es solapar).

        Args:
            otro: El otro tramo.

        Returns:
            `True` si se pisan.
        """
        return self.inicio < otro.fin and otro.inicio < self.fin


def primer_solape(propuesto: Intervalo, ocupados: Iterable[Intervalo]) -> Intervalo | None:
    """El primer tramo ocupado que pisa al propuesto.

    Args:
        propuesto: Tramo a comprobar.
        ocupados: Tramos ya ocupados.

    Returns:
        El que choca, o `None`.
    """
    return next((o for o in ocupados if propuesto.solapa(o)), None)


def huecos_libres(
    *,
    desde: datetime,
    hasta: datetime,
    duracion_minutos: int,
    ventana: VentanaEnvio,
    ocupados: Sequence[Intervalo] = (),
    ahora: datetime,
    paso: timedelta = PASO_HUECOS,
    antelacion: timedelta = ANTELACION_MINIMA,
    maximo: int = MAX_HUECOS,
) -> list[datetime]:
    """Inicios posibles para una llamada, en el horario del negocio y sin solapes.

    Los inicios se alinean al paso contando desde la apertura de cada dia (apertura 08:00 y paso
    de 30 min: 08:00, 08:30...), en la hora local del negocio, asi que un cambio de hora no
    descoloca la agenda.

    Args:
        desde: Inicio de la busqueda (aware).
        hasta: Fin de la busqueda (aware); se acota a `MAX_DIAS_BUSQUEDA` dias.
        duracion_minutos: Duracion de la llamada.
        ventana: Horario y zona del negocio.
        ocupados: Tramos ya ocupados de quien llama.
        ahora: Instante actual (para la antelacion minima).
        paso: Separacion entre inicios.
        antelacion: Antelacion minima respecto a `ahora`.
        maximo: Huecos maximos.

    Returns:
        Inicios en UTC, en orden.
    """
    duracion = timedelta(minutes=duracion_minutos)
    limite = min(hasta, desde + timedelta(days=MAX_DIAS_BUSQUEDA))
    minimo = max(desde, ahora + antelacion)
    ordenados = sorted(ocupados, key=lambda o: o.inicio)
    huecos: list[datetime] = []
    dia = desde.astimezone(ventana.tz).date()
    while len(huecos) < maximo:
        apertura_dia = datetime.combine(dia, datetime.min.time(), tzinfo=ventana.tz)
        if apertura_dia > limite:
            break
        tramo = ventana.horario.get(dia.weekday())
        if tramo is not None:
            apertura = datetime.combine(dia, tramo[0], tzinfo=ventana.tz)
            cierre = datetime.combine(dia, tramo[1], tzinfo=ventana.tz)
            inicio = apertura
            while inicio + duracion <= cierre and len(huecos) < maximo:
                if inicio >= minimo and inicio + duracion <= limite:
                    propuesto = Intervalo(inicio, inicio + duracion)
                    if primer_solape(propuesto, ordenados) is None:
                        huecos.append(inicio.astimezone(timezone.utc))
                inicio += paso
        dia += timedelta(days=1)
    return huecos


def recordatorio_pendiente(llamada: ScheduledCall, ahora: datetime) -> str | None:
    """El recordatorio que toca enviar ahora para una llamada, si alguno.

    Args:
        llamada: Llamada agendada.
        ahora: Instante actual.

    Returns:
        `RECORDATORIO_24H`, `RECORDATORIO_1H` o `None`.
    """
    if llamada.status not in UPCOMING_STATUSES or ahora >= llamada.scheduled_at:
        return None
    falta = llamada.scheduled_at - ahora
    if falta <= _ANTES[RECORDATORIO_1H]:
        return RECORDATORIO_1H if llamada.reminder_1h_sent_at is None else None
    # Sin `created_at` (objeto aun sin guardar) se supone agendada con tiempo.
    agendada_con_tiempo = (
        llamada.created_at is None
        or llamada.scheduled_at - llamada.created_at > _ANTES[RECORDATORIO_24H]
    )
    if falta <= _ANTES[RECORDATORIO_24H] and agendada_con_tiempo:
        return RECORDATORIO_24H if llamada.reminder_24h_sent_at is None else None
    return None


def marcar_recordatorio(llamada: ScheduledCall, cual: str, ahora: datetime) -> None:
    """Anota que se envio un recordatorio.

    Args:
        llamada: Llamada (se modifica en sitio).
        cual: `RECORDATORIO_24H` o `RECORDATORIO_1H`.
        ahora: Instante del envio.

    Raises:
        ValueError: Si `cual` no es un recordatorio conocido.
    """
    if cual == RECORDATORIO_24H:
        llamada.reminder_24h_sent_at = ahora
    elif cual == RECORDATORIO_1H:
        llamada.reminder_1h_sent_at = ahora
    else:
        raise ValueError(f"Recordatorio desconocido: {cual!r}")
