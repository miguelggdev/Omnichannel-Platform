"""Modelos y schemas de leads sin base de datos: indices ciegos, validacion y coherencia."""

import importlib.util
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.encryption import blind_index
from app.models.lead import (
    LEAD_STATUSES,
    LEAD_TEMPERATURES,
    Lead,
    _sincronizar_indices_ciegos,
    canonicalizar_linkedin,
    normalizar_telefono_de_lead,
)
from app.models.lead_source import LEAD_SOURCE_TYPES
from app.schemas.lead import (
    LeadCapture,
    LeadCreate,
    LeadResponse,
    LeadStatus,
    LeadUpdate,
    SourceResponse,
    SourceType,
    StageCreate,
    StageReorder,
    StageUpdate,
    Temperature,
)
from app.services.lead_pipeline import (
    DEFAULT_STAGES,
    DISQUALIFIED_SLUG,
    FIRST_STAGE_SLUG,
    generate_capture_token,
    hash_capture_token,
)

TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _sincronizar(lead: Lead) -> Lead:
    _sincronizar_indices_ciegos(None, None, lead)
    return lead


class TestIndicesCiegos:
    def test_el_email_se_normaliza_antes_de_hashear(self) -> None:
        a = _sincronizar(Lead(client_id=TENANT_A, email="  Ana@Example.COM "))
        b = _sincronizar(Lead(client_id=TENANT_A, email="ana@example.com"))
        assert a.email_hash == b.email_hash == blind_index("ana@example.com", TENANT_A)

    def test_el_mismo_email_en_otro_tenant_da_otro_hash(self) -> None:
        a = _sincronizar(Lead(client_id=TENANT_A, email="ana@example.com"))
        b = _sincronizar(Lead(client_id=TENANT_B, email="ana@example.com"))
        assert a.email_hash != b.email_hash

    @pytest.mark.parametrize(
        "formato", ["+57 300 111-2233", "573001112233", "(+57) 300-111-2233", " +573001112233 "]
    )
    def test_el_telefono_es_el_mismo_en_cualquier_formato(self, formato: str) -> None:
        referencia = _sincronizar(Lead(client_id=TENANT_A, phone="+573001112233"))
        assert _sincronizar(Lead(client_id=TENANT_A, phone=formato)).phone_hash == (
            referencia.phone_hash
        )

    def test_sin_valor_no_hay_hash(self) -> None:
        lead = _sincronizar(Lead(client_id=TENANT_A, email=None, phone=None))
        assert (lead.email_hash, lead.phone_hash) == (None, None)

    def test_quitar_el_valor_borra_el_hash(self) -> None:
        lead = _sincronizar(Lead(client_id=TENANT_A, email="a@b.co", phone="3001112233"))
        assert lead.email_hash
        assert lead.phone_hash
        lead.email = None
        lead.phone = ""
        _sincronizar(lead)
        assert (lead.email_hash, lead.phone_hash) == (None, None)

    @pytest.mark.parametrize(
        ("valor", "esperado"),
        [("+57 300-111", "57300111"), ("abc", "abc"), (" ABC ", "abc"), ("", None), (None, None)],
    )
    def test_normalizar_telefono(self, valor: str | None, esperado: str | None) -> None:
        assert normalizar_telefono_de_lead(valor) == esperado


class TestCoherenciaConLaBase:
    def test_los_literales_del_schema_son_los_del_check(self) -> None:
        """Un valor que el schema acepta y el CHECK rechaza seria un 500."""
        assert set(SourceType.__args__) == set(LEAD_SOURCE_TYPES)  # type: ignore[attr-defined]
        assert set(LeadStatus.__args__) == set(LEAD_STATUSES)  # type: ignore[attr-defined]
        assert set(Temperature.__args__) == set(LEAD_TEMPERATURES)  # type: ignore[attr-defined]

    def test_la_migracion_y_el_modelo_listan_los_mismos_tipos_de_fuente(self) -> None:
        ruta = Path(__file__).parents[2] / "migrations" / "versions" / "025_lead_management.py"
        spec = importlib.util.spec_from_file_location("m025", ruta)
        assert spec is not None
        assert spec.loader is not None
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        assert tuple(modulo.SOURCE_TYPES) == LEAD_SOURCE_TYPES

    def test_las_etapas_por_defecto_son_validas(self) -> None:
        slugs = [s[0] for s in DEFAULT_STAGES]
        assert len(set(slugs)) == len(slugs)
        assert slugs[0] == FIRST_STAGE_SLUG
        assert DISQUALIFIED_SLUG in slugs
        assert {s[0] for s in DEFAULT_STAGES if s[3]} == {"won", "lost", "disqualified"}
        for slug, nombre, color, _ in DEFAULT_STAGES:
            StageCreate(name=nombre, slug=slug, color=color)  # no lanza


class TestToken:
    def test_el_token_es_aleatorio_y_su_hash_reproducible(self) -> None:
        t1, h1 = generate_capture_token()
        t2, h2 = generate_capture_token()
        assert t1 != t2
        assert h1 != h2
        assert hash_capture_token(t1) == h1
        assert len(h1) == 64
        assert t1 not in h1

    def test_el_token_tiene_entropia_suficiente(self) -> None:
        assert len(generate_capture_token()[0]) >= 40


