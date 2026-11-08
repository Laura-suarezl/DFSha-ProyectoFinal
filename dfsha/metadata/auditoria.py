"""Registro de auditoría (D8, docs/etcd-esquema.md).

Cada evento (inicio de sesión, acceso denegado, acción de administración) se guarda en etcd en
/dfsha/audit/<fecha>-<aleatorio> con la fecha, la IP, el usuario, la operación y el resultado.
Los registros cuelgan de un lease de tiempos.auditoria_ttl_s (7 días), así que se borran solos.
Para no crear un lease por evento, se reutiliza uno durante una hora.

Registrar nunca hace fallar la operación auditada: si etcd no responde, el evento va al log.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone

from dfsha.metadata.etcd import ClienteEtcd

log = logging.getLogger(__name__)
_REUSO_LEASE_S = 3600


class Auditoria:
    def __init__(self, cli: ClienteEtcd, prefijo: str, ttl_s: int):
        self.cli = cli
        self.prefijo = f"{prefijo}audit/"
        self.ttl_s = ttl_s
        self._lease: tuple[int, float] | None = None

    async def registrar(self, operacion: str, usuario: str, ip: str, resultado: str) -> None:
        ahora = datetime.now(timezone.utc)
        evento = {"fecha": ahora.isoformat(timespec="milliseconds"), "ip": ip, "usuario": usuario,
                  "operacion": operacion, "resultado": resultado}
        try:
            if self._lease is None or time.monotonic() - self._lease[1] > _REUSO_LEASE_S:
                self._lease = (await self.cli.conceder_lease(self.ttl_s), time.monotonic())
            clave = f"{self.prefijo}{ahora.strftime('%Y%m%dT%H%M%S.%f')}-{uuid.uuid4().hex[:6]}"
            await self.cli.poner(clave, json.dumps(evento), lease=self._lease[0])
        except Exception as e:                       # la auditoría no debe tumbar la operación
            log.warning("no se pudo registrar en auditoría %s: %s", evento, e)


def ip_de(peer: str) -> str:
    """IP del otro extremo a partir de context.peer(), por ejemplo "ipv4:10.0.1.5:51234"."""
    if peer.startswith("ipv4:"):
        return peer[5:].rsplit(":", 1)[0]
    if peer.startswith("ipv6:"):
        return peer[5:].rsplit(":", 1)[0].strip("[]")
    return peer
