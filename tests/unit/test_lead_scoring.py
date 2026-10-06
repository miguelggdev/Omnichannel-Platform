"""Score total del lead: pesos por tenant, redondeo y validaciones."""

import pytest

from app.services.lead_scoring import (
    DEFAULT_WEIGHTS,
    InvalidWeightsError,
    compute_total_score,
    normalizar_pesos,
)


class TestComputeTotalScore:
    def test_pesos_por_defecto_40_30_30(self) -> None:
        # (80*40 + 50*30 + 20*30) / 100 = 53
        assert compute_total_score(80, 50, 20) == 53

    def test_los_extremos(self) -> None:
        assert compute_total_score(0, 0, 0) == 0
        assert compute_total_score(100, 100, 100) == 100

    def test_cada_tenant_pondera_a_su_manera(self) -> None:
        solo_ia = {"fit": 0, "behavioral": 0, "ai": 100}
        assert compute_total_score(10, 20, 90, solo_ia) == 90
        mitad = {"fit": 50, "behavioral": 50, "ai": 0}
        assert compute_total_score(80, 40, 100, mitad) == 60

    def test_los_pesos_no_tienen_que_sumar_100(self) -> None:
        assert compute_total_score(90, 60, 30, {"fit": 2, "behavioral": 1, "ai": 1}) == 68
        assert compute_total_score(90, 60, 30, {"fit": 200, "behavioral": 100, "ai": 100}) == 68

    @pytest.mark.parametrize(
        ("fit", "behavioral", "ai", "esperado"),
        [(1, 0, 0, 0), (2, 1, 0, 1), (3, 0, 0, 1), (5, 0, 0, 2), (0, 2, 0, 1)],
    )
    def test_la_mitad_se_redondea_hacia_arriba(
        self, fit: int, behavioral: int, ai: int, esperado: int
    ) -> None:
        # Con 40/30/30: 5*40/100 = 2.0 ; 3*40/100 = 1.2 ; 2*30/100 = 0.6 ; 1*40/100 = 0.4
        assert compute_total_score(fit, behavioral, ai) == esperado

    def test_el_redondeo_exacto_de_medio_punto(self) -> None:
        # 50*{1,1,0}/2 = 25 exacto; 25.5 -> 26 y 24.5 -> 25 (nunca el .5 bancario)
        iguales = {"fit": 1, "behavioral": 1, "ai": 0}
        assert compute_total_score(25, 26, 0, iguales) == 26  # 25.5
        assert compute_total_score(24, 25, 0, iguales) == 25  # 24.5

    def test_nunca_sale_del_rango_ni_baja_al_subir_un_componente(self) -> None:
        anterior = -1
        for fit in range(0, 101, 5):
            total = compute_total_score(fit, 37, 62)
            assert 0 <= total <= 100
            assert total >= anterior
            anterior = total

    @pytest.mark.parametrize("valor", [-1, 101, 1000])
    @pytest.mark.parametrize("campo", [0, 1, 2])
    def test_un_score_fuera_de_rango_es_un_error(self, campo: int, valor: int) -> None:
        scores = [50, 50, 50]
        scores[campo] = valor
        with pytest.raises(ValueError, match="entre 0 y 100"):
            compute_total_score(*scores)


class TestNormalizarPesos:
    def test_sin_pesos_se_usan_los_de_por_defecto(self) -> None:
        assert normalizar_pesos(None) == DEFAULT_WEIGHTS
        assert normalizar_pesos({}) == DEFAULT_WEIGHTS

    def test_el_por_defecto_no_se_comparte_por_referencia(self) -> None:
        normalizar_pesos(None)["fit"] = 99
        assert DEFAULT_WEIGHTS["fit"] == 40

    @pytest.mark.parametrize(
        "pesos",
        [
            {"fit": 50, "behavioral": 50},  # falta ai
            {"fit": -1, "behavioral": 50, "ai": 51},
            {"fit": 1.5, "behavioral": 50, "ai": 50},
            {"fit": "40", "behavioral": 30, "ai": 30},
            {"fit": True, "behavioral": 30, "ai": 30},
            {"fit": 0, "behavioral": 0, "ai": 0},
        ],
    )
    def test_pesos_inutilizables(self, pesos: dict[str, object]) -> None:
        with pytest.raises(InvalidWeightsError):
            normalizar_pesos(pesos)

    def test_las_claves_de_mas_se_ignoran(self) -> None:
        assert normalizar_pesos({"fit": 1, "behavioral": 2, "ai": 3, "otra": 99}) == {
            "fit": 1,
            "behavioral": 2,
            "ai": 3,
        }
