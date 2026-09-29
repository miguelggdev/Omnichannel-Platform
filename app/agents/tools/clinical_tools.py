"""Tools del agente clinico: dictado medico, RIPS, CIE-10 y CUPS (Sprint 13, Dev B).

Mismo contrato de seguridad que `calendar_tools.py` e `invoice_tools.py`:
`client_id`, `conversation_id` y `contact_id` llegan por
`config["configurable"]` y nunca como argumentos que el LLM pueda rellenar. El
`contact_id` es el del *profesional* que dicta (el paciente se identifica por
su documento).

Las tools son una capa fina sobre `app/services/clinical.py`, que hace cumplir
las reglas en codigo. Cambios sobre el pseudocodigo del spec (§9.2), todos en
ADR-071:

- **`code_cie10`/`code_cups` no caen a un LLM.** Si el codigo no esta en el
  catalogo local, el spec le pide al modelo que lo "busque" y el resultado va a
  un RIPS: un codigo inventado termina en una glosa o en cobrarle a la EPS un
  servicio que no fue. Sin coincidencia se devuelve vacio y el agente le pide
  el codigo al profesional, que es quien lo dicta ("NUNCA sugieras
  diagnosticos", §9.1).
- **Se guarda de verdad.** `create_rips_record` y `get_patient_history` en el
  spec terminan en `# ...` y devuelven `record_id: "..."`.
- **`register_patient_consent` es una tool nueva.** El spec exige el
  consentimiento antes de registrar pero no da forma de registrarlo desde el
  chat.
- **`purpose_code` es obligatorio** (el default `"01"` es "atencion del parto").
- **El consumo de `extract_medical_entities` se contabiliza** en el presupuesto
  de tokens del tenant; el spec llama a OpenAI por fuera de `TokenBudgetGuard`.
- **Los fallos se devuelven como resultado**, no como excepcion: los valida
  `services/clinical.py` y el agente le explica al profesional que corregir.

El agente solo crea *borradores*: revisar y firmar es del profesional por la
API/UI, nunca del LLM.
"""

import json
import logging
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.agents.nodes._llm import extract_usage, get_chat_model, response_text
from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.habeas_data import HabeasDataCompliance, HabeasDataError, normalizar_documento
from app.middleware.token_budget import TokenBudgetGuard
from app.services.clinical import (
    ClinicalValidationError,
    crear_registro_rips,
    obtener_historial,
    validar_registro_rips,
)
from app.services.clinical_catalog import (
    CodigoCatalogo,
    TipoCatalogo,
    buscar_en_catalogo,
    normalizar_codigo,
)

logger = logging.getLogger(__name__)

OPERATION = "clinical_entities"

#: Categorias que puede devolver la extraccion de entidades.
CATEGORIAS_ENTIDADES: tuple[str, ...] = (
    "symptoms",
    "diagnoses",
    "medications",
    "procedures",
    "vital_signs",
    "allergies",
)

MAX_ENTIDADES_POR_CATEGORIA = 30
MAX_LARGO_DICTADO = 8000

#: Candidatos que se traen antes de filtrar por categoria o grupo.
MAX_BUSQUEDA = 2000

SIN_PROFESIONAL = (
    "No se identifico al profesional que dicta en esta conversacion, asi que no puedo "
    "registrar ni consultar datos clinicos."
)

NO_ENCONTRADO = (
    "No hay coincidencias en el catalogo local. No propongas un codigo: pidele al "
    "profesional que lo dicte, o que lo confirme contra el listado oficial."
)


def _client_id(config: RunnableConfig) -> UUID:
    """Extrae el `client_id` inyectado por el nodo, nunca provisto por el LLM.

    Args:
        config: Config que inyecta LangChain desde `.ainvoke(..., config=...)`.

    Returns:
        UUID del tenant dueno de la conversacion.
    """
    return UUID(config["configurable"]["client_id"])


def _uuid_opcional(config: RunnableConfig, clave: str) -> UUID | None:
    """Lee un UUID opcional de `config["configurable"]`.

    Args:
        config: Config que inyecta LangChain.
        clave: `contact_id` o `conversation_id`.

    Returns:
        El UUID, o `None` si no viene.
    """
    valor = config.get("configurable", {}).get(clave)
    return UUID(valor) if valor else None


