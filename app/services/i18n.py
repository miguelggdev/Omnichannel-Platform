"""Internacionalizacion del backend (Sprint 14b, ADR-077).

Contrato: `specs/sprint-14-sandbox-i18n.md` §3, §4 y §4c. Seis idiomas (`es`,
`en`, `pt`, `it`, `de`, `fr`); el espanol es el de referencia y el de rescate.

Desviaciones sobre el spec, todas por lo que midio la implementacion:

- **El corte para confiar en `langdetect` es 20 caracteres, no 10.** Con frases
  de 25 caracteres o mas acierta siempre, pero en frases cortas falla con
  probabilidad alta: "ok perfecto" -> `en` (1.00), "merci beaucoup" -> `ro`,
  "d'accord" -> `it`, "alles klar" -> `et`. Por debajo del corte no se decide
  aqui: se pasa al fallback (un LLM), y si tampoco decide se usa el idioma del
  tenant sin guardar nada, para que el mensaje siguiente pueda volver a intentarlo.
- **Los mensajes de handoff se traducen por motivo** (`handoff_<motivo>`), no con
  un unico `handoff_message`: el nodo ya elegia el texto segun el motivo y un
  mensaje generico perderia esa distincion. `handoff_message` queda como rescate.
- **El espanol que ya enviaba el sistema no cambia** (tuteo: "te transfiero").
  El del spec usa usted; cual de los dos es la voz de la marca es una decision
  de producto, asi que los nodos conservan sus textos en espanol y este servicio
  solo aporta los demas idiomas. Los `handoff_*` en `es` son los de
  `human_handoff.py`, que ahora los lee de aqui para no tener dos copias.
- **`budget_exceeded`, `service_suspended`, `out_of_hours` y `csat_prompt` estan
  traducidos pero ningun flujo los envia todavia:** no existe un mensaje de
  fuera de horario, el aviso de suspension, ni el CSAT usa esta plantilla (su
  texto lleva saludo, enlaces de email y el nombre del contacto).
"""

import logging

from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"es", "en", "pt", "it", "de", "fr"})
DEFAULT_LANGUAGE = "es"

#: Nombre de cada idioma, en espanol (el idioma de los prompts del sistema).
LANGUAGE_NAMES: dict[str, str] = {
    "es": "espanol",
    "en": "ingles",
    "pt": "portugues",
    "it": "italiano",
    "de": "aleman",
    "fr": "frances",
}

#: Menos caracteres que esto y `langdetect` no es fiable (ver el docstring del modulo).
MIN_CHARS_LANGDETECT = 20

#: Probabilidad minima para aceptar lo que dice `langdetect` en un texto largo.
MIN_PROBABILIDAD = 0.90

# `langdetect` es no determinista por defecto: la misma frase puede dar idiomas
# distintos entre ejecuciones. Con semilla fija, un mensaje siempre da lo mismo.
DetectorFactory.seed = 0

