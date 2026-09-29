"""Privacidad de las conversaciones clinicas fuera de `clinical_records` (ADR-072).

Lo que un profesional dicta es dato de salud (Ley 1581, art. 5). El registro
que interesa vive cifrado en `clinical_records`; este modulo evita que el mismo
texto quede ademas en claro en otras tablas:

- `messages.content` y los logs los protege el nodo clinico y `respond`;
- **`call_records.transcript`** (canal de voz, Dev A) es JSONB en claro. Cifrarlo
  se descarto (decision del usuario: rompe el listado de llamadas del CRM); en
  su lugar se **redacta el texto de los turnos** de toda llamada cuya conversacion
  fue clinica y se conservan el rol y la hora de cada turno, que bastan para el
  listado y para auditar la llamada.

El intent solo se conoce dentro del grafo, y la transcripcion la guarda la
sesion de la llamada al colgar (`voice_tasks.guardar_llamada`), a veces *antes*
de que el grafo procese la ultima frase. Por eso la proteccion va por los dos
lados: el nodo clinico marca la conversacion (`metadata.clinical`) y redacta lo
que ya se guardo, y `guardar_llamada` redacta lo que se guarde despues si la
conversacion ya esta marcada.

Toda la llamada se trata como clinica una vez que lo fue: separar turno a turno
exigiria saber cual de ellos fue el dictado, y en un consultorio es preferible
perder detalle de lo no clinico que dejar un dictado en claro.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: Lo que queda en `messages.content` y en `call_records.transcript` de un turno clinico.
CONTENIDO_CLINICO_PROTEGIDO = "[contenido clínico protegido]"

_MARCAR = text(
    """
    UPDATE conversations
    SET metadata = COALESCE(metadata, '{}'::jsonb) || '{"clinical": true}'::jsonb
    WHERE id = :conversation_id AND client_id = :client_id
    """
)

_ES_CLINICA = text(
    """
    SELECT COALESCE((metadata ->> 'clinical')::boolean, false)
    FROM conversations
    WHERE id = :conversation_id AND client_id = :client_id
    """
)

_REDACTAR_LLAMADAS = text(
    """
    UPDATE call_records
    SET transcript = COALESCE(
        (
            SELECT jsonb_agg(jsonb_set(turno, '{text}', to_jsonb(CAST(:marcador AS text))))
            FROM jsonb_array_elements(transcript) AS turno
        ),
        '[]'::jsonb
    )
    WHERE client_id = :client_id
      AND conversation_id = :conversation_id
      AND jsonb_array_length(transcript) > 0
    """
)


def redactar_transcripcion(turnos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reemplaza el texto de cada turno y conserva el resto.

    Args:
        turnos: `[{role, text, timestamp}]` tal como los arma la sesion de voz.

    Returns:
        Los mismos turnos con `text` reemplazado por `CONTENIDO_CLINICO_PROTEGIDO`.
    """
    return [{**turno, "text": CONTENIDO_CLINICO_PROTEGIDO} for turno in turnos]


async def marcar_conversacion_clinica(
    session: AsyncSession, client_id: UUID, conversation_id: UUID
) -> None:
    """Marca la conversacion como clinica en `conversations.metadata`.

    Concatenacion JSONB en un solo `UPDATE`: la columna tambien guarda el
    handoff y otros datos, y no se pisan (misma leccion que BUG-022).

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant dueno.
        conversation_id: Conversacion a marcar.
    """
    await session.execute(
        _MARCAR, {"client_id": str(client_id), "conversation_id": str(conversation_id)}
    )


async def conversacion_es_clinica(
    session: AsyncSession, client_id: UUID, conversation_id: UUID
) -> bool:
    """Si la conversacion ya fue marcada como clinica.

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant dueno.
        conversation_id: Conversacion a consultar.

    Returns:
        `True` si `metadata.clinical` es verdadero.
    """
    resultado = (
        await session.execute(
            _ES_CLINICA, {"client_id": str(client_id), "conversation_id": str(conversation_id)}
        )
    ).scalar_one_or_none()
    return bool(resultado)


async def proteger_llamadas_de_la_conversacion(
    session: AsyncSession, client_id: UUID, conversation_id: UUID
) -> None:
    """Redacta la transcripcion de las llamadas ya guardadas de la conversacion.

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant dueno.
        conversation_id: Conversacion clinica.
    """
    await session.execute(
        _REDACTAR_LLAMADAS,
        {
            "client_id": str(client_id),
            "conversation_id": str(conversation_id),
            "marcador": CONTENIDO_CLINICO_PROTEGIDO,
        },
    )
