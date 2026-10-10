"""Enriquecimiento de leads (Sprint 17): dominio, registro, cache, fusion y orquestador.

Sin base ni red: los proveedores son dobles que cuentan sus llamadas, Redis es un diccionario y
la sesion solo recoge lo que `registrar_actividad` le anade.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, ClassVar

import pytest

from app.models.lead import Lead
from app.models.lead_activity import ACTIVITY_ENRICHED, ACTIVITY_ENRICHMENT_EMPTY, LeadActivity
from app.schemas.lead_scoring import CompanyData, EnrichmentResult, PersonData
from app.services.enrichment import registry
from app.services.enrichment.base import (
    EnrichmentError,
    EnrichmentProvider,
    PersonQuery,
    ProviderNotConfiguredError,
    ProviderTemporaryError,
)
from app.services.enrichment.cache import MISS, CompanyCache
from app.services.enrichment.domain import (
    dominio_de_email,
    dominio_de_url,
    es_correo_gratuito,
    inferir_dominio,
)
from app.services.enrichment.engine import (
    ERROR_FAILED,
    ERROR_NOT_CONFIGURED,
    ERROR_TEMPORARY,
    SKIP_ANONYMIZED,
    SKIP_DELETED,
    SKIP_NO_CONSENT,
    SKIP_NOTHING_TO_LOOK_UP,
    enriquecer_lead,
    sin_autorizacion,
)
from app.services.enrichment.merge import aplicar_a_lead, combinar
from app.services.lead_privacy import anonimizar_lead

AHORA = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


# ─── Dobles ──────────────────────────────────────────────────────────────────────────────────


class FakeRedis:
    """Lo minimo de `redis.asyncio.Redis` que usa la cache, con los TTL a la vista."""

    def __init__(self) -> None:
        self.datos: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    async def get(self, clave: str) -> str | None:
        return self.datos.get(clave)

    async def set(self, clave: str, valor: str, ex: int | None = None) -> None:
        self.datos[clave] = valor
        self.ttl[clave] = ex

    async def delete(self, clave: str) -> None:
        self.datos.pop(clave, None)


class RedisCaido:
    async def get(self, clave: str) -> str | None:
        raise ConnectionError("redis caido")

    async def set(self, clave: str, valor: str, ex: int | None = None) -> None:
        raise ConnectionError("redis caido")

    async def delete(self, clave: str) -> None:
        raise ConnectionError("redis caido")


class SesionFalsa:
    """Recoge lo que se anade (las actividades del historial)."""

    def __init__(self) -> None:
        self.anadidos: list[Any] = []

    def add(self, objeto: Any) -> None:
        self.anadidos.append(objeto)

    def actividades(self) -> list[LeadActivity]:
        return [o for o in self.anadidos if isinstance(o, LeadActivity)]


class Proveedor(EnrichmentProvider):
    """Proveedor de prueba: responde lo que se le diga y cuenta las llamadas."""

    name: ClassVar[str] = "falso"
    supports_company: ClassVar[bool] = True
    supports_person: ClassVar[bool] = True

    def __init__(
        self,
        nombre: str = "falso",
        *,
        empresa: CompanyData | None = None,
        persona: PersonData | None = None,
        error: EnrichmentError | None = None,
        empresas: bool = True,
        personas: bool = True,
    ) -> None:
        self.name = nombre  # type: ignore[misc]
        self.supports_company = empresas  # type: ignore[misc]
        self.supports_person = personas  # type: ignore[misc]
        self.empresa = empresa
        self.persona = persona
        self.error = error
        self.consultas_empresa: list[str] = []
        self.consultas_persona: list[PersonQuery] = []

    async def enrich_company(self, domain: str) -> CompanyData | None:
        self.consultas_empresa.append(domain)
        if self.error:
            raise self.error
        return self.empresa

    async def enrich_person(self, query: PersonQuery) -> PersonData | None:
        self.consultas_persona.append(query)
        if self.error:
            raise self.error
        return self.persona


def _lead(**campos: Any) -> Lead:
    campos.setdefault("first_name", "Ana")
    campos.setdefault("email", "ana@acme.com")
    campos.setdefault("enrichment_data", {})
    return Lead(id=uuid.uuid4(), client_id=TENANT, **campos)


ACME = CompanyData(
    domain="acme.com", name="Acme SAS", industry="Software", employees=120, country="CO"
)


# ─── Dominio ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("dominio", "gratuito"),
    [
        ("gmail.com", True),
        ("hotmail.es", True),
        ("yahoo.com.mx", True),
        ("outlook.fr", True),
        ("aol.com", True),
        ("acme.com", False),
        ("gmailtools.com", False),
        ("live-nation.com", False),
        ("yahoo.acme.com.co", False),
    ],
)
def test_correo_gratuito(dominio: str, gratuito: bool) -> None:
    assert es_correo_gratuito(dominio) is gratuito


@pytest.mark.parametrize(
    ("email", "dominio"),
    [
        ("ana@Acme.COM", "acme.com"),
        ("ana@gmail.com", None),
        ("ana@hotmail.com.ar", None),
        ("sin-arroba", None),
        ("a@b@acme.com", None),
        (None, None),
    ],
)
def test_dominio_de_email(email: str | None, dominio: str | None) -> None:
    assert dominio_de_email(email) == dominio


@pytest.mark.parametrize(
    ("valor", "dominio"),
    [
        ("https://www.Acme.com/contacto?x=1", "acme.com"),
        ("acme.com", "acme.com"),
        ("http://user:pw@acme.com:8080/x", "acme.com"),
        ("http://127.0.0.1/", None),
        ("localhost", None),
        ("no es un dominio", None),
        ("", None),
    ],
)
def test_dominio_de_url(valor: str, dominio: str | None) -> None:
    assert dominio_de_url(valor) == dominio


def test_inferir_dominio_prefiere_el_de_la_empresa_y_descarta_el_gratuito() -> None:
    assert inferir_dominio("acme.com", "ana@otra.com") == "acme.com"
    assert inferir_dominio(None, "ana@otra.com") == "otra.com"
    assert inferir_dominio("gmail.com", "ana@otra.com") == "otra.com"
    assert inferir_dominio("gmail.com", "ana@gmail.com") is None


# ─── Registro ────────────────────────────────────────────────────────────────────────────────


class TestRegistro:
    @pytest.fixture(autouse=True)
    def _limpio(self) -> Any:
        yield
        for nombre in ("t_uno", "t_dos", "t_sin_key"):
            registry.unregister_provider(nombre)

    def test_construye_en_orden_y_sin_repetidos(self) -> None:
        @registry.register_provider
        class Uno(EnrichmentProvider):
            name = "t_uno"
            supports_company = True

        @registry.register_provider
        class Dos(EnrichmentProvider):
            name = "t_dos"
            supports_person = True

        construidos = registry.build_providers(["t_dos", "t_uno", "t_dos"])
        assert [p.name for p in construidos] == ["t_dos", "t_uno"]
        assert {"t_uno", "t_dos"} <= set(registry.available_providers())

    def test_desconocido_es_un_error_de_configuracion(self) -> None:
        with pytest.raises(registry.UnknownProviderError):
            registry.build_providers(["no_existe"])

    def test_sin_credenciales_se_omite(self) -> None:
        @registry.register_provider
        class SinKey(EnrichmentProvider):
            name = "t_sin_key"
            supports_company = True

            def __init__(self) -> None:
                raise ProviderNotConfiguredError("falta la API key")

        assert registry.build_providers(["t_sin_key"]) == []

    def test_nombre_repetido_o_sin_capacidades_se_rechaza(self) -> None:
        @registry.register_provider
        class Uno(EnrichmentProvider):
            name = "t_uno"
            supports_company = True

        with pytest.raises(ValueError, match="Ya hay"):

            @registry.register_provider
            class Otro(EnrichmentProvider):
                name = "t_uno"
                supports_company = True

        with pytest.raises(ValueError, match="supports"):

            @registry.register_provider
            class Nada(EnrichmentProvider):
                name = "t_dos"


# ─── Cache ───────────────────────────────────────────────────────────────────────────────────


class TestCompanyCache:
    async def test_positivo_y_negativo_con_su_ttl(self) -> None:
        redis = FakeRedis()
        cache = CompanyCache(redis, ttl_seconds=1000, negative_ttl_seconds=10)
        assert await cache.get(TENANT, "acme.com") == MISS

        await cache.set(TENANT, "acme.com", ACME)
        encontrado = await cache.get(TENANT, "acme.com")
        assert encontrado.hit
        assert encontrado.company == ACME
        assert redis.ttl[cache.clave(TENANT, "acme.com")] == 1000

        await cache.set(TENANT, "nadie.com", None)
        negativo = await cache.get(TENANT, "nadie.com")
        assert negativo.hit
        assert negativo.company is None
        assert redis.ttl[cache.clave(TENANT, "nadie.com")] == 10

    async def test_cada_tenant_tiene_su_cache(self) -> None:
        redis = FakeRedis()
        cache = CompanyCache(redis)
        await cache.set(TENANT, "acme.com", ACME)
        assert await cache.get(uuid.uuid4(), "acme.com") == MISS

    async def test_redis_caido_es_fallo_de_cache_no_error(self) -> None:
        cache = CompanyCache(RedisCaido())
        assert await cache.get(TENANT, "acme.com") == MISS
        await cache.set(TENANT, "acme.com", ACME)
        await cache.invalidate(TENANT, "acme.com")

    @pytest.mark.parametrize(
        "crudo",
        [
            "no-json",
            json.dumps({"v": 99, "company": None}),
            json.dumps({"v": 1, "company": {"employees": -3}}),
        ],
    )
    async def test_entrada_corrupta_o_de_otra_version_se_ignora(self, crudo: str) -> None:
        redis = FakeRedis()
        cache = CompanyCache(redis)
        redis.datos[cache.clave(TENANT, "acme.com")] = crudo
        assert await cache.get(TENANT, "acme.com") == MISS

    async def test_invalidar(self) -> None:
        cache = CompanyCache(FakeRedis())
        await cache.set(TENANT, "acme.com", ACME)
        await cache.invalidate(TENANT, "acme.com")
        assert await cache.get(TENANT, "acme.com") == MISS


# ─── Fusion ──────────────────────────────────────────────────────────────────────────────────


class TestFusion:
    def test_campo_a_campo_gana_el_primero_que_lo_trae(self) -> None:
        empresa, persona, aportaron = combinar(
            [
                EnrichmentResult(provider="a", company=CompanyData(name="Acme", industry=None)),
                EnrichmentResult(provider="b"),
                EnrichmentResult(
                    provider="c",
                    company=CompanyData(name="ACME Inc", industry="Software"),
                    person=PersonData(job_title="CTO"),
                ),
            ]
        )
        assert empresa == CompanyData(name="Acme", industry="Software")
        assert persona == PersonData(job_title="CTO")
        assert aportaron == ["a", "c"]

    def test_nada_que_combinar(self) -> None:
        assert combinar([]) == (None, None, [])

    def test_rellena_huecos_sin_pisar_y_conserva_lo_demas(self) -> None:
        lead = _lead(
            company_name="Acme (escrito a mano)",
            industry="",
            enrichment_data={"capture": {"utm_source": "google"}},
        )
        rellenados = aplicar_a_lead(
            lead,
            ACME,
            PersonData(job_title="CTO", linkedin_url="https://linkedin.com/in/otra"),
            proveedores=["apollo"],
            ahora=AHORA,
        )
        assert rellenados == ["company_domain", "industry", "company_size", "job_title"]
        assert lead.company_name == "Acme (escrito a mano)"
        assert (lead.industry, lead.company_size, lead.job_title) == ("Software", "51-200", "CTO")
        # El LinkedIn de un proveedor no se escribe en la columna (indice unico, ADR-084).
        assert lead.linkedin_url is None
        assert lead.enrichment_data["capture"] == {"utm_source": "google"}
        assert lead.enrichment_data["company"]["name"] == "Acme SAS"
        assert lead.enrichment_data["person"]["linkedin_url"] == "https://linkedin.com/in/otra"
        assert lead.enrichment_data["enrichment"]["providers"] == ["apollo"]
        assert lead.enriched_at == AHORA


# ─── Orquestador ─────────────────────────────────────────────────────────────────────────────


class TestEnriquecerLead:
    async def test_enriquece_empresa_y_persona_y_lo_anota_sin_datos_personales(self) -> None:
        sesion = SesionFalsa()
        lead = _lead()
        proveedor = Proveedor(empresa=ACME, persona=PersonData(job_title="CTO", country="CO"))
        salida = await enriquecer_lead(
            sesion,  # type: ignore[arg-type]
            lead,
            [proveedor],
            ahora=AHORA,
            enriquecer_persona=True,
        )

        assert salida.skipped is None
        assert salida.providers_used == ["falso"]
        assert proveedor.consultas_empresa == ["acme.com"]
        assert proveedor.consultas_persona[0].email == "ana@acme.com"
        assert lead.job_title == "CTO"
        [actividad] = sesion.actividades()
        assert actividad.activity_type == ACTIVITY_ENRICHED
        assert "ana@acme.com" not in json.dumps(actividad.metadata_)
        assert actividad.metadata_["fields"] == salida.fields_filled

    async def test_la_cache_evita_preguntar_la_empresa(self) -> None:
        cache = CompanyCache(FakeRedis())
        await cache.set(TENANT, "acme.com", ACME)
        proveedor = Proveedor(empresas=True, personas=False)
        salida = await enriquecer_lead(
            SesionFalsa(),
            _lead(),
            [proveedor],
            ahora=AHORA,
            cache=cache,  # type: ignore[arg-type]
        )
        assert proveedor.consultas_empresa == []
        assert salida.company_from_cache
        assert salida.found_anything

    async def test_recuerda_el_no_encontrado_solo_si_alguien_contesto(self) -> None:
        redis = FakeRedis()
        cache = CompanyCache(redis)
        caido = Proveedor(error=ProviderTemporaryError("429", retry_after=30), personas=False)
        salida = await enriquecer_lead(
            SesionFalsa(),
            _lead(),
            [caido],
            ahora=AHORA,
            cache=cache,  # type: ignore[arg-type]
        )
        assert salida.retryable
        assert salida.retry_after == 30
        assert redis.datos == {}  # no se sabe si existe: no se recuerda nada

        vacio = Proveedor(empresa=None, personas=False)
        await enriquecer_lead(SesionFalsa(), _lead(), [vacio], ahora=AHORA, cache=cache)  # type: ignore[arg-type]
        assert (await cache.get(TENANT, "acme.com")).hit

    async def test_un_proveedor_que_falla_no_impide_a_los_demas(self) -> None:
        roto = Proveedor("roto", error=EnrichmentError("respuesta rara"))
        sin_key = Proveedor("sin_key", error=ProviderNotConfiguredError("x"))
        bueno = Proveedor("bueno", empresa=ACME)
        sesion = SesionFalsa()
        salida = await enriquecer_lead(sesion, _lead(), [roto, sin_key, bueno], ahora=AHORA)  # type: ignore[arg-type]
        assert salida.errors == {"roto": ERROR_FAILED, "sin_key": ERROR_NOT_CONFIGURED}
        assert not salida.retryable
        assert salida.providers_used == ["bueno"]

    async def test_sin_datos_personales_hacia_terceros_si_se_pide(self) -> None:
        proveedor = Proveedor(empresa=ACME, persona=PersonData(job_title="CTO"))
        lead = _lead()
        await enriquecer_lead(
            SesionFalsa(),
            lead,
            [proveedor],
            ahora=AHORA,
            enriquecer_persona=False,  # type: ignore[arg-type]
        )
        assert proveedor.consultas_persona == []
        assert lead.job_title is None

    async def test_nada_que_encontrar_se_anota_como_vacio(self) -> None:
        sesion = SesionFalsa()
        salida = await enriquecer_lead(sesion, _lead(), [Proveedor()], ahora=AHORA)  # type: ignore[arg-type]
        assert not salida.found_anything
        [actividad] = sesion.actividades()
        assert actividad.activity_type == ACTIVITY_ENRICHMENT_EMPTY

    async def test_por_defecto_no_sale_ningun_dato_de_la_persona(self) -> None:
        # Pendiente de la consulta legal: sin pedirlo, solo se consulta el dominio.
        proveedor = Proveedor(empresa=ACME, persona=PersonData(job_title="CTO"))
        lead = _lead()
        salida = await enriquecer_lead(SesionFalsa(), lead, [proveedor], ahora=AHORA)  # type: ignore[arg-type]
        assert proveedor.consultas_empresa == ["acme.com"]
        assert proveedor.consultas_persona == []
        assert salida.found_anything
        assert lead.job_title is None

    @pytest.mark.parametrize(
        ("capture", "sale"),
        [
            ({"consent": False}, False),
            ({"consent": True}, True),
            ({"consent": None}, True),  # formulario que no la exige: no se presume negada
            ({}, True),
        ],
    )
    def test_sin_autorizacion_solo_con_false_explicito(
        self, capture: dict[str, Any], sale: bool
    ) -> None:
        lead = _lead()
        lead.enrichment_data = {"capture": capture}
        assert sin_autorizacion(lead) is not sale

    @pytest.mark.parametrize(
        ("preparar", "motivo"),
        [
            (lambda lead: setattr(lead, "deleted_at", AHORA), SKIP_DELETED),
            (anonimizar_lead, SKIP_ANONYMIZED),
            (lambda lead: setattr(lead, "email", "ana@gmail.com"), None),
            (
                lambda lead: setattr(lead, "enrichment_data", {"capture": {"consent": False}}),
                SKIP_NO_CONSENT,
            ),
        ],
    )
    async def test_borrados_y_anonimizados_no_salen_hacia_un_proveedor(
        self, preparar: Any, motivo: str | None
    ) -> None:
        lead = _lead()
        preparar(lead)
        proveedor = Proveedor(empresa=ACME)
        salida = await enriquecer_lead(
            SesionFalsa(),  # type: ignore[arg-type]
            lead,
            [proveedor],
            ahora=AHORA,
            enriquecer_persona=True,
        )
        if motivo is not None:
            assert salida.skipped == motivo
            assert proveedor.consultas_empresa == proveedor.consultas_persona == []
        else:
            # Correo gratuito: no se consulta la empresa "gmail.com", si a la persona.
            assert proveedor.consultas_empresa == []
            assert len(proveedor.consultas_persona) == 1

    async def test_sin_dominio_ni_forma_de_buscar_a_la_persona_no_hace_nada(self) -> None:
        lead = _lead(email=None)
        salida = await enriquecer_lead(SesionFalsa(), lead, [Proveedor()], ahora=AHORA)  # type: ignore[arg-type]
        assert salida.skipped == SKIP_NOTHING_TO_LOOK_UP

    def test_codigos_de_error_estables(self) -> None:
        assert (ERROR_FAILED, ERROR_NOT_CONFIGURED, ERROR_TEMPORARY) == (
            "failed",
            "not_configured",
            "temporary",
        )


# ─── Correcciones de la revision ─────────────────────────────────────────────────────────────


class TestCorreccionesRevision:
    async def test_un_fallo_pasajero_impide_guardar_lo_que_dijo_otro(self) -> None:
        """Si uno fallo pasajeramente, el reintento debe volver a preguntarle: nada a la cache."""
        redis = FakeRedis()
        cache = CompanyCache(redis)
        caido = Proveedor("apollo", error=ProviderTemporaryError("429"), personas=False)
        parcial = Proveedor("clearbit", empresa=CompanyData(name="Acme"), personas=False)
        salida = await enriquecer_lead(
            SesionFalsa(),
            _lead(),
            [caido, parcial],
            ahora=AHORA,
            cache=cache,  # type: ignore[arg-type]
        )
        assert salida.retryable
        assert salida.providers_used == ["clearbit"]
        assert redis.datos == {}

        # Tambien con un "no encontrado" del otro.
        vacio = Proveedor("clearbit", empresa=None, personas=False)
        await enriquecer_lead(SesionFalsa(), _lead(), [caido, vacio], ahora=AHORA, cache=cache)  # type: ignore[arg-type]
        assert redis.datos == {}

    async def test_un_no_encontrado_recordado_no_cuenta_como_acierto(self) -> None:
        cache = CompanyCache(FakeRedis())
        await cache.set(TENANT, "acme.com", None)
        sesion = SesionFalsa()
        salida = await enriquecer_lead(
            sesion,
            _lead(),
            [Proveedor(personas=False)],
            ahora=AHORA,
            cache=cache,  # type: ignore[arg-type]
        )
        assert not salida.company_from_cache
        assert not salida.found_anything
        [actividad] = sesion.actividades()
        assert actividad.activity_type == ACTIVITY_ENRICHMENT_EMPTY

    def test_un_modelo_sin_ningun_dato_no_cuenta_como_aporte(self) -> None:
        empresa, _, aportaron = combinar(
            [EnrichmentResult(provider="hueco", company=CompanyData())]
        )
        assert empresa is None
        assert aportaron == []

    async def test_ttl_cero_no_recuerda_y_no_se_cambia_por_el_de_por_defecto(self) -> None:
        redis = FakeRedis()
        cache = CompanyCache(redis, ttl_seconds=1000, negative_ttl_seconds=0)
        assert cache.negative_ttl == 0
        await cache.set(TENANT, "nadie.com", None)
        assert redis.datos == {}
        await cache.set(TENANT, "acme.com", ACME)
        assert redis.ttl[cache.clave(TENANT, "acme.com")] == 1000
