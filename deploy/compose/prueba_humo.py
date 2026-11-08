"""Prueba de humo del entorno local de desarrollo (deploy/compose/docker-compose.dev.yml).

Nodos de metadatos. Crea un administrador de prueba con contraseña aleatoria (crear-admin dentro
de mds1), busca al líder siguiendo las respuestas NOT_LEADER, inicia sesión, crea un usuario,
comprueba que ese usuario no puede administrar y renueva la sesión.

Nodos de datos. Desde el equipo anfitrión escribe un bloque de 16 MiB en dn1 con la cadena dn1,
dn2 y dn3. Dentro de la red de Docker, dn1 lo reenvía a dn2.dfsha.internal y dn3.dfsha.internal
por sus puertos internos con TLS mutuo. Luego lee el bloque desde cada nodo y comprueba que los
puertos internos no se alcanzan desde fuera de la red.

Uso:  python deploy/compose/prueba_humo.py
Requiere los certificados de deploy/certs/out (crear_certificados.py --local) y el Compose arriba.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import grpc
from google.protobuf import empty_pb2

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))

from dfsha._gen import dfsha_pb2 as pb  # noqa: E402
from dfsha._gen import dfsha_pb2_grpc as pb_grpc  # noqa: E402
from dfsha.common.config import Tls  # noqa: E402
from dfsha.common.errores import detalle_de, motivo_de  # noqa: E402
from dfsha.common.tls import credenciales_canal  # noqa: E402

CA = RAIZ / "deploy" / "certs" / "out" / "ca.crt"
COMPOSE = RAIZ / "deploy" / "compose" / "docker-compose.dev.yml"
PUBLICADOS = {"mds1": 51041, "mds2": 51042, "mds3": 51043,             # puertos de clientes en el anfitrión
              "dn1": 51052, "dn2": 51053, "dn3": 51054, "dn4": 51055}
MIB = 1024 * 1024
GIB = 1024 * MIB


def canal(nodo: str) -> grpc.aio.Channel:
    return grpc.aio.secure_channel(f"127.0.0.1:{PUBLICADOS[nodo]}", credenciales_canal(Tls(ca=str(CA))))


def crear_admin(usuario: str, contrasena: str) -> None:
    subprocess.run(["docker", "compose", "-f", str(COMPOSE), "exec", "-T", "-e", f"DFSHA_CONTRASENA={contrasena}",
                    "mds1", "python", "-m", "dfsha.metadata", "crear-admin", "--usuario", usuario],
                   check=True, capture_output=True)


# Mientras no existe AllocateBlocks, los tokens de acceso se piden a mds1, que tiene la clave HMAC
_TOKENS = """
import asyncio, sys
from dfsha.common import tokens
from dfsha.common.config import cargar
from dfsha.metadata.__main__ import cliente_etcd
from dfsha.metadata.claves import cargar_secretos, envoltorio_desde
async def main():
    cfg = cargar()
    async with cliente_etcd(cfg) as cli:
        s = await cargar_secretos(cli, envoltorio_desde(cfg.kms), cfg.etcd.prefijo)
    for op in (tokens.ESCRIBIR, tokens.LEER):
        print(tokens.emitir(s.hmac_bloques, sys.argv[1], op, "humo", 600).decode())
