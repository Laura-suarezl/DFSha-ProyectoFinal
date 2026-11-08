"""Escenarios obligatorios 1 a 4 y operaciones RF1 a RF3 sobre el entorno local (docker-compose.dev.yml).

Usa la biblioteca del cliente desde el equipo anfitrión. El cliente solo conoce el nombre lógico
del clúster (aquí los 3 puertos publicados de los nodos de metadatos) y descubre al líder solo.
Las fallas se inyectan deteniendo contenedores.

Uso:  python deploy/compose/escenarios.py     (con el Compose arriba y la prueba de humo correcta)
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))

from dfsha._gen import dfsha_pb2 as pb  # noqa: E402
from dfsha.client.cliente import Cliente, ErrorDfsha  # noqa: E402
from dfsha.common.config import Tls, cargar  # noqa: E402
from dfsha.common.tls import credenciales_canal  # noqa: E402

COMPOSE = ["docker", "compose", "-f", str(RAIZ / "deploy" / "compose" / "docker-compose.dev.yml")]
CA = RAIZ / "deploy" / "certs" / "out" / "ca.crt"
MIB = 1024 * 1024
DIRECCIONES = {**{f"mds{i}.dfsha.internal:50051": f"127.0.0.1:{51040 + i}" for i in (1, 2, 3)},
               **{f"dn{i}.dfsha.internal:50052": f"127.0.0.1:{51051 + i}" for i in (1, 2, 3, 4)}}
resultados: list[tuple[str, bool]] = []


def docker(*args) -> None:
    subprocess.run(COMPOSE + list(args), check=True, capture_output=True)


def comprobar(nombre: str, cumple: bool, detalle: str = "") -> None:
    resultados.append((nombre, cumple))
    print(f"[{'ok' if cumple else 'FALLA'}] {nombre}{': ' + detalle if detalle else ''}")


def cliente() -> Cliente:
    return Cliente(cargar(entorno={}), credenciales_canal(Tls(ca=str(CA))),
                   destino_cluster="ipv4:127.0.0.1:51041,127.0.0.1:51042,127.0.0.1:51043", direcciones=DIRECCIONES)


def h(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()[:12]


async def esperado(c: Cliente, ruta: str, datos: bytes) -> bool:
    return await c.get(ruta) == datos


async def main() -> int:
    admin, clave_admin = f"esc{secrets.token_hex(3)}", secrets.token_urlsafe(16)
    subprocess.run(COMPOSE + ["exec", "-T", "-e", f"DFSHA_CONTRASENA={clave_admin}", "mds1", "python", "-m",
                              "dfsha.metadata", "crear-admin", "--usuario", admin], check=True, capture_output=True)
    adm = cliente()
    await adm.login(admin, clave_admin)
    ana, clave = f"ana{secrets.token_hex(3)}", secrets.token_urlsafe(16)
    await adm.llamar("admin", "CreateUser", pb.CreateUserRequest(username=ana, password=clave, quota_bytes=10 ** 10))
    a, b = cliente(), cliente()
    await a.login(ana, clave)
    await b.login(ana, clave)

    # ------------------------------------------------------------ RF1 y RF2
    datos = os.urandom(20 * MIB + 123)
    await a.mkdir("videos")
    v = await a.put(datos, "videos/viaje.mp4")
    comprobar("RF2 put y get de 20 MiB (2 bloques)", await esperado(a, "videos/viaje.mp4", datos), f"versión {v.version}")
    await a.mv("videos/viaje.mp4", "videos/paseo.mp4")
    nombres = [x.name for x in await a.ls("videos")]
    comprobar("RF1 mkdir, mv y ls", nombres == ["paseo.mp4"], str(nombres))
    try:
        await b.ls(f"/{admin}")
        ajeno = "permitido"
    except ErrorDfsha as e:
        ajeno = e.motivo
    comprobar("RF1 otro espacio de usuario", ajeno == "FORBIDDEN", ajeno)

    # ------------------------------------------------------------ RF3
    f = await a.open("videos/paseo.mp4", "rw")
    tramo = await f.read(16 * MIB - 10, 20)
    comprobar("RF3 read de un rango que cruza dos bloques", tramo == datos[16 * MIB - 10:16 * MIB + 10])
    await f.write(18 * MIB, b"X" * 100)
    await f.close()
    datos = datos[:18 * MIB] + b"X" * 100 + datos[18 * MIB + 100:]
    comprobar("RF3 write de 100 bytes en el bloque 1 y close", await esperado(a, "videos/paseo.mp4", datos),
              f"versión {f.version}")
    f = await a.open("videos/paseo.mp4", "a")
    await f.append(b"fin" * 1000)
    await f.close()
    datos += b"fin" * 1000
    comprobar("RF3 append", await esperado(a, "videos/paseo.mp4", datos))
    f = await a.open("videos/paseo.mp4", "rw")
    try:
        await f.write(len(datos) + 10, b"hueco")
        hueco = "aceptado"
    except ErrorDfsha as e:
        hueco = e.motivo
    await f.close()
    comprobar("RF3 escribir más allá del final", hueco == "BAD_OFFSET", hueco)

    # ------------------------------------------------------------ escenario 1
    fa, fb = await a.open("videos/paseo.mp4", "rw"), await b.open("videos/paseo.mp4", "rw")
    await fa.write(0, b"A" * 10)                                       # A toma el lock del bloque 0
    try:
        await fb.lock([0], espera_s=2)
        bloqueado = "concedido"
    except ErrorDfsha as e:
        bloqueado = e.motivo
    await fa.close()                                                   # publica y libera
    await fb.write(0, b"B" * 5)                                        # ahora B obtiene el lock
    try:
        await fb.close()
        conflicto = "aceptado"
    except ErrorDfsha as e:
        conflicto = e.motivo
    fb = await b.open("videos/paseo.mp4", "rw")                        # B vuelve a abrir y repite
    await fb.write(0, b"B" * 5)
    await fb.close()
    datos = b"B" * 5 + b"A" * 5 + datos[10:]
    comprobar("Escenario 1: escrituras concurrentes al mismo bloque",
              bloqueado == "LOCKED" and conflicto == "VERSION_CONFLICT" and await esperado(a, "videos/paseo.mp4", datos),
              f"B esperó ({bloqueado}), su base vieja dio {conflicto}, al reabrir se aplicaron ambos cambios")

    # ------------------------------------------------------------ escenario 2
    docker("stop", "dn3")
    otro = os.urandom(32 * MIB)
    await a.put(otro, "e2.bin")
    abierto = await a.open("e2.bin", "r")
    copias = [len(x.replicas) for x in abierto.bloques.values()]
    await abierto.close()
    comprobar("Escenario 2: réplica desconectada durante la escritura", await esperado(a, "e2.bin", otro),
              f"copias por bloque {copias}, W = 2")
    docker("start", "dn3")

    # ------------------------------------------------------------ escenario 3
    grande = os.urandom(48 * MIB)
    subida = asyncio.create_task(a.put(grande, "e3.bin"))
    await asyncio.sleep(0.4)
    docker("kill", "dn2")
    try:
        await subida
        ok = await esperado(a, "e3.bin", grande)
    except ErrorDfsha as e:
        ok = False
        print("   ", e)
    docker("start", "dn2")
    await asyncio.sleep(3)
    registro = subprocess.run(COMPOSE + ["logs", "--tail", "20", "dn2"], capture_output=True, text=True).stdout
    limpieza = [linea.split("escuchando")[1].split(",")[-1].strip() for linea in registro.splitlines()
                if "temporales borrados" in linea][-1:]
    comprobar("Escenario 3: nodo de datos cae a mitad de un bloque", ok,
              f"archivo completo e íntegro, al reiniciar dn2: {limpieza[0] if limpieza else 'sin registro'}")

    # ------------------------------------------------------------ escenario 4
    antes = a.lider
    docker("stop", antes.split(".")[0])
    inicio = time.monotonic()
    ok = await esperado(a, "videos/paseo.mp4", datos)
    comprobar("Escenario 4: el cliente no conoce las ubicaciones y el líder cae", ok,
              f"líder {antes.split('.')[0]} -> {a.lider.split('.')[0]}, get en {time.monotonic() - inicio:.1f} s")
    docker("start", antes.split(".")[0])

    for c in (a, b, adm):
        await c.cerrar()
    fallas = [n for n, cumple in resultados if not cumple]
    print("\nRESULTADO:", "correcto" if not fallas else f"FALLAS en {fallas}")
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
