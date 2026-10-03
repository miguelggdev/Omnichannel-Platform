"""Servicio clinico: validacion RIPS, registro, historial y autorizacion (Sprint 13).

Aca vive la logica que el spec (§9.2) deja dentro de las tools y con un
`# Guardar en DB # ...` sin implementar. Las tools del agente
(`app/agents/tools/clinical_tools.py`) son una capa fina encima: el LLM decide
*que* llamar, pero las reglas que protegen un registro clinico —documento
valido, un solo diagnostico principal, codigos con formato, consentimiento del
titular, fecha no futura— se hacen cumplir en codigo, no en el prompt.

Cambios sobre el pseudocodigo del spec, todos en ADR-072:

- **`purpose_code` es obligatorio.** El spec lo deja en `"01"` por defecto, que
  en la Resolucion 3374 es *atencion del parto*: un default silencioso habria
  codificado mal cada consulta cuyo profesional no lo dijera (la misma clase de
  defecto que el IVA por defecto de las facturas, ADR-067).
- **El servicio y el tipo RIPS tienen que ser coherentes** (AC-consulta,
  AP-procedimiento, AU-urgencia, AH-hospitalizacion), y un AP exige al menos un
  procedimiento y un AU la causa externa.
- **Exactamente un diagnostico principal**, y a lo sumo tres relacionados (el
  prompt del spec ya dice "principal + 3 relacionados" pero el codigo solo
  pedia "al menos uno").
- **Sin consentimiento vigente no se crea nada** (criterio 11 del spec), y
  se comprueba aca, dentro de la misma transaccion que inserta.
- **Reintentos idempotentes.** Una entrega repetida de la tarea de Celery no
  duplica el borrador: el mismo paciente, fecha y tipo RIPS en la misma
  conversacion devuelve el registro existente.
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import mask_identifier
from app.core.habeas_data import (
    HabeasDataCompliance,
    HabeasDataError,
    hash_paciente,
    normalizar_documento,
)
from app.models.agent_config import AgentConfig
from app.models.call_record import CallRecord
from app.models.clinical_record import (
    RECORD_DRAFT,
    RECORD_REVIEWED,
    RECORD_STATUSES,
    RECORD_TRANSITIONS,
    ClinicalRecord,
    fin_de_retencion,
)
from app.services.clinical_catalog import (
    CIE10_COMMON,
    CIE10_PATTERN,
    CUPS_COMMON,
    CUPS_PATTERN,
    normalizar_codigo,
    normalizar_texto,
    verificar_codigos,
)

logger = logging.getLogger(__name__)

TIPOS_RIPS: tuple[str, ...] = ("AC", "AP", "AU", "AH")

#: Servicio que corresponde a cada archivo RIPS.
SERVICIO_POR_RIPS: dict[str, str] = {
    "AC": "consulta",
    "AP": "procedimiento",
    "AU": "urgencia",
    "AH": "hospitalizacion",
}

TIPOS_DIAGNOSTICO: tuple[str, ...] = ("confirmado", "presuntivo", "impresion")
MAX_DIAGNOSTICOS_RELACIONADOS = 3
MAX_PROCEDIMIENTOS = 20

#: Zona horaria del prestador: la fecha del servicio es la local, no la UTC.
_ZONA_COLOMBIA = ZoneInfo("America/Bogota")

_FINALIDAD_PATTERN = re.compile(r"^(0[1-9]|10)$")
_CAUSA_EXTERNA_PATTERN = re.compile(r"^(0[1-9]|1[0-5])$")

#: Cuanto despues de terminar una llamada se sigue atribuyendole un registro.
#: El agente procesa el ultimo turno en una tarea aparte, que puede terminar
#: despues de que Twilio cierre la llamada.
VENTANA_POSTERIOR_A_LA_LLAMADA = timedelta(minutes=15)

MAX_LARGO_NOTA = 4000
_CAMPOS_SOAP = ("subjective", "objective", "assessment", "plan")


class ClinicalValidationError(ValueError):
    """Un dato del registro clinico no cumple las reglas RIPS."""


def _hoy_colombia() -> date:
    """La fecha de hoy en Colombia (UTC-5), que es la del servicio prestado.

    Returns:
        La fecha local del prestador.
    """
    return datetime.now(_ZONA_COLOMBIA).date()


def _parsear_fecha(valor: str) -> date:
    """Valida la fecha del servicio.

    Args:
        valor: Fecha `YYYY-MM-DD`.

    Returns:
        La fecha.

    Raises:
        ClinicalValidationError: Si no tiene el formato o es futura.
    """
    try:
        fecha = date.fromisoformat((valor or "").strip())
    except ValueError:
        raise ClinicalValidationError(
            "La fecha del servicio debe tener el formato YYYY-MM-DD"
        ) from None
    if fecha > _hoy_colombia():
        raise ClinicalValidationError("La fecha del servicio no puede ser futura")
    return fecha


def _validar_diagnosticos(diagnosticos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Valida y normaliza los diagnosticos CIE-10 dictados.

    Args:
        diagnosticos: `[{code, description?, type}]` con `type` `principal` o
            `relacionado`.

    Returns:
        Los diagnosticos normalizados, con `catalog_verified`.

    Raises:
        ClinicalValidationError: Si no hay exactamente un principal, hay mas de
            tres relacionados, un codigo tiene formato invalido o se repite.
    """
    normalizados: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for item in diagnosticos or []:
        codigo = normalizar_codigo(str(item.get("code") or ""))
        if not CIE10_PATTERN.match(codigo):
            raise ClinicalValidationError(
                f"Codigo CIE-10 invalido: '{codigo}'. Formato esperado: J06.9, I10, A09"
            )
        if codigo in vistos:
            raise ClinicalValidationError(f"El diagnostico {codigo} esta repetido")
        vistos.add(codigo)
        tipo = str(item.get("type") or "").strip().lower()
        if tipo not in ("principal", "relacionado"):
            raise ClinicalValidationError(
                f"El diagnostico {codigo} necesita type 'principal' o 'relacionado'"
            )
        descripcion = str(item.get("description") or "").strip() or CIE10_COMMON.get(codigo, "")
        normalizados.append(
            {
                "code": codigo,
                "description": descripcion,
                "type": tipo,
                "catalog_verified": codigo in CIE10_COMMON,
            }
        )

    principales = [d for d in normalizados if d["type"] == "principal"]
    if len(principales) != 1:
        raise ClinicalValidationError("Se requiere exactamente un diagnostico principal.")
    if len(normalizados) - 1 > MAX_DIAGNOSTICOS_RELACIONADOS:
        raise ClinicalValidationError(
            f"A lo sumo {MAX_DIAGNOSTICOS_RELACIONADOS} diagnosticos relacionados."
        )
    return normalizados


