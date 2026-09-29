"""Nodo del agente clinico: dictado medico, RIPS, CIE-10 y CUPS (Sprint 13, Dev B).

Contrato: `specs/sprint-13-advanced-modules.md` §9.1. Tool calling sobre
`clinical_tools.py`, con temperatura 0: nada de datos medicos "creativos".

Quien puede usarlo
-------------------
El spec pregunta por `state["user_role"]` (rol `medical`) y por
`state["habeas_data_consent"]`; ninguno de los dos existe en
`ConversationState`, y ademas el grafo atiende a *contactos* de un canal, no a
usuarios con rol. Se resuelve igual que el agente de marketing (BUG-045,
ADR-070):

- el agente tiene que estar en `agent_configs.config.enabled_agents`;
- y solo atiende a los contactos declarados en
  `agent_configs.config.clinical.professional_contact_ids` (se administran con
  `PUT /api/v1/clinical/settings`). Habilitar el agente no dice nada de quien
  escribe, y este grafo es el mismo que le contesta a los clientes finales por
  WhatsApp: sin la lista, cualquiera podria pedirle historial de un paciente.

El consentimiento de Habeas Data no se pregunta aca: depende del *paciente*, que
no se conoce hasta que el profesional dicta el documento. Lo exige la tool
`create_rips_record`, dentro de la misma transaccion que inserta (no lo puede
saltar el modelo).

Con los datos de salud en la conversacion, `logged_node()` no guarda el texto de
lo dictado ni de la respuesta en `agent_action_logs` (ver
`app/agents/middleware/logging_middleware.py`).
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import update

from app.agents.nodes._agent_tools import responder_con_tools
from app.agents.nodes._state import ConversationState
from app.agents.nodes._tenant import get_agent_settings
from app.agents.tools.clinical_tools import CLINICAL_TOOLS
from app.core.database import tenant_session
from app.models.message import Message
from app.services.channel_identity import identidad_verificada
from app.services.clinical import es_profesional_clinico
from app.services.clinical_privacy import (
    CONTENIDO_CLINICO_PROTEGIDO,
    marcar_conversacion_clinica,
    proteger_llamadas_de_la_conversacion,
)

logger = logging.getLogger(__name__)

OPERATION = "clinical"
AGENTE = "clinical"
INTENT = "clinical"

MENSAJE_NO_HABILITADO = (
    "El modulo clinico no esta habilitado para esta cuenta. "
    "Un administrador puede activarlo desde la configuracion."
)

#: Respuesta a un contacto que no es profesional del tenant. No menciona
#: pacientes ni registros: quien escribe es, casi siempre, un cliente.
MENSAJE_NO_AUTORIZADO = (
    "No puedo ayudarte con eso por este canal. Si tienes otra consulta, con gusto te ayudo."
)

SYSTEM_PROMPT_TEMPLATE = """Eres un agente clinico especializado en documentacion medica colombiana.

Tu funcion es ayudar al profesional de salud que te dicta a:
1. Extraer entidades medicas del dictado (sintomas, diagnosticos, procedimientos, medicamentos)
2. Codificar diagnosticos segun CIE-10 y procedimientos segun CUPS
3. Estructurar notas clinicas en formato SOAP (Subjetivo, Objetivo, Evaluacion, Plan)
4. Crear registros RIPS (Resolucion 3374 de 2000) en BORRADOR
5. Consultar el historial de registros de un paciente

Reglas ESTRICTAS:
- NUNCA sugieras diagnosticos ni procedimientos. Solo codifica lo que el profesional
  dicte de forma explicita.
- Si una herramienta no encuentra un codigo, NO propongas uno: pidele al profesional
  que lo dicte.
- Si hay mas de un codigo posible, presenta las opciones y deja que el profesional elija.
- Antes de guardar, MUESTRA el detalle completo (paciente, fecha, tipo RIPS,
  diagnosticos, procedimientos, finalidad) y pide confirmacion expresa del profesional.
- La finalidad de la atencion (01 a 10) y, en urgencias, la causa externa las dicta el
  profesional: no las asumas.
- Los datos del paciente son CONFIDENCIALES (Ley 1581 de 2012, Habeas Data).
- Solo registra datos de un paciente si el profesional confirma que el paciente autorizo
  el tratamiento de sus datos de salud; en ese caso registra la autorizacion con
  register_patient_consent y luego crea el registro. Si la herramienta responde que
  falta la autorizacion, explicaselo al profesional, no insistas.
- Los registros que creas quedan en borrador: la revision y la firma son del profesional.
- No repitas el documento completo del paciente en tus respuestas.

