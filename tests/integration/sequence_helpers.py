"""Ayudas de los tests de integracion de las secuencias de follow-up (Sprint 18, Dev B)."""

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import text

from app.core.database import tenant_session
from app.schemas.lead_sequence import (
    MessageStep,
    SequenceDefinition,
    TaskStep,
    TriggerConditions,
    WaitStep,
)
from app.services import lead_outreach
from app.services.lead_sequences import crear_secuencia, inscribir
from app.services.messaging.base import MessageContent, TemplateMessage

#: Un miercoles a las 10:00 en Bogota: dentro del horario por defecto (L-V 08-18).
AHORA = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


async def sql(client_id: uuid.UUID, consulta: str, **params: Any) -> list[Any]:
    """Ejecuta SQL crudo dentro del tenant y devuelve las filas."""
    async with tenant_session(client_id) as s:
        resultado = await s.execute(text(consulta), params)
        return list(resultado.all()) if resultado.returns_rows else []


async def uno(client_id: uuid.UUID, consulta: str, **params: Any) -> Any:
    """La primera fila de una consulta."""
    return (await sql(client_id, consulta, **params))[0]


async def usuario(client_id: uuid.UUID, rol: str = "agent") -> uuid.UUID:
    """Inserta un usuario activo del tenant."""
    uid = uuid.uuid4()
    await sql(
        client_id,
        "INSERT INTO users (id, client_id, email, password_hash, first_name, last_name, role, "
        "is_active) VALUES (:i, :c, :e, 'x', 'Marta', 'P', CAST(:r AS user_role), true)",
        i=str(uid),
        c=str(client_id),
        e=f"u-{uid.hex[:12]}@example.com",
        r=rol,
    )
    return uid


def definicion(*pasos: Any, nombre: str | None = None, **campos: Any) -> SequenceDefinition:
    """Una secuencia valida con los pasos dados."""
    return SequenceDefinition(
        name=nombre or f"Secuencia {uuid.uuid4().hex[:8]}", steps=list(pasos), **campos
    )


def mensaje(cuerpo: str = "Hola {{first_name}}, somos {{business_name}}", **campos: Any) -> Any:
    """Un paso `message` de plantilla."""
    return MessageStep(body=cuerpo, **campos)


def espera(dias: int = 1, **campos: Any) -> WaitStep:
    """Un paso `wait` en dias."""
    return WaitStep(amount=dias, unit="days", **campos)


def tarea(titulo: str = "Llamar al lead", **campos: Any) -> TaskStep:
    """Un paso `task`."""
    return TaskStep(title=titulo, **campos)


def disparador(*eventos: str, **campos: Any) -> TriggerConditions:
    """Condiciones de disparo con esos eventos."""
    return TriggerConditions(events=list(eventos), **campos)


async def crear_sec(
    client_id: uuid.UUID, *pasos: Any, activa: bool = True, **campos: Any
) -> uuid.UUID:
    """Guarda una secuencia por el servicio y devuelve su id."""
    async with tenant_session(client_id) as s:
        secuencia = await crear_secuencia(s, client_id, definicion(*pasos, **campos))
        secuencia.is_active = activa
        return secuencia.id


async def inscribir_lead(
    client_id: uuid.UUID,
    lead_id: uuid.UUID,
    sequence_id: uuid.UUID,
    *,
    toca_en: datetime = AHORA,
) -> uuid.UUID:
    """Inscribe un lead con el primer paso vencido en `toca_en`; devuelve el id de la inscripcion."""
    from sqlalchemy import select

    from app.models.lead import Lead
    from app.models.lead_sequence import LeadSequence

    async with tenant_session(client_id) as s:
        lead = (await s.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
        secuencia = (
            await s.execute(select(LeadSequence).where(LeadSequence.id == sequence_id))
        ).scalar_one()
        inscripcion = await inscribir(s, lead, secuencia, ahora=toca_en, next_step_at=toca_en)
        return inscripcion.id


class ProveedorFalso:
    """Proveedor de mensajeria que anota lo enviado y falla a pedido.

    Attributes:
        enviados: `(canal, destino, texto, metadata)` de cada mensaje de texto.
        plantillas: `(canal, destino, nombre)` de cada plantilla.
        falla: Excepcion a lanzar en el siguiente envio (se queda hasta que se quite).
    """

    def __init__(self) -> None:
        """Crea el proveedor sin fallos."""
        self.enviados: list[tuple[str, str, str, dict[str, Any] | None]] = []
        self.plantillas: list[tuple[str, str, str]] = []
        self.falla: Exception | None = None
        self.canal = ""

    async def send_message(self, to: str, content: MessageContent, channel_config: Any) -> str:
        """Anota el envio, o falla."""
        if self.falla is not None:
            raise self.falla
        self.enviados.append((self.canal, to, content.text or "", content.metadata))
        return f"ext-{len(self.enviados)}"

    async def send_template(self, to: str, template: TemplateMessage, channel_config: Any) -> str:
        """Anota la plantilla, o falla."""
        if self.falla is not None:
            raise self.falla
        self.plantillas.append((self.canal, to, template.template_name))
        return f"tpl-{len(self.plantillas)}"


def instalar_proveedor_falso(monkeypatch: pytest.MonkeyPatch) -> ProveedorFalso:
    """Sustituye el canal real por un `ProveedorFalso` (todos los canales, sin credenciales)."""
    falso = ProveedorFalso()

    def config(canal: str) -> tuple[str, dict[str, Any]]:
        falso.canal = canal
        return "falso", {}

    monkeypatch.setattr(lead_outreach, "get_channel_config", config)
    monkeypatch.setattr(lead_outreach, "get_messaging_provider", lambda nombre, cfg=None: falso)
    return falso