asyncio.run(main())
"""


def tokens_de_prueba(block_id: str) -> tuple[bytes, bytes]:
    r = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "exec", "-T", "mds1", "python", "-c", _TOKENS, block_id],
                       check=True, capture_output=True, text=True)
    escritura, lectura = r.stdout.split()
    return escritura.encode(), lectura.encode()


async def buscar_lider(usuario: str, contrasena: str, espera_s: float = 60) -> tuple[str, pb.LoginResponse]:
    """Inicia sesión en mds1 y sigue la respuesta NOT_LEADER hasta el líder.

    Si un nodo no responde (por ejemplo, está detenido), prueba con el siguiente.
    """
    candidatos = ["mds1", "mds2", "mds3"]
    nodo, limite = "mds1", time.monotonic() + espera_s
    while True:
        try:
            async with canal(nodo) as c:
                r = await pb_grpc.MetadataServiceStub(c).Login(pb.LoginRequest(username=usuario, password=contrasena))
            return nodo, r
        except grpc.aio.AioRpcError as e:
            if motivo_de(e) == "NOT_LEADER":
                nodo = detalle_de(e).leader.split(".", 1)[0]          # mds2.dfsha.internal es mds2
                continue
            if time.monotonic() > limite:
                raise
            nodo = candidatos[(candidatos.index(nodo) + 1) % 3]        # caído o aún arrancando
            await asyncio.sleep(1)


async def probar_metadatos(fallas: list[str]) -> None:
    admin, contrasena = f"humo{secrets.token_hex(3)}", secrets.token_urlsafe(16)
    crear_admin(admin, contrasena)
    lider, sesion = await buscar_lider(admin, contrasena)
    print(f"Líder de metadatos: {lider}, sesión {sesion.session_id} iniciada por {admin}")
    token = (("authorization", f"Bearer {sesion.access_token}"),)

    async with canal(lider) as c:
        meta, adm = pb_grpc.MetadataServiceStub(c), pb_grpc.AdminServiceStub(c)
        usuario, clave = f"ana{secrets.token_hex(3)}", secrets.token_urlsafe(16)
        await adm.CreateUser(pb.CreateUserRequest(username=usuario, password=clave, quota_bytes=GIB), metadata=token)
        r = await meta.Login(pb.LoginRequest(username=usuario, password=clave))
        print(f"Usuario {usuario} creado e inicia sesión: {bool(r.access_token)}")
        try:
            await adm.CreateUser(pb.CreateUserRequest(username="otro", password=clave),
                                 metadata=(("authorization", f"Bearer {r.access_token}"),))
            motivo = "permitido"
        except grpc.aio.AioRpcError as e:
            motivo = motivo_de(e)
        print(f"Un usuario sin rol administrador intenta crear usuarios: {motivo}")
        if motivo != "FORBIDDEN":
            fallas.append("un usuario sin rol administrador pudo administrar")
        renovada = await meta.Renew(pb.RenewRequest(session_id=sesion.session_id), metadata=token)
        print(f"Renew de la sesión: le quedan {renovada.ttl_s} s")
        if renovada.ttl_s <= 0:
            fallas.append("la sesión no se renovó")
        nodos = await esperar_nodos(adm, token)
        print("Nodos de datos vistos por el líder:", ", ".join(f"{n.name.split('.')[0]} {pb.NodeState.Name(n.state)[11:]}"
                                                               for n in nodos.nodes))
        if [n.state for n in nodos.nodes] != [pb.NODE_STATE_ALIVE] * 4:
            fallas.append("el líder no ve los 4 nodos de datos vivos")
    await comprobar_seguidores(lider, admin, contrasena, fallas)
    return lider, token


async def esperar_nodos(adm, token, espera_s: float = 20) -> pb.NodeList:
    """ListNodes hasta ver los 4 nodos de datos (se registran unos segundos después de arrancar)."""
    limite = time.monotonic() + espera_s
    while True:
        nodos = await adm.ListNodes(empty_pb2.Empty(), metadata=token)
        if len(nodos.nodes) == 4 or time.monotonic() > limite:
            return nodos
        await asyncio.sleep(1)


async def comprobar_seguidores(lider: str, admin: str, contrasena: str, fallas: list[str]) -> None:
    for seguidor in sorted({"mds1", "mds2", "mds3"} - {lider}):
        async with canal(seguidor) as c:
            try:
                await pb_grpc.MetadataServiceStub(c).Login(pb.LoginRequest(username=admin, password=contrasena))
                respuesta = "atendió el Login"
            except grpc.aio.AioRpcError as e:
                respuesta = f"{motivo_de(e)} {detalle_de(e).leader if detalle_de(e) else ''}".strip()
        print(f"{seguidor} responde: {respuesta}")
        if respuesta != f"NOT_LEADER {lider}.dfsha.internal":
            fallas.append(f"{seguidor} no remitió al líder")


def trozos(block_id: str, datos: bytes, cadena_restante: list[str], token: bytes = b""):
    huella = hashlib.sha256(datos).hexdigest()
    for n, i in enumerate(range(0, len(datos), MIB)):
        t = pb.ChunkPayload(sequence_number=n, data=datos[i:i + MIB], is_last_chunk=i + MIB >= len(datos))
        if n == 0:
            t.block_id, t.block_checksum, t.access_token = block_id, huella, token
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

    lider, token_admin = await probar_metadatos(fallas)

    datos = os.urandom(16 * MIB)
    huella = hashlib.sha256(datos).hexdigest()
    block_id = f"b-{int.from_bytes(os.urandom(8), 'big'):016x}"
    escritura, lectura = tokens_de_prueba(block_id)
    async with canal("dn1") as c:
        try:
            await pb_grpc.DataServiceStub(c).WriteBlock(trozos(block_id, datos, []))
            sin_token = "aceptada"
        except grpc.aio.AioRpcError as e:
            sin_token = motivo_de(e)
        print(f"Escritura sin token de acceso: {sin_token}")
        if sin_token != "TOKEN_INVALID":
            fallas.append("un nodo de datos aceptó una escritura sin token")
        r = await pb_grpc.DataServiceStub(c).WriteBlock(
            trozos(block_id, datos, ["dn2.dfsha.internal:50054", "dn3.dfsha.internal:50054"], escritura))
    copias = list(r.stored_on)
    print(f"Escritura de 16 MiB por la cadena: copias en {copias}")
    if copias != ["dn1.dfsha.internal", "dn2.dfsha.internal", "dn3.dfsha.internal"]:
        fallas.append("la cadena no guardó las 3 copias")

    for nodo in ("dn1", "dn2", "dn3"):
        async with canal(nodo) as c:
            leido = b"".join([t.data async for t in pb_grpc.DataServiceStub(c).ReadBlock(
                pb.ReadBlockRequest(block_id=block_id, access_token=lectura))])
        igual = hashlib.sha256(leido).hexdigest() == huella
        print(f"Lectura desde {nodo}: {'idéntica' if igual else 'DISTINTA'}")
        if not igual:
            fallas.append(f"la copia de {nodo} no coincide")

    async with canal(lider) as c:                                      # los avisos llegan al líder
        await asyncio.sleep(0.5)
        nodos = await pb_grpc.AdminServiceStub(c).ListNodes(empty_pb2.Empty(), metadata=token_admin)
    con_bloque = sorted(n.name.split(".")[0] for n in nodos.nodes if n.block_count)
    print(f"El líder sabe que el bloque está en: {con_bloque}")
    if con_bloque != ["dn1", "dn2", "dn3"]:
        fallas.append("el líder no recibió los avisos BlockReceived")

    async with canal("dn4") as c:
        salud = await pb_grpc.DataServiceStub(c).CheckBlockHealth(pb.HealthCheckRequest(block_ids=[block_id]))
    print(f"dn4, que no estaba en la cadena, tiene el bloque: {salud.blocks[0].present}")
    if salud.blocks[0].present:
        fallas.append("dn4 no debía tener el bloque")

    for interno in (50054, 50053, 2379):
        abierto = puerto_abierto(interno)
        print(f"Puerto interno {interno} alcanzable desde el anfitrión: {abierto}")
        if abierto:
            fallas.append(f"el puerto interno {interno} no debería estar publicado")

    print("\nRESULTADO:", "correcto" if not fallas else "FALLAS: " + ", ".join(fallas))
    return 0 if not fallas else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
