"""Schemas del CRUD de templates de tenant (Sprint 10, Dev B).

Lo que expone la API y lo que no
---------------------------------
El snapshot completo (`tenant_templates.config`) lleva los prompts de sistema,
la configuracion de agentes y las rutas de los documentos del tenant origen.
Un `admin` puede listar los templates publicos de otros tenants para elegir
uno, pero no leer su snapshot: `TemplateResponse` trae solo un resumen
(`TemplateSummary`, cuantos de cada cosa). El snapshot entero sale unicamente
en `TemplateDetailResponse`, que es solo para `super_admin`.

`InstantiationResponse.progress` puede traer el password temporal del admin
del tenant nuevo (ver ADR-064): ese endpoint tambien es solo `super_admin`.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

#: Campos de un `agent_config` que un override puede sobrescribir al
#: instanciar. `TenantCloner.instantiate()` hace `AgentConfig(**campos)` con el
#: resultado, asi que un override sin filtrar podia fijar `id`, `client_id` o
#: cualquier columna — o tumbar la task con un `TypeError` minutos despues de
#: devolver 202. Se valida aca, en el request, con un 422 inmediato.
CAMPOS_OVERRIDE_PERMITIDOS: frozenset[str] = frozenset(
    {
        "system_prompt",
        "welcome_message",
        "handoff_message",
        "model",
        "temperature",
        "max_tokens",
        "training_mode",
        "similarity_threshold",
        "config",
    }
)


class TemplateCreate(BaseModel):
    """Alta de un template a partir de un tenant existente.

    Attributes:
        name: Nombre para mostrar en el listado.
        description: Descripcion libre.
        source_client_id: Tenant del que se toma el snapshot.
        is_public: Si los `admin` de otros tenants lo pueden ver en el listado.
    """

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    source_client_id: UUID
    is_public: bool = False


class TemplateInstantiate(BaseModel):
    """Pedido de crear un tenant nuevo desde un template.

    Attributes:
        tenant_name: Nombre visible del tenant nuevo.
        admin_email: Email del usuario admin que se crea para el tenant.
        overrides: `{"agent_configs": {<nombre>: {<campo>: <valor>}}}`, opcional.
    """

    tenant_name: str = Field(min_length=1, max_length=200)
    admin_email: EmailStr
    overrides: dict[str, Any] | None = None

    @field_validator("overrides")
    @classmethod
    def _overrides_acotados(cls, valor: dict[str, Any] | None) -> dict[str, Any] | None:
        """Rechaza overrides que no sean de campos permitidos de `agent_configs`.

        Args:
            valor: Overrides recibidos.

        Returns:
            Los mismos overrides, si son validos.

        Raises:
            ValueError: Si traen claves fuera de `agent_configs` o campos que no
                estan en `CAMPOS_OVERRIDE_PERMITIDOS`.
        """
        if valor is None:
            return None
        desconocidas = set(valor) - {"agent_configs"}
        if desconocidas:
            raise ValueError(f"Overrides no soportados: {sorted(desconocidas)}")
        por_agente = valor.get("agent_configs", {})
        if not isinstance(por_agente, dict):
            raise ValueError("`overrides.agent_configs` tiene que ser un objeto por nombre")
        for nombre, campos in por_agente.items():
            if not isinstance(campos, dict):
                raise ValueError(f"El override de '{nombre}' tiene que ser un objeto")
            prohibidos = set(campos) - CAMPOS_OVERRIDE_PERMITIDOS
            if prohibidos:
                raise ValueError(
                    f"Campos no sobrescribibles en '{nombre}': {sorted(prohibidos)}. "
                    f"Permitidos: {sorted(CAMPOS_OVERRIDE_PERMITIDOS)}"
                )
        return valor


class TemplateSummary(BaseModel):
    """Cuantos elementos de cada tipo trae el snapshot, sin su contenido.

    Attributes:
        agent_configs: Configuraciones de agente.
        quick_replies: Respuestas rapidas.
        tags: Etiquetas.
        documents: Documentos (se clonan y se reindexan).
    """

    agent_configs: int
    quick_replies: int
    tags: int
    documents: int

    @classmethod
    def del_snapshot(cls, config: dict[str, Any]) -> "TemplateSummary":
        """Cuenta los elementos de un snapshot.

        Args:
            config: `tenant_templates.config`.

        Returns:
            El resumen.
        """

        def _n(clave: str) -> int:
            valor = config.get(clave)
            return len(valor) if isinstance(valor, list) else 0

        return cls(
            agent_configs=_n("agent_configs"),
            quick_replies=_n("quick_replies"),
            tags=_n("tags"),
            documents=_n("documents"),
        )


class TemplateResponse(BaseModel):
    """Template en el listado: metadatos y resumen, nunca el snapshot.

    Attributes:
        id: UUID del template.
        name: Nombre.
        description: Descripcion.
        source_client_id: Tenant origen.
        is_public: Si es visible para los admin de otros tenants.
        version: Version del formato del snapshot.
        summary: Cuantos elementos trae.
        created_at: Fecha de creacion.
    """

    id: UUID
    name: str
    description: str | None
    source_client_id: UUID
    is_public: bool
    version: str
    summary: TemplateSummary
    created_at: datetime


class TemplateDetailResponse(TemplateResponse):
    """Template con el snapshot completo. Solo `super_admin`.

    Attributes:
        created_by: Usuario que lo creo.
        config: Snapshot completo.
    """

    created_by: UUID
    config: dict[str, Any]


class TemplateListResponse(BaseModel):
    """Pagina de templates.

    Attributes:
        items: Templates de la pagina.
        total: Total que cumple el filtro de visibilidad.
        page: Pagina actual, desde 1.
        page_size: Tamano de pagina.
    """

    items: list[TemplateResponse]
    total: int
    page: int
    page_size: int


class InstantiationAccepted(BaseModel):
    """Acuse de una instanciacion encolada.

    Attributes:
        instantiation_id: Id para consultar el avance.
        status: Estado inicial (`pending`).
    """

    instantiation_id: UUID
    status: str


class InstantiationResponse(BaseModel):
    """Estado de una instanciacion. Solo `super_admin`.

    Attributes:
        id: UUID de la instanciacion.
        template_id: Template del que se clona.
        target_client_id: Tenant creado, cuando ya existe.
        status: `pending`, `processing`, `completed` o `failed`.
        progress: Avance y, mientras no haya flujo de invitacion, el password
            temporal del admin del tenant nuevo (ADR-064).
        error_message: Motivo del fallo, si fallo.
        started_at: Cuando empezo.
        completed_at: Cuando termino.
        created_at: Cuando se encolo.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    template_id: UUID
    target_client_id: UUID | None
    status: str
    progress: dict[str, Any]
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