SYSTEM_MESSAGES: dict[str, dict[str, str]] = {
    "es": {
        "budget_exceeded": "Su presupuesto de tokens se ha agotado. Contacte a su administrador.",
        "service_suspended": "El servicio ha sido suspendido. Contacte al administrador.",
        "handoff_message": "Le transfiero con un agente humano. Por favor espere un momento.",
        "out_of_hours": "Estamos fuera de horario de atención. Nuestro horario es {hours}.",
        "welcome": ("¡Hola! Soy el asistente virtual de {business_name}. ¿En qué puedo ayudarle?"),
        "farewell": ("¡Gracias por contactarnos! Si necesita algo más, no dude en escribirnos."),
        "csat_prompt": "¿Cómo calificaría su experiencia? (1-5 estrellas)",
        "fallback": "Disculpa, no entendi tu mensaje. Podrias reformularlo?",
        "handoff_insufficient_context": (
            "No tengo suficiente informacion para responder tu consulta. Te estoy "
            "transfiriendo con un agente humano que podra ayudarte mejor."
        ),
        "handoff_budget_exceeded": (
            "Te estoy transfiriendo con un agente humano para atenderte personalmente."
        ),
        "handoff_human_request": "Entendido, te transfiero con un agente humano ahora mismo.",
        "handoff_complaint": (
            "Lamento la situacion. Te transfiero con un agente especializado para resolver tu caso."
        ),
        "handoff_transcription_failed": (
            "No pude escuchar tu audio. Te comunico con una persona del equipo para que te ayude."
        ),
        "handoff_negative_sentiment": (
            "Siento mucho la molestia. Te comunico con una persona del equipo para que "
            "revise tu caso directamente."
        ),
        "handoff_scheduling_unavailable": (
            "Hubo un problema al gestionar tu cita. Te transfiero con un agente humano "
            "para ayudarte a agendarla."
        ),
    },
    "en": {
        "budget_exceeded": "Your token budget has been exceeded. Please contact your administrator.",
        "service_suspended": "The service has been suspended. Please contact the administrator.",
        "handoff_message": "I'm transferring you to a human agent. Please wait a moment.",
        "out_of_hours": "We are outside business hours. Our schedule is {hours}.",
        "welcome": "Hello! I'm the virtual assistant for {business_name}. How can I help you?",
        "farewell": (
            "Thank you for contacting us! If you need anything else, don't hesitate to write."
        ),
        "csat_prompt": "How would you rate your experience? (1-5 stars)",
        "fallback": "Sorry, I didn't understand your message. Could you rephrase it?",
        "handoff_insufficient_context": (
            "I don't have enough information to answer your question. I'm transferring "
            "you to a human agent who can help you better."
        ),
        "handoff_budget_exceeded": "I'm transferring you to a human agent to assist you personally.",
        "handoff_human_request": "Understood, I'm transferring you to a human agent right now.",
        "handoff_complaint": (
            "I'm sorry about the situation. I'm transferring you to a specialized agent "
            "to resolve your case."
        ),
        "handoff_transcription_failed": (
            "I couldn't hear your audio. I'm connecting you with a member of the team "
            "who will help you."
        ),
        "handoff_negative_sentiment": (
            "I'm very sorry for the trouble. I'm connecting you with a member of the team "
            "to review your case directly."
        ),
        "handoff_scheduling_unavailable": (
            "There was a problem managing your appointment. I'm transferring you to a "
            "human agent to help you book it."
        ),
    },
    "pt": {
        "budget_exceeded": (
            "Seu orçamento de tokens foi excedido. Entre em contato com o administrador."
        ),
        "service_suspended": "O serviço foi suspenso. Entre em contato com o administrador.",
        "handoff_message": "Estou transferindo você para um agente humano. Aguarde um momento.",
        "out_of_hours": "Estamos fora do horário de atendimento. Nosso horário é {hours}.",
        "welcome": "Olá! Sou o assistente virtual de {business_name}. Como posso ajudá-lo?",
        "farewell": (
            "Obrigado por nos contatar! Se precisar de mais alguma coisa, não hesite em escrever."
        ),
        "csat_prompt": "Como você avaliaria sua experiência? (1-5 estrelas)",
        "fallback": "Desculpe, não entendi sua mensagem. Poderia reformulá-la?",
        "handoff_insufficient_context": (
            "Não tenho informações suficientes para responder sua consulta. Estou "
            "transferindo você para um agente humano que poderá ajudá-lo melhor."
        ),
        "handoff_budget_exceeded": (
            "Estou transferindo você para um agente humano para atendê-lo pessoalmente."
        ),
        "handoff_human_request": "Entendido, estou transferindo você para um agente humano agora.",
        "handoff_complaint": (
            "Lamento a situação. Estou transferindo você para um agente especializado "
            "para resolver o seu caso."
        ),
        "handoff_transcription_failed": (
            "Não consegui ouvir o seu áudio. Estou conectando você com uma pessoa da "
            "equipe para ajudá-lo."
        ),
        "handoff_negative_sentiment": (
            "Sinto muito pelo transtorno. Estou conectando você com uma pessoa da equipe "
            "para revisar o seu caso diretamente."
        ),
        "handoff_scheduling_unavailable": (
            "Houve um problema ao gerenciar o seu agendamento. Estou transferindo você "
            "para um agente humano para ajudá-lo a agendar."
        ),
    },
    "it": {
        "budget_exceeded": "Il budget di token è stato superato. Contattare l'amministratore.",
        "service_suspended": "Il servizio è stato sospeso. Contattare l'amministratore.",
        "handoff_message": "La trasferisco a un agente umano. Attenda un momento.",
        "out_of_hours": "Siamo fuori dall'orario di lavoro. Il nostro orario è {hours}.",
        "welcome": "Ciao! Sono l'assistente virtuale di {business_name}. Come posso aiutarla?",
        "farewell": (
            "Grazie per averci contattato! Se ha bisogno di altro, non esiti a scriverci."
        ),
        "csat_prompt": "Come valuterebbe la sua esperienza? (1-5 stelle)",
        "fallback": "Mi scusi, non ho capito il suo messaggio. Potrebbe riformularlo?",
        "handoff_insufficient_context": (
            "Non ho abbastanza informazioni per rispondere alla sua richiesta. La sto "
            "trasferendo a un agente umano che potrà aiutarla meglio."
        ),
        "handoff_budget_exceeded": (
            "La sto trasferendo a un agente umano per assisterla personalmente."
        ),
        "handoff_human_request": "Capito, la trasferisco subito a un agente umano.",
        "handoff_complaint": (
            "Mi dispiace per la situazione. La trasferisco a un agente specializzato "
            "per risolvere il suo caso."
        ),
        "handoff_transcription_failed": (
            "Non sono riuscito ad ascoltare il suo audio. La metto in contatto con una "
            "persona del team che la aiuterà."
        ),
        "handoff_negative_sentiment": (
            "Mi dispiace molto per il disagio. La metto in contatto con una persona del "
            "team per esaminare direttamente il suo caso."
        ),
        "handoff_scheduling_unavailable": (
            "Si è verificato un problema nella gestione del suo appuntamento. La "
            "trasferisco a un agente umano per aiutarla a prenotarlo."
        ),
    },
    "de": {
        "budget_exceeded": (
            "Ihr Token-Budget wurde überschritten. Bitte kontaktieren Sie Ihren Administrator."
        ),
        "service_suspended": (
            "Der Service wurde ausgesetzt. Bitte kontaktieren Sie den Administrator."
        ),
        "handoff_message": (
            "Ich verbinde Sie mit einem menschlichen Agenten. Bitte warten Sie einen Moment."
        ),
        "out_of_hours": "Wir sind außerhalb der Geschäftszeiten. Unsere Öffnungszeiten sind {hours}.",
        "welcome": (
            "Hallo! Ich bin der virtuelle Assistent von {business_name}. Wie kann ich Ihnen helfen?"
        ),
        "farewell": (
            "Vielen Dank für Ihre Kontaktaufnahme! Wenn Sie weitere Fragen haben, schreiben Sie uns."
        ),
        "csat_prompt": "Wie würden Sie Ihre Erfahrung bewerten? (1-5 Sterne)",
        "fallback": "Entschuldigung, ich habe Ihre Nachricht nicht verstanden. Können Sie sie umformulieren?",
        "handoff_insufficient_context": (
            "Ich habe nicht genügend Informationen, um Ihre Anfrage zu beantworten. Ich "
            "verbinde Sie mit einem menschlichen Agenten, der Ihnen besser helfen kann."
        ),
        "handoff_budget_exceeded": (
            "Ich verbinde Sie mit einem menschlichen Agenten, der Sie persönlich betreut."
        ),
        "handoff_human_request": "Verstanden, ich verbinde Sie sofort mit einem menschlichen Agenten.",
        "handoff_complaint": (
            "Es tut mir leid. Ich verbinde Sie mit einem spezialisierten Agenten, um Ihren "
            "Fall zu lösen."
        ),
        "handoff_transcription_failed": (
            "Ich konnte Ihre Audionachricht nicht hören. Ich verbinde Sie mit einem "
            "Teammitglied, das Ihnen hilft."
        ),
        "handoff_negative_sentiment": (
            "Es tut mir sehr leid für die Unannehmlichkeiten. Ich verbinde Sie mit einem "
            "Teammitglied, das Ihren Fall direkt prüft."
        ),
        "handoff_scheduling_unavailable": (
            "Bei der Verwaltung Ihres Termins ist ein Problem aufgetreten. Ich verbinde "
            "Sie mit einem menschlichen Agenten, der Ihnen bei der Buchung hilft."
        ),
    },
    "fr": {
        "budget_exceeded": (
            "Votre budget de tokens a été dépassé. Veuillez contacter votre administrateur."
        ),
        "service_suspended": "Le service a été suspendu. Veuillez contacter l'administrateur.",
        "handoff_message": "Je vous transfère à un agent humain. Veuillez patienter un instant.",
        "out_of_hours": "Nous sommes en dehors des heures d'ouverture. Nos horaires sont {hours}.",
        "welcome": (
            "Bonjour ! Je suis l'assistant virtuel de {business_name}. Comment puis-je vous aider ?"
        ),
        "farewell": (
            "Merci de nous avoir contactés ! Si vous avez besoin d'autre chose, n'hésitez pas."
        ),
        "csat_prompt": "Comment évalueriez-vous votre expérience ? (1-5 étoiles)",
        "fallback": "Désolé, je n'ai pas compris votre message. Pourriez-vous le reformuler ?",
        "handoff_insufficient_context": (
            "Je n'ai pas assez d'informations pour répondre à votre demande. Je vous "
            "transfère à un agent humain qui pourra mieux vous aider."
        ),
        "handoff_budget_exceeded": (
            "Je vous transfère à un agent humain pour vous aider personnellement."
        ),
        "handoff_human_request": "Compris, je vous transfère à un agent humain tout de suite.",
        "handoff_complaint": (
            "Je suis désolé pour la situation. Je vous transfère à un agent spécialisé "
            "pour résoudre votre cas."
        ),
        "handoff_transcription_failed": (
            "Je n'ai pas pu entendre votre message audio. Je vous mets en relation avec "
            "un membre de l'équipe qui vous aidera."
        ),
        "handoff_negative_sentiment": (
            "Je suis vraiment désolé pour la gêne occasionnée. Je vous mets en relation "
            "avec un membre de l'équipe pour examiner directement votre cas."
        ),
        "handoff_scheduling_unavailable": (
            "Un problème est survenu lors de la gestion de votre rendez-vous. Je vous "
            "transfère à un agent humain pour vous aider à le réserver."
        ),
    },
}


