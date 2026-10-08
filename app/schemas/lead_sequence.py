"""Schemas de las secuencias de follow-up (Sprint 18, slice Dev A).

Es el contrato de `CRUD /api/v1/lead-sequences` (Dev B) y lo que el motor
(`app/services/lead_sequence_engine.py`) da por valido. Lo que se comprueba aqui y no en la
base:

- la configuracion de cada paso segun su tipo (`message`, `wait`, `condition`, `task`);
- que las plantillas solo usen variables conocidas (`{{first_name}}`...): una variable inventada
  fallaria al enviar, a las 3 de la manana, sin nadie mirando;
- el grafo de la secuencia: los saltos van a pasos que existen y **ningun bucle se queda sin una
  espera**, que mandaria mensajes sin parar al lead.

En la base, cada paso es `step_type` + `config`; aqui un paso es un solo objeto con `type`.
`paso_desde_fila()` y `paso_a_fila()` convierten entre las dos formas.
"""

import re
from datetime import timedelta
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

from app.schemas.lead import SourceType, Temperature

#: Canales por los que se puede iniciar un contacto. El webchat no (el visitante tiene que estar
#: en la pagina) y la voz es el Sprint 19.
OutreachChannel = Literal["whatsapp", "email", "instagram", "facebook", "telegram"]
OUTREACH_CHANNELS: tuple[str, ...] = ("whatsapp", "email", "instagram", "facebook", "telegram")

#: Variables que una plantilla puede usar. Ver `renderizar_plantilla()` en el motor.
TEMPLATE_VARIABLES: frozenset[str] = frozenset(
    {"first_name", "last_name", "company_name", "job_title", "business_name", "agent_name"}
)
_VARIABLE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_]{0,49}$")

MAX_STEPS = 30
#: Espera maxima de un paso: una secuencia no deberia dormir mas de un trimestre.
MAX_WAIT = timedelta(days=90)


def variables_de_plantilla(texto: str) -> set[str]:
    """Las variables `{{nombre}}` que aparecen en un texto.

    Args:
        texto: Plantilla.

    Returns:
        Los nombres, sin espacios.
    """
    return {m.group(1) for m in _VARIABLE.finditer(texto)}


def _validar_plantilla(texto: str | None) -> str | None:
    """Rechaza variables desconocidas y llaves sueltas que no son variables."""
    if texto is None:
        return None
    desconocidas = variables_de_plantilla(texto) - TEMPLATE_VARIABLES
    if desconocidas:
        raise ValueError(
            f"Variables desconocidas: {sorted(desconocidas)}. "
            f"Disponibles: {sorted(TEMPLATE_VARIABLES)}"
        )
    sin_variables = _VARIABLE.sub("", texto)
    if "{{" in sin_variables or "}}" in sin_variables:
        raise ValueError("Hay llaves {{ }} que no forman una variable")
    return texto


# ─── Configuracion de cada tipo de paso ─────────────────────────────────────────────────────


class Branch(BaseModel):
    """Que hacer tras una condicion: seguir, salir de la secuencia o saltar a un paso."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["continue", "exit", "goto"] = "continue"
    goto_position: int | None = Field(default=None, ge=1, le=MAX_STEPS)

    @model_validator(mode="after")
    def _goto_con_destino(self) -> Self:
        """Un `goto` necesita destino y solo un `goto` lo lleva."""
        if (self.action == "goto") != (self.goto_position is not None):
            raise ValueError("goto_position va con action='goto' y solo con ella")
        return self


class MessageStep(BaseModel):
    """Enviar un mensaje al lead.

    Attributes:
        channel: Canal concreto o `auto` (el primero disponible de `channel_priority`).
        mode: `template` (texto fijo con variables) o `ai` (lo redacta el agente, Dev B).
        body: Texto de la plantilla (obligatorio en `template`).
        subject: Asunto, solo para email.
        ai_instructions: Indicaciones para el modo `ai`.
        whatsapp_template: Plantilla aprobada por Meta. WhatsApp no deja iniciar una
            conversacion fuera de la ventana de 24 h con texto libre: sin ella, el envio por
            WhatsApp de un primer contacto fallara.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["message"] = "message"
    channel: OutreachChannel | Literal["auto"] = "auto"
    mode: Literal["template", "ai"] = "template"
    body: str | None = Field(default=None, min_length=1, max_length=4000)
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    ai_instructions: str | None = Field(default=None, min_length=1, max_length=1000)
    whatsapp_template: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("body", "subject")
    @classmethod
    def _plantilla(cls, valor: str | None) -> str | None:
        """Solo variables conocidas."""
        return _validar_plantilla(valor)

    @model_validator(mode="after")
    def _coherente(self) -> Self:
        """Un mensaje de plantilla necesita texto; el asunto solo tiene sentido en email."""
        if self.mode == "template" and not self.body:
            raise ValueError("Un mensaje en modo 'template' necesita body")
        if self.subject and self.channel not in ("email", "auto"):
            raise ValueError("subject solo aplica al canal email")
        return self


