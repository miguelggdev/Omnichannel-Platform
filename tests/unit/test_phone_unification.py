"""Tests de la unificacion automatica de contactos por telefono verificado.

Lo que se fija aqui es la **prudencia**: unir dos contactos que no son la misma
persona mezcla conversaciones de gente distinta, asi que cada test de "no une"
protege una fuga entre personas o entre tenants. La decision es una funcion pura
(`decidir`); la orquestacion con la base se prueba con dobles y, contra Postgres
real con RLS, en `tests/integration/test_multichannel.py`.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.schemas.message import NormalizedMessage
from app.services import phone_unification as pu
from app.services.phone_unification import (
    Decision,
    Ficha,
    Resultado,
    decidir,
    normalizar_telefono,
    telefono_verificado,
)

AHORA = datetime(2026, 9, 1, tzinfo=timezone.utc)
CLIENT_ID = uuid.uuid4()
TELEFONO = "573001112233"
H = frozenset({"hash-con-mas", "hash-sin-mas"})


def _ficha(**campos: Any) -> Ficha:
    """Ficha de contacto con valores neutros que cada test sobreescribe."""
    base: dict[str, Any] = {
        "contact_id": uuid.uuid4(),
        "creado": AHORA,
        "borrado_gdpr": False,
        "hashes_telefono": frozenset(),
        "canales": frozenset(),
    }
    base.update(campos)
    return Ficha(**base)


def _mensaje(canal: str, sender: str = "123", **extra: Any) -> NormalizedMessage:
    """Mensaje entrante normalizado."""
    return NormalizedMessage(
        channel=canal,  # type: ignore[arg-type]
        sender_identifier=sender,
        text="hola",
        timestamp=AHORA,
        external_message_id="x1",
        raw_payload={},
        **extra,
    )


class TestNormalizarTelefono:
    """Ante la duda, `None`: no se unifica."""

    @pytest.mark.parametrize(
        ("valor", "esperado"),
        [
            ("573001112233", "573001112233"),
            ("+573001112233", "573001112233"),
            ("+57 300 111-2233", "573001112233"),
            ("(57) 300.111.2233", "573001112233"),
        ],
    )
    def test_lleva_a_solo_digitos(self, valor: str, esperado: str) -> None:
        assert normalizar_telefono(valor) == esperado

    @pytest.mark.parametrize(
        "valor",
        [
            None,
            "",
            "   ",
            "abc",
            "5730011122a3",
            "1234567",  # 7 digitos: demasiado corto
            "1" * 16,  # demasiado largo para E.164
            "0573001112233",  # un codigo de pais no empieza en 0
            "3001112233x",
        ],
    )
    def test_lo_que_no_parece_un_telefono_es_none(self, valor: str | None) -> None:
        assert normalizar_telefono(valor) is None


class TestTelefonoVerificado:
    """Solo lo que el propio canal garantiza."""

    def test_whatsapp_verifica_al_remitente(self) -> None:
        assert telefono_verificado(_mensaje("whatsapp", "+573001112233")) == "573001112233"

    def test_telegram_usa_el_telefono_verificado_no_el_id_del_chat(self) -> None:
        msg = _mensaje("telegram", "573001112233", verified_phone="+57 310 000 0000")

        assert telefono_verificado(msg) == "573100000000"

    def test_un_id_de_telegram_con_forma_de_telefono_no_cuenta(self) -> None:
        """Un chat id numerico "parece" un telefono pero no lo es."""
        assert telefono_verificado(_mensaje("telegram", "573001112233")) is None

    @pytest.mark.parametrize("canal", ["instagram", "facebook", "email", "webchat"])
    def test_los_demas_canales_nunca_verifican(self, canal: str) -> None:
        """Ni siquiera si un payload trajera `verified_phone`."""
        msg = _mensaje(canal, "573001112233", verified_phone="573001112233")

        assert telefono_verificado(msg) is None

    def test_whatsapp_con_remitente_que_no_es_telefono(self) -> None:
        assert telefono_verificado(_mensaje("whatsapp", "no-es-un-numero")) is None


class TestDecidir:
    """La decision pura sobre dos contactos que comparten un telefono."""

    def test_une_dos_contactos_sin_conflicto(self) -> None:
        wa = _ficha(hashes_telefono=frozenset({"hash-sin-mas"}), canales=frozenset({"whatsapp"}))
        tg = _ficha(canales=frozenset({"telegram"}))

        decision = decidir(tg, wa, H)

        assert decision.resultado is Resultado.FUSIONADO

    def test_sobrevive_el_mas_antiguo(self) -> None:
        viejo = _ficha(creado=AHORA - timedelta(days=30), canales=frozenset({"whatsapp"}))
        nuevo = _ficha(creado=AHORA, canales=frozenset({"telegram"}))

        d1 = decidir(nuevo, viejo, H)
        d2 = decidir(viejo, nuevo, H)

        assert d1.destino == d2.destino == viejo.contact_id
        assert d1.origen == d2.origen == nuevo.contact_id

    def test_el_desempate_no_depende_del_orden_en_que_se_llame(self) -> None:
        """Dos tareas concurrentes deben elegir el mismo superviviente."""
        a = _ficha(canales=frozenset({"whatsapp"}))
        b = _ficha(canales=frozenset({"telegram"}))

        assert decidir(a, b, H).destino == decidir(b, a, H).destino

    def test_un_contacto_borrado_por_gdpr_no_se_une(self) -> None:
        borrado = _ficha(borrado_gdpr=True, canales=frozenset({"whatsapp"}))
        vivo = _ficha(canales=frozenset({"telegram"}))

        for a, b in ((borrado, vivo), (vivo, borrado)):
            decision = decidir(a, b, H)
            assert decision.resultado is Resultado.CONFLICTO
            assert decision.origen is None

    def test_otro_telefono_en_cualquiera_de_los_dos_no_se_une(self) -> None:
        """Dos telefonos distintos: no son la misma persona con certeza."""
        con_otro = _ficha(hashes_telefono=frozenset({"hash-de-otro-numero"}))
        limpio = _ficha(canales=frozenset({"telegram"}))

        for a, b in ((con_otro, limpio), (limpio, con_otro)):
            assert decidir(a, b, H).resultado is Resultado.CONFLICTO

    def test_un_telefono_propio_y_uno_ajeno_juntos_no_se_unen(self) -> None:
        mixto = _ficha(hashes_telefono=frozenset({"hash-sin-mas", "hash-de-otro-numero"}))

        assert decidir(mixto, _ficha(), H).resultado is Resultado.CONFLICTO

    def test_mismo_canal_en_ambos_no_se_une(self) -> None:
        """Dos cuentas de Telegram distintas reclamando el mismo telefono."""
        uno = _ficha(canales=frozenset({"telegram", "whatsapp"}))
        otro = _ficha(canales=frozenset({"telegram"}))

        assert decidir(uno, otro, H).resultado is Resultado.CONFLICTO

    def test_canales_disjuntos_si(self) -> None:
        uno = _ficha(canales=frozenset({"telegram"}))
        otro = _ficha(canales=frozenset({"instagram", "email"}))

        assert decidir(uno, otro, H).resultado is Resultado.FUSIONADO

    def test_el_conflicto_explica_el_motivo(self) -> None:
        decision = decidir(
            _ficha(canales=frozenset({"telegram"})), _ficha(canales=frozenset({"telegram"})), H
        )

        assert decision == Decision(Resultado.CONFLICTO, motivo="mismo canal en ambos contactos")


class FakeSession:
    """Sesion que solo acepta el lock advisory y lo registra."""

    def __init__(self) -> None:
        self.sentencias: list[str] = []
        self.agregados: list[Any] = []

    async def execute(self, stmt: Any, params: Any = None) -> None:
        self.sentencias.append(str(stmt))

    def add(self, obj: Any) -> None:
        self.agregados.append(obj)

    async def flush(self) -> None:
        return None


@pytest.fixture
def escenario(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Sustituye la capa de base de datos de `unificar_por_telefono`.

    Args:
        monkeypatch: Fixture de pytest.

    Returns:
        Espacio de nombres para configurar (`otros`, `fichas`) y observar
        (`merges`, `registrados`, `trazas`).
    """
    e = SimpleNamespace(
        otros={}, fichas={}, merges=[], registrados=[], trazas=[], session=FakeSession()
    )

    async def _otros(session: Any, client_id: Any, actual: Any, hashes: Any) -> dict[Any, Any]:
        session.sentencias.append("leer-titulares")
        return e.otros

    async def _ficha_db(session: Any, client_id: Any, contacto: Any) -> Ficha:
        return e.fichas[contacto.id]

    async def _registrar(session: Any, client_id: Any, contact_id: Any, telefono: str) -> None:
        e.registrados.append((contact_id, telefono))

    async def _traza(session: Any, client_id: Any, sup: Any, absorbido: Any) -> None:
        e.trazas.append((sup.id, absorbido))

    async def _merge(self: Any, source: Any, target: Any, client_id: Any) -> None:
        e.merges.append((source, target, client_id))

    monkeypatch.setattr(pu, "_otros_titulares", _otros)
    monkeypatch.setattr(pu, "_ficha", _ficha_db)
    monkeypatch.setattr(pu, "_registrar", _registrar)
    monkeypatch.setattr(pu, "_dejar_traza", _traza)
    monkeypatch.setattr(pu.ContactUnifier, "merge", _merge)
    return e


