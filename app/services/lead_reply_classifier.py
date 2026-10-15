"""Clasificacion de las respuestas de un lead a una secuencia (Sprint 18, Dev B).

Funcion pura y **sin IA**: corre en el camino del webhook entrante, que debe responder en
milisegundos y no gastar tokens en cada mensaje. Reglas por palabras, en espanol, ingles y
portugues. Prefiere equivocarse hacia lo mas inocuo:

- `unsubscribe`: pide expresamente no recibir mas mensajes. Saca de **todas** las secuencias y
  bloquea nuevas (Ley 1581 / RGPD): solo con frases inequivocas ("baja", "stop", "no me escriban").
- `negative`: rechaza la oferta ("no me interesa"). Tambien bloquea (`OPT_OUT_EXITS`), asi que solo
  frases claras; "ahora no" o "mas adelante" **no** cuentan, son una respuesta normal.
- `out_of_office`: autorespuesta de ausencia. **No** saca de la secuencia: no es una persona
  contestando, y el lead sigue sin haber leido nada.
- `question`: pregunta ("cuanto cuesta?"). Es una respuesta: saca de la secuencia para que una
  persona o el agente contesten.
- `positive`: muestra interes ("me interesa", "si quiero"). Idem.
- `neutral`: cualquier otra cosa, tambien una respuesta.

El texto se normaliza (minusculas, sin acentos, sin signos) antes de buscar, y las frases se buscan
como palabras completas: "stop" no encaja en "desktop".
"""

import re
import unicodedata
from typing import Literal

ReplyKind = Literal["unsubscribe", "negative", "out_of_office", "question", "positive", "neutral"]

#: Respuestas que cuentan como "el lead contesto" (todas menos la ausencia).
RESPUESTAS_REALES: frozenset[str] = frozenset(
    {"unsubscribe", "negative", "question", "positive", "neutral"}
)

#: Longitud maxima que se analiza: una respuesta larga no aporta y la regex no debe pagarla.
MAX_CHARS_ANALIZADOS = 1000

_BAJA = (
    "stop",
    "baja",
    "dar de baja",
    "darme de baja",
    "dame de baja",
    "unsubscribe",
    "remove me",
    "no me escriban",
    "no me escribas",
    "no me escriba",
    "no me contacten",
    "no me contactes",
    "no me llamen",
    "no quiero recibir",
    "no deseo recibir",
    "dejen de escribirme",
    "deja de escribirme",
    "dejen de contactarme",
    "cancelar suscripcion",
    "cancela mi suscripcion",
    "parem de me escrever",
    "nao quero receber",
    "do not contact",
    "dont contact me",
    "stop messaging",
    "stop emailing",
)
_NEGATIVA = (
    "no me interesa",
    "no estoy interesado",
    "no estoy interesada",
    "no nos interesa",
    "no gracias",
    "no, gracias",
    "no necesito",
    "no necesitamos",
    "no estamos interesados",
    "not interested",
    "no interest",
    "nao tenho interesse",
    "nao estou interessado",
    "nao estou interessada",
    "nao precisamos",
)
_AUSENCIA = (
    "fuera de la oficina",
    "fuera de oficina",
    "estare fuera",
    "respuesta automatica",
    "mensaje automatico",
    "auto reply",
    "autoreply",
    "automatic reply",
    "out of office",
    "out of the office",
    "away from the office",
    "resposta automatica",
    "fora do escritorio",
)
_POSITIVA = (
    "me interesa",
    "nos interesa",
    "estoy interesado",
    "estoy interesada",
    "quiero saber mas",
    "quiero mas informacion",
    "si quiero",
    "claro que si",
    "agendemos",
    "agendar",
    "hablemos",
    "llamame",
    "llamenme",
    "interested",
    "sounds good",
    "lets talk",
    "lets schedule",
    "tenho interesse",
    "vamos conversar",
)
_PREGUNTA = (
    "cuanto cuesta",
    "cuanto vale",
    "que precio",
    "cual es el precio",
    "como funciona",
    "que incluye",
    "how much",
    "how does",
    "what is the price",
    "quanto custa",
)

_NO_ALFANUMERICO = re.compile(r"[^a-z0-9?\s]")
_ESPACIOS = re.compile(r"\s+")


def normalizar(texto: str) -> str:
    """Minusculas, sin acentos ni signos (salvo `?`) y con los espacios colapsados.

    Args:
        texto: Texto original.

    Returns:
        El texto normalizado, recortado a `MAX_CHARS_ANALIZADOS`.
    """
    sin_acentos = unicodedata.normalize("NFKD", texto[:MAX_CHARS_ANALIZADOS].lower())
    sin_marcas = "".join(c for c in sin_acentos if not unicodedata.combining(c))
    sin_signos = _NO_ALFANUMERICO.sub(" ", sin_marcas.replace("'", ""))
    return _ESPACIOS.sub(" ", sin_signos).strip()


def _contiene(texto: str, frases: tuple[str, ...]) -> bool:
    """Si alguna frase aparece como palabras completas."""
    relleno = f" {texto} "
    return any(f" {normalizar(f)} " in relleno for f in frases)


def clasificar_respuesta(texto: str | None) -> ReplyKind:
    """Que tipo de respuesta es el mensaje de un lead.

    El orden importa: la ausencia va primero (una autorespuesta puede decir "no me escriban a este
    correo hasta el lunes"), despues la baja, la negativa, la pregunta y por ultimo el interes.

    Args:
        texto: Contenido del mensaje (`None` para un adjunto o un sticker).

    Returns:
        El tipo; `neutral` si no encaja con nada o no hay texto.
    """
    if not texto or not texto.strip():
        return "neutral"
    limpio = normalizar(texto)
    if _contiene(limpio, _AUSENCIA):
        return "out_of_office"
    if _contiene(limpio, _BAJA):
        return "unsubscribe"
    if _contiene(limpio, _NEGATIVA):
        return "negative"
    if "?" in limpio or _contiene(limpio, _PREGUNTA):
        return "question"
    if _contiene(limpio, _POSITIVA):
        return "positive"
    return "neutral"
