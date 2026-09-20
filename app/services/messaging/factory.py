"""Factory de MessagingProvider — resuelve el proveedor por el nombre de la URL.

El endpoint recibe `POST /api/v1/webhooks/{provider}/{channel}`; `provider` es
exactamente la clave que se busca aqui (`ycloud`, `meta`, ...).

Contrato: `specs/sprint-04-webhooks.md` §5.
"""

from typing import Any, cast

from app.services.messaging.base import MessagingProvider
from app.services.messaging.email_provider import EmailProvider
from app.services.messaging.meta import MetaProvider
from app.services.messaging.telegram import TelegramProvider
from app.services.messaging.webchat import WebchatProvider
from app.services.messaging.ycloud import YCloudProvider

_PROVIDERS: dict[str, type[MessagingProvider]] = {
    "ycloud": YCloudProvider,
    "meta": MetaProvider,  # Instagram DM + Facebook Messenger, diferenciados por config
    "telegram": TelegramProvider,  # Sprint 9
    "email": EmailProvider,  # Sprint 9
    # Webchat no entra por el endpoint HTTP (su `validate_signature` siempre
    # rechaza); se registra para que el nodo `respond` pueda enviar por el.
    "webchat": WebchatProvider,  # Sprint 9
}


def get_messaging_provider(
    provider_name: str, provider_config: dict[str, Any] | None = None
) -> MessagingProvider:
    """Instancia el `MessagingProvider` correspondiente al nombre recibido.

    `YCloudProvider` no necesita config para construirse (la credencial de
    envio llega por llamada, no por el constructor). `MetaProvider` si la
    necesita para saber su sub-canal (`instagram` o `facebook`).

    Args:
        provider_name: Nombre del proveedor tomado de la URL del webhook.
        provider_config: Config extra del proveedor. Para `meta`, debe traer
            al menos `channel`.

    Returns:
        Instancia del provider solicitado.

    Raises:
        ValueError: Si `provider_name` no esta registrado.
    """
    provider_class = _PROVIDERS.get(provider_name)
    if provider_class is None:
        raise ValueError(
            f"Provider '{provider_name}' no registrado. Disponibles: {list(_PROVIDERS.keys())}"
        )

    if provider_name == "meta":
        # MetaProvider es el unico registrado con constructor propio (necesita
        # el sub-canal). _PROVIDERS lo tipa como el ABC generico para que
        # test_todos_los_providers_implementan_el_abc pueda iterarlo con
        # issubclass(); el cast solo informa a mypy de la firma real aqui.
        return cast("type[MetaProvider]", provider_class)(provider_config or {})

    return provider_class()
