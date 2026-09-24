"""Tests de `CSATService` y de los endpoints de CSAT (Sprint 11, Dev B).

`build_survey_message`/`extract_rating_from_text` son funciones puras: se
prueban directo. `process_response` usa la misma sesion falsa con cola de
resultados que el resto de la suite. Los endpoints (`GET /csat/respond`,
`GET /admin/csat/summary`) van via `authenticated_client`/`api_client`
(HTTP real contra la app), monkeypatcheando `tenant_session`.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.v1 import admin as admin_module
from app.api.v1 import csat as csat_api_module
from app.services import csat as csat_service
from app.services.csat import (
    STATUS_RESPONDED,
    build_survey_message,
    extract_rating_from_text,
    process_response,
    survey_exists,
)
from tests.unit.agent_doubles import fake_tenant_session


class _Resultado:
    def __init__(self, valor: Any = None) -> None:
        self._valor = valor

    def scalar_one_or_none(self) -> Any:
        return self._valor

    def one(self) -> Any:
        return self._valor

    def all(self) -> list:
        return list(self._valor) if isinstance(self._valor, list) else []


class _Sesion:
    def __init__(self, resultados: list[_Resultado] | None = None) -> None:
        self._resultados = list(resultados or [])
        self.flushed = 0

    async def execute(self, stmt: object = None, params: object = None) -> _Resultado:
        return self._resultados.pop(0) if self._resultados else _Resultado(None)

    async def flush(self) -> None:
        self.flushed += 1


def _survey(**over):
    base = {
        "id": uuid.uuid4(),
        "client_id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
        "status": "sent",
        "rating": None,
        "comment": None,
        "responded_at": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


# ─── build_survey_message ────────────────────────────────────────────────────


class TestBuildSurveyMessage:
    def test_texto_incluye_el_nombre(self) -> None:
        assert "Ada" in build_survey_message("whatsapp", "Ada")

    def test_sin_nombre_no_deja_espacio_de_mas(self) -> None:
        """Sin contact_name, el saludo es "Hola," y no "Hola ,"."""
        texto = build_survey_message("whatsapp", "")
        assert texto.startswith("Hola, ")

    def test_canal_no_email_pide_responder_con_numero(self) -> None:
        texto = build_survey_message("telegram", "Ada")
        assert "Responde solo con el numero" in texto
        assert "http" not in texto

    def test_email_sin_app_public_url_cae_a_texto_plano(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin URL publica configurada, un link relativo no sirve en un email."""
        monkeypatch.setattr(
            csat_service, "get_settings", lambda: SimpleNamespace(APP_PUBLIC_URL="")
        )
        texto = build_survey_message("email", "Ada", survey_id=uuid.uuid4(), client_id=uuid.uuid4())
        assert "http" not in texto

    def test_email_con_app_public_url_agrega_los_cinco_links(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            csat_service,
            "get_settings",
            lambda: SimpleNamespace(APP_PUBLIC_URL="https://api.ejemplo.com"),
        )
        survey_id, client_id = uuid.uuid4(), uuid.uuid4()

        texto = build_survey_message("email", "Ada", survey_id=survey_id, client_id=client_id)

        for rating in range(1, 6):
            assert f"rating={rating}" in texto
        assert f"survey_id={survey_id}" in texto
        assert f"client_id={client_id}" in texto


# ─── extract_rating_from_text ────────────────────────────────────────────────


class TestExtractRating:
    @pytest.mark.parametrize(
        ("texto", "esperado"),
        [
            ("5", 5),
            ("Le doy un 4, gracias", 4),
            ("  3  ", 3),
            ("excelente", None),
            ("", None),
            (None, None),
            ("el 15 de mayo", None),  # no debe leer el "1" de "15"
            ("10", None),  # tampoco el "1" de "10"
        ],
    )
    def test_casos(self, texto: str | None, esperado: int | None) -> None:
        assert extract_rating_from_text(texto) == esperado


# ─── survey_exists / process_response ────────────────────────────────────────


class TestSurveyExists:
    @pytest.mark.asyncio
    async def test_existe(self) -> None:
        sesion = _Sesion(resultados=[_Resultado(uuid.uuid4())])
        assert await survey_exists(sesion, uuid.uuid4()) is True

    @pytest.mark.asyncio
    async def test_no_existe(self) -> None:
        sesion = _Sesion(resultados=[_Resultado(None)])
        assert await survey_exists(sesion, uuid.uuid4()) is False