class WaitStep(BaseModel):
    """Esperar antes del siguiente paso.

    Attributes:
        amount: Cantidad.
        unit: `minutes`, `hours` o `days`.
        business_hours_only: Si el siguiente paso debe caer en horario de atencion del negocio.
        smart_timing: Si el siguiente paso debe caer a la hora en que el lead suele contestar.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["wait"] = "wait"
    amount: int = Field(ge=1, le=129_600)
    unit: Literal["minutes", "hours", "days"] = "days"
    business_hours_only: bool = True
    smart_timing: bool = False

    @property
    def delta(self) -> timedelta:
        """La espera como `timedelta`."""
        return timedelta(**{self.unit: self.amount})

    @model_validator(mode="after")
    def _tope(self) -> Self:
        """Ninguna espera de mas de `MAX_WAIT`."""
        if self.delta > MAX_WAIT:
            raise ValueError(f"Una espera no puede superar {MAX_WAIT.days} dias")
        return self


ConditionCheck = Literal[
    "replied", "no_reply", "email_opened", "link_clicked", "score_at_least", "stage_is",
    "has_channel",
]  # fmt: skip


class ConditionStep(BaseModel):
    """Bifurcar segun lo que hizo el lead desde que entro en la secuencia.

    Attributes:
        check: Que comprobar. `replied`/`no_reply`/`email_opened`/`link_clicked` miran lo
            ocurrido desde la inscripcion; `score_at_least` el `total_score`; `stage_is` el slug de
            la etapa; `has_channel` si el lead es alcanzable por un canal.
        value: Umbral (`score_at_least`), slug (`stage_is`) o canal (`has_channel`).
        if_true: Que hacer si se cumple.
        if_false: Que hacer si no.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["condition"] = "condition"
    check: ConditionCheck
    # Estrictos: con `int | str` pydantic convertiria `true` en 1 y `"50"` en 50.
    value: StrictInt | StrictStr | None = None
    if_true: Branch = Field(default_factory=Branch)
    if_false: Branch = Field(default_factory=Branch)

    @model_validator(mode="after")
    def _valor_segun_check(self) -> Self:
        """Cada comprobacion lleva su tipo de valor (o ninguno)."""
        if self.check == "score_at_least":
            if not isinstance(self.value, int):
                raise ValueError("score_at_least necesita un entero 0-100")
            if not 0 <= self.value <= 100:
                raise ValueError("score_at_least necesita un entero 0-100")
        elif self.check == "stage_is":
            if not isinstance(self.value, str) or not _SLUG.match(self.value):
                raise ValueError("stage_is necesita el slug de una etapa")
        elif self.check == "has_channel":
            if self.value not in OUTREACH_CHANNELS:
                raise ValueError(f"has_channel necesita uno de {OUTREACH_CHANNELS}")
        elif self.value is not None:
            raise ValueError(f"{self.check} no lleva value")
        return self


