"""Tests del nodo de modo entrenamiento (Sprint 6).

Contrato: `specs/sprint-06-langgraph.md` §10. Lo critico aqui es que la respuesta
generada **no** llegue al contacto: queda en `pending_responses` esperando que un
supervisor la apruebe.
"""

import uuid
from typing import Any

import pytest

from app.agents.nodes import training_approval as nodo
from app.models.pending_response import PendingResponse
from tests.unit.agent_doubles import FakeSession, estado, parchear_tenant_session

RESPUESTA = "Atendemos de 8 a 18. [Fuente: FAQ, pag. 1]"


def _preparar(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeSession, list[dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    """Deja el nodo listo con base, envio y notificaciones falsos.

    Args:
        monkeypatch: Fixture de pytest.

    Returns:
        Tupla `(sesion, envios, avisos)`.
    """
    sesion = parchear_tenant_session(monkeypatch, nodo, FakeSession())

    envios: list[dict[str, Any]] = []

    async def _deliver(**kwargs: Any) -> str:
        envios.append(kwargs)
        return "ext-1"

    monkeypatch.setattr(nodo, "deliver_message", _deliver)

    avisos: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        nodo,
        "enqueue_notification",
        lambda task_attr, **kwargs: avisos.append((task_attr, kwargs)) or True,
    )
    return sesion, envios, avisos


class TestModoEntrenamiento:
    """La respuesta se retiene; el contacto recibe solo el aviso de revision."""

    async def test_guarda_la_respuesta_como_pendiente(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pregunta y respuesta generada quedan en `pending_responses`."""
        sesion, _, _ = _preparar(monkeypatch)
        state = estado(response_text=RESPUESTA, message={"text": "Cual es el horario?"})

        await nodo.training_approval_node(state)

        pendientes = sesion.agregados_de(PendingResponse)
        assert len(pendientes) == 1
        assert pendientes[0].question == "Cual es el horario?"
        assert pendientes[0].generated_response == RESPUESTA
        assert pendientes[0].status == "pending"
        assert pendientes[0].conversation_id == uuid.UUID(state["conversation_id"])

    async def test_no_envia_la_respuesta_real(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Al contacto le llega el aviso de revision, nunca la respuesta candidata."""
        _, envios, _ = _preparar(monkeypatch)

        resultado = await nodo.training_approval_node(estado(response_text=RESPUESTA))

        assert envios[0]["text"] == nodo.WAITING_MESSAGE
        assert RESPUESTA not in envios[0]["text"]
        assert resultado["response_text"] is None
        assert resultado["training_mode"] is True

    async def test_avisa_al_supervisor_con_el_id_pendiente(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La notificacion lleva el id de la respuesta a revisar."""
        sesion, _, avisos = _preparar(monkeypatch)

        await nodo.training_approval_node(estado(response_text=RESPUESTA))

        pendiente = sesion.agregados_de(PendingResponse)[0]
        assert avisos[0][0] == "notify_pending_response"
        assert avisos[0][1]["pending_response_id"] == str(pendiente.id)

    async def test_sin_respuesta_generada_igual_deja_constancia(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Llegar sin texto es un error de routing, pero la pregunta no se pierde."""
        sesion, envios, _ = _preparar(monkeypatch)

        resultado = await nodo.training_approval_node(
            estado(response_text=None, message={"text": "Hola?"})
        )

        pendientes = sesion.agregados_de(PendingResponse)
        assert pendientes[0].question == "Hola?"
        assert pendientes[0].generated_response == ""
        assert envios[0]["text"] == nodo.WAITING_MESSAGE
        assert resultado["response_text"] is None
