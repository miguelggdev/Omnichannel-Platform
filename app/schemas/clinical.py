"""Schemas del modulo clinico (Sprint 13, Dev B).

- `ClinicalSettings`: quien puede operar el agente clinico por el chat
  (`agent_configs.config.clinical.professional_contact_ids`, ver
  `services/clinical.es_profesional_clinico`).
- `PatientRef`: el paciente se identifica por tipo y numero de documento en el
  **cuerpo** de un POST, nunca en la URL: los paths quedan en los logs de
  Traefik y de acceso, y el documento es dato personal.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

#: Tope para que la configuracion no crezca sin control dentro de un JSONB.
MAX_PROFESIONALES = 50


class ClinicalSettings(BaseModel):
    """Configuracion vigente del agente clinico.

    Attributes:
        professional_contact_ids: Contactos autorizados a operar el agente.
    """

    professional_contact_ids: list[UUID]


class ClinicalSettingsUpdate(BaseModel):
    """Cambio de la configuracion: el campo presente **reemplaza** la lista.

    Attributes:
        professional_contact_ids: Nueva lista de profesionales, opcional.
    """

    professional_contact_ids: list[UUID] | None = Field(default=None, max_length=MAX_PROFESIONALES)

    @field_validator("professional_contact_ids")
    @classmethod
    def _sin_repetidos(cls, valor: list[UUID] | None) -> list[UUID] | None:
        """Quita duplicados conservando el orden."""
        return None if valor is None else list(dict.fromkeys(valor))


class PatientRef(BaseModel):
    """Documento que identifica a un paciente.

    Attributes:
        document_type: CC, TI, CE, PA, RC, MS, AS o NU.
        document_number: Numero de documento.
    """

    document_type: str = Field(min_length=2, max_length=5)
    document_number: str = Field(min_length=4, max_length=30)


class ConsentCreate(PatientRef):
    """Autorizacion del titular para tratar sus datos de salud.

    Attributes:
        consent_type: Como se otorgo.
        contact_id: El titular, si tambien es un contacto del tenant.
    """

    consent_type: Literal["verbal", "digital", "written"]
    contact_id: UUID | None = None