class TaskStep(BaseModel):
    """Crear una tarea para una persona del equipo (p. ej. llamar al lead).

    Attributes:
        title: Que hay que hacer (texto del tenant, sin variables del lead).
        assign_to: `lead_owner` (el responsable del lead) o `unassigned`.
        due_in_hours: Plazo.
        pause_until_done: Si la secuencia espera a que la tarea se complete.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["task"] = "task"
    title: str = Field(min_length=1, max_length=200)
    assign_to: Literal["lead_owner", "unassigned"] = "lead_owner"
    due_in_hours: int = Field(default=24, ge=1, le=720)
    pause_until_done: bool = False


StepDefinition = Annotated[
    MessageStep | WaitStep | ConditionStep | TaskStep, Field(discriminator="type")
]
_STEP_ADAPTER: TypeAdapter[MessageStep | WaitStep | ConditionStep | TaskStep] = TypeAdapter(
    StepDefinition
)


def paso_desde_fila(
    step_type: str, config: dict[str, Any]
) -> MessageStep | WaitStep | ConditionStep | TaskStep:
    """Reconstruye un paso a partir de su fila (`step_type` + `config`).

    Args:
        step_type: `lead_sequence_steps.step_type`.
        config: `lead_sequence_steps.config`.

    Returns:
        El paso validado.

    Raises:
        pydantic.ValidationError: Si la fila no es un paso valido.
    """
    return _STEP_ADAPTER.validate_python({**config, "type": step_type})


def paso_a_fila(
    paso: MessageStep | WaitStep | ConditionStep | TaskStep,
) -> tuple[str, dict[str, Any]]:
    """Separa un paso en `(step_type, config)` para guardarlo.

    Args:
        paso: Paso validado.

    Returns:
        El tipo y la configuracion (sin `type` y sin los valores por defecto nulos).
    """
    return paso.type, paso.model_dump(mode="json", exclude={"type"}, exclude_none=True)


# ─── Disparadores y definicion completa ─────────────────────────────────────────────────────

TriggerEvent = Literal["lead_created", "stage_changed", "score_changed"]


class TriggerConditions(BaseModel):
    """Cuando inscribir automaticamente a un lead.

    Sin `events` la secuencia solo se usa a mano. Con eventos, el lead entra si cumple **todos**
    los filtros que tengan valor (una lista vacia o `None` no filtra).

    Attributes:
        events: Momentos en que se evalua.
        source_types: Tipos de fuente del lead.
        stages: Slugs de etapa.
        temperatures: `cold`, `warm`, `hot`.
        min_total_score: Score total minimo.
        min_fit_score: FIT minimo.
    """

    model_config = ConfigDict(extra="forbid")

    events: list[TriggerEvent] = Field(default_factory=list)
    source_types: list[SourceType] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list, max_length=50)
    temperatures: list[Temperature] = Field(default_factory=list)
    min_total_score: int | None = Field(default=None, ge=0, le=100)
    min_fit_score: int | None = Field(default=None, ge=0, le=100)

    @field_validator("stages")
    @classmethod
    def _slugs(cls, valores: list[str]) -> list[str]:
        """Slugs de etapa validos."""
        for valor in valores:
            if not _SLUG.match(valor):
                raise ValueError(f"Slug de etapa no valido: {valor!r}")
        return list(dict.fromkeys(valores))

    @property
    def automatica(self) -> bool:
        """Si algun evento la dispara (si no, es solo manual)."""
        return bool(self.events)


def _prioridad_por_defecto() -> list[OutreachChannel]:
    """La misma prioridad que el `server_default` de `lead_sequences.channel_priority`."""
    return ["whatsapp", "email", "instagram"]


class SequenceDefinition(BaseModel):
    """Una secuencia completa, lista para guardarse.

    Attributes:
        name: Nombre (unico por tenant).
        description: Descripcion.
        trigger_conditions: Cuando inscribir automaticamente.
        channel_priority: Canales en orden para los mensajes en `auto`.
        steps: Pasos en orden; la posicion es el indice + 1.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    trigger_conditions: TriggerConditions = Field(default_factory=TriggerConditions)
    channel_priority: list[OutreachChannel] = Field(
        default_factory=_prioridad_por_defecto, min_length=1
    )
    steps: list[StepDefinition] = Field(min_length=1, max_length=MAX_STEPS)

    @field_validator("channel_priority")
    @classmethod
    def _sin_repetidos(cls, valores: list[str]) -> list[str]:
        """Cada canal una sola vez."""
        if len(set(valores)) != len(valores):
            raise ValueError("channel_priority no puede repetir canales")
        return valores

    @model_validator(mode="after")
    def _grafo_valido(self) -> Self:
        """Saltos a pasos existentes, algo que haga al lead y ningun bucle sin espera."""
        validar_grafo(self.steps)
        return self


def _saltos(paso: MessageStep | WaitStep | ConditionStep | TaskStep) -> list[int]:
    """Destinos de los `goto` de un paso."""
    if not isinstance(paso, ConditionStep):
        return []
    return [
        rama.goto_position
        for rama in (paso.if_true, paso.if_false)
        if rama.action == "goto" and rama.goto_position is not None
    ]


def validar_grafo(pasos: list[MessageStep | WaitStep | ConditionStep | TaskStep]) -> None:
    """Comprueba que una lista de pasos sea una secuencia ejecutable.

    Reglas: al menos un `message` o `task` (si no, la secuencia no hace nada); los `goto` van a
    una posicion que existe y distinta de la propia; y todo salto hacia atras (o al mismo sitio)
    encierra un bucle que **debe contener un `wait`**: un bucle sin espera enviaria mensajes sin
    parar o giraria sin fin.

    Args:
        pasos: Pasos en orden (posicion = indice + 1).

    Raises:
        ValueError: Con el motivo, si la secuencia no es valida.
    """
    if not any(isinstance(p, MessageStep | TaskStep) for p in pasos):
        raise ValueError("La secuencia necesita al menos un paso 'message' o 'task'")
    total = len(pasos)
    for posicion, paso in enumerate(pasos, start=1):
        for destino in _saltos(paso):
            if destino > total:
                raise ValueError(f"El paso {posicion} salta al paso {destino}, que no existe")
            if destino == posicion:
                raise ValueError(f"El paso {posicion} salta a si mismo")
            if destino < posicion and not any(
                isinstance(p, WaitStep) for p in pasos[destino - 1 : posicion]
            ):
                raise ValueError(
                    f"El salto del paso {posicion} al {destino} forma un bucle sin 'wait'"
                )