def _fallo(mensaje: str) -> dict[str, Any]:
    """Respuesta de error uniforme de las tools.

    Args:
        mensaje: Que corregir, en lenguaje que el agente pueda repetir.

    Returns:
        `{"success": False, "error": mensaje}`.
    """
    return {"success": False, "error": mensaje}


def _en_categoria_cie10(codigo: str, categoria: str) -> bool:
    """Si un codigo CIE-10 cae en una categoria (`J`, `J00`, `A00-B99`).

    Args:
        codigo: Codigo CIE-10, por ejemplo `J06.9`.
        categoria: Letra, codigo de tres caracteres o rango `A00-B99`.

    Returns:
        `True` si el codigo pertenece a la categoria.
    """
    categoria = normalizar_codigo(categoria)
    base = codigo[:3]
    if "-" in categoria:
        inicio, _, fin = categoria.partition("-")
        return inicio <= base <= fin
    return codigo.startswith(categoria)


def _limpiar_entidades(crudo: Any) -> dict[str, list[dict[str, Any]]]:
    """Reduce la salida del modelo a las categorias y campos esperados.

    El JSON del LLM no es de confianza: una categoria inesperada o una lista
    enorme se descartan en vez de pasarlas al prompt siguiente.

    Args:
        crudo: Lo que devolvio `json.loads()`.

    Returns:
        `categoria -> [{text, normalized, negated}]` con las seis categorias.
    """
    limpio: dict[str, list[dict[str, Any]]] = {cat: [] for cat in CATEGORIAS_ENTIDADES}
    if not isinstance(crudo, dict):
        return limpio
    for categoria in CATEGORIAS_ENTIDADES:
        items = crudo.get(categoria)
        if not isinstance(items, list):
            continue
        for item in items[:MAX_ENTIDADES_POR_CATEGORIA]:
            if not isinstance(item, dict) or not item.get("text"):
                continue
            limpio[categoria].append(
                {
                    "text": str(item["text"])[:200],
                    "normalized": str(item.get("normalized") or "")[:200],
                    "negated": bool(item.get("negated", False)),
                }
            )
    return limpio


@tool(parse_docstring=True)
async def extract_medical_entities(text: str, config: RunnableConfig) -> dict[str, Any]:
    """Extrae entidades medicas de un dictado: sintomas, diagnosticos, medicamentos, procedimientos, signos vitales y alergias.

    Args:
        text: Texto transcrito del dictado medico.
    """
    texto = (text or "").strip()
    if not texto:
        return _fallo("El dictado esta vacio")
    if len(texto) > MAX_LARGO_DICTADO:
        return _fallo(f"El dictado supera {MAX_LARGO_DICTADO} caracteres; dividelo en partes")

    client_id = _client_id(config)
    modelo = get_settings().OPENAI_FALLBACK_MODEL
    llm = get_chat_model(modelo, temperature=0.0).bind(response_format={"type": "json_object"})
    respuesta = await llm.ainvoke(
        [
            {
                "role": "system",
                "content": (
                    "Extrae entidades medicas del siguiente dictado. Responde un JSON con las "
                    f"categorias: {', '.join(CATEGORIAS_ENTIDADES)}. Cada entidad lleva: text "
                    "(como fue dicho), normalized (termino medico estandar) y negated (bool, "
                    "true si el profesional la nego). No inventes: solo lo mencionado "
                    "explicitamente."
                ),
            },
            {"role": "user", "content": texto},
        ]
    )
    prompt_tokens, completion_tokens = extract_usage(respuesta)
    await TokenBudgetGuard.record_usage(
        client_id=str(client_id),
        conversation_id=config.get("configurable", {}).get("conversation_id"),
        model=modelo,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        operation=OPERATION,
    )

    try:
        crudo = json.loads(response_text(respuesta))
    except json.JSONDecodeError:
        return _fallo("No se pudo interpretar la extraccion; reintenta con el dictado")
    return {"success": True, "entities": _limpiar_entidades(crudo)}


async def _buscar(
    config: RunnableConfig, tipo: TipoCatalogo, consulta: str, limite: int
) -> list[CodigoCatalogo]:
    """Busca en el catalogo oficial (o en el de referencia si aun no se cargo).

    Args:
        config: Config con el `client_id` del tenant.
        tipo: `cie10` o `cups`.
        consulta: Codigo o texto libre.
        limite: Maximo de resultados.

    Returns:
        Coincidencias del catalogo.
    """
    async with tenant_session(_client_id(config)) as session:
        return await buscar_en_catalogo(session, tipo, consulta, limite)