Formato RIPS:
- AC (Consulta): finalidad, causa externa si aplica, diagnostico principal + hasta 3 relacionados
- AP (Procedimiento): al menos un codigo CUPS, finalidad
- AU (Urgencia): causa externa obligatoria, diagnostico principal
- AH (Hospitalizacion): diagnostico principal, causa externa

Fecha actual: {fecha}
"""


async def _proteger_mensaje_entrante(state: ConversationState) -> None:
    """Saca lo dictado de `messages` y de la transcripcion de la llamada.

    El dictado ya se uso (esta en el estado en memoria y en la respuesta del
    LLM); dejarlo ademas en claro lo pondria en el historial, en el inbox, en
    el export RGPD de contactos y —si fue por telefono— en
    `call_records.transcript`, fuera del cifrado y de la retencion de la
    historia clinica. El registro que interesa vive cifrado en
    `clinical_records`.

    Hace tres cosas en una transaccion: reemplaza `messages.content` del mensaje
    entrante por un marcador, marca la conversacion como clinica
    (`conversations.metadata.clinical`, que consulta `voice_tasks` para redactar
    la transcripcion que se guarde despues) y redacta la transcripcion ya
    guardada. Ver `app/services/clinical_privacy.py`.

    Es un control de privacidad: si falla se registra como error, pero no
    tumba la respuesta al profesional.

    Args:
        state: Estado del grafo; usa `client_id`, `conversation_id` y
            `message.external_message_id`.
    """
    if not state.get("conversation_id"):
        return
    client_id = UUID(state["client_id"])
    conversation_id = UUID(state["conversation_id"])
    externo = (state.get("message") or {}).get("external_message_id")
    try:
        async with tenant_session(client_id) as session:
            if externo:
                await session.execute(
                    update(Message)
                    .where(
                        Message.client_id == client_id,
                        Message.conversation_id == conversation_id,
                        Message.direction == "inbound",
                        Message.external_message_id == externo,
                    )
                    .values(content=CONTENIDO_CLINICO_PROTEGIDO)
                )
            await marcar_conversacion_clinica(session, client_id, conversation_id)
            # Una llamada corta se guarda antes de ligarse a la conversacion:
            # ademas de por conversacion, se busca por el CallSid del mensaje
            # (`CallSid:indice`, ver `voice_tasks`).
            call_sid = (
                externo.split(":", 1)[0] if externo and state.get("channel") == "voice" else None
            )
            await proteger_llamadas_de_la_conversacion(
                session, client_id, conversation_id, call_sid
            )
    except Exception:
        logger.exception("No se pudo proteger el contenido clinico de la conversacion")


async def clinical_agent_node(state: ConversationState) -> dict[str, Any]:
    """Atiende un dictado o consulta clinica con el modelo del tenant y sus tools.

    Args:
        state: Estado del grafo; usa `client_id`, `conversation_id`,
            `contact_id`, `message` y `model_to_use`.

    Returns:
        Dict parcial con `response_text` e `intent`.
    """
    client_id = state["client_id"]
    ajustes = await get_agent_settings(UUID(client_id))

    if AGENTE not in ajustes.enabled_agents:
        logger.info("Agente clinico no habilitado para el tenant %s", client_id)
        return {"response_text": MENSAJE_NO_HABILITADO, "intent": INTENT}

    # Sin gastar una llamada al LLM: un cliente final que dice "necesito el
    # historial de un paciente" no tiene que llegar a ver las tools.
    # Y solo cuenta un canal que identifique de verdad al contacto: el caller ID
    # de una llamada o el `From` de un email se falsifican.
    autorizado = identidad_verificada(state.get("channel"))
    if autorizado:
        async with tenant_session(UUID(client_id)) as session:
            autorizado = await es_profesional_clinico(
                session, UUID(client_id), state.get("contact_id")
            )
    if not autorizado:
        logger.warning(
            "Contacto %s del tenant %s pidio el agente clinico sin ser profesional "
            "o por un canal sin identidad verificada (%s)",
            state.get("contact_id"),
            client_id,
            state.get("channel"),
        )
        return {"response_text": MENSAJE_NO_AUTORIZADO, "intent": INTENT}

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        fecha=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    try:
        texto = await responder_con_tools(
            state=dict(state),
            tools=CLINICAL_TOOLS,
            system_prompt=system_prompt,
            modelo=state.get("model_to_use") or ajustes.model,
            operacion=OPERATION,
            # Datos medicos: la temperatura mas baja del grafo (spec §11).
            temperatura=0.0,
        )
    finally:
        # Tambien si el LLM fallo: el reintento de la tarea usa el texto del
        # argumento de Celery, no el de la base.
        await _proteger_mensaje_entrante(state)
    return {"response_text": texto, "intent": INTENT}
