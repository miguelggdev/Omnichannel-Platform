"""Tests del flujo de vinculo de telefono en Telegram (boton `request_contact`).

Ni pedir el numero ni agradecerlo pasan por la IA: un LLM no debe decidir cuando
pedir un dato personal. Y la respuesta no debe revelar si el numero coincidio con
otro contacto.
"""

from datetime import datetime, timezone
from typing import Any

import pytest

from app.schemas.message import NormalizedMessage
from app.services import contact_request as cr
from app.services.contact_request import respuesta_del_flujo_de_telefono

AHORA = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _msg(canal: str = "telegram", texto: str | None = "hola", **extra: Any) -> NormalizedMessage:
    """Mensaje entrante normalizado."""
    return NormalizedMessage(
        channel=canal,  # type: ignore[arg-type]
        sender_identifier="789",
        text=texto,
        timestamp=AHORA,
        external_message_id="x1",
        raw_payload={},
        **extra,
    )


class TestSolicitud:
    """`/vincular` ofrece el boton; el bot nunca lo pide por iniciativa propia."""

    @pytest.mark.parametrize(
        "texto",
        ["/vincular", "/link", "/VINCULAR", "  /vincular  ", "/vincular@MiBot", "/link por favor"],
    )
    def test_los_comandos_ofrecen_el_boton(self, texto: str) -> None:
        respuesta = respuesta_del_flujo_de_telefono(_msg(texto=texto))

        assert respuesta is not None
        assert respuesta.text == cr.TEXTO_SOLICITUD
        assert respuesta.metadata == {"request_contact": cr.ETIQUETA_BOTON}

    @pytest.mark.parametrize(
        "texto",
        [
            None,
            "",
            "   ",
            "hola",
            "/start",
            "/ayuda",
            "quiero vincular mi cuenta",  # el comando no esta al inicio
            "vincular",
            "/vincularme",  # otro comando distinto
        ],
    )
    def test_el_resto_de_mensajes_sigue_a_la_ia(self, texto: str | None) -> None:
        assert respuesta_del_flujo_de_telefono(_msg(texto=texto)) is None

    def test_el_primer_mensaje_no_pide_el_telefono(self) -> None:
        """Pedir un dato personal sin que nadie lo pida es intrusivo."""
        assert respuesta_del_flujo_de_telefono(_msg(texto="Hola, buenas tardes")) is None

    @pytest.mark.parametrize("canal", ["whatsapp", "instagram", "facebook", "email"])
    def test_solo_aplica_a_telegram(self, canal: str) -> None:
        assert respuesta_del_flujo_de_telefono(_msg(canal, texto="/vincular")) is None


class TestAgradecimiento:
    """Al compartir el numero se agradece y se retira el teclado."""

    def test_con_telefono_verificado_agradece_y_retira_el_teclado(self) -> None:
        respuesta = respuesta_del_flujo_de_telefono(
            _msg(texto="Compartio su numero de telefono", verified_phone="+573001234567")
        )

        assert respuesta is not None
        assert respuesta.text == cr.TEXTO_AGRADECIMIENTO
        assert respuesta.metadata == {"remove_keyboard": True}

    def test_no_revela_si_hubo_coincidencia_con_otro_contacto(self) -> None:
        """Decir "te reconoci en WhatsApp" confirmaria que ese numero ya existe."""
        texto = cr.TEXTO_AGRADECIMIENTO.lower()

        for palabra in ("whatsapp", "reconoc", "unific", "vincul", "encontr"):
            assert palabra not in texto

    def test_un_telefono_no_verificable_no_se_agradece(self) -> None:
        """Sin `verified_phone` (tarjeta ajena o reenviada) sigue a la IA."""
        assert (
            respuesta_del_flujo_de_telefono(_msg(texto="Contacto compartido: Grace +57300")) is None
        )

    def test_un_verified_phone_invalido_no_cuenta(self) -> None:
        assert respuesta_del_flujo_de_telefono(_msg(verified_phone="abc")) is None

    def test_el_agradecimiento_gana_sobre_un_comando(self) -> None:
        respuesta = respuesta_del_flujo_de_telefono(
            _msg(texto="/vincular", verified_phone="+573001234567")
        )

        assert respuesta is not None
        assert respuesta.metadata == {"remove_keyboard": True}
