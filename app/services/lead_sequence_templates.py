"""Secuencias predefinidas que un tenant puede copiar (Sprint 18).

Tres puntos de partida del spec: nurturing de leads entrantes, prospeccion en frio y
reactivacion. Son `SequenceDefinition` normales: se validan al importar este modulo (un error en
una plantilla rompe el arranque y los tests, no el envio de un lunes), y el tenant las guarda con
`crear_secuencia()` y luego las edita como cualquier otra.

Ninguna se dispara sola salvo la de entrantes: inscribir en frio o reactivar a alguien es una
decision comercial que toma una persona.
"""

from app.schemas.lead_sequence import (
    Branch,
    ConditionStep,
    MessageStep,
    SequenceDefinition,
    TaskStep,
    TriggerConditions,
    WaitStep,
)

INBOUND_NURTURING = "inbound_nurturing"
OUTBOUND_COLD = "outbound_cold"
REENGAGEMENT = "reengagement"

_SALIR = Branch(action="exit")

PLANTILLAS: dict[str, SequenceDefinition] = {
    INBOUND_NURTURING: SequenceDefinition(
        name="Nurturing de leads entrantes",
        description=(
            "Bienvenida inmediata a quien dejo sus datos, dos recordatorios y una tarea para "
            "llamar si no contesta. Sale en cuanto el lead responde."
        ),
        trigger_conditions=TriggerConditions(events=["lead_created"]),
        steps=[
            MessageStep(
                body=(
                    "Hola {{first_name}}, gracias por escribir a {{business_name}}. "
                    "Soy {{agent_name}}. ¿En que te puedo ayudar?"
                ),
                subject="Gracias por tu interes en {{business_name}}",
            ),
            WaitStep(amount=1, unit="days"),
            ConditionStep(check="replied", if_true=_SALIR),
            MessageStep(
                body=(
                    "Hola {{first_name}}, ¿pudiste revisar lo que te enviamos? "
                    "Quedo atento a cualquier pregunta."
                )
            ),
            WaitStep(amount=3, unit="days", smart_timing=True),
            ConditionStep(check="replied", if_true=_SALIR),
            TaskStep(title="Llamar al lead: no respondio a dos mensajes", due_in_hours=24),
        ],
    ),
    OUTBOUND_COLD: SequenceDefinition(
        name="Prospeccion en frio",
        description=(
            "Primer contacto por email con seguimiento y un ultimo mensaje por otro canal. "
            "Inscribir a mano."
        ),
        channel_priority=["email", "whatsapp"],
        steps=[
            MessageStep(
                channel="email",
                subject="{{company_name}} y {{business_name}}",
                body=(
                    "Hola {{first_name}}, vi que eres {{job_title}} en {{company_name}}. "
                    "Ayudamos a equipos como el tuyo a responder mas rapido a sus clientes. "
                    "¿Te interesa una llamada de 15 minutos?"
                ),
            ),
            WaitStep(amount=3, unit="days"),
            ConditionStep(check="replied", if_true=_SALIR),
            MessageStep(
                channel="email",
                subject="Re: {{company_name}} y {{business_name}}",
                body="Hola {{first_name}}, solo queria asegurarme de que te llego mi mensaje.",
            ),
            WaitStep(amount=4, unit="days", smart_timing=True),
            ConditionStep(check="replied", if_true=_SALIR),
            MessageStep(
                body=(
                    "Hola {{first_name}}, te escribi por correo hace unos dias. "
                    "Si no es buen momento, no hay problema."
                )
            ),
        ],
    ),
    REENGAGEMENT: SequenceDefinition(
        name="Reactivacion",
        description=(
            "Para leads que se enfriaron: un mensaje, una espera larga y una tarea si vuelve a "
            "mostrar interes. Inscribir a mano."
        ),
        steps=[
            MessageStep(
                body=(
                    "Hola {{first_name}}, hace tiempo que no hablamos. En {{business_name}} "
                    "tenemos novedades que pueden interesarte. ¿Te cuento?"
                )
            ),
            WaitStep(amount=7, unit="days", smart_timing=True),
            ConditionStep(check="no_reply", if_true=_SALIR),
            TaskStep(title="El lead reactivado respondio: dar seguimiento", due_in_hours=4),
        ],
    ),
}