class TestSchemasDeLead:
    def test_hace_falta_alguna_forma_de_contacto(self) -> None:
        with pytest.raises(ValidationError, match="al menos email"):
            LeadCreate(first_name="Ana")
        assert LeadCreate(email="ana@example.com").email == "ana@example.com"
        assert LeadCreate(phone="+57 300 111-2233").phone
        assert LeadCreate(linkedin_url="https://linkedin.com/in/ana").linkedin_url

    @pytest.mark.parametrize("telefono", ["abc", "12", "+57;DROP", "1" * 30])
    def test_telefono_invalido(self, telefono: str) -> None:
        with pytest.raises(ValidationError):
            LeadCreate(email="a@b.co", phone=telefono)

    @pytest.mark.parametrize(
        "url", ["http://linkedin.com/in/a", "javascript:alert(1)", "linkedin.com/in/a"]
    )
    def test_linkedin_solo_https(self, url: str) -> None:
        with pytest.raises(ValidationError, match="https"):
            LeadCreate(email="a@b.co", linkedin_url=url)

    def test_valor_y_moneda(self) -> None:
        with pytest.raises(ValidationError):
            LeadCreate(email="a@b.co", estimated_value=-1)
        with pytest.raises(ValidationError):
            LeadCreate(email="a@b.co", currency="usd")
        with pytest.raises(ValidationError):
            LeadCreate(email="a@b.co", temperature="tibio")  # type: ignore[arg-type]
        assert LeadCreate(email="a@b.co", estimated_value="1500.50", currency="COP")

    def test_los_scores_y_el_tenant_no_se_pueden_enviar(self) -> None:
        """`extra` se ignora: un cliente no puede fijar su propio score ni su `client_id`."""
        lead = LeadCreate.model_validate(
            {"email": "a@b.co", "fit_score": 100, "total_score": 100, "client_id": str(TENANT_B)}
        )
        assert not hasattr(lead, "fit_score")
        assert not hasattr(lead, "client_id")

    def test_actualizar_exige_algun_campo(self) -> None:
        with pytest.raises(ValidationError, match="al menos un campo"):
            LeadUpdate()
        assert LeadUpdate(job_title=None).model_fields_set == {"job_title"}

    def test_la_respuesta_no_expone_hashes_ni_enriquecimiento_crudo(self) -> None:
        campos = set(LeadResponse.model_fields)
        assert not {"email_hash", "phone_hash", "enrichment_data", "deleted_at"} & campos


class TestSchemasDeEtapasYFuentes:
    @pytest.mark.parametrize("slug", ["Nuevo", "con espacio", "x-y", "", "a" * 51, "_a"])
    def test_slug_invalido(self, slug: str) -> None:
        with pytest.raises(ValidationError):
            StageCreate(name="X", slug=slug)

    @pytest.mark.parametrize("color", ["red", "#fff", "#GGGGGG", "112233"])
    def test_color_invalido(self, color: str) -> None:
        with pytest.raises(ValidationError):
            StageCreate(name="X", slug="x", color=color)
        with pytest.raises(ValidationError):
            StageUpdate(color=color)

    def test_la_etapa_no_cambia_de_slug(self) -> None:
        assert "slug" not in StageUpdate.model_fields

    def test_reordenar_rechaza_repetidos_y_vacio(self) -> None:
        a = uuid.uuid4()
        with pytest.raises(ValidationError, match="repetidas"):
            StageReorder(stage_ids=[a, a])
        with pytest.raises(ValidationError):
            StageReorder(stage_ids=[])

    def test_la_fuente_nunca_expone_el_hash_del_token(self) -> None:
        assert "capture_token_hash" not in SourceResponse.model_fields

    def test_tipo_de_fuente_desconocido(self) -> None:
        from app.schemas.lead import SourceCreate

        with pytest.raises(ValidationError):
            SourceCreate(name="X", source_type="tiktok")  # type: ignore[arg-type]

    def test_captura_publica_lleva_honeypot_y_utm(self) -> None:
        c: Any = LeadCapture(email="a@b.co", utm_source="google", website="http://spam")
        assert c.website == "http://spam"
        assert c.utm_source == "google"
        with pytest.raises(ValidationError):
            LeadCapture(first_name="solo nombre")


class TestCanonizarLinkedin:
    @pytest.mark.parametrize(
        ("original", "esperado"),
        [
            ("HTTP://LinkedIn.com/in/Ana/?trk=x#y", "https://linkedin.com/in/ana"),
            ("https://www.linkedin.com/in/ana///", "https://www.linkedin.com/in/ana"),
            ("  https://linkedin.com/in/ana  ", "https://linkedin.com/in/ana"),
            ("https://linkedin.com/in/ana?a=b", "https://linkedin.com/in/ana"),
            ("", None),
            ("   ", None),
            (None, None),
        ],
    )
    def test_canonizar(self, original: str | None, esperado: str | None) -> None:
        assert canonicalizar_linkedin(original) == esperado

    def test_el_listener_la_canoniza_al_escribir(self) -> None:
        lead = _sincronizar(
            Lead(client_id=TENANT_A, linkedin_url="HTTP://LinkedIn.com/in/Ana/?x=1")
        )
        assert lead.linkedin_url == "https://linkedin.com/in/ana"
        assert _sincronizar(Lead(client_id=TENANT_A, linkedin_url="  ")).linkedin_url is None


class TestInstanteMonotono:
    def test_nunca_se_repite_ni_retrocede_aunque_el_reloj_coincida(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import datetime as dt

        from app.services import lead_activity

        fijo = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

        class RelojParado(dt.datetime):
            @classmethod
            def now(cls, tz: dt.tzinfo | None = None) -> "RelojParado":  # type: ignore[override]
                return cls(2026, 1, 1, tzinfo=tz)

        monkeypatch.setattr(lead_activity, "datetime", RelojParado)
        monkeypatch.setattr(lead_activity, "_ultimo_instante", fijo - dt.timedelta(seconds=1))
        instantes = [lead_activity.instante_monotono() for _ in range(5)]
        assert instantes == sorted(set(instantes))
        assert instantes[0] == fijo
        assert instantes[4] == fijo + dt.timedelta(microseconds=4)