@tool(parse_docstring=True)
async def code_cie10(diagnosis: str, config: RunnableConfig) -> dict[str, Any]:
    """Codifica un diagnostico dictado en CIE-10 usando el catalogo.

    Args:
        diagnosis: Diagnostico dictado (texto libre) o un codigo CIE-10.
    """
    coincidencias = await _buscar(config, "cie10", diagnosis, 5)
    if not coincidencias:
        return {"matches": [], "note": NO_ENCONTRADO}
    return {
        "matches": [{**c, "confidence": "catalog"} for c in coincidencias],
        "source": "catalog",
        "note": (
            "Si hay mas de una coincidencia, presentaselas al profesional para que elija. "
            "Verifica el codigo contra el listado oficial CIE-10."
        ),
    }


@tool(parse_docstring=True)
async def code_cups(procedure: str, config: RunnableConfig) -> dict[str, Any]:
    """Codifica un procedimiento dictado en CUPS usando el catalogo.

    Args:
        procedure: Procedimiento dictado (texto libre) o un codigo CUPS.
    """
    coincidencias = await _buscar(config, "cups", procedure, 5)
    if not coincidencias:
        return {"matches": [], "note": NO_ENCONTRADO}
    return {
        "matches": [{**c, "confidence": "catalog"} for c in coincidencias],
        "source": "catalog",
        "note": "Verifica el codigo CUPS contra la normativa vigente del Ministerio de Salud.",
    }


@tool(parse_docstring=True)
async def search_cie10(
    query: str, config: RunnableConfig, category: str | None = None
) -> dict[str, Any]:
    """Busca codigos CIE-10 por texto libre y, opcionalmente, por categoria.

    Args:
        query: Texto de busqueda o parte de un codigo.
        category: Letra, codigo de tres caracteres o rango (`J`, `J00`, `A00-B99`).
    """
    resultados = await _buscar(config, "cie10", query, MAX_BUSQUEDA)
    if category:
        resultados = [r for r in resultados if _en_categoria_cie10(r["code"], category)]
    return {"results": resultados[:10], "total": len(resultados)}


@tool(parse_docstring=True)
async def search_cups(
    query: str, config: RunnableConfig, group: str | None = None
) -> dict[str, Any]:
    """Busca codigos CUPS por texto libre y, opcionalmente, por grupo.

    Args:
        query: Texto de busqueda o parte de un codigo.
        group: Grupo CUPS: los dos primeros digitos (`89` consultas, `87` radiologia).
    """
    resultados = await _buscar(config, "cups", query, MAX_BUSQUEDA)
    if group:
        resultados = [r for r in resultados if r["code"].startswith(group.strip())]
    return {"results": resultados[:10], "total": len(resultados)}


