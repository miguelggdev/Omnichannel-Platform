"""Tests de la maquina de estados de conversaciones.

No tocan la base: `ConversationLifecycle.transition()` solo muta el objeto que
recibe, el commit lo hace quien llama.
"""

import uuid

import pytest

from app.core.exceptions import AppException
from app.services.conversation_lifecycle import (
    VALID_TRANSITIONS,
    ConversationLifecycle,
)
from tests.unit.crm_doubles import CrmSession, FakeConversation

ESTADOS = (
    "new",
    "bot_active",
    "human_active",
    "waiting_human",
    "waiting_client",
    "resolved",
    "archived",
)


@pytest.fixture
def lifecycle() -> ConversationLifecycle:
    """Instancia con una sesion falsa: las transiciones no la usan."""
    return ConversationLifecycle(CrmSession())  # type: ignore[arg-type]


# ─── Tabla de transiciones ───────────────────────────────────────────────────


def test_la_tabla_cubre_los_siete_estados() -> None:
    """Los 7 estados del enum de la base estan en la tabla, ni uno mas ni menos."""
    assert set(VALID_TRANSITIONS) == set(ESTADOS)


def test_ningun_destino_es_un_estado_inexistente() -> None:
    """Ninguna transicion apunta a un estado que la base no acepta."""
    for origen, destinos in VALID_TRANSITIONS.items():
        for destino in destinos:
            assert destino in ESTADOS, f"{origen} -> {destino} no es un estado valido"


def test_archived_es_terminal() -> None:
    """Desde archived no se sale."""
    assert ConversationLifecycle.get_valid_transitions("archived") == []


def test_resolved_solo_va_a_archived() -> None:
    """Una conversacion resuelta no se reabre: el webhook abre una nueva."""
    assert ConversationLifecycle.get_valid_transitions("resolved") == ["archived"]


def test_un_estado_desconocido_no_tiene_transiciones() -> None:
    """Un status que no existe no habilita ningun salto."""
    assert ConversationLifecycle.get_valid_transitions("inventado") == []
    assert ConversationLifecycle.validate_transition("inventado", "bot_active") is False


@pytest.mark.parametrize(
    ("origen", "destino"),
    [(o, d) for o, destinos in VALID_TRANSITIONS.items() for d in destinos],
)
def test_validate_transition_acepta_las_de_la_tabla(origen: str, destino: str) -> None:
    """Todo lo que la tabla declara valido, validate_transition lo acepta."""
    assert ConversationLifecycle.validate_transition(origen, destino) is True


@pytest.mark.parametrize(
    ("origen", "destino"),
    [
        (o, d)
        for o in ESTADOS
        for d in ESTADOS
        if d not in VALID_TRANSITIONS[o]  # incluye los saltos a si mismo
    ],
)
def test_validate_transition_rechaza_el_resto(origen: str, destino: str) -> None:
    """Cualquier salto fuera de la tabla se rechaza, incluido el de un estado a si mismo."""
    assert ConversationLifecycle.validate_transition(origen, destino) is False


# ─── transition() ────────────────────────────────────────────────────────────


async def test_transicion_valida_cambia_el_status(lifecycle: ConversationLifecycle) -> None:
    """La transicion permitida deja el estado nuevo en el objeto."""
    conv = FakeConversation(status="new")

    await lifecycle.transition(conv, "bot_active")  # type: ignore[arg-type]

    assert conv.status == "bot_active"


async def test_transicion_invalida_levanta_400(lifecycle: ConversationLifecycle) -> None:
    """Un salto fuera de la tabla es un 400, no un estado corrupto."""
    conv = FakeConversation(status="archived")

    with pytest.raises(AppException) as exc:
        await lifecycle.transition(conv, "bot_active")  # type: ignore[arg-type]

    assert exc.value.status_code == 400
    # El estado no se toco.
    assert conv.status == "archived"


async def test_el_mensaje_de_error_lista_las_transiciones_posibles(
    lifecycle: ConversationLifecycle,
) -> None:
    """El 400 dice a donde SI se puede ir, para que el cliente no adivine."""
    conv = FakeConversation(status="new")

    with pytest.raises(AppException) as exc:
        await lifecycle.transition(conv, "resolved")  # type: ignore[arg-type]

    assert "bot_active" in exc.value.message
    assert "human_active" in exc.value.message


async def test_resolver_sella_resolved_at(lifecycle: ConversationLifecycle) -> None:
    """Pasar a resolved deja la marca de tiempo que usa el auto-archivado."""
    conv = FakeConversation(status="waiting_client", resolved_at=None)

    await lifecycle.transition(conv, "resolved")  # type: ignore[arg-type]

    assert conv.resolved_at is not None
    assert conv.resolved_at.tzinfo is not None, "resolved_at debe llevar timezone"


async def test_archivar_conserva_resolved_at(lifecycle: ConversationLifecycle) -> None:
    """Archivar no borra cuando se resolvio."""
    conv = FakeConversation(status="resolved")
    conv.resolved_at = "2026-09-01"

    await lifecycle.transition(conv, "archived")  # type: ignore[arg-type]

    assert conv.status == "archived"
    assert conv.resolved_at == "2026-09-01"


async def test_human_active_asigna_al_usuario(lifecycle: ConversationLifecycle) -> None:
    """Pasar a human_active con user_id deja la conversacion asignada."""
    conv = FakeConversation(status="waiting_human", assigned_user_id=None)
    agente = uuid.uuid4()

    await lifecycle.transition(conv, "human_active", user_id=agente)  # type: ignore[arg-type]

    assert conv.assigned_user_id == agente


async def test_human_active_sin_user_id_no_pisa_la_asignacion(
    lifecycle: ConversationLifecycle,
) -> None:
    """Sin user_id la asignacion previa se respeta."""
    previo = uuid.uuid4()
    conv = FakeConversation(status="waiting_human", assigned_user_id=previo)

    await lifecycle.transition(conv, "human_active")  # type: ignore[arg-type]

    assert conv.assigned_user_id == previo


async def test_volver_al_bot_suelta_la_asignacion(lifecycle: ConversationLifecycle) -> None:
    """Si la conversacion vuelve al bot, deja de estar en la bandeja del agente."""
    conv = FakeConversation(status="waiting_client", assigned_user_id=uuid.uuid4())

    await lifecycle.transition(conv, "bot_active")  # type: ignore[arg-type]

    assert conv.assigned_user_id is None


async def test_transition_no_escribe_updated_at(lifecycle: ConversationLifecycle) -> None:
    """`updated_at` lo pone PostgreSQL (onupdate), no el proceso de Python.

    El auto-cierre compara `updated_at` contra `now()` del servidor; si aqui se
    escribiera la hora local, cualquier desfase correria el umbral de 24 h.
    """
    conv = FakeConversation(status="new")
    original = conv.updated_at

    await lifecycle.transition(conv, "bot_active")  # type: ignore[arg-type]

    assert conv.updated_at == original


async def test_el_handoff_no_atendido_se_puede_cerrar(lifecycle: ConversationLifecycle) -> None:
    """waiting_human -> resolved: unica diferencia deliberada con la spec §9.

    `human_handoff.py` (Sprint 6) deja la conversacion en `waiting_human` y
    ningun nodo vuelve a tocarla. Sin esta transicion, un handoff que nadie
    atiende no se puede cerrar ni archivar nunca.
    """
    conv = FakeConversation(status="waiting_human")

    await lifecycle.transition(conv, "resolved")  # type: ignore[arg-type]

    assert conv.status == "resolved"
    assert conv.resolved_at is not None
