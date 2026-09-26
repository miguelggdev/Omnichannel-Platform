"""Schemas de la configuracion del agente de marketing (BUG-045, ADR-070).

Vive en `agent_configs.config.marketing` de la configuracion activa del tenant:

- `operator_contact_ids`: contactos que pueden operar el agente de marketing
  por el chat (sin lista, nadie; ver `services/campaigns.es_operador_de_marketing`).
- `approved_templates`: plantillas de WhatsApp aprobadas por Meta (sin lista,
  no sale ninguna campana de WhatsApp).
"""

from uuid import UUID

from pydantic import BaseModel, Field, field_validator

#: Topes para que la configuracion no crezca sin control dentro de un JSONB.
MAX_OPERADORES = 50
MAX_PLANTILLAS = 200
MAX_LARGO_PLANTILLA = 4096


class MarketingSettings(BaseModel):
    """Configuracion vigente del agente de marketing.

    Attributes:
        operator_contact_ids: Contactos autorizados a operar el agente.
        approved_templates: Plantillas de WhatsApp aprobadas.
    """

    operator_contact_ids: list[UUID]
    approved_templates: list[str]


class MarketingSettingsUpdate(BaseModel):
    """Cambio parcial: cada campo presente **reemplaza** su lista completa.

    Attributes:
        operator_contact_ids: Nueva lista de operadores, opcional.
        approved_templates: Nueva lista de plantillas aprobadas, opcional.
    """

    operator_contact_ids: list[UUID] | None = Field(default=None, max_length=MAX_OPERADORES)
    approved_templates: list[str] | None = Field(default=None, max_length=MAX_PLANTILLAS)

    @field_validator("operator_contact_ids")
    @classmethod
    def _sin_repetidos(cls, valor: list[UUID] | None) -> list[UUID] | None:
        """Quita duplicados conservando el orden."""
        return None if valor is None else list(dict.fromkeys(valor))

    @field_validator("approved_templates")
    @classmethod
    def _plantillas_limpias(cls, valor: list[str] | None) -> list[str] | None:
        """Rechaza plantillas vacias o enormes y quita duplicados.

        Se compara con el texto exacto de la campana
        (`services/campaigns.plantilla_aprobada`), asi que no se recorta el
        contenido: solo se rechaza lo que queda vacio.

        Args:
            valor: Plantillas recibidas.

        Returns:
            Las plantillas, sin duplicados.

        Raises:
            ValueError: Si alguna esta vacia o supera el largo maximo.
        """
        if valor is None:
            return None
        for plantilla in valor:
            if not plantilla.strip():
                raise ValueError("Una plantilla aprobada no puede estar vacia")
            if len(plantilla) > MAX_LARGO_PLANTILLA:
                raise ValueError(
                    f"Una plantilla aprobada no puede superar {MAX_LARGO_PLANTILLA} caracteres"
                )
        return list(dict.fromkeys(valor))
