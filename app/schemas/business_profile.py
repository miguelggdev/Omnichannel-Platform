"""Schemas del perfil del negocio (Sprint 15, fase 2)."""

import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

DIAS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_HORA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_TELEFONO = re.compile(r"^\+?[0-9 ()\-]{6,25}$")
_HANDLE = re.compile(r"^[A-Za-z0-9._]{1,50}$")


class DaySchedule(BaseModel):
    """Horario de un dia: abierto o cerrado y, si abre, de que hora a que hora (HH:MM)."""

    is_open: bool = True
    open_time: str | None = "08:00"
    close_time: str | None = "18:00"

    @field_validator("open_time", "close_time")
    @classmethod
    def _hora_valida(cls, valor: str | None) -> str | None:
        """Una hora HH:MM de 00:00 a 23:59."""
        if valor is not None and not _HORA.match(valor):
            raise ValueError("La hora debe ser HH:MM, de 00:00 a 23:59")
        return valor

    @model_validator(mode="after")
    def _abre_antes_de_cerrar(self) -> "DaySchedule":
        """Un dia abierto necesita las dos horas y la de cierre es posterior a la de apertura."""
        if not self.is_open:
            return self
        if self.open_time is None or self.close_time is None:
            raise ValueError("Un dia abierto necesita hora de apertura y de cierre")
        if self.close_time <= self.open_time:
            raise ValueError("La hora de cierre debe ser posterior a la de apertura")
        return self


def _horario_por_defecto() -> dict[str, DaySchedule]:
    """Lunes a viernes de 08:00 a 18:00; fin de semana cerrado."""
    return {dia: DaySchedule(is_open=dia not in ("saturday", "sunday")) for dia in DIAS}


class OperatingHours(BaseModel):
    """Horario semanal. Cada dia es opcional en una actualizacion; los demas no cambian."""

    monday: DaySchedule | None = None
    tuesday: DaySchedule | None = None
    wednesday: DaySchedule | None = None
    thursday: DaySchedule | None = None
    friday: DaySchedule | None = None
    saturday: DaySchedule | None = None
    sunday: DaySchedule | None = None


class SocialMedia(BaseModel):
    """Enlaces del negocio. Los handles van sin `@`."""

    facebook: AnyHttpUrl | None = None
    instagram: str | None = None
    twitter: str | None = None
    whatsapp: str | None = None

    @field_validator("instagram", "twitter", mode="before")
    @classmethod
    def _handle(cls, valor: Any) -> Any:
        """Quita el `@` inicial y valida que sea un handle."""
        if valor in (None, ""):
            return None
        limpio = str(valor).strip().lstrip("@")
        if not _HANDLE.match(limpio):
            raise ValueError("Handle no valido: letras, numeros, punto y guion bajo")
        return limpio

    @field_validator("whatsapp", mode="before")
    @classmethod
    def _whatsapp(cls, valor: Any) -> Any:
        """Un numero con codigo de pais."""
        if valor in (None, ""):
            return None
        if not _TELEFONO.match(str(valor).strip()):
            raise ValueError("Numero de WhatsApp no valido")
        return str(valor).strip()


class BusinessProfileUpdate(BaseModel):
    """Cuerpo de `PUT /api/v1/admin/business-profile`: solo se cambia lo que se envia.

    Enviar un campo como `null` lo borra; omitirlo lo deja como esta. El nombre del negocio
    no se puede borrar.
    """

    business_name: str | None = Field(default=None, min_length=1, max_length=255)
    business_type: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    phone: str | None = None
    email: EmailStr | None = None
    website: AnyHttpUrl | None = None
    address: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=100)
    country: str | None = None
    timezone: str | None = None
    social_media: SocialMedia | None = None
    operating_hours: OperatingHours | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    logo_url: AnyHttpUrl | None = None
    welcome_message: str | None = Field(default=None, max_length=1000)
    handoff_message: str | None = Field(default=None, max_length=1000)

    @field_validator("phone", mode="before")
    @classmethod
    def _telefono(cls, valor: Any) -> Any:
        if valor in (None, ""):
            return valor
        if not _TELEFONO.match(str(valor).strip()):
            raise ValueError("Telefono no valido")
        return str(valor).strip()

    @field_validator("country", mode="before")
    @classmethod
    def _pais(cls, valor: Any) -> Any:
        """Codigo ISO de dos letras, en mayusculas."""
        if valor in (None, ""):
            return valor
        if not re.fullmatch(r"[A-Za-z]{2}", str(valor).strip()):
            raise ValueError("Pais: codigo ISO de dos letras (ej. CO)")
        return str(valor).strip().upper()

    @field_validator("timezone")
    @classmethod
    def _zona(cls, valor: str | None) -> str | None:
        """Una zona horaria IANA que el servidor conozca."""
        if valor in (None, ""):
            return valor
        try:
            ZoneInfo(valor)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Zona horaria IANA desconocida (ej. America/Bogota)") from exc
        return valor

    @field_validator("primary_color", "secondary_color")
    @classmethod
    def _color(cls, valor: str | None) -> str | None:
        """Hexadecimal de seis cifras, ej. #1D4ED8."""
        if valor in (None, ""):
            return valor
        if not _COLOR.match(valor):
            raise ValueError("Color hexadecimal de seis cifras (ej. #1D4ED8)")
        return valor.lower()

    @model_validator(mode="after")
    def _al_menos_un_campo(self) -> "BusinessProfileUpdate":
        """Una peticion sin ningun cambio es un error del cliente."""
        if not self.model_fields_set:
            raise ValueError("Indica al menos un campo a cambiar")
        if "business_name" in self.model_fields_set and self.business_name is None:
            raise ValueError("El nombre del negocio no se puede borrar")
        return self


class BusinessProfile(BaseModel):
    """El perfil completo tal como se devuelve; lo no configurado es `None`."""

    business_name: str
    business_type: str | None = None
    description: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    address: str | None = None
    city: str | None = None
    country: str | None = None
    timezone: str | None = None
    social_media: dict[str, str | None] = Field(default_factory=dict)
    operating_hours: dict[str, DaySchedule] = Field(default_factory=_horario_por_defecto)
    primary_color: str | None = None
    secondary_color: str | None = None
    logo_url: str | None = None
    #: Del agente activo; `None` si el tenant no tiene agente (no se pueden guardar).
    welcome_message: str | None = None
    handoff_message: str | None = None
    has_agent: bool = True
