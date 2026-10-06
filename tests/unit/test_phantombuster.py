"""Lectura tolerante del webhook de Phantombuster, sin red ni base de datos."""

import json
from typing import Any

import pytest

from app.services.lead_import import campos_de_objeto
from app.services.phantombuster import MAX_PERFILES, extraer_perfiles

PERFILES = [
    {"profileUrl": "https://linkedin.com/in/ana", "fullName": "Ana Gomez", "company": "ACME"},
    {"profileUrl": "https://linkedin.com/in/beto", "firstName": "Beto", "title": "CTO"},
]


def _webhook(resultado: Any, **extra: Any) -> dict[str, Any]:
    return {
        "agentId": "123",
        "containerId": "456",
        "exitCode": 0,
        "exitMessage": "finished",
        "resultObject": resultado,
        **extra,
    }


class TestExtraer:
    def test_resultobject_como_texto_json_que_es_como_lo_documenta_phantombuster(self) -> None:
        e = extraer_perfiles(_webhook(json.dumps(PERFILES)))
        assert (e.recibidos, e.exit_ok, e.solo_enlaces, e.motivo) == (2, True, False, None)
        assert e.perfiles == PERFILES

    def test_resultobject_ya_decodificado_lista_directa_y_formas_con_lista_dentro(self) -> None:
        assert extraer_perfiles(_webhook(PERFILES)).recibidos == 2
        assert extraer_perfiles(PERFILES).recibidos == 2  # reenviado por Zapier/Make
        for clave in ("results", "profiles", "data", "items", "leads"):
            assert extraer_perfiles(_webhook(json.dumps({clave: PERFILES}))).recibidos == 2, clave

    def test_un_objeto_con_campos_reconocibles_es_un_unico_perfil(self) -> None:
        e = extraer_perfiles(_webhook(json.dumps(PERFILES[0])))
        assert e.perfiles == [PERFILES[0]]

    @pytest.mark.parametrize(
        "resultado",
        [
            {"csvUrl": "https://cache.example/x.csv", "jsonUrl": "https://cache.example/x.json"},
            {"csvURL": "http://169.254.169.254/latest/meta-data"},
        ],
    )
    def test_si_solo_hay_enlaces_no_se_sigue_ninguno_y_se_informa(
        self, resultado: dict[str, Any]
    ) -> None:
        e = extraer_perfiles(_webhook(json.dumps(resultado)))
        assert e.solo_enlaces is True
        assert e.perfiles == []
        assert e.recibidos == 0

    @pytest.mark.parametrize(
        "extra",
        [{"exitCode": 1}, {"exitMessage": "killed"}, {"exitMessage": "global timeout"}],
    )
    def test_un_agente_que_no_termino_bien_no_importa(self, extra: dict[str, Any]) -> None:
        e = extraer_perfiles(_webhook(json.dumps(PERFILES), **extra))
        assert (e.exit_ok, e.perfiles) == (False, [])

    @pytest.mark.parametrize(
        "payload",
        [
            _webhook("esto no es json {"),
            _webhook(None),
            _webhook(json.dumps({"algo": "raro"})),
            _webhook(json.dumps(["texto", "suelto"])),
            ["texto", 1],
            "cadena",
            42,
            None,
        ],
    )
    def test_formas_no_reconocidas_no_rompen_ni_importan_nada(self, payload: Any) -> None:
        e = extraer_perfiles(payload)
        assert e.perfiles == []
        assert e.motivo

    def test_se_recorta_a_500_pero_se_informa_el_total(self) -> None:
        muchos = [{"profileUrl": f"https://linkedin.com/in/p{i}"} for i in range(MAX_PERFILES + 50)]
        e = extraer_perfiles(_webhook(json.dumps(muchos)))
        assert (len(e.perfiles), e.recibidos) == (MAX_PERFILES, MAX_PERFILES + 50)


class TestCamposDeObjeto:
    def test_alias_de_phantombuster_y_valores_escalares(self) -> None:
        campos = campos_de_objeto(
            {
                "profileUrl": "https://linkedin.com/in/ana",
                "fullName": "Ana Gomez",
                "company": "ACME",
                "title": "CTO",
                "phone": 573001112233,
                "rareza": "ignorada",
            }
        )
        assert campos == {
            "linkedin_url": "https://linkedin.com/in/ana",
            "full_name": "Ana Gomez",
            "company_name": "ACME",
            "job_title": "CTO",
            "phone": "573001112233",
        }

    def test_objetos_anidados_booleanos_y_vacios_se_ignoran_y_gana_la_primera_clave(self) -> None:
        campos = campos_de_objeto(
            {
                "email": {"valor": "x@example.com"},
                "company": "",
                "title": True,
                "name": "Primero",
                "fullName": "Segundo",
                "profileUrl": None,
            }
        )
        assert campos == {"full_name": "Primero"}
