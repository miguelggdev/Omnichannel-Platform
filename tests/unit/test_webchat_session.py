"""Tests de la sesion firmada del visitante de Webchat.

El token es lo unico que separa a un visitante de otro: sin el, conocer o adivinar
un `visitor_id` no debe permitir leer su conversacion ni escribir en su nombre.
"""

import uuid

import pytest

from app.core.config import get_settings
from app.services import webchat_session as ws
from app.services.webchat_session import emitir_sesion, verificar_sesion

TENANT = uuid.uuid4()
AHORA = 1_800_000_000.0


class TestEmitir:
    def test_el_token_verifica_y_devuelve_el_visitante(self) -> None:
        sesion = emitir_sesion(TENANT, ahora=AHORA)

        assert verificar_sesion(sesion.token, TENANT, ahora=AHORA + 10) == sesion.visitor_id

    def test_el_visitante_es_aleatorio_de_128_bits(self) -> None:
        ids = {emitir_sesion(TENANT).visitor_id for _ in range(3000)}

        assert len(ids) == 3000
        assert all(len(v) == 32 and int(v, 16) >= 0 for v in ids)

    def test_el_visitor_id_por_si_solo_no_es_un_token(self) -> None:
        """Conocerlo (los agentes lo ven en la bandeja) no da acceso a la sesion."""
        sesion = emitir_sesion(TENANT, ahora=AHORA)

        assert verificar_sesion(sesion.visitor_id, TENANT, ahora=AHORA) is None

    def test_caduca_a_los_dias_configurados(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(get_settings(), "WEBCHAT_SESSION_TTL_DAYS", 2)
        sesion = emitir_sesion(TENANT, ahora=AHORA)

        assert verificar_sesion(sesion.token, TENANT, ahora=AHORA + 2 * 86400 - 1) is not None
        assert verificar_sesion(sesion.token, TENANT, ahora=AHORA + 2 * 86400 + 1) is None


class TestVerificar:
    """Nada de esto puede colar una sesion ni lanzar una excepcion."""

    def test_un_token_de_otro_tenant_no_vale(self) -> None:
        """La firma va ligada al tenant: no se puede usar un token en otro."""
        sesion = emitir_sesion(TENANT, ahora=AHORA)

        assert verificar_sesion(sesion.token, uuid.uuid4(), ahora=AHORA) is None

    def test_cambiar_el_visitante_invalida_la_firma(self) -> None:
        sesion = emitir_sesion(TENANT, ahora=AHORA)
        _, caduca, firma = sesion.token.split(".")
        otro = emitir_sesion(TENANT, ahora=AHORA).visitor_id

        assert verificar_sesion(f"{otro}.{caduca}.{firma}", TENANT, ahora=AHORA) is None

    def test_alargar_la_caducidad_invalida_la_firma(self) -> None:
        sesion = emitir_sesion(TENANT, ahora=AHORA)
        visitor, caduca, firma = sesion.token.split(".")

        assert (
            verificar_sesion(f"{visitor}.{int(caduca) + 10**9}.{firma}", TENANT, ahora=AHORA)
            is None
        )

    def test_una_firma_alterada_no_vale(self) -> None:
        sesion = emitir_sesion(TENANT, ahora=AHORA)
        visitor, caduca, firma = sesion.token.split(".")
        alterada = ("A" if firma[0] != "A" else "B") + firma[1:]

        assert verificar_sesion(f"{visitor}.{caduca}.{alterada}", TENANT, ahora=AHORA) is None

    def test_un_token_firmado_con_otra_clave_no_vale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sesion = emitir_sesion(TENANT, ahora=AHORA)
        monkeypatch.setattr(get_settings(), "JWT_SECRET", "otra-clave-completamente-distinta-32ch")

        assert verificar_sesion(sesion.token, TENANT, ahora=AHORA) is None

    @pytest.mark.parametrize(
        "token",
        [
            None,
            "",
            "abc",
            "a.b",
            "a.b.c.d",
            "x" * 32 + ".notanumber.firma",
            "x" * 31 + ".123.firma",
            "é" * 32 + ".123.éé",  # no ASCII: compare_digest lanzaria con str
            "." * 5,
        ],
    )
    def test_entradas_malformadas_devuelven_none_sin_lanzar(self, token: str | None) -> None:
        assert verificar_sesion(token, TENANT, ahora=AHORA) is None


class TestClave:
    def test_la_clave_no_es_el_jwt_secret_directamente(self) -> None:
        """Un token de Webchat no debe poder confundirse con un JWT de la app."""
        assert ws._clave() != get_settings().JWT_SECRET.encode()
        assert len(ws._clave()) == 32
