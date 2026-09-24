"""Respuesta a encuestas CSAT via link de email (Sprint 11, Dev B).

GET /api/v1/csat/respond   registra el rating y muestra una pagina de agradecimiento

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

from fastapi import APIRouter, Query
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
async def respond_csat_via_link(
    survey_id: UUID = Query(...),
    client_id: UUID = Query(...),
    rating: int = Query(..., ge=1, le=5),
) -> HTMLResponse:
    """Registra la respuesta de una encuesta CSAT enviada por email.

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