def _contacto(**extra: Any) -> Any:
    """Doble de `Contact` con lo minimo que usa la orquestacion."""
    return SimpleNamespace(id=uuid.uuid4(), **extra)


async def _unificar(e: SimpleNamespace, contacto: Any) -> Any:
    return await pu.unificar_por_telefono(e.session, CLIENT_ID, contacto, TELEFONO)


class TestOrquestacion:
    """`unificar_por_telefono` decide con lo que la base le devuelve."""

    async def test_sin_otros_titulares_registra_el_telefono(
        self, escenario: SimpleNamespace
    ) -> None:
        """Telegram prueba el telefono y nadie mas lo tiene: queda para despues."""
        tg = _contacto()
        escenario.fichas[tg.id] = _ficha(contact_id=tg.id, canales=frozenset({"telegram"}))

        res = await _unificar(escenario, tg)

        assert res.resultado is Resultado.REGISTRADO
        assert res.contacto is tg
        assert escenario.registrados == [(tg.id, TELEFONO)]
        assert escenario.merges == []

    async def test_si_el_contacto_ya_conoce_el_telefono_no_hace_nada(
        self, escenario: SimpleNamespace
    ) -> None:
        wa = _contacto()
        hashes = pu._hashes(TELEFONO, CLIENT_ID)
        escenario.fichas[wa.id] = _ficha(contact_id=wa.id, hashes_telefono=frozenset(hashes))

        res = await _unificar(escenario, wa)

        assert res.resultado is Resultado.SIN_CAMBIOS
        assert escenario.registrados == []

    async def test_no_registra_un_segundo_telefono_distinto(
        self, escenario: SimpleNamespace
    ) -> None:
        tg = _contacto()
        escenario.fichas[tg.id] = _ficha(
            contact_id=tg.id, hashes_telefono=frozenset({"otro-numero"})
        )

        res = await _unificar(escenario, tg)

        assert res.resultado is Resultado.CONFLICTO
        assert escenario.registrados == []

    async def test_un_unico_titular_compatible_se_une(self, escenario: SimpleNamespace) -> None:
        tg = _contacto()
        wa = _contacto()
        escenario.otros = {wa.id: wa}
        escenario.fichas[tg.id] = _ficha(
            contact_id=tg.id, creado=AHORA, canales=frozenset({"telegram"})
        )
        escenario.fichas[wa.id] = _ficha(
            contact_id=wa.id,
            creado=AHORA - timedelta(days=1),
            canales=frozenset(),
            hashes_telefono=frozenset(pu._hashes(TELEFONO, CLIENT_ID)),
        )

        res = await _unificar(escenario, tg)

        assert res.resultado is Resultado.FUSIONADO
        # El mas antiguo (WhatsApp) sobrevive; se une dentro del tenant del mensaje.
        assert escenario.merges == [(tg.id, wa.id, CLIENT_ID)]
        assert res.contacto is wa
        assert escenario.trazas == [(wa.id, tg.id)]

    async def test_el_superviviente_es_el_actual_si_es_el_mas_antiguo(
        self, escenario: SimpleNamespace
    ) -> None:
        actual = _contacto()
        otro = _contacto()
        escenario.otros = {otro.id: otro}
        escenario.fichas[actual.id] = _ficha(
            contact_id=actual.id, creado=AHORA - timedelta(days=9), canales=frozenset({"telegram"})
        )
        escenario.fichas[otro.id] = _ficha(
            contact_id=otro.id,
            creado=AHORA,
            canales=frozenset({"instagram"}),
            hashes_telefono=frozenset(pu._hashes(TELEFONO, CLIENT_ID)),
        )

        res = await _unificar(escenario, actual)

        assert res.contacto is actual
        assert escenario.merges == [(otro.id, actual.id, CLIENT_ID)]

    async def test_dos_titulares_es_ambiguo_y_no_toca_nada(
        self, escenario: SimpleNamespace
    ) -> None:
        """Ej.: un duplicado previo `+57…` y `57…`: no se sabe cual es el bueno."""
        tg = _contacto()
        a, b = _contacto(), _contacto()
        escenario.otros = {a.id: a, b.id: b}
        escenario.fichas[tg.id] = _ficha(contact_id=tg.id)

        res = await _unificar(escenario, tg)

        assert res.resultado is Resultado.AMBIGUO
        assert res.contacto is tg
        assert escenario.merges == []
        assert escenario.registrados == []

    async def test_un_conflicto_no_une_ni_registra(self, escenario: SimpleNamespace) -> None:
        tg = _contacto()
        wa = _contacto()
        escenario.otros = {wa.id: wa}
        escenario.fichas[tg.id] = _ficha(contact_id=tg.id, canales=frozenset({"telegram"}))
        escenario.fichas[wa.id] = _ficha(
            contact_id=wa.id,
            canales=frozenset({"telegram"}),  # otra cuenta de Telegram
            hashes_telefono=frozenset(pu._hashes(TELEFONO, CLIENT_ID)),
        )

        res = await _unificar(escenario, tg)

        assert res.resultado is Resultado.CONFLICTO
        assert res.contacto is tg
        assert escenario.merges == []
        assert escenario.registrados == []

    async def test_toma_el_lock_antes_de_leer(self, escenario: SimpleNamespace) -> None:
        """Sin el lock, dos tareas concurrentes podrian fusionarse en sentidos opuestos."""
        tg = _contacto()
        escenario.fichas[tg.id] = _ficha(contact_id=tg.id)

        await _unificar(escenario, tg)

        sentencias = escenario.session.sentencias
        assert sentencias[0].startswith("SELECT pg_advisory_xact_lock")
        assert sentencias[1] == "leer-titulares"


class TestHashes:
    """El indice ciego separa tenants y formatos."""

    def test_cubre_el_numero_con_y_sin_mas(self) -> None:
        """YCloud a veces entrega `+57…` y a veces `57…`."""
        assert len(pu._hashes(TELEFONO, CLIENT_ID)) == 2

    def test_el_mismo_telefono_en_otro_tenant_no_comparte_hash(self) -> None:
        """Es lo que impide cruzar personas entre tenants aunque haya un bug de filtro."""
        otro_tenant = uuid.uuid4()

        assert not pu._hashes(TELEFONO, CLIENT_ID) & pu._hashes(TELEFONO, otro_tenant)
