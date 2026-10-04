"""Schemas de la API de feature flags (Sprint 14, ADR-076)."""

from pydantic import BaseModel, StrictBool, StrictInt


class FeatureFlagUpdate(BaseModel):
    """Cuerpo de `PUT /api/v1/admin/feature-flags/{flag}`.

    Attributes:
        value: `true`/`false`, o un porcentaje entero de 0 a 100 de rollout
            gradual. Las flags de agente solo admiten el booleano.
    """

    value: StrictBool | StrictInt


class FeatureFlagItem(BaseModel):
    """Estado de una flag en un tenant.

    Attributes:
        flag: Nombre de la flag.
        value: Valor guardado; `None` si el tenant no la ha configurado (en un
            agente, eso significa que manda `enabled_agents`).
        enforced: Si alguna feature la lee hoy. Una flag sin aplicar se guarda,
            pero no cambia el comportamiento de la plataforma.
    """

    flag: str
    value: bool | int | None
    enforced: bool


class FeatureFlagsResponse(BaseModel):
    """Todas las flags conocidas con su valor en el tenant.

    Attributes:
        flags: Una entrada por flag de `KNOWN_FLAGS`, en orden estable.
    """

    flags: list[FeatureFlagItem]