def normalizar_idioma(valor: object) -> str | None:
    """Reduce un codigo de idioma a uno de los seis soportados.

    Acepta variantes regionales y mayusculas (`"pt-BR"`, `"EN"`).

    Args:
        valor: Lo que haya llegado (un codigo ISO 639-1, o cualquier cosa).

    Returns:
        El codigo soportado, o `None` si no lo es.
    """
    if not isinstance(valor, str):
        return None
    base = valor.strip().lower().replace("_", "-").split("-")[0]
    return base if base in SUPPORTED_LANGUAGES else None


def detectar_idioma(texto: str | None) -> str | None:
    """Detecta el idioma de un texto con `langdetect`, solo si es de fiar.

    Devuelve `None`, y no una conjetura, cuando el texto es corto (ver
    `MIN_CHARS_LANGDETECT`), cuando `langdetect` no esta seguro o cuando el
    idioma no es uno de los seis soportados: el llamante decide el rescate.

    Args:
        texto: Mensaje del contacto.

    Returns:
        Uno de `SUPPORTED_LANGUAGES`, o `None` si no se puede afirmar.
    """
    limpio = (texto or "").strip()
    if len(limpio) < MIN_CHARS_LANGDETECT:
        return None
    try:
        mejor = detect_langs(limpio)[0]
    except LangDetectException:
        return None
    if mejor.prob < MIN_PROBABILIDAD:
        return None
    return mejor.lang if mejor.lang in SUPPORTED_LANGUAGES else None


