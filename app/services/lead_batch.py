"""Alta masiva de leads con deduplicacion: la usan la importacion de archivos y el webhook de
Phantombuster (Sprint 16, ADR-084).

Recibe filas **ya validadas** (`FilaImportada`) y las inserta en la sesion del tenant:

1. Calcula las claves de deduplicacion de cada fila (indice ciego del email, del telefono y URL
   canonica de LinkedIn: no se descifra nada).
2. Descarta las que ya existen entre los leads no borrados del tenant, y las repetidas dentro del
   propio lote (la primera gana). Nunca actualiza un lead existente.
3. Inserta el resto de una vez; si otra alta se cuela entre la comprobacion y el insert (el indice
   unico lo detiene) repite **fila a fila con SAVEPOINT** para no perder las buenas.
4. Escribe una actividad por lead creado, con el lote como metadata (sin datos personales).

Con `dry_run` hace los pasos 1 y 2 y no escribe nada.
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import blind_index
from app.models.lead import Lead, canonicalizar_linkedin, normalizar_telefono_de_lead
from app.services.lead_activity import registrar_actividad
from app.services.lead_import import FilaImportada

MOTIVO_EXISTENTE = "ya existe un lead con ese email, telefono o LinkedIn"
MOTIVO_REPETIDA = "repetida en el archivo"
_LOTE_IN = 1000


@dataclass
class ResultadoLote:
    """Resultado de un alta masiva.

    Attributes:
        creadas: Leads creados (con `dry_run`, los que se habrian creado).
        duplicadas: Filas omitidas por duplicado.
        rechazos: `(numero_de_fila, motivo)` de cada duplicada, en orden de fila.
        batch_id: Identificador del lote, o `None` en un `dry_run`.
    """

    creadas: int = 0
    duplicadas: int = 0
    rechazos: list[tuple[int, str]] = field(default_factory=list)
    batch_id: str | None = None


Claves = tuple[str | None, str | None, str | None]


def claves_de_fila(client_id: UUID, datos: dict[str, Any]) -> Claves:
    """`(email_hash, phone_hash, linkedin_canonica)` de una fila.

    Args:
        client_id: Tenant (entra en los indices ciegos).
        datos: Campos de la fila.

    Returns:
        Las tres claves; `None` donde la fila no trae ese dato.
    """
    email = datos.get("email")
    digitos = normalizar_telefono_de_lead(datos.get("phone"))
    return (
        blind_index(email, client_id) if email else None,
        blind_index(digitos, client_id) if digitos else None,
        canonicalizar_linkedin(datos.get("linkedin_url")),
    )


async def _existentes(
    session: AsyncSession, client_id: UUID, claves: list[Claves]
) -> tuple[set[str], set[str], set[str]]:
    """Cuales de esas claves ya tiene un lead no borrado del tenant (por lotes)."""
    encontrados: tuple[set[str], set[str], set[str]] = (set(), set(), set())
    columnas = (Lead.email_hash, Lead.phone_hash, Lead.linkedin_url)
    for posicion, columna in enumerate(columnas):
        valores = sorted({c[posicion] for c in claves if c[posicion]})  # type: ignore[type-var]
        for i in range(0, len(valores), _LOTE_IN):
            filas = await session.execute(
                select(columna).where(
                    Lead.client_id == client_id,
                    Lead.deleted_at.is_(None),
                    columna.in_(valores[i : i + _LOTE_IN]),
                )
            )
            encontrados[posicion].update(f for f in filas.scalars() if f)
    return encontrados


async def insertar_lote(
    session: AsyncSession,
    client_id: UUID,
    filas: list[FilaImportada],
    *,
    etapa_id: UUID | None,
    source_id: UUID | None,
    assigned_user_id: UUID | None,
    user_id: UUID | None,
    tipo_actividad: str,
    clave_lote: str,
    ahora: Any,
    extra_enrichment: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> ResultadoLote:
    """Deduplica e inserta las filas validadas de un lote.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        filas: Filas ya validadas (sin errores).
        etapa_id: Etapa inicial de todos los leads.
        source_id: Fuente de todos los leads.
        assigned_user_id: Usuario asignado a todos.
        user_id: Quien lanza el lote (`None` si es una automatizacion).
        tipo_actividad: Tipo de actividad a registrar por lead (`imported`, `captured`...).
        clave_lote: Clave de `enrichment_data` bajo la que se guarda el lote (`import`,
            `capture`).
        ahora: Instante comun del lote (`datetime`).
        extra_enrichment: Datos que se anaden al lote en `enrichment_data[clave_lote]`.
        dry_run: Solo calcular, sin escribir.

    Returns:
        Cuantas se crearon, cuantas eran duplicadas y por que.
    """
    resultado = ResultadoLote()
    claves = {f.numero: claves_de_fila(client_id, f.datos) for f in filas}
    ya = await _existentes(session, client_id, list(claves.values()))
    vistos: tuple[set[str], set[str], set[str]] = (set(), set(), set())
    nuevas: list[FilaImportada] = []
    for f in filas:
        k = claves[f.numero]
        if any(k[i] and k[i] in ya[i] for i in range(3)):
            motivo: str | None = MOTIVO_EXISTENTE
        elif any(k[i] and k[i] in vistos[i] for i in range(3)):
            motivo = MOTIVO_REPETIDA
        else:
            motivo = None
        if motivo:
            resultado.duplicadas += 1
            resultado.rechazos.append((f.numero, motivo))
            continue
        for i in range(3):
            if k[i]:
                vistos[i].add(k[i])  # type: ignore[arg-type]
        nuevas.append(f)

    if dry_run or not nuevas:
        resultado.creadas = len(nuevas)
        return resultado

    resultado.batch_id = str(uuid4())
    lote = {
        **(extra_enrichment or {}),
        "batch_id": resultado.batch_id,
        "at": ahora.isoformat(),
    }

    def construir(f: FilaImportada) -> Lead:
        return Lead(
            client_id=client_id,
            source_id=source_id,
            pipeline_stage_id=etapa_id,
            assigned_user_id=assigned_user_id,
            last_activity_at=ahora,
            enrichment_data={clave_lote: lote},
            **f.datos,
        )

    def anotar(lead: Lead) -> None:
        registrar_actividad(
            session,
            client_id=client_id,
            lead_id=lead.id,
            tipo=tipo_actividad,
            user_id=user_id,
            batch_id=resultado.batch_id,
            source_id=source_id,
        )

    try:
        async with session.begin_nested():
            leads = [construir(f) for f in nuevas]
            session.add_all(leads)
            await session.flush()
            for lead in leads:
                anotar(lead)
            await session.flush()
        resultado.creadas = len(leads)
    except IntegrityError:
        # Una carrera con otra alta: se repite fila a fila para no perder las buenas.
        for f in nuevas:
            try:
                async with session.begin_nested():
                    lead = construir(f)
                    session.add(lead)
                    await session.flush()
                    anotar(lead)
                    await session.flush()
                resultado.creadas += 1
            except IntegrityError:
                resultado.duplicadas += 1
                resultado.rechazos.append((f.numero, MOTIVO_EXISTENTE))
        resultado.rechazos.sort()
    return resultado
