"""Prototipo de la etapa 4: escritura en cadena entre 3 nodos de datos en procesos separados.

Arranca tres procesos "python -m dfsha.datanode", cada uno con su directorio de datos y su
puerto, configurados solo con variables de entorno. Se comunican por la red (127.0.0.1).

Uso:  python prototipos/cadena-datanodes/prueba_cadena.py
Escribe los resultados en prototipos/cadena-datanodes/resultados.json.

Escenarios:
  1. Un bloque de 16 MiB por la cadena de 3 nodos: tres copias idénticas.
  2. Rendimiento: un nodo frente a la cadena de 3, y trozo de 1 MiB frente a 64 KiB (D5).
  3. El último nodo muere (kill) a mitad del bloque: quedan 2 copias (W = 2).
  4. El nodo del medio muere: queda 1 copia y el cliente reenvía por otra cadena con el mismo id.
  5. Huella incorrecta: ningún nodo publica el bloque ni deja temporales.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import grpc

RAIZ = Path(__file__).resolve().parents[2]
AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

from dfsha._gen import dfsha_pb2 as pb  # noqa: E402
from dfsha._gen import dfsha_pb2_grpc as pb_grpc  # noqa: E402
from dfsha.common.errores import motivo_de  # noqa: E402

MIB = 1024 * 1024
PUERTOS = {"dn1": 50061, "dn2": 50062, "dn3": 50063}
DIR = {n: f"127.0.0.1:{p}" for n, p in PUERTOS.items()}
resultados: dict[str, dict] = {}


def informar(escenario: str, **datos) -> None:
    resultados[escenario] = datos
    print(f"\n[{escenario}]")
    for k, v in datos.items():
        print(f"  {k}: {v}")


class Nodos:
    """Arranca, mata y vuelve a arrancar los procesos de los nodos de datos."""

    def __init__(self, base: Path):
        self.base = base
        self.procesos: dict[str, subprocess.Popen] = {}

    def arrancar(self, nombre: str) -> None:
        entorno = dict(os.environ,
                       PYTHONPATH=str(RAIZ),
                       DFSHA_NODO_NOMBRE=nombre,
                       DFSHA_NODO_DIRECTORIO_DATOS=str(self.base / nombre),
                       DFSHA_CLUSTER_PUERTO_DATOS=str(PUERTOS[nombre]),
                       DFSHA_TLS_HABILITADO="false")
        registro = open(self.base / f"{nombre}.log", "a", encoding="utf-8")
        self.procesos[nombre] = subprocess.Popen(
            [sys.executable, "-m", "dfsha.datanode", "--escucha", "127.0.0.1"],
            env=entorno, stdout=registro, stderr=subprocess.STDOUT)

    def matar(self, nombre: str) -> None:
        """Muerte abrupta del proceso (equivalente a kill -9)."""
        self.procesos[nombre].kill()
        self.procesos[nombre].wait()

    def detener_todos(self) -> None:
        for p in self.procesos.values():
            if p.poll() is None:
                p.kill()
                p.wait()

    async def esperar_listo(self, nombre: str) -> None:
        async with grpc.aio.insecure_channel(DIR[nombre]) as canal:
            await asyncio.wait_for(canal.channel_ready(), 30)

    def bloques(self, nombre: str) -> list[str]:
        return sorted(p.name for p in (self.base / nombre / "blocks").glob("b-*") if p.suffix == "")

    def temporales(self, nombre: str) -> list[str]:
        return sorted(p.name for p in (self.base / nombre / "tmp").glob("*"))

    def registro(self, nombre: str) -> str:
        return (self.base / f"{nombre}.log").read_text(encoding="utf-8", errors="replace")


def trozos(block_id: str, datos: bytes, cadena_restante: list[str], tamano_trozo: int,
           huella: str | None = None, al_enviar=None):
    huella = huella or hashlib.sha256(datos).hexdigest()
    for n, i in enumerate(range(0, len(datos), tamano_trozo)):
        if al_enviar:
            al_enviar(n)
        t = pb.ChunkPayload(sequence_number=n, data=datos[i:i + tamano_trozo],
                            is_last_chunk=i + tamano_trozo >= len(datos))
        if n == 0:
            t.block_id, t.block_checksum = block_id, huella
            t.downstream_nodes.extend(cadena_restante)
        yield t


async def escribir(cadena: list[str], block_id: str, datos: bytes, tamano_trozo: int = MIB, **kw):
    async with grpc.aio.insecure_channel(DIR[cadena[0]]) as canal:
        stub = pb_grpc.DataServiceStub(canal)
        return await stub.WriteBlock(trozos(block_id, datos, [DIR[n] for n in cadena[1:]], tamano_trozo, **kw))


async def leer(nombre: str, block_id: str) -> bytes:
    async with grpc.aio.insecure_channel(DIR[nombre]) as canal:
        stub = pb_grpc.DataServiceStub(canal)
        return b"".join([t.data async for t in stub.ReadBlock(pb.ReadBlockRequest(block_id=block_id))])


def id_bloque(n: int) -> str:
    return f"b-{n:016x}"


async def escenario_1(nodos: Nodos) -> None:
    datos = os.urandom(16 * MIB)
    inicio = time.perf_counter()
    r = await escribir(["dn1", "dn2", "dn3"], id_bloque(1), datos)
    segundos = time.perf_counter() - inicio
    huella = hashlib.sha256(datos).hexdigest()
    lecturas = {n: hashlib.sha256(await leer(n, id_bloque(1))).hexdigest() == huella for n in PUERTOS}
    informar("1_cadena_de_3", copias=list(r.stored_on), segundos=round(segundos, 3),
             mb_por_segundo=round(16 / segundos, 1), lectura_identica_en_cada_nodo=lecturas)


async def escenario_2(nodos: Nodos) -> None:
    condiciones = [("1 nodo, trozo 1 MiB", ["dn1"], MIB),
                   ("cadena de 3, trozo 1 MiB", ["dn1", "dn2", "dn3"], MIB),
                   ("cadena de 3, trozo 64 KiB", ["dn1", "dn2", "dn3"], 64 * 1024)]
    datos = os.urandom(16 * MIB)
    siguiente_id = 100
    medidas = {}
    for nombre, cadena, trozo in condiciones:
        tiempos = []
        for repeticion in range(6):                     # 1 de calentamiento y 5 medidas
            siguiente_id += 1
            inicio = time.perf_counter()
            r = await escribir(cadena, id_bloque(siguiente_id), datos, trozo)
            t = time.perf_counter() - inicio
            assert len(r.stored_on) == len(cadena)
            if repeticion:
                tiempos.append(16 / t)
        medidas[nombre] = {"media_mb_s": round(statistics.mean(tiempos), 1),
                           "desviacion_mb_s": round(statistics.stdev(tiempos), 1)}
    informar("2_rendimiento", bloque="16 MiB", repeticiones="1 de calentamiento y 5 medidas",
             condiciones=medidas,
             costo_de_la_cadena=f"{medidas['cadena de 3, trozo 1 MiB']['media_mb_s'] / medidas['1 nodo, trozo 1 MiB']['media_mb_s']:.0%} del rendimiento de un solo nodo")


async def escenario_caida(nodos: Nodos, victima: str, escenario: str, block_id: str) -> None:
    datos = os.urandom(16 * MIB)

    def matar_a_mitad(n):
        if n == 10:
            nodos.matar(victima)

    inicio = time.perf_counter()
    r = await escribir(["dn1", "dn2", "dn3"], block_id, datos, al_enviar=matar_a_mitad)
    primera = {"copias": list(r.stored_on), "segundos": round(time.perf_counter() - inicio, 2)}
    reenvio = None
    if len(r.stored_on) < 2:                             # no se cumple W = 2: cadena nueva sin la víctima
        nueva = [n for n in ("dn1", "dn2", "dn3") if n != victima]
        r2 = await escribir(nueva, block_id, datos)
        reenvio = {"cadena": nueva, "copias": list(r2.stored_on)}
    temporales_antes = nodos.temporales(victima)
    nodos.arrancar(victima)
    await nodos.esperar_listo(victima)
    huella = hashlib.sha256(datos).hexdigest()
    copias_correctas = {}
    for n in (reenvio or primera)["copias"]:
        copias_correctas[n] = hashlib.sha256(await leer(n, block_id)).hexdigest() == huella
    informar(escenario, nodo_muerto=f"{victima} (kill a mitad del bloque, al enviar el trozo 10 de 16)",
             primera_escritura=primera, reenvio_por_otra_cadena=reenvio,
             temporales_que_dejo_la_caida=temporales_antes,
             bloque_en_el_nodo_muerto_tras_reiniciar=block_id in nodos.bloques(victima),
             temporales_tras_reiniciar=nodos.temporales(victima),
             huella_correcta_en_las_copias=copias_correctas)


async def escenario_5(nodos: Nodos) -> None:
    try:
        await escribir(["dn1", "dn2", "dn3"], id_bloque(5), os.urandom(4 * MIB), huella="0" * 64)
        respuesta = "aceptado (inesperado)"
    except grpc.aio.AioRpcError as e:
        respuesta = f"{e.code().name} con motivo {motivo_de(e)}"
    await asyncio.sleep(0.5)
    informar("5_huella_incorrecta", respuesta=respuesta,
             bloque_publicado_en={n: id_bloque(5) in nodos.bloques(n) for n in PUERTOS},
             temporales={n: nodos.temporales(n) for n in PUERTOS})


async def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="dfsha-cadena-"))
    nodos = Nodos(base)
    try:
        for n in PUERTOS:
            nodos.arrancar(n)
        for n in PUERTOS:
            await nodos.esperar_listo(n)
        await escenario_1(nodos)
        await escenario_2(nodos)
        await escenario_caida(nodos, "dn3", "3_cae_el_ultimo_nodo", id_bloque(3))
        await escenario_caida(nodos, "dn2", "4_cae_el_nodo_del_medio", id_bloque(4))
        await escenario_5(nodos)
    finally:
        nodos.detener_todos()
        shutil.rmtree(base, ignore_errors=True)
    # Finales de línea de Linux en cualquier sistema, igual que en el repositorio (.gitattributes)
    (AQUI / "resultados.json").write_text(json.dumps(resultados, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
