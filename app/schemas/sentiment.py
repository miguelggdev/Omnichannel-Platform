"""Schema del resultado del analisis de sentimiento (Sprint 10, Dev B)."""

from enum import StrEnum

from pydantic import BaseModel, Field


class SentimentLevel(StrEnum):
    """Nivel de sentimiento de un mensaje del contacto."""

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    VERY_NEGATIVE = "very_negative"


#: Valor de cada nivel en una escala de 0 a 100, para promediarlo (lo usa el
#: scoring predictivo de contactos, `app/services/contact_scoring.py`).
PUNTAJE_SENTIMIENTO: dict[str, float] = {
    SentimentLevel.POSITIVE.value: 100.0,
    SentimentLevel.NEUTRAL.value: 50.0,
    SentimentLevel.NEGATIVE.value: 20.0,
    SentimentLevel.VERY_NEGATIVE.value: 0.0,
}


class SentimentResult(BaseModel):
    """Salida estructurada del clasificador de sentimiento.

    Attributes:
        sentiment: Nivel detectado.
        score: Confianza de la clasificacion, de 0 a 1.
        reasoning: Explicacion breve, para auditar la decision.
    """

    sentiment: SentimentLevel = Field(description="Nivel de sentimiento detectado")
    score: float = Field(ge=0.0, le=1.0, description="Confianza del analisis (0-1)")
    reasoning: str = Field(max_length=300, description="Breve explicacion del sentimiento")