def _validar_procedimientos(procedimientos: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Valida y normaliza los procedimientos CUPS dictados.

    Args:
        procedimientos: `[{code, description?, laterality?}]`.

    Returns:
        Los procedimientos normalizados, con `catalog_verified`.

    Raises:
        ClinicalValidationError: Si un codigo tiene formato invalido, se repite
            o hay demasiados.
    """
    if len(procedimientos or []) > MAX_PROCEDIMIENTOS:
        raise ClinicalValidationError(f"A lo sumo {MAX_PROCEDIMIENTOS} procedimientos.")
    normalizados: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for item in procedimientos or []:
        codigo = normalizar_codigo(str(item.get("code") or ""))
        if not CUPS_PATTERN.match(codigo):
            raise ClinicalValidationError(
                f"Codigo CUPS invalido: '{codigo}'. Debe tener seis digitos"
            )
        if codigo in vistos:
            raise ClinicalValidationError(f"El procedimiento {codigo} esta repetido")
        vistos.add(codigo)
        normalizados.append(
            {
                "code": codigo,
                "description": str(item.get("description") or "").strip()
                or CUPS_COMMON.get(codigo, ""),
                "laterality": item.get("laterality"),
                "catalog_verified": codigo in CUPS_COMMON,
            }
        )
    return normalizados


def _validar_notas(notas: dict[str, Any] | None) -> dict[str, str]:
    """Valida las notas SOAP: solo las cuatro secciones y con tope de largo.

    Args:
        notas: `{subjective, objective, assessment, plan}`.

    Returns:
        Las secciones presentes, como texto.

    Raises:
        ClinicalValidationError: Si hay una seccion desconocida o una nota enorme.
    """
    limpias: dict[str, str] = {}
    for clave, valor in (notas or {}).items():
        if clave not in _CAMPOS_SOAP:
            raise ClinicalValidationError(
                f"Seccion SOAP desconocida: '{clave}'. Validas: {list(_CAMPOS_SOAP)}"
            )
        texto = str(valor or "").strip()
        if len(texto) > MAX_LARGO_NOTA:
            raise ClinicalValidationError(
                f"La seccion '{clave}' supera {MAX_LARGO_NOTA} caracteres"
            )
        if texto:
            limpias[clave] = texto
    return limpias


def validar_registro_rips(
    *,
    rips_type: str,
    service_type: str,
    service_date: str,
    diagnosis_codes: list[dict[str, Any]],
    procedure_codes: list[dict[str, Any]] | None,
    purpose_code: str,
    external_cause: str | None,
    diagnosis_type: str | None,
    notes: dict[str, Any] | None,
) -> dict[str, Any]:
    """Valida todos los campos de un registro RIPS y devuelve los normalizados.

    Args:
        rips_type: AC, AP, AU o AH.
        service_type: consulta, procedimiento, urgencia u hospitalizacion.
        service_date: Fecha `YYYY-MM-DD`.
        diagnosis_codes: Diagnosticos CIE-10.
        procedure_codes: Procedimientos CUPS.
        purpose_code: Finalidad (01-10).
        external_cause: Causa externa (01-15); obligatoria en urgencias.
        diagnosis_type: confirmado, presuntivo o impresion.
        notes: Notas SOAP.

    Returns:
        Dict con las claves `rips_type`, `service_type`, `service_date`,
        `diagnosis_codes`, `procedure_codes`, `purpose_code`, `external_cause`,
        `diagnosis_type` y `structured_notes`, ya normalizadas.

    Raises:
        ClinicalValidationError: Con el primer incumplimiento encontrado.
    """
    tipo_rips = (rips_type or "").strip().upper()
    if tipo_rips not in TIPOS_RIPS:
        raise ClinicalValidationError(f"Tipo RIPS invalido. Validos: {list(TIPOS_RIPS)}")

    servicio = normalizar_texto(service_type or "")
    if servicio != SERVICIO_POR_RIPS[tipo_rips]:
        raise ClinicalValidationError(
            f"Un registro {tipo_rips} corresponde a '{SERVICIO_POR_RIPS[tipo_rips]}', "
            f"no a '{service_type}'"
        )

    fecha = _parsear_fecha(service_date)
    diagnosticos = _validar_diagnosticos(diagnosis_codes)
    procedimientos = _validar_procedimientos(procedure_codes)
    if tipo_rips == "AP" and not procedimientos:
        raise ClinicalValidationError("Un registro AP necesita al menos un procedimiento CUPS.")

    finalidad = (purpose_code or "").strip()
    if not _FINALIDAD_PATTERN.match(finalidad):
        raise ClinicalValidationError("La finalidad debe ser un codigo de 01 a 10")

    causa = (external_cause or "").strip() or None
    if causa is not None and not _CAUSA_EXTERNA_PATTERN.match(causa):
        raise ClinicalValidationError("La causa externa debe ser un codigo de 01 a 15")
    if tipo_rips == "AU" and causa is None:
        raise ClinicalValidationError("Un registro AU necesita la causa externa.")

    tipo_dx = normalizar_texto(diagnosis_type) if diagnosis_type else None
    if tipo_dx is not None and tipo_dx not in TIPOS_DIAGNOSTICO:
        raise ClinicalValidationError(
            f"Tipo de diagnostico invalido. Validos: {list(TIPOS_DIAGNOSTICO)}"
        )

    return {
        "rips_type": tipo_rips,
        "service_type": servicio,
        "service_date": fecha,
        "diagnosis_codes": diagnosticos,
        "procedure_codes": procedimientos,
        "purpose_code": finalidad,
        "external_cause": causa,
        "diagnosis_type": tipo_dx,
        "structured_notes": _validar_notas(notas=notes),
    }


def resumen_registro(registro: ClinicalRecord, documento: str) -> dict[str, Any]:
    """Resumen de un registro para devolverselo al agente, con el documento enmascarado.

    Args:
        registro: Registro clinico.
        documento: Numero de documento en claro (ya normalizado).

    Returns:
        Resumen sin documento completo ni contenido de las notas.
    """
    principal = next(
        (d["code"] for d in (registro.diagnosis_codes or []) if d.get("type") == "principal"), None
    )
    return {
        "record_id": str(registro.id),
        "status": registro.status,
        "patient": f"{registro.patient_document_type} {mask_identifier(documento)}",
        "date": registro.service_date.isoformat(),
        "type": registro.rips_type,
        "main_diagnosis": principal,
        "procedures": len(registro.procedure_codes or []),
    }


async def _aplicar_catalogo_oficial(session: AsyncSession, datos: dict[str, Any]) -> None:
    """Contrasta los codigos con el catalogo oficial, si esta cargado.

    Con el catalogo oficial en la base, un codigo que no exista en el es un
    error de dictado y se rechaza; los que existen quedan `catalog_verified` y
    heredan la descripcion oficial si el profesional no la dicto. Con las
    tablas vacias no hace nada (rige el subconjunto de referencia).

    Args:
        session: Sesion de base de datos.
        datos: Salida de `validar_registro_rips()`; se modifica en el lugar.

    Raises:
        ClinicalValidationError: Si un codigo no esta en el catalogo oficial.
    """
    for tipo, clave in (("cie10", "diagnosis_codes"), ("cups", "procedure_codes")):
        items: list[dict[str, Any]] = datos[clave]
        cargado, encontrados = await verificar_codigos(
            session,
            tipo,  # type: ignore[arg-type]
            [i["code"] for i in items],
        )
        if not cargado:
            continue
        for item in items:
            oficial = encontrados.get(item["code"])
            if oficial is None:
                raise ClinicalValidationError(
                    f"El codigo {item['code']} no existe en el catalogo oficial "
                    f"{tipo.upper()}. Pidele al profesional que lo confirme."
                )
            item["catalog_verified"] = True
            item["description"] = item["description"] or oficial


async def llamada_en_curso(
    session: AsyncSession,
    client_id: UUID,
    conversation_id: UUID,
    instante: datetime,
) -> UUID | None:
    """Llamada de la conversacion durante la cual ocurre `instante`.

    Una conversacion de voz puede acumular varias llamadas del mismo contacto:
    se toma la que ya habia empezado y sigue abierta, o que termino hace menos
    de `VENTANA_POSTERIOR_A_LA_LLAMADA`. Una llamada anterior ya cerrada no
    cuenta: atribuirle un dictado de otra llamada seria un dato falso en el
    historial.

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant dueno.
        conversation_id: Conversacion del dictado.
        instante: Momento del dictado.

    Returns:
        El `id` del `CallRecord`, o `None` si no hay una llamada que lo explique
        (el caso normal en WhatsApp, Telegram, etc.).
    """
    return (
        await session.execute(
            select(CallRecord.id)
            .where(
                CallRecord.client_id == client_id,
                CallRecord.conversation_id == conversation_id,
                CallRecord.started_at <= instante,
                (CallRecord.ended_at.is_(None))
                | (CallRecord.ended_at >= instante - VENTANA_POSTERIOR_A_LA_LLAMADA),
            )
            .order_by(CallRecord.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def vincular_registros_con_llamada(
    session: AsyncSession,
    *,
    client_id: UUID,
    call_record_id: UUID,
    conversation_id: UUID,
    started_at: datetime,
    ended_at: datetime | None,
) -> int:
    """Liga a una llamada los registros clinicos que se dictaron durante ella.

    Cubre el orden contrario al de `crear_registro_rips`: el `conversation_id`
    de un `CallRecord` solo se conoce cuando ya hay mensajes de la llamada, asi
    que el registro puede haberse creado antes de poder enlazarlo.

    Solo toca borradores y registros revisados: el trigger de retencion de la
    migracion 017 rechaza cualquier cambio en uno firmado o enviado, y ese
    error abortaria la transaccion que esta guardando la llamada. Un registro
    firmado antes de enlazarse queda sin llamada.

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant dueno.
        call_record_id: Llamada a la que se ligan.
        conversation_id: Conversacion de la llamada.
        started_at: Inicio de la llamada.
        ended_at: Fin de la llamada, si ya termino.

    Returns:
        Cuantos registros se ligaron.
    """
    hasta = (
        ended_at + VENTANA_POSTERIOR_A_LA_LLAMADA
        if ended_at is not None
        else datetime.now(timezone.utc)
    )
    resultado = await session.execute(
        update(ClinicalRecord)
        .where(
            ClinicalRecord.client_id == client_id,
            ClinicalRecord.conversation_id == conversation_id,
            ClinicalRecord.call_record_id.is_(None),
            ClinicalRecord.status.in_((RECORD_DRAFT, RECORD_REVIEWED)),
            ClinicalRecord.created_at >= started_at,
            ClinicalRecord.created_at <= hasta,
        )
        .values(call_record_id=call_record_id)
        .returning(ClinicalRecord.id)
    )
    return len(resultado.all())


async def crear_registro_rips(
    session: AsyncSession,
    *,
    client_id: UUID,
    dictated_by_contact_id: UUID,
    conversation_id: UUID | None,
    document_type: str,
    document_number: str,
    patient_name: str | None,
    specialty: str | None,
    datos: dict[str, Any],
) -> dict[str, Any]:
    """Crea un registro RIPS en borrador si el titular autorizo el tratamiento.

    Args:
        session: Sesion con el contexto de tenant aplicado; la transaccion la
            cierra el llamante.
        client_id: Tenant dueno.
        dictated_by_contact_id: Profesional que dicta.
        conversation_id: Conversacion en curso, si la hay.
        document_type: Tipo de documento del paciente.
        document_number: Numero de documento del paciente.
        patient_name: Nombre del paciente, opcional.
        specialty: Especialidad, opcional.
        datos: Salida de `validar_registro_rips()`.

    Returns:
        `{"success": True, ...resumen}`; con `"duplicate": True` si ya existia
        el mismo borrador. Si falta la autorizacion, `{"success": False,
        "error": "consent_required", "message": ...}`.

    Raises:
        HabeasDataError: Si el documento no es valido.
        ClinicalValidationError: Si un codigo no existe en el catalogo oficial.
    """
    tipo, numero = normalizar_documento(document_type, document_number)
    hash_documento = hash_paciente(client_id, tipo, numero)
    await _aplicar_catalogo_oficial(session, datos)

    consentimiento = await HabeasDataCompliance.verify_consent(session, client_id, tipo, numero)
    if not consentimiento["has_consent"]:
        return {
            "success": False,
            "error": "consent_required",
            "message": (
                "El paciente no tiene autorizacion vigente para el tratamiento de sus datos de "
                "salud (Ley 1581 de 2012, art. 6). Pidele al profesional que confirme que el "
                "paciente la otorgo y registrala con register_patient_consent antes de guardar."
            ),
        }

    # Serializa los dictados del mismo paciente: sin el lock, dos entregas
    # simultaneas de la tarea no verian el borrador de la otra y lo duplicarian.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:clave))"),
        {"clave": f"clinical:{client_id}:{hash_documento}"},
    )

    if conversation_id is not None:
        existente = (
            await session.execute(
                select(ClinicalRecord)
                .where(
                    ClinicalRecord.client_id == client_id,
                    ClinicalRecord.patient_document_hash == hash_documento,
                    ClinicalRecord.conversation_id == conversation_id,
                    ClinicalRecord.service_date == datos["service_date"],
                    ClinicalRecord.rips_type == datos["rips_type"],
                    ClinicalRecord.status == RECORD_DRAFT,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existente is not None:
            return {"success": True, "duplicate": True, **resumen_registro(existente, numero)}

    ahora = datetime.now(timezone.utc)
    # Si el dictado llego por voz, el registro apunta a la llamada. Sin
    # conversacion o sin llamada (WhatsApp, Telegram...) queda en `None`.
    llamada = (
        await llamada_en_curso(session, client_id, conversation_id, ahora)
        if conversation_id is not None
        else None
    )
    registro = ClinicalRecord(
        client_id=client_id,
        dictated_by_contact_id=dictated_by_contact_id,
        conversation_id=conversation_id,
        call_record_id=llamada,
        patient_document_type=tipo,
        patient_document_number=numero,
        patient_document_hash=hash_documento,
        patient_name=(patient_name or "").strip() or None,
        service_date=datos["service_date"],
        service_type=datos["service_type"],
        specialty=(specialty or "").strip() or None,
        diagnosis_codes=datos["diagnosis_codes"],
        procedure_codes=datos["procedure_codes"],
        diagnosis_type=datos["diagnosis_type"],
        structured_notes=datos["structured_notes"],
        rips_type=datos["rips_type"],
        purpose_code=datos["purpose_code"],
        external_cause=datos["external_cause"],
        consent_given=ahora,
        consent_type=consentimiento["consent_type"],
        data_processing_authorized=datetime.fromisoformat(consentimiento["consent_date"]),
        status=RECORD_DRAFT,
    )
    session.add(registro)
    await session.flush()
    return {"success": True, "duplicate": False, **resumen_registro(registro, numero)}


async def obtener_historial(
    session: AsyncSession,
    *,
    client_id: UUID,
    document_type: str,
    document_number: str,
    limite: int = 10,
) -> list[dict[str, Any]]:
    """Lista los registros anteriores de un paciente, sin notas ni documento.

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant dueno.
        document_type: Tipo de documento del paciente.
        document_number: Numero de documento del paciente.
        limite: Maximo de registros (tope de 20).

    Returns:
        Fecha, tipo, diagnostico principal y estado de cada registro, del mas
        reciente al mas antiguo.

    Raises:
        HabeasDataError: Si el documento no es valido.
    """
    hash_documento = hash_paciente(client_id, document_type, document_number)
    filas = (
        (
            await session.execute(
                select(ClinicalRecord)
                .where(
                    ClinicalRecord.client_id == client_id,
                    ClinicalRecord.patient_document_hash == hash_documento,
                    ClinicalRecord.anonymized_at.is_(None),
                )
                .order_by(ClinicalRecord.service_date.desc(), ClinicalRecord.created_at.desc())
                .limit(max(1, min(limite, 20)))
            )
        )
        .scalars()
        .all()
    )
    historial: list[dict[str, Any]] = []
    for fila in filas:
        principal = next(
            (d for d in (fila.diagnosis_codes or []) if d.get("type") == "principal"), {}
        )
        historial.append(
            {
                "record_id": str(fila.id),
                "date": fila.service_date.isoformat(),
                "type": fila.rips_type,
                "main_diagnosis": principal.get("code"),
                "main_diagnosis_description": principal.get("description"),
                "status": fila.status,
            }
        )
    return historial


async def leer_config_clinica(session: AsyncSession, client_id: UUID) -> dict[str, Any]:
    """Lee `agent_configs.config.clinical` de la configuracion activa del tenant.

    Misma "configuracion activa" que usa todo el grafo
    (`_tenant.get_agent_settings`): la `agent_config` activa mas antigua.

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant dueno de la configuracion.

    Returns:
        El objeto `clinical`, o un dict vacio si el tenant no declaro nada.
    """
    config = (
        await session.execute(
            select(AgentConfig)
            .where(AgentConfig.client_id == client_id, AgentConfig.is_active.is_(True))
            .order_by(AgentConfig.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    clinica = ((config.config or {}) if config else {}).get("clinical", {})
    return clinica if isinstance(clinica, dict) else {}


async def es_profesional_clinico(
    session: AsyncSession, client_id: UUID, contact_id: str | UUID | None
) -> bool:
    """Si el contacto de la conversacion puede operar el agente clinico.

    El spec (§9.1) mira `state["user_role"]`, un campo que el estado del grafo
    no tiene: el grafo atiende a *contactos* de un canal, no a usuarios con
    rol. Sin este chequeo, cualquier cliente que escribiera "necesito un
    registro clinico" por WhatsApp llegaria al agente que lee y escribe datos
    de salud de terceros. Mismo criterio que BUG-045 (marketing): solo operan
    los contactos declarados en
    `agent_configs.config.clinical.professional_contact_ids`, y sin lista,
    nadie.

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant dueno de la conversacion.
        contact_id: Contacto de la conversacion, si lo hay.

    Returns:
        `True` si el contacto esta en la lista de profesionales del tenant.
    """
    if not contact_id:
        return False
    profesionales = (await leer_config_clinica(session, client_id)).get(
        "professional_contact_ids", []
    )
    if not isinstance(profesionales, list):
        return False
    return str(contact_id) in {str(p) for p in profesionales}


__all__ = [
    "SERVICIO_POR_RIPS",
    "TIPOS_RIPS",
    "ClinicalValidationError",
    "HabeasDataError",
    "RegistroNoEncontradoError",
    "TransicionInvalidaError",
    "avanzar_registro",
    "crear_registro_rips",
    "es_profesional_clinico",
    "leer_config_clinica",
    "listar_registros",
    "obtener_historial",
    "obtener_registro",
    "resumen_registro",
    "validar_registro_rips",
]


class RegistroNoEncontradoError(LookupError):
    """El registro clinico no existe en este tenant."""


class TransicionInvalidaError(ValueError):
    """El registro no esta en el estado desde el que se pidio avanzar."""


def _detalle(registro: ClinicalRecord, ultima_atencion: date) -> dict[str, Any]:
    """Arma la vista de un registro para quien lo revisa, con el documento enmascarado.

    Args:
        registro: Registro clinico.
        ultima_atencion: Fecha de la ultima atencion del paciente.

    Returns:
        Datos del registro; nunca el documento completo ni el dictado crudo.
    """
    return {
        "id": str(registro.id),
        "status": registro.status,
        "patient_document_type": registro.patient_document_type,
        "patient_document": mask_identifier(registro.patient_document_number),
        "patient_name": registro.patient_name,
        "service_date": registro.service_date.isoformat(),
        "service_type": registro.service_type,
        "specialty": registro.specialty,
        "rips_type": registro.rips_type,
        "purpose_code": registro.purpose_code,
        "external_cause": registro.external_cause,
        "diagnosis_type": registro.diagnosis_type,
        "diagnosis_codes": registro.diagnosis_codes or [],
        "procedure_codes": registro.procedure_codes or [],
        "structured_notes": registro.structured_notes or {},
        "dictated_by_contact_id": (
            str(registro.dictated_by_contact_id) if registro.dictated_by_contact_id else None
        ),
        "reviewed_by": str(registro.reviewed_by) if registro.reviewed_by else None,
        "signed_at": registro.signed_at.isoformat() if registro.signed_at else None,
        "anonymized": registro.anonymized_at is not None,
        "retention_until": fin_de_retencion(ultima_atencion).isoformat(),
    }


async def listar_registros(
    session: AsyncSession,
    *,
    client_id: UUID,
    estado: str | None = None,
    limite: int = 50,
    desplazamiento: int = 0,
) -> list[dict[str, Any]]:
    """Lista registros clinicos para revision, del mas reciente al mas antiguo.

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant dueno.
        estado: Filtra por estado, si se da.
        limite: Maximo de filas (tope de 200).
        desplazamiento: Filas a saltar.

    Returns:
        Resumen por registro: sin notas ni documento.

    Raises:
        ClinicalValidationError: Si `estado` no existe.
    """
    if estado is not None and estado not in RECORD_STATUSES:
        raise ClinicalValidationError(f"Estado invalido. Validos: {list(RECORD_STATUSES)}")
    consulta = select(ClinicalRecord).where(ClinicalRecord.client_id == client_id)
    if estado is not None:
        consulta = consulta.where(ClinicalRecord.status == estado)
    filas = (
        (
            await session.execute(
                consulta.order_by(
                    ClinicalRecord.service_date.desc(), ClinicalRecord.created_at.desc()
                )
                .limit(max(1, min(limite, 200)))
                .offset(max(0, desplazamiento))
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(f.id),
            "status": f.status,
            "service_date": f.service_date.isoformat(),
            "rips_type": f.rips_type,
            "main_diagnosis": next(
                (d["code"] for d in (f.diagnosis_codes or []) if d.get("type") == "principal"), None
            ),
            "anonymized": f.anonymized_at is not None,
        }
        for f in filas
    ]


async def obtener_registro(
    session: AsyncSession, *, client_id: UUID, record_id: UUID
) -> dict[str, Any]:
    """Devuelve el detalle de un registro para su revision.

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant dueno.
        record_id: Registro pedido.

    Returns:
        El detalle, con la fecha hasta la que se conserva.

    Raises:
        RegistroNoEncontradoError: Si no existe en este tenant.
    """
    registro = (
        await session.execute(
            select(ClinicalRecord).where(
                ClinicalRecord.id == record_id, ClinicalRecord.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if registro is None:
        raise RegistroNoEncontradoError(str(record_id))
    ultima = (
        await session.execute(
            select(func.max(ClinicalRecord.service_date)).where(
                ClinicalRecord.client_id == client_id,
                ClinicalRecord.patient_document_hash == registro.patient_document_hash,
            )
        )
    ).scalar_one_or_none()
    return _detalle(registro, ultima or registro.service_date)


async def avanzar_registro(
    session: AsyncSession,
    *,
    client_id: UUID,
    record_id: UUID,
    destino: str,
    user_id: UUID,
) -> dict[str, Any]:
    """Avanza un registro al siguiente estado: `draft -> reviewed -> signed -> submitted`.

    Es un `UPDATE ... WHERE status = <origen>` atomico: dos firmas simultaneas
    no se pisan y la segunda recibe `TransicionInvalidaError`. El trigger de la
    base (migracion 017) hace cumplir lo mismo aunque alguien salte esta
    funcion. Un registro firmado ya no se puede modificar ni borrar durante 20
    anos.

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant dueno.
        record_id: Registro a avanzar.
        destino: `reviewed`, `signed` o `submitted`.
        user_id: Usuario que lo hace; queda como `reviewed_by` al revisar.

    Returns:
        El detalle del registro ya avanzado.

    Raises:
        RegistroNoEncontradoError: Si no existe en este tenant.
        TransicionInvalidaError: Si el registro no esta en el estado previo, o
            ya fue anonimizado.
    """
    origen = next((o for o, d in RECORD_TRANSITIONS.items() if d == destino), None)
    if origen is None:
        raise TransicionInvalidaError(f"Estado destino invalido: {destino}")

    valores: dict[str, Any] = {"status": destino}
    if destino == "reviewed":
        valores["reviewed_by"] = user_id
    elif destino == "signed":
        valores["signed_at"] = datetime.now(timezone.utc)

    actualizado = (
        await session.execute(
            update(ClinicalRecord)
            .where(
                ClinicalRecord.id == record_id,
                ClinicalRecord.client_id == client_id,
                ClinicalRecord.status == origen,
                ClinicalRecord.anonymized_at.is_(None),
            )
            .values(**valores)
            .returning(ClinicalRecord.id)
        )
    ).scalar_one_or_none()

    if actualizado is None:
        existe = (
            await session.execute(
                select(ClinicalRecord.status).where(
                    ClinicalRecord.id == record_id, ClinicalRecord.client_id == client_id
                )
            )
        ).scalar_one_or_none()
        if existe is None:
            raise RegistroNoEncontradoError(str(record_id))
        raise TransicionInvalidaError(
            f"El registro esta en '{existe}'; para pasar a '{destino}' tiene que estar en '{origen}'"
        )
    return await obtener_registro(session, client_id=client_id, record_id=record_id)
