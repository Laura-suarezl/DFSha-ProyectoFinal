"""Prueba de humo del entorno local de desarrollo (deploy/compose/docker-compose.dev.yml).

Desde el equipo anfitrión escribe un bloque de 16 MiB en dn1 con la cadena dn1, dn2 y dn3. Dentro
de la red de Docker, dn1 lo reenvía a dn2.dfsha.internal y dn3.dfsha.internal por sus puertos
internos con TLS mutuo. Luego lee el bloque desde cada nodo y comprueba que los puertos internos
no se alcanzan desde fuera de la red.

Uso:  python deploy/compose/prueba_humo.py
Requiere los certificados de deploy/certs/out (crear_certificados.py --local) y el Compose arriba.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import sys
from pathlib import Path

import grpc

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))

from dfsha._gen import dfsha_pb2 as pb  # noqa: E402
from dfsha._gen import dfsha_pb2_grpc as pb_grpc  # noqa: E402
from dfsha.common.config import Tls  # noqa: E402
from dfsha.common.tls import credenciales_canal  # noqa: E402

CA = RAIZ / "deploy" / "certs" / "out" / "ca.crt"
PUBLICADOS = {"dn1": 51052, "dn2": 51053, "dn3": 51054, "dn4": 51055}   # puertos de clientes en el anfitrión
MIB = 1024 * 1024


def canal(nodo: str) -> grpc.aio.Channel:
    return grpc.aio.secure_channel(f"127.0.0.1:{PUBLICADOS[nodo]}", credenciales_canal(Tls(ca=str(CA))))


def trozos(block_id: str, datos: bytes, cadena_restante: list[str]):
    huella = hashlib.sha256(datos).hexdigest()
    for n, i in enumerate(range(0, len(datos), MIB)):
        t = pb.ChunkPayload(sequence_number=n, data=datos[i:i + MIB], is_last_chunk=i + MIB >= len(datos))
        if n == 0:
            t.block_id, t.block_checksum = block_id, huella
            t.downstream_nodes.extend(cadena_restante)
        yield t


def puerto_abierto(puerto: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", puerto)) == 0


async def main() -> int:
    if not CA.exists():
        print("Faltan los certificados. Ejecuta: python deploy/certs/crear_certificados.py --local")
        return 1
    fallas = []
    for nodo in PUBLICADOS:
        async with canal(nodo) as c:
            await asyncio.wait_for(c.channel_ready(), 30)

    datos = os.urandom(16 * MIB)
    huella = hashlib.sha256(datos).hexdigest()
    block_id = f"b-{int.from_bytes(os.urandom(8), 'big'):016x}"
    async with canal("dn1") as c:
        r = await pb_grpc.DataServiceStub(c).WriteBlock(
            trozos(block_id, datos, ["dn2.dfsha.internal:50054", "dn3.dfsha.internal:50054"]))
    copias = list(r.stored_on)
    print(f"Escritura de 16 MiB por la cadena: copias en {copias}")
    if copias != ["dn1.dfsha.internal", "dn2.dfsha.internal", "dn3.dfsha.internal"]:
        fallas.append("la cadena no guardó las 3 copias")

    for nodo in ("dn1", "dn2", "dn3"):
        async with canal(nodo) as c:
            leido = b"".join([t.data async for t in pb_grpc.DataServiceStub(c).ReadBlock(
                pb.ReadBlockRequest(block_id=block_id))])
        igual = hashlib.sha256(leido).hexdigest() == huella
        print(f"Lectura desde {nodo}: {'idéntica' if igual else 'DISTINTA'}")
        if not igual:
            fallas.append(f"la copia de {nodo} no coincide")

    async with canal("dn4") as c:
        salud = await pb_grpc.DataServiceStub(c).CheckBlockHealth(pb.HealthCheckRequest(block_ids=[block_id]))
    print(f"dn4, que no estaba en la cadena, tiene el bloque: {salud.blocks[0].present}")
    if salud.blocks[0].present:
        fallas.append("dn4 no debía tener el bloque")

    for interno in (50054, 50053):
        abierto = puerto_abierto(interno)
        print(f"Puerto interno {interno} alcanzable desde el anfitrión: {abierto}")
        if abierto:
            fallas.append(f"el puerto interno {interno} no debería estar publicado")

    print("\nRESULTADO:", "correcto" if not fallas else "FALLAS: " + ", ".join(fallas))
    return 0 if not fallas else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
