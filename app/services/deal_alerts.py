"""Alerta por Telegram cuando se gana un deal grande (Sprint 19, Dev B).

El umbral es **por moneda** (`DealAlertsConfig.thresholds`): 5.000 USD y 5.000 COP no se comparan,
y una moneda sin umbral no alerta. El aviso sale por el bot de canal de la plataforma al chat que
el tenant configuro; el bot tiene que estar en ese chat.

Reglas:

- **Despues del commit** y **sin propagar errores**: una alerta que falla no puede deshacer ni
  retrasar el cierre de un deal. Quien llama la lanza cuando la transaccion ya termino.
- **Una vez por deal ganado:** una marca en Redis (`SET NX EX`, 30 dias) evita repetir el aviso si
  el deal se reabre y se vuelve a ganar, o si el cliente reintenta la peticion.
- El texto lleva titulo, importe, moneda y responsable (nombre de pila): el chat es del tenant. El
  titulo es texto libre y se limpia (`limpiar`) y recorta antes de enviarse.
- Nunca el detalle del error de Telegram al log (puede traer el chat): el tipo basta.
"""

import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import tenant_session
from app.models.client import Client
from app.models.deal import DEAL_WON, Deal
from app.models.user import User
from app.schemas.lead_automation import DealAlertsConfig
from app.services.dedup import get_redis
from app.services.lead_qualification import cargar_automatizacion
from app.services.messaging.base import MessageContent
from app.services.messaging.telegram import TelegramProvider
from app.services.prompt_safety import limpiar

logger = logging.getLogger(__name__)

#: Cuanto se recuerda que un deal ya alerto.
VIDA_MARCA_SEGUNDOS = 30 * 24 * 3600
MAX_CHARS_TITULO = 120


def supera_umbral(config: DealAlertsConfig, valor: Decimal, moneda: str) -> bool:
    """Si un deal de ese valor y moneda debe alertar.

    Args:
        config: Configuracion de alertas del tenant.
        valor: Valor del deal.
        moneda: Moneda ISO del deal.

    Returns:
        `True` si la alerta esta activa, hay umbral para esa moneda y el valor lo alcanza.
    """
    if not config.enabled or not config.telegram_chat_id:
        return False
    umbral = config.thresholds.get(moneda)
    return umbral is not None and valor >= umbral


def formatear_importe(valor: Decimal, moneda: str) -> str:
    """`12,500.00 USD`: separador de miles y dos decimales, sin depender de la configuracion.

    Args:
        valor: Importe.
        moneda: Moneda ISO.

    Returns:
        El texto.
    """
    return f"{valor:,.2f} {moneda}"


def armar_texto(titulo: str, valor: Decimal, moneda: str, responsable: str | None) -> str:
    """El mensaje de la alerta.

    Args:
        titulo: Titulo del deal (texto libre: se limpia).
        valor: Importe.
        moneda: Moneda ISO.
        responsable: Nombre de pila de quien lo gano, si hay.

    Returns:
        El texto, en una pieza.
    """
    limpio = limpiar(titulo, MAX_CHARS_TITULO, una_linea=True) or "(sin titulo)"
    quien = f"\nResponsable: {limpiar(responsable, 80, una_linea=True)}" if responsable else ""
    return f"Deal ganado: {limpio}\nImporte: {formatear_importe(valor, moneda)}{quien}"


async def _marcar_primera_vez(client_id: UUID, deal_id: UUID) -> bool:
    """`True` si este deal aun no habia alertado (o si Redis no responde: mejor repetir)."""
    try:
        return bool(
            await get_redis().set(
                f"deal-alert:{client_id}:{deal_id}", "1", nx=True, ex=VIDA_MARCA_SEGUNDOS
            )
        )
    except Exception:
        logger.warning("Redis no responde: la alerta del deal %s sigue sin marca", deal_id)
        return True


async def _soltar_marca(client_id: UUID, deal_id: UUID) -> None:
    """Quita la marca de "ya alerto" (si falla, caduca sola)."""
    try:
        await get_redis().delete(f"deal-alert:{client_id}:{deal_id}")
    except Exception:
        logger.warning("No se pudo soltar la marca de la alerta del deal %s", deal_id)


async def alertar_deal_ganado(client_id: UUID, deal_id: UUID) -> bool:
    """Avisa por Telegram de un deal recien ganado, si supera el umbral de su moneda.

    Lee el deal y la configuracion en su propia sesion (ya con el commit hecho) y no propaga
    errores.

    Args:
        client_id: Tenant.
        deal_id: Deal que acaba de pasar a `closed_won`.

    Returns:
        `True` si se envio el aviso.
    """
    try:
        async with tenant_session(client_id) as session:
            deal = (
                await session.execute(
                    select(Deal).where(Deal.id == deal_id, Deal.client_id == client_id)
                )
            ).scalar_one_or_none()
            if deal is None or deal.stage != DEAL_WON:
                return False
            bruto = (
                await session.execute(
                    select(Client.lead_automation_config).where(Client.id == client_id)
                )
            ).scalar_one_or_none()
            config = cargar_automatizacion(bruto, client_id=client_id).deal_alerts
            if not supera_umbral(config, deal.value, deal.currency):
                return False
            responsable = None
            if deal.assigned_user_id is not None:
                responsable = (
                    await session.execute(
                        select(User.first_name).where(
                            User.id == deal.assigned_user_id, User.client_id == client_id
                        )
                    )
                ).scalar_one_or_none()
            texto = armar_texto(deal.title, deal.value, deal.currency, responsable)
            chat = config.telegram_chat_id
        token = get_settings().TELEGRAM_CHANNEL_BOT_TOKEN
        if not token or chat is None:
            logger.warning(
                "Alerta de deal sin bot de Telegram configurado (client_id=%s)", client_id
            )
            return False
        if not await _marcar_primera_vez(client_id, deal_id):
            return False
        try:
            await TelegramProvider().send_message(
                to=chat, content=MessageContent(text=texto), channel_config={"bot_token": token}
            )
        except Exception:
            await _soltar_marca(client_id, deal_id)  # un aviso que no salio puede reintentarse
            raise
    except Exception as exc:
        logger.warning(
            "No se pudo enviar la alerta del deal %s (client_id=%s): %s",
            deal_id,
            client_id,
            type(exc).__name__,
        )
        return False
    return True
