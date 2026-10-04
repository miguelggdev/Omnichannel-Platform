"""Tests de las feature flags por tenant (Sprint 14, ADR-076)."""

import json
import uuid
from typing import Any

import pytest

from app.agents.nodes import _tenant
from app.agents.nodes._tenant import get_agent_settings
from app.agents.nodes.intent_router import available_intents
from app.api.v1 import feature_flags as api_modulo
from app.core import feature_flags as modulo
from app.core.feature_flags import (
    AGENT_FLAGS,
    KNOWN_FLAGS,
    FeatureFlags,
    SinAgenteConfigurableError,
    agentes_habilitados,
    en_rollout,
    flags_del_tenant,
    validar_valor,
)
from tests.unit.agent_doubles import FakeSession, parchear_tenant_session

URL = "/api/v1/admin/feature-flags"


class _RedisFalso:
    """Redis en memoria; `roto=True` simula un Redis caido."""

    def __init__(self, roto: bool = False) -> None:
        self.datos: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.roto = roto

    async def get(self, clave: str) -> str | None:
        if self.roto:
            raise ConnectionError("redis caido")
        return self.datos.get(clave)

    async def setex(self, clave: str, ttl: int, valor: str) -> None:
        if self.roto:
            raise ConnectionError("redis caido")
        self.datos[clave] = valor
        self.ttl[clave] = ttl

    async def delete(self, clave: str) -> None:
        if self.roto:
            raise ConnectionError("redis caido")
        self.datos.pop(clave, None)


class TestFlagsDelTenant:
    def test_sin_config_o_sin_flags_es_vacio(self) -> None:
        assert flags_del_tenant(None) == {}
        assert flags_del_tenant({}) == {}
        assert flags_del_tenant({"feature_flags": "no-es-un-dict"}) == {}

    def test_descarta_lo_que_no_es_bool_ni_entero(self) -> None:
        """El JSONB lo puede haber escrito alguien a mano."""
        config = {"feature_flags": {"a": True, "b": 50, "c": "si", "d": None, "e": [1], "f": 1.5}}

        assert flags_del_tenant(config) == {"a": True, "b": 50}


class TestAgentesHabilitados:
    TODOS = ("rag", "scheduling", "financial", "marketing", "clinical")

    def test_sin_flags_manda_enabled_agents(self) -> None:
        """Los tenants existentes no cambian."""
        assert agentes_habilitados(self.TODOS, {}) == self.TODOS

    def test_una_flag_en_false_apaga_ese_agente(self) -> None:
        resultado = agentes_habilitados(self.TODOS, {"enable_clinical": False})

        assert resultado == ("rag", "scheduling", "financial", "marketing")

    def test_una_flag_en_true_no_habilita_lo_que_enabled_agents_no_declara(self) -> None:
        """La flag restringe, no reemplaza: `enabled_agents` sigue mandando."""
        assert agentes_habilitados(("rag",), {"enable_clinical": True}) == ("rag",)

    def test_un_valor_raro_apaga_el_agente(self) -> None:
        """Lado seguro: algo escrito a mano que no es booleano no deja pasar al agente."""
        assert "clinical" not in agentes_habilitados(self.TODOS, {"enable_clinical": 100})

    def test_rag_y_los_otros_agentes_no_se_tocan(self) -> None:
        flags = dict.fromkeys(AGENT_FLAGS, False)

        assert agentes_habilitados(self.TODOS, flags) == ("rag",)

    def test_las_flags_sin_aplicar_no_afectan_a_ningun_agente(self) -> None:
        assert agentes_habilitados(self.TODOS, {"enable_voice": False}) == self.TODOS

    def test_conserva_el_orden(self) -> None:
        orden = ("clinical", "rag", "marketing")

        assert agentes_habilitados(orden, {"enable_marketing": False}) == ("clinical", "rag")


