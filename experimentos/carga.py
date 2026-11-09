"""Carga de los experimentos (D11). Corre en una máquina cliente de AWS, dentro del contenedor cliente:
    docker compose exec -T -e DFSHA_USUARIO=... -e DFSHA_CONTRASENA=... cliente python /trabajo/carga.py <experimento> ...

Cada subcomando imprime al final una línea "RESULTADO {json}" que recoge experimentos/ejecutar.py.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import time
from pathlib import Path

from dfsha._gen import dfsha_pb2 as pb
from dfsha.client.cliente import Cliente, ErrorDfsha
from dfsha.client.datos import cifrar_bloque, sha256
from dfsha.common.config import cargar
from dfsha.common.tls import credenciales_canal

MIB = 1024 * 1024
TRABAJO = Path("/trabajo")


async def cliente(usuario=None, contrasena=None) -> Cliente:
    cfg = cargar()
    c = Cliente(cfg, credenciales_canal(cfg.tls))
    await c.login(usuario or os.environ["DFSHA_USUARIO"], contrasena or os.environ["DFSHA_CONTRASENA"])
    try:
        await c.mkdir("exp")                           # directorio de los experimentos
    except ErrorDfsha as e:
        if e.motivo != "EXISTS":
            raise
    return c


def archivo_local(mib: int) -> Path:
    """Archivo aleatorio de prueba, creado una vez por tamaño."""
    p = TRABAJO / f"datos-{mib}MiB.bin"
    if not p.exists() or p.stat().st_size != mib * MIB:
        with open(p, "wb") as f:
            for _ in range(mib):
                f.write(os.urandom(MIB))
    return p


def huella_archivo(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while bloque := f.read(8 * MIB):
            h.update(bloque)
    return h.hexdigest()


async def cronometrar(corrutina) -> float:
    t = time.perf_counter()
    await corrutina
    return time.perf_counter() - t


# ------------------------------------------------------------------ E1, E2, E3, E4 y trozo
async def transferencia(a) -> dict:
    """put y get de un archivo de a.mib MiB, con calentamiento y a.repeticiones medidas."""
    c = await cliente()
    local, vuelta = archivo_local(a.mib), TRABAJO / "vuelta.bin"
    esperado = huella_archivo(local)
    ruta = a.ruta or f"exp/{a.mib}MiB.bin"
    put, get, iguales = [], [], []
    for i in range(a.repeticiones + 1):                 # la primera es de calentamiento
        tp = await cronometrar(c.put(local, ruta))
        tg = await cronometrar(c.get(ruta, vuelta))
        if i:
            put.append(tp)
            get.append(tg)
            iguales.append(huella_archivo(vuelta) == esperado)
    abierto = await c.open(ruta, "r")
    copias = [len(b.replicas) for b in abierto.bloques.values()]
    await abierto.close()
    await c.cerrar()
    return {"mib": a.mib, "put_s": put, "get_s": get, "integro": all(iguales), "bloques": len(copias),
            "copias_por_bloque": copias}


# ------------------------------------------------------------------ E0
async def rf3(a) -> dict:
    """open, read de un rango, write de 100 bytes en el bloque 7, append y lock. Lo escrito solo se ve tras close."""
    c, otro = await cliente(), await cliente()
    ruta = "exp/rf3.bin"
    local = archivo_local(128)
    await c.put(local, ruta)
    original = local.read_bytes()
    pasos = {}
    f = await c.open(ruta, "rw")
    t = time.perf_counter()
    pasos["read_rango"] = await f.read(7 * 16 * MIB - 50, 100) == original[7 * 16 * MIB - 50:7 * 16 * MIB + 50]
    pasos["read_s"] = time.perf_counter() - t
    pasos["lock"] = (await f.lock([7]))[0].fencing_token > 0
    await f.write(7 * 16 * MIB + 10, b"Z" * 100)
    antes = await otro.open(ruta, "r")
    pasos["invisible_antes_de_close"] = await antes.read(7 * 16 * MIB + 10, 100) == original[7 * 16 * MIB + 10:7 * 16 * MIB + 110]
    await antes.close()
    t = time.perf_counter()
    await f.close()
    pasos["close_s"] = time.perf_counter() - t
    despues = await otro.open(ruta, "r")
    pasos["visible_tras_close"] = await despues.read(7 * 16 * MIB + 10, 100) == b"Z" * 100
    pasos["bloque_6_intacto"] = await despues.read(6 * 16 * MIB, 64) == original[6 * 16 * MIB:6 * 16 * MIB + 64]
    await despues.close()
    g = await c.open(ruta, "a")
    await g.append(b"FIN" * 10)
    await g.close()
    final = await c.stat(ruta)
    pasos["append"] = final.entry.size == len(original) + 30
    pasos["version_final"] = final.entry.version
    for x in (c, otro):
        await x.cerrar()
    return pasos


# ------------------------------------------------------------------ E7
async def concurrencia(a) -> dict:
    """a.escritores escritores a la vez, en el mismo bloque (cada uno su tramo) o en bloques distintos."""
    ruta = "exp/concurrencia.bin"
    clientes = [await cliente() for _ in range(a.escritores)]

    async def escritor(i: int, c: Cliente) -> dict:
        indice = a.base + i
        desplazamiento = indice * 64 if a.modo == "mismo" else indice * 16 * MIB
        marca = f"[{a.prefijo}{i:02d}]".encode().ljust(64, b".")
        reintentos, t = 0, time.perf_counter()
        while True:
            try:
                f = await c.open(ruta, "rw")
                await f.write(desplazamiento, marca)
                await f.close()
                break
            except ErrorDfsha as e:
                if e.motivo not in ("VERSION_CONFLICT", "LOCKED"):
                    raise
                reintentos += 1
                try:
                    await c.llamar("meta", "Close", pb.CloseRequest(fd=f.fd))
                except Exception:
                    pass
        return {"s": time.perf_counter() - t, "reintentos": reintentos, "desplazamiento": desplazamiento, "marca": marca.decode()}

    t = time.perf_counter()
    res = await asyncio.gather(*(escritor(i, c) for i, c in enumerate(clientes)))
    total = time.perf_counter() - t
    for c in clientes:
        await c.cerrar()
    return {"modo": a.modo, "escritores": a.escritores, "total_s": total, "lat_s": [r["s"] for r in res],
            "reintentos": sum(r["reintentos"] for r in res), "marcas": [[r["desplazamiento"], r["marca"]] for r in res]}


def marcas_esperadas(modo: str, escritores: int) -> list:
    """Las marcas que escriben los dos clientes (prefijo a con base 0 y prefijo b con base n)."""
    r = []
    for prefijo, base in (("a", 0), ("b", escritores)):
        for i in range(escritores):
            indice = base + i
            r.append((indice * 64 if modo == "mismo" else indice * 16 * MIB, f"[{prefijo}{i:02d}]".ljust(64, ".")))
    return r


async def verificar_marcas(a) -> dict:
    c = await cliente()
    f = await c.open("exp/concurrencia.bin", "r")
    marcas = marcas_esperadas(a.modo, a.escritores)
    presentes = [(await f.read(d, 64)).decode(errors="replace") == m for d, m in marcas]
    await f.close()
    await c.cerrar()
    return {"marcas": len(marcas), "presentes": sum(presentes)}


# ------------------------------------------------------------------ E11 y E6
async def metadatos(a) -> dict:
    """a.clientes clientes haciendo stat y ls durante a.segundos. Latencias y operaciones por segundo."""
    clientes = [await cliente() for _ in range(a.clientes)]
    await clientes[0].mkdir("exp/meta") if not await _existe(clientes[0], "exp/meta") else None
    latencias, fin = [], time.perf_counter() + a.segundos

    async def bucle(c: Cliente):
        while time.perf_counter() < fin:
            t = time.perf_counter()
            await c.stat("exp/meta")
            await c.ls("exp")
            latencias.append((time.perf_counter() - t) / 2)
    await asyncio.gather(*(bucle(c) for c in clientes))
    for c in clientes:
        await c.cerrar()
    lat = sorted(latencias)
    return {"clientes": a.clientes, "ops_s": 2 * len(lat) / a.segundos, "p50_ms": 1000 * lat[len(lat) // 2],
            "p95_ms": 1000 * lat[int(len(lat) * 0.95)], "muestras": len(lat)}


async def _existe(c: Cliente, ruta: str) -> bool:
    try:
        await c.stat(ruta)
        return True
    except ErrorDfsha:
        return False


async def disponibilidad(a) -> dict:
    """Hace stat cada 50 ms durante a.segundos y mide el mayor hueco sin respuesta (caída del líder)."""
    c = await cliente()
    ultimo, hueco, fallos, lideres = time.perf_counter(), 0.0, 0, set()
    fin = time.perf_counter() + a.segundos
    while time.perf_counter() < fin:
        try:
            await c.stat("exp")
            ahora = time.perf_counter()
            hueco, ultimo = max(hueco, ahora - ultimo), ahora
            lideres.add(c.lider)
        except ErrorDfsha:
            fallos += 1
        await asyncio.sleep(0.05)
    await c.cerrar()
    return {"hueco_max_s": hueco, "fallos": fallos, "lideres": sorted(x for x in lideres if x)}


# ------------------------------------------------------------------ E9 y E10
async def seguridad(a) -> dict:
    ana = await cliente()
    pedro = await cliente(a.otro, a.otro_clave)
    await ana.put(b"secreto de ana " * 1000, "exp/privado.txt")
    r = {}
    try:
        await pedro.get(f"/{ana.usuario}/exp/privado.txt")
        r["otro_usuario"] = "permitido"
    except ErrorDfsha as e:
        r["otro_usuario"] = e.motivo
    f = await ana.open("exp/privado.txt", "r")
    b = next(iter(f.bloques.values()))
    await f.close()
    for caso, token in (("token_valido", b.access_token), ("sin_token", b""), ("token_alterado", b.access_token[:-4] + b"AAAA")):
        try:
            stub = ana.transferencia._stub(b.replicas[0])
            datos = b"".join([t.data async for t in stub.ReadBlock(pb.ReadBlockRequest(block_id=b.block_id, access_token=token))])
            r[caso] = "aceptado" if sha256(datos) == b.cipher_checksum else "datos distintos"
        except Exception as e:
            from dfsha.common.errores import motivo_de
            r[caso] = motivo_de(e) if hasattr(e, "code") else str(e)
    sin_jwt = Cliente(ana.cfg, ana.credenciales)
    try:
        await sin_jwt.llamar("meta", "List", pb.PathRequest(path=f"/{ana.usuario}"), autenticado=False)
        r["sin_jwt"] = "permitido"
    except ErrorDfsha as e:
        r["sin_jwt"] = e.motivo
    r["bloque"] = b.block_id
    r["replicas"] = list(b.replicas)
    for x in (ana, pedro, sin_jwt):
        await x.cerrar()
    return r


async def cifrado(a) -> dict:
    """Costo del cifrado: AES-256-GCM y SHA-256 sobre bloques de 16 MiB en esta máquina."""
    bloque, clave = os.urandom(16 * MIB), os.urandom(32)
    t = time.perf_counter()
    for i in range(8):
        cifrar_bloque(clave, "f", i, bloque)
    aes = 8 * 16 / (time.perf_counter() - t)
    t = time.perf_counter()
    for _ in range(8):
        sha256(bloque)
    sha = 8 * 16 / (time.perf_counter() - t)
    return {"aes_gcm_mib_s": aes, "sha256_mib_s": sha}


async def estado(a) -> dict:
    c = await cliente()
    try:
        st = await c.stat(a.ruta)
        abierto = await c.open(a.ruta, "r")
        bloques = [abierto.bloques[i] for i in sorted(abierto.bloques)]
        await abierto.close()
        r = {"version": st.entry.version, "tamano": st.entry.size, "copias": [len(b.replicas) for b in bloques],
             "replicas": [[x.split(".")[0] for x in b.replicas] for b in bloques]}
    except ErrorDfsha as e:
        r = {"error": e.motivo}
    r["lider"] = (c.lider or "").split(".")[0]
    await c.cerrar()
    return r


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="exp", required=True)
    t = sub.add_parser("transferencia")
    t.add_argument("--mib", type=int, required=True)
    t.add_argument("--repeticiones", type=int, default=3)
    t.add_argument("--ruta")
    sub.add_parser("rf3")
    c = sub.add_parser("concurrencia")
    c.add_argument("--escritores", type=int, required=True)
    c.add_argument("--modo", choices=["mismo", "distintos"], required=True)
    c.add_argument("--base", type=int, default=0)
    c.add_argument("--prefijo", default="w")
    v = sub.add_parser("verificar_marcas")
    v.add_argument("--modo", choices=["mismo", "distintos"], required=True)
    v.add_argument("--escritores", type=int, required=True)
    m = sub.add_parser("metadatos")
    m.add_argument("--clientes", type=int, required=True)
    m.add_argument("--segundos", type=int, default=10)
    d = sub.add_parser("disponibilidad")
    d.add_argument("--segundos", type=int, default=30)
    s = sub.add_parser("seguridad")
    s.add_argument("--otro", required=True)
    s.add_argument("--otro-clave", required=True)
    sub.add_parser("cifrado")
    e = sub.add_parser("estado")
    e.add_argument("--ruta", required=True)
    a = p.parse_args()
    r = asyncio.run(globals()[a.exp](a))
    print("RESULTADO " + json.dumps(r))


if __name__ == "__main__":
    main()
