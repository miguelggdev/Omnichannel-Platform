"""Motor de las secuencias de follow-up: que toca hacer ahora con una inscripcion (Sprint 18).

Funcion pura. Recibe el estado de la inscripcion, los pasos y una foto del lead (`LeadSnapshot`)
y devuelve una `Decision`: enviar un mensaje (y por que canal), crear una tarea, esperar,
terminar o salir. **No envia nada ni toca la base**: la tarea de Celery de Dev B ejecuta la
accion y luego registra el avance (`app/services/lead_sequences.registrar_avance`).

Las condiciones se resuelven aqui mismo, sin esperar: una condicion no es un "paso" para el lead,
asi que el motor encadena condiciones y saltos hasta llegar a algo que hacer. Para que un error
de configuracion no deje una inscripcion girando, hay un tope de pasos por inscripcion
(`MAX_STEPS_PER_ENROLLMENT`) ademas de la validacion del grafo (`validar_grafo`).

Guardas antes de cualquier paso (en este orden): secuencia apagada, lead borrado, lead
anonimizado por RGPD y lead cerrado (`won`/`lost`/`disqualified`): en todos los casos se sale con
su codigo. Nunca se le escribe a alguien que pidio la supresion de sus datos.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from app.models.lead_sequence import (
    EXIT_CONDITION,
    EXIT_GDPR,
    EXIT_LEAD_CLOSED,
    EXIT_LEAD_DELETED,
    EXIT_NO_CHANNEL,
    EXIT_SEQUENCE_DISABLED,
    EXIT_STEP_LIMIT,
)
from app.schemas.lead_sequence import (
    Branch,
    ConditionStep,
    MessageStep,
    TaskStep,
    WaitStep,
)

#: Pasos (incluidas condiciones) que una inscripcion puede ejecutar en toda su vida.
MAX_STEPS_PER_ENROLLMENT = 200

Paso = MessageStep | WaitStep | ConditionStep | TaskStep


@dataclass(frozen=True)
class LeadSnapshot:
    """Lo que el motor necesita saber del lead, leido justo antes de decidir.

    Attributes:
        status: `leads.status`.
        deleted: Si tiene soft delete.
        anonymized: Si paso por la supresion RGPD.
        stage_slug: Slug de su etapa.
        total_score: Score total.
        channels: Canales por los que se le puede escribir.
        replied: Si escribio desde que entro en la secuencia.
        email_opened: Si abrio un email desde que entro.
        link_clicked: Si hizo clic en un enlace desde que entro.
        assigned_user_id: Su responsable.
    """

    status: str = "active"
    deleted: bool = False
    anonymized: bool = False
    stage_slug: str | None = None
    total_score: int = 0
    channels: frozenset[str] = frozenset()
    replied: bool = False
    email_opened: bool = False
    link_clicked: bool = False
    assigned_user_id: UUID | None = None


# ─── Acciones ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SendMessage:
    """Enviar el mensaje del paso `position` por `channel`."""

    position: int
    step: MessageStep
    channel: str


@dataclass(frozen=True)
class CreateTask:
    """Crear la tarea del paso `position`."""

    position: int
    step: TaskStep


@dataclass(frozen=True)
class Wait:
    """Esperar segun el paso `position` (quien llama calcula `next_step_at`)."""

    position: int
    step: WaitStep


@dataclass(frozen=True)
class Complete:
    """No quedan pasos: la secuencia termino."""


@dataclass(frozen=True)
class Exit:
    """Salir de la secuencia por `reason` (codigo `EXIT_*`)."""

    reason: str


Accion = SendMessage | CreateTask | Wait | Complete | Exit


@dataclass(frozen=True)
class Decision:
    """Lo que toca hacer y como queda la inscripcion despues.

    Attributes:
        action: La accion.
        next_step: Posicion del paso siguiente tras ejecutar la accion.
        steps_consumed: Pasos recorridos para llegar aqui (condiciones, saltados y el propio).
        skipped: Posiciones de mensajes saltados por no tener el lead ese canal.
    """

    action: Accion
    next_step: int
    steps_consumed: int
    skipped: tuple[int, ...] = field(default_factory=tuple)


# ─── Condiciones y canales ──────────────────────────────────────────────────────────────────


def evaluar_condicion(paso: ConditionStep, lead: LeadSnapshot) -> bool:
    """Evalua la comprobacion de un paso `condition` contra el lead.

    Args:
        paso: Paso de condicion.
        lead: Foto del lead.

    Returns:
        Si se cumple.
    """
    match paso.check:
        case "replied":
            return lead.replied
        case "no_reply":
            return not lead.replied
        case "email_opened":
            return lead.email_opened
        case "link_clicked":
            return lead.link_clicked
        case "score_at_least":
            return isinstance(paso.value, int) and lead.total_score >= paso.value
        case "stage_is":
            return lead.stage_slug is not None and lead.stage_slug == paso.value
        case "has_channel":
            return paso.value in lead.channels
    return False  # pragma: no cover - el Literal no admite mas valores


def elegir_canal(
    paso: MessageStep, prioridad: Sequence[str], disponibles: frozenset[str]
) -> str | None:
    """El canal por el que enviar un mensaje.

    Args:
        paso: Paso de mensaje (`channel` concreto o `auto`).
        prioridad: `channel_priority` de la secuencia.
        disponibles: Canales del lead.

    Returns:
        El canal, o `None` si el lead no es alcanzable por el pedido (o por ninguno de la
        prioridad, en `auto`).
    """
    if paso.channel != "auto":
        return paso.channel if paso.channel in disponibles else None
    return next((c for c in prioridad if c in disponibles), None)


# ─── Decision ───────────────────────────────────────────────────────────────────────────────


def _guarda(lead: LeadSnapshot, secuencia_activa: bool) -> str | None:
    """Motivo para salir antes de mirar ningun paso, o `None`."""
    if not secuencia_activa:
        return EXIT_SEQUENCE_DISABLED
    if lead.deleted:
        return EXIT_LEAD_DELETED
    if lead.anonymized:
        return EXIT_GDPR
    if lead.status != "active":
        return EXIT_LEAD_CLOSED
    return None


def decidir(
    *,
    pasos: Sequence[Paso],
    current_step: int,
    steps_executed: int,
    lead: LeadSnapshot,
    channel_priority: Sequence[str],
    secuencia_activa: bool = True,
) -> Decision:
    """Decide que hacer con una inscripcion activa a la que le toca avanzar.

    Args:
        pasos: Pasos de la secuencia en orden (posicion = indice + 1), ya validados.
        current_step: Paso que toca (desde 1).
        steps_executed: Pasos ya ejecutados por esta inscripcion.
        lead: Foto del lead.
        channel_priority: Prioridad de canales de la secuencia.
        secuencia_activa: `lead_sequences.is_active`.

    Returns:
        La decision.
    """
    motivo = _guarda(lead, secuencia_activa)
    if motivo is not None:
        return Decision(Exit(motivo), next_step=current_step, steps_consumed=0)

    posicion = current_step
    consumidos = 0
    saltados: list[int] = []
    while True:
        if posicion > len(pasos):
            return Decision(Complete(), posicion, consumidos, tuple(saltados))
        if steps_executed + consumidos >= MAX_STEPS_PER_ENROLLMENT:
            return Decision(Exit(EXIT_STEP_LIMIT), posicion, consumidos, tuple(saltados))
        paso = pasos[posicion - 1]
        consumidos += 1

        if isinstance(paso, ConditionStep):
            rama: Branch = paso.if_true if evaluar_condicion(paso, lead) else paso.if_false
            if rama.action == "exit":
                return Decision(Exit(EXIT_CONDITION), posicion, consumidos, tuple(saltados))
            posicion = (
                rama.goto_position if rama.action == "goto" and rama.goto_position else posicion + 1
            )
            continue

        if isinstance(paso, MessageStep):
            canal = elegir_canal(paso, channel_priority, lead.channels)
            if canal is None:
                if paso.channel == "auto":
                    # Ningun canal de la prioridad: no hay forma de seguir hablandole.
                    return Decision(Exit(EXIT_NO_CHANNEL), posicion, consumidos, tuple(saltados))
                # Un paso atado a un canal que este lead no tiene se salta (p. ej. el email de
                # seguimiento a un lead que solo dejo telefono).
                saltados.append(posicion)
                posicion += 1
                continue
            return Decision(
                SendMessage(posicion, paso, canal), posicion + 1, consumidos, tuple(saltados)
            )

        if isinstance(paso, TaskStep):
            return Decision(CreateTask(posicion, paso), posicion + 1, consumidos, tuple(saltados))

        return Decision(Wait(posicion, paso), posicion + 1, consumidos, tuple(saltados))


# ─── Plantillas ─────────────────────────────────────────────────────────────────────────────

_VARIABLE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_ESPACIO_ANTES_DE_PUNTUACION = re.compile(r"[ \t]+([,.;:!?])")
_ESPACIOS = re.compile(r"[ \t]{2,}")


def renderizar_plantilla(texto: str, variables: Mapping[str, str | None]) -> str:
    """Sustituye `{{variable}}` por su valor.

    No usa `str.format`: una plantilla escrita por el tenant con `{0.__class__}` podria leer
    atributos de los objetos. Aqui solo se reconoce `{{nombre}}` y solo se sustituye por texto.
    Una variable sin valor desaparece y se limpia el espacio que deja ("Hola {{first_name}}," sin
    nombre queda "Hola,").

    Args:
        texto: Plantilla (ya validada por el schema).
        variables: Valores; los que falten o sean `None` quedan vacios.

    Returns:
        El texto final.
    """
    resultado = _VARIABLE.sub(lambda m: (variables.get(m.group(1)) or "").strip(), texto)
    resultado = _ESPACIO_ANTES_DE_PUNTUACION.sub(r"\1", resultado)
    return _ESPACIOS.sub(" ", resultado).strip()