@tool(parse_docstring=True)
async def register_patient_consent(
    document_type: str,
    document_number: str,
    consent_type: str,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Registra que el paciente autorizo el tratamiento de sus datos de salud. Usala solo cuando el profesional confirme que el paciente dio su autorizacion.

    Args:
        document_type: Tipo de documento del paciente (CC, TI, CE, PA, RC, MS, AS, NU).
        document_number: Numero de documento del paciente.
        consent_type: Como se otorgo: verbal, digital o written.
    """
    profesional = _uuid_opcional(config, "contact_id")
    if profesional is None:
        return _fallo(SIN_PROFESIONAL)
    client_id = _client_id(config)
    try:
        normalizar_documento(document_type, document_number)
        async with tenant_session(client_id) as session:
            return await HabeasDataCompliance.register_consent(
                session,
                client_id,
                document_type,
                document_number,
                consent_type,
                registered_by_contact_id=profesional,
            )
    except HabeasDataError as exc:
        return _fallo(str(exc))


@tool(parse_docstring=True)
async def create_rips_record(
    patient_document_type: str,
    patient_document_number: str,
    service_date: str,
    service_type: str,
    rips_type: str,
    diagnosis_codes: list[dict[str, Any]],
    purpose_code: str,
    config: RunnableConfig,
    procedure_codes: list[dict[str, Any]] | None = None,
    external_cause: str | None = None,
    diagnosis_type: str | None = None,
    patient_name: str | None = None,
    specialty: str | None = None,
    notes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Crea un registro RIPS en BORRADOR. Solo despues de que el profesional confirme el detalle, y solo si el paciente autorizo el tratamiento de sus datos.

    Args:
        patient_document_type: Tipo de documento (CC, TI, CE, PA, RC, MS, AS, NU).
        patient_document_number: Numero de documento del paciente.
        service_date: Fecha del servicio (YYYY-MM-DD).
        service_type: consulta, procedimiento, urgencia u hospitalizacion; debe corresponder al tipo RIPS.
        rips_type: AC (consulta), AP (procedimiento), AU (urgencia) o AH (hospitalizacion).
        diagnosis_codes: Lista de diagnosticos [{code, description, type}], con type principal (exactamente uno) o relacionado (hasta tres).
        purpose_code: Finalidad de la atencion, de 01 a 10; el profesional debe dictarla.
        procedure_codes: Procedimientos [{code, description, laterality}]; obligatorios en AP.
        external_cause: Causa externa, de 01 a 15; obligatoria en AU.
        diagnosis_type: confirmado, presuntivo o impresion.
        patient_name: Nombre del paciente, si el profesional lo dicto.
        specialty: Especialidad medica.
        notes: Notas SOAP con las claves subjective, objective, assessment y plan.
    """
    profesional = _uuid_opcional(config, "contact_id")
    if profesional is None:
        return _fallo(SIN_PROFESIONAL)
    client_id = _client_id(config)

    try:
        # Antes de abrir la sesion: un documento invalido no debe gastar una
        # conexion del pool (ni llegar a la base).
        normalizar_documento(patient_document_type, patient_document_number)
        datos = validar_registro_rips(
            rips_type=rips_type,
            service_type=service_type,
            service_date=service_date,
            diagnosis_codes=diagnosis_codes,
            procedure_codes=procedure_codes,
            purpose_code=purpose_code,
            external_cause=external_cause,
            diagnosis_type=diagnosis_type,
            notes=notes,
        )
        async with tenant_session(client_id) as session:
            resultado = await crear_registro_rips(
                session,
                client_id=client_id,
                dictated_by_contact_id=profesional,
                conversation_id=_uuid_opcional(config, "conversation_id"),
                document_type=patient_document_type,
                document_number=patient_document_number,
                patient_name=patient_name,
                specialty=specialty,
                datos=datos,
            )
    except (ClinicalValidationError, HabeasDataError) as exc:
        return _fallo(str(exc))

    if not resultado["success"]:
        return resultado

    sin_verificar = [
        d["code"]
        for d in datos["diagnosis_codes"] + datos["procedure_codes"]
        if not d["catalog_verified"]
    ]
    resultado["message"] = (
        "Registro RIPS creado en borrador. Requiere revision y firma del profesional."
    )
    if sin_verificar:
        resultado["unverified_codes"] = sin_verificar
        resultado["warning"] = (
            "Estos codigos no estan en el catalogo local y se guardaron tal como se dictaron; "
            "conviene confirmarlos contra el listado oficial antes de firmar."
        )
    return resultado


@tool(parse_docstring=True)
async def get_patient_history(
    document_type: str,
    document_number: str,
    config: RunnableConfig,
    limit: int = 10,
) -> dict[str, Any]:
    """Consulta el historial de registros RIPS de un paciente (fecha, tipo, diagnostico principal y estado). No incluye notas.

    Args:
        document_type: Tipo de documento del paciente.
        document_number: Numero de documento del paciente.
        limit: Maximo de registros a devolver (hasta 20).
    """
    if _uuid_opcional(config, "contact_id") is None:
        return _fallo(SIN_PROFESIONAL)
    client_id = _client_id(config)
    try:
        normalizar_documento(document_type, document_number)
        async with tenant_session(client_id) as session:
            historial = await obtener_historial(
                session,
                client_id=client_id,
                document_type=document_type,
                document_number=document_number,
                limite=limit,
            )
    except HabeasDataError as exc:
        return _fallo(str(exc))

    # Solo la cantidad: el documento del paciente no va a los logs.
    logger.info(
        "Historial clinico consultado en el tenant %s (%d registros)", client_id, len(historial)
    )
    return {
        "records": historial,
        "total_count": len(historial),
        "note": "El historial clinico es confidencial (Ley 1581 de 2012).",
    }


CLINICAL_TOOLS = [
    extract_medical_entities,
    code_cie10,
    code_cups,
    search_cie10,
    search_cups,
    register_patient_consent,
    create_rips_record,
    get_patient_history,
]
