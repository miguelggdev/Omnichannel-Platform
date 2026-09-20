"""Lo que `docker-compose.yml` publica hacia fuera del host."""

from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"

# Servicios de observabilidad sin autenticacion propia (Jaeger, Prometheus) o
# con credenciales por defecto (Grafana), cuyas series/trazas llevan `client_id`.
OBSERVABILIDAD = ("jaeger", "prometheus", "grafana")

# Puertos que SI pueden salir a todas las interfaces: el gateway publico.
PUBLICOS = {("traefik", "80:80"), ("traefik", "443:443")}


def _servicios() -> dict[str, Any]:
    """Carga los servicios del compose.

    Returns:
        Diccionario nombre -> definicion.
    """
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


class TestPuertosPublicados:
    """Un `"9090:9090"` publica en 0.0.0.0, no en localhost."""

    @pytest.mark.parametrize("servicio", OBSERVABILIDAD)
    def test_la_observabilidad_solo_escucha_en_localhost(self, servicio: str) -> None:
        """Sin el prefijo `127.0.0.1:` cualquiera que llegue a la IP del host entra.

        Los comentarios del compose decian "solo localhost" mientras la sintaxis
        publicaba en todas las interfaces: Jaeger y Prometheus no autentican, y
        las series de Prometheus llevan `client_id` (actividad y costos por
        tenant).
        """
        puertos = _servicios()[servicio].get("ports", [])

        assert puertos, f"{servicio} ya no publica ningun puerto: revisar este test"
        for mapeo in puertos:
            assert str(mapeo).startswith("127.0.0.1:"), (
                f"{servicio} publica {mapeo!r} en todas las interfaces"
            )

    def test_solo_el_gateway_publica_a_todas_las_interfaces(self) -> None:
        """Cualquier otro puerto publicado tiene que ir a `127.0.0.1`.

        Cubre lo que la lista de observabilidad no nombra: Redis (con
        `requirepass`, pero un Redis abierto a internet es blanco de fuerza
        bruta) y el dashboard de Traefik (`api.insecure: true`, sin
        autenticacion). Un servicio nuevo con `ports:` mal escrito falla aqui.
        """
        expuestos = [
            (nombre, str(mapeo))
            for nombre, servicio in _servicios().items()
            for mapeo in servicio.get("ports", [])
            if not str(mapeo).startswith("127.0.0.1:") and (nombre, str(mapeo)) not in PUBLICOS
        ]

        assert not expuestos, f"puertos publicados en todas las interfaces: {expuestos}"
