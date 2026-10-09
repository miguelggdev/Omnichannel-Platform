"""Calculadores del Sprint 17: ICP, FIT y comportamiento (funciones puras, sin base)."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models import lead_score as modelo_score
from app.schemas.lead_scoring import COMPANY_SIZE_BUCKETS, CompanyData, IcpConfig, PersonData
from app.services.lead_behavioral import BehavioralSignals, calcular_behavioral
from app.services.lead_fit import (
    EXCLUDED,
    MATCH,
    MISSING,
    NO_MATCH,
    NOT_CONFIGURED,
    PARTIAL,
    FitInput,
    calcular_fit,
    cargar_icp,
    contiene_termino,
    fit_input_de_lead,
    parsear_tamano_empresa,
    tramo_por_empleados,
)

AHORA = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _icp(**campos: object) -> IcpConfig:
    return IcpConfig.model_validate(campos)


# ─── IcpConfig ───────────────────────────────────────────────────────────────────────────────


class TestIcpConfig:
    def test_quita_repetidos_ignorando_acentos_y_conserva_la_grafia(self) -> None:
        icp = _icp(industries=["Tecnología", " tecnologia ", "Salud"])
        assert icp.industries == ["Tecnología", "Salud"]

    def test_paises_en_mayusculas_y_sin_repetir(self) -> None:
        assert _icp(countries=["co", "CO", " mx "]).countries == ["CO", "MX"]

    @pytest.mark.parametrize("pais", ["Colombia", "COL", "1A", ""])
    def test_pais_que_no_es_iso_alfa2_se_rechaza(self, pais: str) -> None:
        with pytest.raises(ValidationError):
            _icp(countries=[pais])

    def test_tramos_en_orden_natural_y_sin_repetir(self) -> None:
        icp = _icp(company_sizes=["5001+", "11-50", "11-50"])
        assert icp.company_sizes == ["11-50", "5001+"]

    def test_un_tramo_inventado_se_rechaza(self) -> None:
        with pytest.raises(ValidationError):
            _icp(company_sizes=["10-20"])

    def test_no_puede_incluir_y_excluir_lo_mismo(self) -> None:
        with pytest.raises(ValidationError):
            _icp(job_titles=["Director"], excluded_job_titles=["director"])

    def test_pesos_que_suman_cero_se_rechazan(self) -> None:
        with pytest.raises(ValidationError):
            _icp(weights={"industry": 0, "company_size": 0, "job_title": 0, "region": 0})

    def test_campos_desconocidos_se_rechazan(self) -> None:
        with pytest.raises(ValidationError):
            _icp(industrias=["software"])

    def test_termino_sin_letras_se_rechaza(self) -> None:
        with pytest.raises(ValidationError):
            _icp(industries=["---"])

    def test_configurado_exige_una_dimension_con_peso(self) -> None:
        assert not _icp().configurado
        assert not _icp(excluded_industries=["apuestas"]).configurado
        assert not _icp(
            industries=["software"], weights={"industry": 0, "company_size": 1}
        ).configurado
        assert _icp(countries=["CO"]).configurado


class TestCargarIcp:
    def test_vacio_o_ausente_es_sin_icp(self) -> None:
        assert cargar_icp(None) is None
        assert cargar_icp({}) is None

    def test_invalido_es_sin_icp_y_no_rompe(self) -> None:
        assert cargar_icp({"industries": "software"}) is None

    def test_sin_dimensiones_es_sin_icp(self) -> None:
        assert cargar_icp({"excluded_industries": ["apuestas"]}) is None

    def test_valido(self) -> None:
        icp = cargar_icp({"industries": ["software"]})
        assert icp is not None
        assert icp.industries == ["software"]


class TestDatosNormalizados:
    def test_dominio_se_normaliza_y_uno_raro_se_descarta(self) -> None:
        assert CompanyData(domain="WWW.Acme.COM.").domain == "acme.com"
        assert CompanyData(domain="http://acme.com/x").domain is None
        assert CompanyData(domain="10.0.0.1").domain is None

    def test_pais_que_no_es_codigo_se_descarta(self) -> None:
        assert CompanyData(country="Colombia").country is None
        assert PersonData(country="co").country == "CO"

    def test_campos_desconocidos_del_proveedor_se_ignoran(self) -> None:
        assert CompanyData.model_validate({"name": "Acme", "raw_api_field": 1}).name == "Acme"


# ─── Tamano de empresa ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("texto", "tramo"),
    [
        ("11-50", "11-50"),
        ("201 - 500 empleados", "201-500"),
        ("1K-5K", "1001-5000"),
        ("1.001-5.000", "1001-5000"),
        ("5000+", "5001+"),
        ("mas de 1000", "1001-5000"),
        ("más de 10", "11-50"),
        ("10,001+", "5001+"),
        ("120", "51-200"),
        ("0", "1-10"),
        ("50-250", "51-200"),
        ("10.5k employees", "5001+"),
        ("1,5k", "1001-5000"),
        ("2.5M", "5001+"),
        ("pyme", None),
        ("", None),
        (None, None),
    ],
)
def test_parsear_tamano_empresa(texto: str | None, tramo: str | None) -> None:
    assert parsear_tamano_empresa(texto) == tramo


@pytest.mark.parametrize(
    ("empleados", "tramo"),
    [
        (1, "1-10"),
        (10, "1-10"),
        (11, "11-50"),
        (200, "51-200"),
        (5000, "1001-5000"),
        (5001, "5001+"),
    ],
)
def test_tramo_por_empleados_en_los_bordes(empleados: int, tramo: str) -> None:
    assert tramo_por_empleados(empleados) == tramo


def test_los_tramos_de_la_tabla_y_del_schema_coinciden() -> None:
    assert {tramo_por_empleados(n) for n in (1, 11, 51, 201, 501, 1001, 5001)} == set(
        COMPANY_SIZE_BUCKETS
    )


# ─── Entrada del FIT ─────────────────────────────────────────────────────────────────────────


class TestFitInputDeLead:
    def test_las_columnas_mandan_sobre_lo_enriquecido(self) -> None:
        datos = fit_input_de_lead(
            industry="Software",
            company_size="11-50",
            job_title="CTO",
            enrichment_data={
                "company": {"industry": "Retail", "employees": 9000, "country": "MX"},
                "person": {"job_title": "Becario", "country": "AR"},
            },
        )
        assert datos == FitInput(
            industry="Software", company_size="11-50", job_title="CTO", country="MX"
        )

    def test_lo_enriquecido_cubre_huecos(self) -> None:
        datos = fit_input_de_lead(
            industry="  ",
            company_size=None,
            job_title=None,
            enrichment_data={
                "company": {"industry": "Salud", "employee_range": "51-200"},
                "person": {"job_title": "Gerente", "country": "CO"},
            },
        )
        assert datos == FitInput(
            industry="Salud", company_size="51-200", job_title="Gerente", country="CO"
        )

    def test_empleados_numericos_si_no_hay_rango(self) -> None:
        datos = fit_input_de_lead(
            industry=None, company_size=None, job_title=None,
            enrichment_data={"company": {"employees": 300}},
        )  # fmt: skip
        assert datos.company_size == "201-500"

    @pytest.mark.parametrize("raro", [{"company": "acme"}, {"company": {"employees": True}}, []])
    def test_enrichment_data_con_forma_inesperada_no_rompe(self, raro: object) -> None:
        datos = fit_input_de_lead(
            industry=None,
            company_size=None,
            job_title=None,
            enrichment_data=raro,  # type: ignore[arg-type]
        )
        assert datos == FitInput()


# ─── Comparacion de terminos ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("texto", "termino", "esperado"),
    [
        ("Directora de Tecnología", "director", True),  # prefijo de una palabra larga
        ("Gerente General de Compras", "gerente de compras", True),  # en cualquier orden
        ("CTO", "cto", True),
        ("CTOs Council", "cto", False),  # las cortas solo encajan completas
        ("Tienda de ropa", "ti", False),
        ("Software empresarial", "software", True),
        ("Hardware", "software", False),
        ("", "software", False),
        ("Software", "---", False),
    ],
)
def test_contiene_termino(texto: str, termino: str, esperado: bool) -> None:
    assert contiene_termino(texto, termino) is esperado


# ─── FIT ─────────────────────────────────────────────────────────────────────────────────────

ICP_COMPLETO = _icp(
    industries=["software", "salud digital"],
    company_sizes=["51-200"],
    job_titles=["director", "cto"],
    excluded_job_titles=["practicante"],
    countries=["CO", "MX"],
)


class TestCalcularFit:
    def test_encaje_perfecto_es_100(self) -> None:
        r = calcular_fit(
            FitInput("Software B2B", "51-200", "Directora Comercial", "CO"), ICP_COMPLETO
        )
        assert r.score == 100
        assert r.aplicable
        assert {d: v["result"] for d, v in r.factors["dimensions"].items()} == {
            "industry": MATCH,
            "company_size": MATCH,
            "job_title": MATCH,
            "region": MATCH,
        }
        assert r.factors["completeness"] == "4/4"

    def test_sin_icp_no_es_aplicable(self) -> None:
        r = calcular_fit(FitInput("Software"), None)
        assert not r.aplicable
        assert r.factors["reason"] == "icp_not_configured"

    def test_lo_que_falta_cuenta_cero_y_no_reparte_su_peso(self) -> None:
        r = calcular_fit(FitInput(industry="Software"), ICP_COMPLETO)
        # Solo el sector (30 de 100): un lead del que solo se sabe eso no puede sacar 100.
        assert r.score == 30
        assert r.factors["dimensions"]["job_title"]["result"] == MISSING
        assert r.factors["completeness"] == "1/4"

    def test_credito_parcial_por_tramo_vecino_y_sector_mas_generico(self) -> None:
        r = calcular_fit(FitInput("Salud", "11-50", "Director", "CO"), ICP_COMPLETO)
        assert r.factors["dimensions"]["industry"]["result"] == PARTIAL
        assert r.factors["dimensions"]["company_size"]["result"] == PARTIAL
        # 15 + 12.5 + 30 + 15 = 72.5 -> la mitad sube.
        assert r.score == 73

    def test_un_cargo_excluido_deja_el_fit_en_cero(self) -> None:
        r = calcular_fit(
            FitInput("Software", "51-200", "Practicante de Ventas", "CO"), ICP_COMPLETO
        )
        assert r.score == 0
        assert r.factors["excluded_by"] == "job_title"
        assert r.factors["dimensions"]["job_title"]["result"] == EXCLUDED
        assert all(d["points"] == 0 for d in r.factors["dimensions"].values())

    def test_excluir_es_por_palabra_completa_no_por_prefijo(self) -> None:
        icp = _icp(job_titles=["director"], excluded_job_titles=["intern", "practica"])
        internacional = calcular_fit(FitInput(job_title="International Sales Director"), icp)
        assert internacional.score == 100
        assert calcular_fit(FitInput(job_title="Directora de practica clinica"), icp).score == 0
        assert calcular_fit(FitInput(job_title="Interns program director"), icp).score == 0
        # La inclusion sigue encajando por prefijo.
        assert calcular_fit(FitInput(job_title="Directora comercial"), icp).score == 100

    def test_una_dimension_solo_con_exclusiones_filtra_pero_no_puntua(self) -> None:
        icp = _icp(countries=["CO"], excluded_industries=["apuestas"])
        assert calcular_fit(FitInput("Retail", country="CO"), icp).score == 100
        assert calcular_fit(FitInput("Apuestas deportivas", country="CO"), icp).score == 0

    def test_dimensiones_sin_configurar_no_cuentan(self) -> None:
        r = calcular_fit(FitInput(country="MX"), _icp(countries=["CO", "MX"]))
        assert r.score == 100
        assert r.factors["dimensions"]["industry"]["result"] == NOT_CONFIGURED

    def test_una_dimension_con_peso_cero_no_cuenta(self) -> None:
        icp = _icp(
            industries=["software"],
            countries=["CO"],
            weights={"industry": 0, "company_size": 25, "job_title": 30, "region": 15},
        )
        assert calcular_fit(FitInput("Retail", country="CO"), icp).score == 100

    def test_pais_distinto_no_encaja(self) -> None:
        r = calcular_fit(FitInput(country="AR"), _icp(countries=["CO"]))
        assert (r.score, r.factors["dimensions"]["region"]["result"]) == (0, NO_MATCH)

    def test_los_factores_no_llevan_los_datos_del_lead(self) -> None:
        r = calcular_fit(
            FitInput("Biotecnologia Andina", "51-200", "Jefa de Laboratorio", "CO"), ICP_COMPLETO
        )
        volcado = json.dumps(r.factors).lower()
        assert "biotecnologia" not in volcado
        assert "laboratorio" not in volcado


# ─── Comportamiento ──────────────────────────────────────────────────────────────────────────


class TestCalcularBehavioral:
    def test_sin_ninguna_senal_es_cero_y_aplicable(self) -> None:
        r = calcular_behavioral(BehavioralSignals(), AHORA)
        assert (r.score, r.aplicable) == (0, True)

    @pytest.mark.parametrize(
        ("hace", "puntos"),
        [
            (timedelta(hours=2), 30),
            (timedelta(days=2), 25),
            (timedelta(days=6), 20),
            (timedelta(days=10), 14),
            (timedelta(days=29), 8),
            (timedelta(days=80), 3),
            (timedelta(days=200), 0),
            (-timedelta(days=1), 30),  # reloj adelantado: cuenta como ahora, no mas
        ],
    )
    def test_recencia(self, hace: timedelta, puntos: int) -> None:
        r = calcular_behavioral(BehavioralSignals(last_activity_at=AHORA - hace), AHORA)
        assert r.factors["components"]["recency"] == puntos

    def test_volumen_con_tope(self) -> None:
        assert calcular_behavioral(BehavioralSignals(inbound_messages=3), AHORA).score == 15
        assert calcular_behavioral(BehavioralSignals(inbound_messages=40), AHORA).score == 30

    @pytest.mark.parametrize(
        ("minutos", "puntos"),
        [((5.0, 30.0, 400.0), 25), ((90.0,), 18), ((2000.0,), 10), ((10_000.0,), 5)],
    )
    def test_respuesta_por_mediana(self, minutos: tuple[float, ...], puntos: int) -> None:
        r = calcular_behavioral(BehavioralSignals(response_minutes=minutos), AHORA)
        assert r.factors["components"]["responsiveness"] == puntos

    def test_escribir_primero_cuenta_si_nunca_contesto(self) -> None:
        r = calcular_behavioral(BehavioralSignals(lead_initiated=True), AHORA)
        assert r.factors["components"]["responsiveness"] == 15

    def test_engagement_con_tope(self) -> None:
        r = calcular_behavioral(BehavioralSignals(link_clicks=1, email_opens=2), AHORA)
        assert r.factors["components"]["engagement"] == 9
        r = calcular_behavioral(BehavioralSignals(link_clicks=9, email_opens=9), AHORA)
        assert r.factors["components"]["engagement"] == 15

    def test_todo_al_maximo_es_100(self) -> None:
        senales = BehavioralSignals(
            last_activity_at=AHORA,
            inbound_messages=10,
            response_minutes=(3.0,),
            link_clicks=5,
        )
        assert calcular_behavioral(senales, AHORA).score == 100


def test_tipos_de_score_iguales_en_modelo_y_migracion() -> None:
    ruta = Path(__file__).parents[2] / "migrations" / "versions" / "027_lead_scores.py"
    spec = importlib.util.spec_from_file_location("m027", ruta)
    assert spec is not None
    assert spec.loader is not None
    migracion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migracion)
    assert modelo_score.SCORE_TYPES == migracion.SCORE_TYPES