class TestValidarValor:
    @pytest.mark.parametrize("flag", list(AGENT_FLAGS))
    def test_una_flag_de_agente_solo_admite_booleano(self, flag: str) -> None:
        assert validar_valor(flag, True) is True
        assert validar_valor(flag, False) is False
        with pytest.raises(ValueError, match="agente"):
            validar_valor(flag, 50)

    def test_las_demas_admiten_porcentaje(self) -> None:
        assert validar_valor("enable_reranking", 0) == 0
        assert validar_valor("enable_reranking", 50) == 50
        assert validar_valor("enable_reranking", 100) == 100

    @pytest.mark.parametrize("valor", [-1, 101, "50", None, 1.5, [1]])
    def test_porcentajes_fuera_de_rango_o_de_otro_tipo_se_rechazan(self, valor: object) -> None:
        with pytest.raises(ValueError, match="porcentaje"):
            validar_valor("enable_reranking", valor)


class TestRollout:
    def test_es_determinista(self) -> None:
        cliente = uuid.uuid4()

        assert en_rollout(cliente, "f", "contacto-1", 50) == en_rollout(
            cliente, "f", "contacto-1", 50
        )

    def test_cero_no_activa_a_nadie_y_cien_activa_a_todos(self) -> None:
        cliente = uuid.uuid4()
        entidades = [f"e{i}" for i in range(500)]

        assert not any(en_rollout(cliente, "f", e, 0) for e in entidades)
        assert all(en_rollout(cliente, "f", e, 100) for e in entidades)

    def test_al_50_por_ciento_activa_a_la_mitad(self) -> None:
        """Test estadistico del spec: ~50% de 10.000 entidades."""
        cliente = uuid.uuid4()

        activas = sum(en_rollout(cliente, "f", f"e{i}", 50) for i in range(10_000))

        assert 4_700 <= activas <= 5_300

    def test_subir_el_porcentaje_nunca_saca_a_quien_ya_estaba(self) -> None:
        cliente = uuid.uuid4()
        entidades = [f"e{i}" for i in range(2_000)]
        al_20 = {e for e in entidades if en_rollout(cliente, "f", e, 20)}
        al_60 = {e for e in entidades if en_rollout(cliente, "f", e, 60)}

        assert al_20 <= al_60

    def test_dos_flags_no_activan_a_las_mismas_entidades(self) -> None:
        cliente = uuid.uuid4()
        entidades = [f"e{i}" for i in range(2_000)]
        a = {e for e in entidades if en_rollout(cliente, "una", e, 50)}
        b = {e for e in entidades if en_rollout(cliente, "otra", e, 50)}

        assert a != b


