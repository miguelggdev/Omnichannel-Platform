"""Tests del nodo de presupuesto y del registro de consumo (Sprint 6).

Contrato: `specs/sprint-06-langgraph.md` §6 y §7. Ni Redis ni PostgreSQL son
reales: se sustituyen `tenant_session` y `get_redis` en cada modulo bajo prueba.
"""

import json
import uuid
from typing import Any

import pytest

from app.agents.nodes import token_budget as nodo
from app.agents.nodes._tenant import AgentSettings
from app.middleware import token_budget as guard_module
from app.middleware.token_budget import TokenBudgetGuard, estimate_cost_usd
from app.models.token_usage_log import TokenUsageLog
from tests.unit.agent_doubles import (
    FakeRedis,
    FakeSession,
    estado,
    parchear_agent_settings,
    parchear_tenant_session,
)

TENANT_MODEL = "gpt-4o"


class FakeBudget:
    """Fila de `token_budgets` con lo que leen el nodo y el guard."""

    def __init__(self, total_budget: int, used_tokens: int) -> None:
        """Prepara el presupuesto.

        Args:
            total_budget: Tokens contratados en el mes.
            used_tokens: Tokens ya consumidos.
        """
        self.total_budget = total_budget
        self.used_tokens = used_tokens


def _preparar(
    monkeypatch: pytest.MonkeyPatch,
    budget: FakeBudget | None,
    redis: FakeRedis | None = None,
    settings: AgentSettings | None = None,
) -> tuple[FakeSession, FakeRedis]:
    """Deja el nodo listo para correr sin base ni Redis reales.

    Args:
        monkeypatch: Fixture de pytest.
        budget: Fila de presupuesto que devuelve la consulta, o None.
        redis: Doble de Redis; si no se da, se crea uno vacio.
        settings: Configuracion del agente; por defecto, modelo del tenant.

    Returns:
        Tupla `(sesion, redis)` para inspeccionar en el test.
    """
    sesion = FakeSession(resultados=[budget])
    parchear_tenant_session(monkeypatch, nodo, sesion)
    fake_redis = redis or FakeRedis()
    monkeypatch.setattr(nodo, "get_redis", lambda: fake_redis)
    parchear_agent_settings(monkeypatch, nodo, settings or AgentSettings(model=TENANT_MODEL))
    return sesion, fake_redis


# ─── Umbrales de ADR-004 ─────────────────────────────────────────────────────


