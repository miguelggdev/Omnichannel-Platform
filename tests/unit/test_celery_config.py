"""Coherencia de `celery_config.py` con las tareas que de verdad existen."""

import importlib

from app.tasks.celery_app import TASK_MODULES, celery_app


class TestBeatSchedule:
    """Cada entrada periodica tiene que apuntar a una tarea registrada."""

    def test_toda_entrada_apunta_a_una_tarea_registrada(self) -> None:
        """Beat publica el mensaje aunque la tarea no exista.

        Un worker que no la conoce lo rechaza como "unregistered task" en cada
        ciclo, indefinidamente, y la funcionalidad parece activa sin correr
        nunca. Es el mismo defecto que ya tuvo `auto-close-conversations` en
        Sprint 7 (nombre distinto al de la tarea) y que Sprint 2 dejo en tres
        entradas de features que todavia no existen.
        """
        for modulo in TASK_MODULES:
            importlib.import_module(modulo)

        registradas = set(celery_app.tasks)
        huerfanas = {
            nombre: entrada["task"]
            for nombre, entrada in celery_app.conf.beat_schedule.items()
            if entrada["task"] not in registradas
        }

        assert not huerfanas, f"entradas de beat_schedule sin tarea registrada: {huerfanas}"

    def test_la_cola_de_cada_entrada_existe(self) -> None:
        """La cola de `options` tiene que ser una que algun worker consuma."""
        colas = {q.name for q in celery_app.conf.task_queues}
        for nombre, entrada in celery_app.conf.beat_schedule.items():
            cola = entrada.get("options", {}).get("queue")
            assert cola in colas, f"{nombre}: la cola {cola!r} no esta declarada"
