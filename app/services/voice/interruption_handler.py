"""Deteccion de interrupciones (barge-in) del canal de voz (Sprint 13, Dev A).

Cuando el cliente empieza a hablar mientras el agente todavia esta
respondiendo, hay que callar al agente: nadie espera a que una maquina termine
un parrafo para poder decir "no, eso no es lo que pregunte".

Deteccion por energia (RMS), como propone el spec, con dos ajustes:

- **Una instancia por llamada.** El spec comparte un solo detector en el
  `CallManager` para todas las llamadas del proceso: la cuenta de tramas con
  voz de una llamada se sumaba a la de otra, y una podia interrumpir al agente
  de la otra.
- **La duracion se mide en milisegundos de audio, no en tramas.** El spec
  cuenta 3 tramas "consecutivas" sin decir de que tamano; Twilio manda tramas
  de 20 ms, asi que 3 son 60 ms, menos que una silaba.

El umbral es mas alto que el de la deteccion de voz normal
(`VOICE_BARGE_IN_RMS_THRESHOLD` > `VOICE_SPEECH_RMS_THRESHOLD`): mientras el
agente habla, parte de su propia voz vuelve por la linea como eco, y ese eco
no puede cortarlo. Para produccion con mucho ruido de fondo, un VAD entrenado
(Silero) seria mejor; queda anotado en el ADR-071.
"""

from app.services.voice.audio import SAMPLE_RATE, rms_pcm16


class InterruptionHandler:
    """Detector de interrupciones de una llamada.

    Attributes:
        threshold_rms: Energia minima para considerar que el cliente habla.
        min_speech_ms: Voz sostenida necesaria para confirmar la interrupcion.
    """

    def __init__(self, threshold_rms: int, min_speech_ms: int) -> None:
        """Crea el detector.

        Args:
            threshold_rms: Energia minima de voz.
            min_speech_ms: Milisegundos seguidos de voz que confirman la interrupcion.
        """
        self.threshold_rms = threshold_rms
        self.min_speech_ms = min_speech_ms
        self._voz_ms = 0.0

    def detect(self, pcm16: bytes) -> bool:
        """Procesa una trama y dice si el cliente interrumpio.

        Args:
            pcm16: Trama de audio del cliente, PCM de 16 bits a 8 kHz.

        Returns:
            True una sola vez por interrupcion, al confirmarse; despues vuelve a
            contar desde cero.
        """
        if rms_pcm16(pcm16) < self.threshold_rms:
            self._voz_ms = 0.0
            return False
        self._voz_ms += len(pcm16) / 2 / SAMPLE_RATE * 1000
        if self._voz_ms >= self.min_speech_ms:
            self._voz_ms = 0.0
            return True
        return False

    def reset(self) -> None:
        """Olvida la voz acumulada (el agente dejo de hablar)."""
        self._voz_ms = 0.0
