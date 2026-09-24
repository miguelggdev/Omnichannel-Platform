"""Respuesta a encuestas CSAT via link de email (Sprint 11, Dev B).

GET  /api/v1/csat/respond   pagina de confirmacion (no registra nada)
POST /api/v1/csat/respond   registra el rating y muestra la pagina de agradecimiento

Dos pasos, no uno, aposta: un GET que registrara el rating de una vez es lo
que hacia la primera version de este endpoint, y es exactamente el patron que
un escaner de enlaces de un gateway de correo corporativo o un antivirus
dispara solo, sin que el humano llegue a abrir el email — el email manda 5
links (uno por nota), y el que el escaner toque primero queda grabado como si
el contacto lo hubiera elegido. El GET ahora es de solo lectura (ni siquiera
consulta la base) y solo arma un formulario; el registro real pasa por el POST
que ese formulario manda, algo que un prefetcher de enlaces no hace.

Publico a proposito: quien hace click viene de un email, sin JWT. La seguridad
la da `survey_id` (un UUID que nadie adivina), no una sesion — ver el docstring
de `build_survey_message()` en `app/services/csat.py` sobre por que el link
tambien lleva `client_id`.

No hay frontend (Sprint 15 sigue pendiente), asi que la respuesta es HTML
minimo servido por la propia API en vez de una redireccion a una pagina que
todavia no existe.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse

from app.core.database import tenant_session
from app.services.csat import process_response

logger = logging.getLogger(__name__)

router = APIRouter()


def _pagina(titulo: str, mensaje: str) -> HTMLResponse:
    """Pagina HTML minima de agradecimiento o error.

    Args:
        titulo: Encabezado corto.
        mensaje: Texto explicativo.

    Returns:
        Respuesta HTML autocontenida (sin CSS externo, sin JS).
    """
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><title>{titulo}</title></head>
<body style="font-family: sans-serif; text-align: center; padding: 3rem;">
<h1>{titulo}</h1><p>{mensaje}</p>
</body></html>""")


@router.get("/respond", response_class=HTMLResponse)
async def confirmar_respuesta_csat(
    survey_id: UUID = Query(...),
    client_id: UUID = Query(...),
    rating: int = Query(..., ge=1, le=5),
) -> HTMLResponse:
    """Pagina de confirmacion del rating elegido. No toca la base.

    Args:
        survey_id: Encuesta que se responde.
        client_id: Tenant de la encuesta.
        rating: Calificacion 1-5, ya validada por FastAPI.

    Returns:
        Un formulario que POSTea los mismos datos a este mismo path.
    """
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><title>Confirmar respuesta</title></head>
<body style="font-family: sans-serif; text-align: center; padding: 3rem;">
<h1>¿Calificas la atencion con {rating}?</h1>
<form method="post" action="/api/v1/csat/respond">
<input type="hidden" name="survey_id" value="{survey_id}">
<input type="hidden" name="client_id" value="{client_id}">
<input type="hidden" name="rating" value="{rating}">
<button type="submit" style="font-size: 1.2rem; padding: 0.6rem 1.5rem;">Confirmar</button>
</form>
</body></html>""")


@router.post("/respond", response_class=HTMLResponse)
async def respond_csat_via_link(
    survey_id: UUID = Form(...),
    client_id: UUID = Form(...),
    rating: int = Form(..., ge=1, le=5),
) -> HTMLResponse:
    """Registra la respuesta de una encuesta CSAT, confirmada por el propio contacto.

    Args:
        survey_id: Encuesta que se responde.
        client_id: Tenant de la encuesta (ver docstring del modulo).
        rating: Calificacion 1-5, ya validada por FastAPI antes de llegar aca.

    Returns:
        Pagina de agradecimiento, o de error si el link ya no es valido.
    """
    async with tenant_session(client_id) as session:
        survey = await process_response(session, client_id, survey_id, rating)

    if survey is None:
        return _pagina(
            "Encuesta no encontrada",
            "Este enlace ya no es valido. Puede que la encuesta haya expirado.",
        )

    logger.info("CSAT %s respondida via email: rating=%s", survey_id, rating)
    return _pagina("¡Gracias por tu respuesta!", "Tu opinion nos ayuda a mejorar.")