class TestProcessResponse:
    @pytest.mark.asyncio
    async def test_rating_fuera_de_rango_no_toca_la_base(self) -> None:
        sesion = _Sesion()

        resultado = await process_response(sesion, uuid.uuid4(), uuid.uuid4(), rating=6)

        assert resultado is None
        assert sesion.flushed == 0

    @pytest.mark.asyncio
    async def test_encuesta_inexistente(self) -> None:
        sesion = _Sesion(resultados=[_Resultado(None)])

        resultado = await process_response(sesion, uuid.uuid4(), uuid.uuid4(), rating=5)

        assert resultado is None

    @pytest.mark.asyncio
    async def test_encuesta_ya_respondida_no_se_sobrescribe(self) -> None:
        encuesta = _survey(status=STATUS_RESPONDED, rating=2)
        sesion = _Sesion(resultados=[_Resultado(encuesta)])

        resultado = await process_response(sesion, uuid.uuid4(), uuid.uuid4(), rating=5)

        assert resultado is encuesta
        assert encuesta.rating == 2  # no se piso con el 5 nuevo

    @pytest.mark.asyncio
    async def test_registra_rating_y_comentario(self) -> None:
        encuesta = _survey()
        sesion = _Sesion(resultados=[_Resultado(encuesta)])

        resultado = await process_response(
            sesion, uuid.uuid4(), uuid.uuid4(), rating=4, comment="Muy buena atencion"
        )

        assert resultado is encuesta
        assert encuesta.rating == 4
        assert encuesta.comment == "Muy buena atencion"
        assert encuesta.status == STATUS_RESPONDED
        assert encuesta.responded_at is not None


# ─── GET /csat/respond ────────────────────────────────────────────────────────


class TestRespondViaLink:
    URL = "/api/v1/csat/respond"

    async def test_no_requiere_jwt(self, api_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Publico: sin Authorization no debe dar 401 (esta en PUBLIC_PATHS)."""
        encuesta = _survey()
        monkeypatch.setattr(
            csat_api_module,
            "tenant_session",
            fake_tenant_session(_Sesion(resultados=[_Resultado(encuesta)])),
        )

        response = await api_client.get(
            self.URL,
            params={"survey_id": str(uuid.uuid4()), "client_id": str(uuid.uuid4()), "rating": 5},
        )

        assert response.status_code == 200
        assert "Gracias" in response.text

    async def test_encuesta_inexistente_muestra_pagina_de_error(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            csat_api_module,
            "tenant_session",
            fake_tenant_session(_Sesion(resultados=[_Resultado(None)])),
        )

        response = await api_client.get(
            self.URL,
            params={"survey_id": str(uuid.uuid4()), "client_id": str(uuid.uuid4()), "rating": 3},
        )

        assert response.status_code == 200
        assert "no encontrada" in response.text.lower()

    async def test_rating_fuera_de_rango_es_422(self, api_client: Any) -> None:
        response = await api_client.get(
            self.URL,
            params={"survey_id": str(uuid.uuid4()), "client_id": str(uuid.uuid4()), "rating": 9},
        )

        assert response.status_code == 422


# ─── GET /admin/csat/summary ──────────────────────────────────────────────────


class TestCsatSummary:
    URL = "/api/v1/admin/csat/summary"

    async def test_resumen_con_datos(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agregado = SimpleNamespace(
            total=10, respuestas=4, promedio=4.25, promotores=3, detractores=1
        )
        sesion = _Sesion(
            resultados=[
                _Resultado(agregado),
                _Resultado([(4, 2), (5, 1), (2, 1)]),
                _Resultado([]),
            ]
        )
        monkeypatch.setattr(admin_module, "tenant_session", fake_tenant_session(sesion))

        response = await authenticated_client.get(self.URL)

        assert response.status_code == 200
        cuerpo = response.json()
        assert cuerpo["total_surveys_sent"] == 10
        assert cuerpo["total_responses"] == 4
        assert cuerpo["response_rate"] == 40.0
        assert cuerpo["average_rating"] == 4.25
        assert cuerpo["promoters"] == 3
        assert cuerpo["detractors"] == 1
        assert cuerpo["distribution"] == {"4": 2, "5": 1, "2": 1}

    async def test_sin_encuestas_no_divide_por_cero(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agregado = SimpleNamespace(
            total=0, respuestas=0, promedio=None, promotores=0, detractores=0
        )
        sesion = _Sesion(resultados=[_Resultado(agregado), _Resultado([]), _Resultado([])])
        monkeypatch.setattr(admin_module, "tenant_session", fake_tenant_session(sesion))

        response = await authenticated_client.get(self.URL)

        assert response.status_code == 200
        assert response.json()["response_rate"] == 0.0
        assert response.json()["average_rating"] == 0.0
