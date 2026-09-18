"""Maquina de estados del ciclo de vida de una conversacion.

Los 7 estados del modelo (`conversations.status`) no son intercambiables: hay un
orden y solo ciertos saltos tienen sentido. Este modulo es el unico lugar donde
esas reglas viven, para que el endpoint de cambio de estado, el de asignacion y
el worker de auto-cierre no las reimplementen cada uno a su manera.

    new            -> bot_active, human_active
    bot_active     -> human_active, waiting_human, resolved
    human_active   -> waiting_client, resolved
    waiting_human  -> human_active, resolved
    waiting_client -> human_active, bot_active, resolved
    resolved       -> archived
    archived       -> (terminal)

La unica diferencia con la tabla de `specs/sprint-07-scheduling-crm.md` §9 es
`waiting_human -> resolved`. Sin ella, un handoff que nadie atiende queda
atrapado: desde `waiting_human` la spec solo deja ir a `human_active`, ningun
nodo del grafo vuelve a tocar esa conversacion (`human_handoff.py`, Sprint 6, la
deja ahi) y las dos reglas de auto-cierre miran `waiting_client` y `resolved`,
no `waiting_human`. La conversacion nunca se podria cerrar ni archivar.

`resolved` y `archived` son terminales para *esa* conversacion: no se reabren.
Cuando un contacto vuelve a escribir, `webhook_processor._get_or_create_conversation()`
descarta las cerradas (`CLOSED_STATUSES`) y abre una nueva. Por eso no existe
ninguna transicion de vuelta desde `resolved`, y `resolved_at` nunca se limpia.
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import VALIDATION_ERROR, AppException
from app.core.metrics import record_conversation_resolved
from app.models.conversation import Conversation

logger = logging.getLogger(__name__)

# Transiciones permitidas: {estado_actual: (estados_alcanzables, ...)}.
VALID_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "new": ("bot_active", "human_active"),
    "bot_active": ("human_active", "waiting_human", "resolved"),
    "human_active": ("waiting_client", "resolved"),
    "waiting_human": ("human_active", "resolved"),
    "waiting_client": ("human_active", "bot_active", "resolved"),
    "resolved": ("archived",),
    "archived": (),
}

# Estados desde los que ya no hay vuelta atras.
TERMINAL_STATUSES = ("archived",)


class ConversationLifecycle:
    """Aplica las transiciones de estado de una conversacion.

    No abre ni cierra la transaccion: recibe la conversacion ya cargada en una
    sesion con contexto de tenant y solo muta el objeto. El commit lo hace quien
    llama (el endpoint, dentro de su `tenant_session()`).

    Attributes:
        session: Sesion de la que proviene la conversacion. Se guarda para que
            los llamadores puedan encadenar operaciones sin pasarla de nuevo.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Guarda la sesion de trabajo.

        Args:
            session: AsyncSession con contexto de tenant.
        """
        self.session = session

    async def transition(
        self,
        conversation: Conversation,
        new_status: str,
        user_id: UUID | None = None,
    ) -> Conversation:
        """Mueve la conversacion a `new_status` aplicando los efectos laterales.

        Efectos por estado destino:
            - `human_active`: si viene `user_id`, queda asignada a ese agente.
            - `resolved`: sella `resolved_at`.
            - `bot_active`: suelta la asignacion humana (vuelve al bot).

        `updated_at` no se toca a mano: la columna tiene `onupdate=func.now()` y
        el UPDATE lo refresca solo. Escribirlo aqui daria la hora del proceso de
        Python en vez de la del servidor, y el auto-cierre compara contra `now()`
        de PostgreSQL.

        Args:
            conversation: Conversacion a mover, ya cargada en la sesion.
            new_status: Estado destino.
            user_id: Agente humano a asignar cuando el destino es `human_active`.

        Returns:
            La misma conversacion, ya mutada.

        Raises:
            AppException: 400 si la transicion no esta permitida.
        """
        current_status = conversation.status

        if new_status not in VALID_TRANSITIONS.get(current_status, ()):
            permitidas = VALID_TRANSITIONS.get(current_status, ())
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message=(
                    f"Transicion no permitida: {current_status} -> {new_status}. "
                    f"Desde '{current_status}' se puede pasar a: "
                    f"{', '.join(permitidas) if permitidas else 'ningun estado (terminal)'}"
                ),
            )

        conversation.status = new_status

        if new_status == "human_active" and user_id is not None:
            conversation.assigned_user_id = user_id

        if new_status == "resolved":
            conversation.resolved_at = datetime.now(timezone.utc)
            # Quien cierra: un humano si la conversacion estaba en sus manos, el
            # agente si venia del bot. Es la base de la "tasa de resolucion" del
            # dashboard de agentes: sin esta distincion, las conversaciones que
            # cierra un humano tras un handoff contarian como exito de la IA.
            cerrada_por = (
                "human" if current_status in ("human_active", "waiting_human") else "agent"
            )
            record_conversation_resolved(str(conversation.client_id), cerrada_por)

        if new_status == "bot_active":
            # Devolver la conversacion al bot implica que ya no hay humano a cargo;
            # dejar el assigned_user_id puesto haria que la bandeja del agente la
            # siguiera mostrando como suya.
            conversation.assigned_user_id = None

        logger.info(
            "Conversacion %s: %s -> %s",
            conversation.id,
            current_status,
            new_status,
        )
        return conversation

    @staticmethod
    def validate_transition(current_status: str, new_status: str) -> bool:
        """Dice si una transicion esta permitida, sin ejecutarla.

        Args:
            current_status: Estado de partida.
            new_status: Estado destino.

        Returns:
            True si la transicion es valida.
        """
        return new_status in VALID_TRANSITIONS.get(current_status, ())

    @staticmethod
    def get_valid_transitions(status: str) -> list[str]:
        """Lista los estados alcanzables desde `status`.

        Args:
            status: Estado de partida.

        Returns:
            Estados a los que se puede pasar; lista vacia si es terminal o si el
            estado no existe.
        """
        return list(VALID_TRANSITIONS.get(status, ()))