class TestUmbrales:
    """Los tres niveles de degradacion del presupuesto."""

    async def test_uso_bajo_queda_en_ok_con_el_modelo_del_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con 50% consumido se sigue con el modelo configurado por el tenant."""
        _preparar(monkeypatch, FakeBudget(total_budget=10_000, used_tokens=5_000))

        resultado = await nodo.token_budget_check_node(estado())

        assert resultado["budget_status"] == "ok"
        assert resultado["model_to_use"] == TENANT_MODEL
        assert resultado["budget_usage_pct"] == pytest.approx(50.0)
        assert resultado["requires_handoff"] is False

    async def test_uso_alto_degrada_el_modelo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Entre 90% y 99% se baja al modelo barato, sin escalar."""
        _preparar(monkeypatch, FakeBudget(total_budget=10_000, used_tokens=9_200))

        resultado = await nodo.token_budget_check_node(estado())

        assert resultado["budget_status"] == "degraded"
        assert resultado["model_to_use"] == "gpt-4o-mini"
        assert resultado["requires_handoff"] is False

    async def test_justo_en_90_ya_degrada(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El umbral es inclusivo: 90% exacto ya degrada."""
        _preparar(monkeypatch, FakeBudget(total_budget=10_000, used_tokens=9_000))

        resultado = await nodo.token_budget_check_node(estado())

        assert resultado["budget_status"] == "degraded"

    async def test_presupuesto_agotado_escala_a_humano(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Al 100% o mas se escala sin gastar un token mas."""
        _preparar(monkeypatch, FakeBudget(total_budget=10_000, used_tokens=10_500))

        resultado = await nodo.token_budget_check_node(estado())

        assert resultado["budget_status"] == "exceeded"
        assert resultado["requires_handoff"] is True
        assert resultado["handoff_reason"] == "budget_exceeded"

    async def test_sin_fila_de_presupuesto_no_hay_limite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un tenant sin presupuesto configurado no se queda sin servicio."""
        _preparar(monkeypatch, None)

        resultado = await nodo.token_budget_check_node(estado())

        assert resultado["budget_status"] == "ok"
        assert resultado["budget_usage_pct"] == 0.0


# ─── Cache de Redis ──────────────────────────────────────────────────────────


class TestCache:
    """La cache ahorra consultas, pero no es una dependencia dura."""

    async def test_el_uso_se_cachea_tras_consultar_la_base(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Despues de leer la base, el uso queda en Redis con la clave del tenant."""
        _, redis = _preparar(monkeypatch, FakeBudget(total_budget=1_000, used_tokens=100))
        state = estado()

        await nodo.token_budget_check_node(state)

        clave = nodo.cache_key(state["client_id"])
        assert json.loads(redis.store[clave]) == {"total_budget": 1_000, "used_tokens": 100}

    async def test_con_cache_no_se_consulta_la_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si la clave esta en Redis, el nodo no toca `token_budgets`."""
        state = estado()
        redis = FakeRedis()
        redis.store[nodo.cache_key(state["client_id"])] = json.dumps(
            {"total_budget": 10_000, "used_tokens": 9_500}
        )
        sesion, _ = _preparar(monkeypatch, None, redis=redis)

        resultado = await nodo.token_budget_check_node(state)

        assert resultado["budget_status"] == "degraded"
        # La unica consulta posible seria la del presupuesto: no debe ocurrir.
        assert sesion.executed == []

    async def test_redis_caido_no_tumba_el_nodo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin Redis se consulta la base y se sigue."""
        _preparar(
            monkeypatch,
            FakeBudget(total_budget=10_000, used_tokens=200),
            redis=FakeRedis(falla=True),
        )

        resultado = await nodo.token_budget_check_node(estado())

        assert resultado["budget_status"] == "ok"
        assert resultado["budget_usage_pct"] == pytest.approx(2.0)

    async def test_cache_corrupta_se_ignora(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un JSON invalido en la cache no rompe: se relee de la base."""
        state = estado()
        redis = FakeRedis()
        redis.store[nodo.cache_key(state["client_id"])] = "{no es json"
        _preparar(monkeypatch, FakeBudget(total_budget=100, used_tokens=99), redis=redis)

        resultado = await nodo.token_budget_check_node(state)

        assert resultado["budget_status"] == "degraded"


# ─── Registro de consumo ─────────────────────────────────────────────────────


class TestRegistroDeConsumo:
    """`TokenBudgetGuard.record_usage()` — §7 de la spec."""

    async def test_registra_el_log_y_suma_al_presupuesto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Escribe en `token_usage_logs` y actualiza `token_budgets` con un UPDATE atomico.

        BUG-022: el incremento ya no es un SELECT + `budget.used_tokens = ... + n`
        en Python (lost update bajo concurrencia), sino un
        `UPDATE ... SET used_tokens = used_tokens + :n` -- se verifica inspeccionando
        los parametros compilados de la sentencia, no un objeto mutado.
        """
        sesion = FakeSession()
        parchear_tenant_session(monkeypatch, guard_module, sesion)
        redis = FakeRedis()
        monkeypatch.setattr(guard_module, "get_redis", lambda: redis)
        client_id = uuid.uuid4()

        await TokenBudgetGuard.record_usage(
            client_id=str(client_id),
            conversation_id=str(uuid.uuid4()),
            model="gpt-4o",
            prompt_tokens=120,
            completion_tokens=30,
            operation="rag_query",
        )

        logs = sesion.agregados_de(TokenUsageLog)
        assert len(logs) == 1
        assert logs[0].total_tokens == 150
        assert logs[0].operation == "rag_query"

        # El UPDATE es el unico statement que pasa por session.execute() en este flujo.
        [update_stmt] = sesion.executed
        parametros = update_stmt.compile().params
        assert parametros["used_tokens_1"] == 150
        assert parametros["client_id_1"] == client_id

    async def test_invalida_la_cache_del_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tras registrar, la clave de presupuesto se borra de Redis."""
        sesion = FakeSession()
        parchear_tenant_session(monkeypatch, guard_module, sesion)
        redis = FakeRedis()
        monkeypatch.setattr(guard_module, "get_redis", lambda: redis)
        client_id = uuid.uuid4()

        await TokenBudgetGuard.record_usage(
            client_id=client_id,
            conversation_id=None,
            model="gpt-4o-mini",
            prompt_tokens=10,
            completion_tokens=5,
            operation="intent_routing",
        )

        assert redis.borradas == [f"token_budget:{client_id}"]

    async def test_sin_tokens_no_escribe_nada(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Una respuesta sin datos de consumo no ensucia la contabilidad."""
        sesion = FakeSession()
        parchear_tenant_session(monkeypatch, guard_module, sesion)

        await TokenBudgetGuard.record_usage(
            client_id=uuid.uuid4(),
            conversation_id=None,
            model="gpt-4o",
            prompt_tokens=0,
            completion_tokens=0,
            operation="intent_routing",
        )

        assert sesion.added == []

    async def test_un_fallo_de_base_no_propaga(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """La contabilidad es best-effort: no puede tumbar la conversacion."""

        def _explota(client_id: Any) -> Any:
            raise RuntimeError("base caida")

        monkeypatch.setattr(guard_module, "tenant_session", _explota)

        await TokenBudgetGuard.record_usage(
            client_id=uuid.uuid4(),
            conversation_id=None,
            model="gpt-4o",
            prompt_tokens=10,
            completion_tokens=10,
            operation="rag_query",
        )


class TestCosto:
    """Estimacion de costo, la unica parte que no se persiste."""

    def test_costo_de_un_modelo_conocido(self) -> None:
        """1M de tokens de entrada de gpt-4o cuestan 2.50 USD."""
        assert estimate_cost_usd("gpt-4o", 1_000_000, 0) == pytest.approx(2.50)

    def test_modelo_desconocido_usa_la_tarifa_mas_barata(self) -> None:
        """Un modelo fuera de la tabla no infla el costo estimado."""
        assert estimate_cost_usd("modelo-inventado", 1_000_000, 0) == pytest.approx(0.15)