class TestFeatureFlagsConRedis:
    @staticmethod
    def _servicio(
        monkeypatch: pytest.MonkeyPatch, config: dict[str, Any] | None, redis: _RedisFalso
    ) -> tuple[FeatureFlags, FakeSession]:
        sesion = parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[config]))
        return FeatureFlags(redis), sesion

    async def test_lee_de_la_base_y_cachea_con_ttl_de_cinco_minutos(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cliente = uuid.uuid4()
        redis = _RedisFalso()
        servicio, sesion = self._servicio(
            monkeypatch, {"feature_flags": {"enable_clinical": False}}, redis
        )

        assert await servicio.get_all(cliente) == {"enable_clinical": False}

        assert json.loads(redis.datos[f"ff:{cliente}"]) == {"enable_clinical": False}
        assert redis.ttl[f"ff:{cliente}"] == 300
        assert len(sesion.executed) == 1

    async def test_la_segunda_lectura_sale_del_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cliente = uuid.uuid4()
        servicio, sesion = self._servicio(
            monkeypatch, {"feature_flags": {"enable_clinical": False}}, _RedisFalso()
        )

        await servicio.get_all(cliente)
        await servicio.get_all(cliente)

        assert len(sesion.executed) == 1

    async def test_un_tenant_sin_flags_tambien_se_cachea(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si no, cada consulta de una flag ausente iria a la base."""
        cliente = uuid.uuid4()
        servicio, sesion = self._servicio(monkeypatch, {}, _RedisFalso())

        await servicio.get_all(cliente)
        await servicio.get_all(cliente)

        assert len(sesion.executed) == 1

    async def test_si_redis_cae_se_lee_de_la_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Redis es una optimizacion, no una dependencia."""
        servicio, sesion = self._servicio(
            monkeypatch, {"feature_flags": {"enable_clinical": False}}, _RedisFalso(roto=True)
        )

        assert await servicio.get_all(uuid.uuid4()) == {"enable_clinical": False}
        assert len(sesion.executed) == 1

    async def test_is_enabled_usa_el_default_si_la_flag_no_esta(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        servicio, _ = self._servicio(monkeypatch, {}, _RedisFalso())
        cliente = uuid.uuid4()

        assert await servicio.is_enabled(cliente, "enable_clinical") is False
        assert await servicio.is_enabled(cliente, "enable_clinical", default=True) is True

    @pytest.mark.parametrize(
        ("guardado", "esperado"),
        [(True, True), (False, False), (100, True), (99, False), (0, False)],
    )
    async def test_is_enabled_con_porcentaje_solo_cuenta_al_cien(
        self, monkeypatch: pytest.MonkeyPatch, guardado: bool | int, esperado: bool
    ) -> None:
        servicio, _ = self._servicio(
            monkeypatch, {"feature_flags": {"enable_reranking": guardado}}, _RedisFalso()
        )

        assert await servicio.is_enabled(uuid.uuid4(), "enable_reranking") is esperado

    async def test_is_enabled_percentage_reparte_por_entidad(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        servicio, _ = self._servicio(
            monkeypatch, {"feature_flags": {"enable_reranking": 50}}, _RedisFalso()
        )
        cliente = uuid.uuid4()

        activas = sum(
            [
                await servicio.is_enabled_percentage(cliente, "enable_reranking", f"e{i}")
                for i in range(1_000)
            ]
        )

        assert 400 <= activas <= 600

    async def test_una_flag_sin_configurar_no_activa_a_nadie(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        servicio, _ = self._servicio(monkeypatch, {}, _RedisFalso())

        assert await servicio.is_enabled_percentage(uuid.uuid4(), "enable_reranking", "e1") is False

    async def test_set_flag_invalida_el_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Criterio de aceptacion: el siguiente request ve el valor nuevo."""
        cliente = uuid.uuid4()
        redis = _RedisFalso()
        redis.datos[f"ff:{cliente}"] = json.dumps({"enable_clinical": True})
        sesion = parchear_tenant_session(
            monkeypatch, modulo, FakeSession(resultados=[uuid.uuid4(), None])
        )

        await FeatureFlags(redis).set_flag(cliente, "enable_clinical", False)

        assert f"ff:{cliente}" not in redis.datos
        assert len(sesion.executed) == 2

    async def test_set_flag_sin_agente_activo_falla(self, monkeypatch: pytest.MonkeyPatch) -> None:
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[None]))

        with pytest.raises(SinAgenteConfigurableError):
            await FeatureFlags(_RedisFalso()).set_flag(uuid.uuid4(), "enable_clinical", True)

    async def test_set_flag_funciona_aunque_redis_este_caido(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[uuid.uuid4(), None]))

        await FeatureFlags(_RedisFalso(roto=True)).set_flag(uuid.uuid4(), "enable_clinical", True)


class _AgentConfigFalso:
    def __init__(self, config: dict[str, Any]) -> None:
        self.name = "Asistente"
        self.model = "gpt-4o"
        self.temperature = 0.3
        self.system_prompt = None
        self.welcome_message = None
        self.handoff_message = None
        self.training_mode = False
        self.similarity_threshold = 0.80
        self.config = config


class TestIntegracionConElGrafo:
    """`get_agent_settings()` es lo que leen el router y los nueve nodos."""

    async def test_una_flag_apagada_quita_el_agente_del_router_y_de_su_nodo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = {
            "enabled_agents": ["rag", "clinical", "marketing"],
            "feature_flags": {"enable_clinical": False},
        }
        parchear_tenant_session(
            monkeypatch, _tenant, FakeSession(resultados=[_AgentConfigFalso(config)])
        )

        ajustes = await get_agent_settings(uuid.uuid4())

        assert ajustes.enabled_agents == ("rag", "marketing")
        assert "clinical" not in available_intents(ajustes.enabled_agents)
        assert "marketing" in available_intents(ajustes.enabled_agents)

    async def test_con_la_flag_encendida_el_agente_sigue_disponible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = {"enabled_agents": ["rag", "clinical"], "feature_flags": {"enable_clinical": True}}
        parchear_tenant_session(
            monkeypatch, _tenant, FakeSession(resultados=[_AgentConfigFalso(config)])
        )

        ajustes = await get_agent_settings(uuid.uuid4())

        assert "clinical" in available_intents(ajustes.enabled_agents)

    async def test_un_tenant_sin_flags_se_comporta_como_antes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = {"enabled_agents": ["rag", "clinical"]}
        parchear_tenant_session(
            monkeypatch, _tenant, FakeSession(resultados=[_AgentConfigFalso(config)])
        )

        assert (await get_agent_settings(uuid.uuid4())).enabled_agents == ("rag", "clinical")


class TestApi:
    @staticmethod
    def _preparar(
        monkeypatch: pytest.MonkeyPatch, resultados: list[Any], redis: _RedisFalso | None = None
    ) -> tuple[FakeSession, _RedisFalso]:
        redis = redis or _RedisFalso()
        monkeypatch.setattr("app.services.dedup.get_redis", lambda: redis)
        sesion = parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=resultados))
        return sesion, redis

    @pytest.mark.parametrize("rol", ["agent", "supervisor", "medical"])
    @pytest.mark.parametrize(("metodo", "ruta"), [("get", ""), ("put", "/enable_clinical")])
    async def test_solo_administradores(
        self, authenticated_client_factory: Any, rol: str, metodo: str, ruta: str
    ) -> None:
        cliente = authenticated_client_factory(role=rol)
        kwargs = {} if metodo == "get" else {"json": {"value": True}}

        response = await getattr(cliente, metodo)(f"{URL}{ruta}", **kwargs)

        assert response.status_code == 403

    async def test_lista_todas_las_flags_y_marca_las_que_no_se_aplican(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._preparar(monkeypatch, [{"feature_flags": {"enable_clinical": False}}])
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.get(URL)

        assert response.status_code == 200
        flags = {f["flag"]: f for f in response.json()["flags"]}
        assert list(flags) == list(KNOWN_FLAGS)
        assert flags["enable_clinical"] == {
            "flag": "enable_clinical",
            "value": False,
            "enforced": True,
            "editable": True,
        }
        assert flags["enable_marketing"]["value"] is None
        assert flags["enable_voice"]["enforced"] is False
        assert flags["enable_sandbox"]["enforced"] is True  # la exige la API del sandbox
        assert flags["enable_sandbox"]["editable"] is False  # un admin la ve, no la cambia

    async def test_el_super_admin_ve_enable_sandbox_como_editable(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._preparar(monkeypatch, [{"feature_flags": {}}])
        cliente = authenticated_client_factory(role="super_admin")

        response = await cliente.get(URL)

        flags = {f["flag"]: f for f in response.json()["flags"]}
        assert flags["enable_sandbox"]["editable"] is True

    async def test_un_admin_no_puede_cambiar_enable_sandbox(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sesion, _ = self._preparar(monkeypatch, [])
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.put(f"{URL}/enable_sandbox", json={"value": True})

        assert response.status_code == 403
        assert sesion.executed == []  # ni siquiera toca la base

    async def test_el_super_admin_cambia_enable_sandbox_de_otro_tenant(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        otro = uuid.uuid4()
        sesion, _ = self._preparar(
            monkeypatch,
            [uuid.uuid4(), None, {"feature_flags": {"enable_sandbox": True}}],
        )
        cliente = authenticated_client_factory(role="super_admin")

        response = await cliente.put(
            f"{URL}/enable_sandbox", params={"client_id": str(otro)}, json={"value": True}
        )

        assert response.status_code == 200, response.text
        assert any(str(otro) in str(p) for p in sesion.params)

    async def test_un_admin_no_puede_operar_sobre_otro_tenant(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._preparar(monkeypatch, [])
        cliente = authenticated_client_factory(role="admin")

        for metodo, kwargs in (("get", {}), ("put", {"json": {"value": True}})):
            ruta = URL if metodo == "get" else f"{URL}/enable_clinical"
            response = await getattr(cliente, metodo)(
                ruta, params={"client_id": str(uuid.uuid4())}, **kwargs
            )
            assert response.status_code == 403, metodo

    async def test_un_admin_sigue_cambiando_las_flags_de_agente(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._preparar(
            monkeypatch, [uuid.uuid4(), None, {"feature_flags": {"enable_clinical": False}}]
        )
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.put(f"{URL}/enable_clinical", json={"value": False})

        assert response.status_code == 200

    async def test_cambia_una_flag_e_invalida_el_cache(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        tenant_a_id: uuid.UUID,
    ) -> None:
        redis = _RedisFalso()
        redis.datos[f"ff:{tenant_a_id}"] = json.dumps({"enable_clinical": True})
        sesion, _ = self._preparar(
            monkeypatch,
            [uuid.uuid4(), None, {"feature_flags": {"enable_clinical": False}}],
            redis,
        )
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.put(f"{URL}/enable_clinical", json={"value": False})

        assert response.status_code == 200, response.text
        valores = {f["flag"]: f["value"] for f in response.json()["flags"]}
        assert valores["enable_clinical"] is False
        # La escritura invalido el cache; la lectura posterior lo volvio a llenar con lo nuevo.
        assert json.loads(redis.datos[f"ff:{tenant_a_id}"]) == {"enable_clinical": False}
        assert len(sesion.executed) == 3

    async def test_una_flag_desconocida_da_404(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._preparar(monkeypatch, [])
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.put(f"{URL}/enable_inventada", json={"value": True})

        assert response.status_code == 404

    @pytest.mark.parametrize(
        ("flag", "valor"),
        [("enable_clinical", 50), ("enable_reranking", 101), ("enable_reranking", -1)],
    )
    async def test_un_valor_invalido_da_400(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        flag: str,
        valor: int,
    ) -> None:
        self._preparar(monkeypatch, [])
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.put(f"{URL}/{flag}", json={"value": valor})

        assert response.status_code == 400

    @pytest.mark.parametrize("valor", ["true", None, 1.5])
    async def test_un_tipo_que_no_es_bool_ni_entero_se_rechaza(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch, valor: object
    ) -> None:
        self._preparar(monkeypatch, [])
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.put(f"{URL}/enable_reranking", json={"value": valor})

        assert response.status_code in (400, 422)

    async def test_un_porcentaje_se_acepta_en_las_flags_que_lo_admiten(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._preparar(
            monkeypatch,
            [uuid.uuid4(), None, {"feature_flags": {"enable_reranking": 25}}],
        )
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.put(f"{URL}/enable_reranking", json={"value": 25})

        assert response.status_code == 200, response.text
        valores = {f["flag"]: f["value"] for f in response.json()["flags"]}
        assert valores["enable_reranking"] == 25

    async def test_un_tenant_sin_agente_da_400(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._preparar(monkeypatch, [None])
        cliente = authenticated_client_factory(role="admin")

        response = await cliente.put(f"{URL}/enable_clinical", json={"value": True})

        assert response.status_code == 400


def test_el_modulo_de_la_api_expone_su_router() -> None:
    """Falla si alguien renombra el router sin actualizar `main.py`."""
    assert api_modulo.router is not None