def instruccion_de_idioma(idioma: str | None) -> str:
    """Frase para el prompt de sistema que fija el idioma de la respuesta.

    Args:
        idioma: Codigo de idioma detectado, si se conoce.

    Returns:
        La instruccion (p. ej. `"Responde siempre en ingles (en)."`), o una
        cadena vacia si el idioma no se conoce o no esta soportado.
    """
    codigo = normalizar_idioma(idioma)
    if codigo is None:
        return ""
    return f"Responde siempre en {LANGUAGE_NAMES[codigo]} ({codigo})."


def con_idioma(prompt: str | None, idioma: str | None) -> str:
    """Agrega al prompt de sistema la instruccion de responder en `idioma`.

    Args:
        prompt: Prompt de sistema, o `None`/vacio si no hay uno.
        idioma: Codigo de idioma detectado, si se conoce.

    Returns:
        El prompt tal cual si no hay idioma que fijar; con la instruccion al
        final si lo hay; y solo la instruccion si no habia prompt.
    """
    instruccion = instruccion_de_idioma(idioma)
    base = prompt or ""
    if not instruccion:
        return base
    return f"{base}\n\n{instruccion}" if base else instruccion


def get_system_message(key: str, language: str | None = None, **kwargs: str) -> str:
    """Devuelve un mensaje del sistema en el idioma indicado.

    Un idioma no soportado cae al espanol, y una clave que no existe en el
    idioma cae a su version en espanol; si tampoco esta, a la propia clave.

    Args:
        key: Clave del mensaje (p. ej. `welcome`, `handoff_complaint`).
        language: Codigo de idioma ISO 639-1.
        **kwargs: Variables de la plantilla (p. ej. `business_name`, `hours`).

    Returns:
        El mensaje con las variables ya interpoladas.

    Raises:
        KeyError: Si la plantilla pide una variable que no se paso.
    """
    mensajes = SYSTEM_MESSAGES[normalizar_idioma(language) or DEFAULT_LANGUAGE]
    plantilla = mensajes.get(key, SYSTEM_MESSAGES[DEFAULT_LANGUAGE].get(key, key))
    return plantilla.format(**kwargs) if kwargs else plantilla
