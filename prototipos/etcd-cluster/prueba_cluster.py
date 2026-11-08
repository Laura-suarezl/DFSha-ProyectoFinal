"""Prototipo de la etapa 4: riesgos del uso de etcd en un clúster de 3 miembros.

Requiere el clúster de prototipos/etcd-cluster/docker-compose.yml en marcha y Docker disponible,
porque el escenario 3 detiene y vuelve a arrancar un miembro de etcd.

Uso:  python prototipos/etcd-cluster/prueba_cluster.py
Escribe los resultados en prototipos/etcd-cluster/resultados.json.

Escenarios:
  1. Elección del líder de metadatos entre 3 candidatos y cambio de líder si el líder cae.
  2. Comparación de liderazgo: un líder que perdió el liderazgo ya no puede escribir.
  3. Sesión, lock y commit sobreviven a la caída de cada miembro de etcd, uno por turno.
  3b. Sin mayoría de etcd las escrituras se rechazan, y al volver la mayoría la sesión sigue viva.
  4. Jerarquía de locks: el lock del archivo entero frente a los locks de bloque.
  5. Commit del manifiesto con reintento cuando otro escritor cambió bloques distintos.
  6. Límites de etcd: operaciones por transacción y tamaño de una petición.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import grpc

from dfsha._gen.etcd.api.etcdserverpb import rpc_pb2 as rpc
from dfsha._gen.etcd.api.etcdserverpb import rpc_pb2_grpc as rpc_grpc
from dfsha.metadata.etcd import ClienteEtcd, EtcdNoDisponible, LockOcupado

MIEMBROS = {"dfsha-proto-etcd1": 23791, "dfsha-proto-etcd2": 23792, "dfsha-proto-etcd3": 23793}
DESTINO = "ipv4:" + ",".join(f"127.0.0.1:{p}" for p in MIEMBROS.values())
P = "/dfsha-proto/"
AQUI = Path(__file__).resolve().parent
resultados: dict[str, dict] = {}


def informar(escenario: str, **datos) -> None:
    resultados[escenario] = datos
    print(f"\n[{escenario}]")
    for k, v in datos.items():
        print(f"  {k}: {v}")


async def lider_de_etcd() -> str:
    """Contenedor del miembro que es líder de Raft en este momento."""
    ids = {}
    for contenedor, puerto in MIEMBROS.items():
        async with grpc.aio.insecure_channel(f"127.0.0.1:{puerto}") as canal:
            try:
                st = await rpc_grpc.MaintenanceStub(canal).Status(rpc.StatusRequest(), timeout=2)
                ids[st.header.member_id] = contenedor
                lider = st.leader
            except grpc.aio.AioRpcError:
                continue
    return ids[lider]


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True)


# ---------------------------------------------------------------------------------------
async def escenario_1_y_2(cli: ClienteEtcd) -> None:
    nombre = P + "election/metadata"
    candidatos = {}
    for n in ("mds1", "mds2", "mds3"):
        lease = await cli.conceder_lease(3)                       # lider_ttl_s = 3
        candidatos[n] = (lease, cli.mantener_vivo(lease, 1.0))
    tareas = {n: asyncio.create_task(cli.postular(nombre, l, n)) for n, (l, _) in candidatos.items()}

    hecho, _ = await asyncio.wait(tareas.values(), return_when=asyncio.FIRST_COMPLETED)
    primero = next(n for n, t in tareas.items() if t in hecho)
    lid1 = tareas[primero].result()
    otros = [n for n in tareas if n != primero]

    # El líder "se cae": deja de renovar su lease sin revocarlo (como un proceso muerto).
    await candidatos[primero][1].detener()
    caida = time.monotonic()
    hecho, _ = await asyncio.wait([tareas[n] for n in otros], return_when=asyncio.FIRST_COMPLETED)
    tiempo_cambio = time.monotonic() - caida
    segundo = next(n for n in otros if tareas[n] in hecho)
    lid2 = tareas[segundo].result()

    # Escenario 2: el líder caído intenta escribir con su comparación de liderazgo.
    r_viejo = await cli.txn([lid1.comparacion()], [cli.op_poner(P + "prueba-liderazgo", "viejo")])
    r_nuevo = await cli.txn([lid2.comparacion()], [cli.op_poner(P + "prueba-liderazgo", "nuevo")])

    # Renuncia ordenada del segundo líder: el tercero asume casi de inmediato.
    tercero = next(n for n in otros if n != segundo)
    renuncia = time.monotonic()
    await cli.renunciar(lid2)
    await tareas[tercero]
    tiempo_renuncia = time.monotonic() - renuncia

    informar("1_eleccion", primer_lider=primero, nuevo_lider_tras_caida=segundo,
             segundos_hasta_nuevo_lider=round(tiempo_cambio, 2), ttl_del_lease_de_lider_s=3,
             lider_tras_renuncia=tercero, segundos_tras_renuncia=round(tiempo_renuncia, 3),
             lider_visible_por_api=await cli.lider(nombre))
    informar("2_comparacion_de_liderazgo", escritura_del_lider_caido=r_viejo.succeeded,
             escritura_del_lider_vigente=r_nuevo.succeeded)
    for n, (lease, mantenedor) in candidatos.items():
        await mantenedor.detener()
        await cli.revocar_lease(lease)


# ---------------------------------------------------------------------------------------
async def esperar_miembro(contenedor: str) -> float:
    """Espera a que un miembro reiniciado vuelva a responder. Devuelve los segundos que tardó."""
    inicio = time.monotonic()
    async with grpc.aio.insecure_channel(f"127.0.0.1:{MIEMBROS[contenedor]}") as canal:
        while True:
            try:
                await rpc_grpc.MaintenanceStub(canal).Status(rpc.StatusRequest(), timeout=1)
                return time.monotonic() - inicio
            except grpc.aio.AioRpcError:
                await asyncio.sleep(0.2)


async def escenario_3(cli: ClienteEtcd) -> None:
    """Se detiene cada miembro por turno (nunca dos a la vez). Así en algún momento cae el miembro
    al que está conectado el keep-alive de la sesión y se prueba su reconexión."""
    sesion = await cli.conceder_lease(30)                       # sesión de cliente (D4)
    mantenedor = cli.mantener_vivo(sesion, 10)
    clave, token = await cli.bloquear(P + "locks/f1/b7", sesion, 10)
    await cli.poner(P + "files/f1", '{"version": 1}')
    vueltas = []
    orden = [await lider_de_etcd()] + [m for m in MIEMBROS if m != await lider_de_etcd()]
    for contenedor in orden:
        era_lider = contenedor == await lider_de_etcd()
        docker("stop", "-t", "0", contenedor)
        detenido = time.monotonic()
        fallos = 0
        while time.monotonic() - detenido < 12:               # más que un intervalo de renovación
            try:
                await cli.obtener(P + "files/f1")
                await cli.poner(P + "latido-prueba", str(time.time()))
            except Exception:
                fallos += 1
            await asyncio.sleep(0.2)
        lock = await cli.obtener(clave)
        commit = await cli.txn([cli.comparar_creacion(clave, token)],
                               [cli.op_poner(P + "files/f1", '{"version": 2}')])
        vueltas.append({
            "miembro_detenido": contenedor, "era_lider_de_raft": era_lider,
            "operaciones_fallidas_en_12_s": fallos,
            "ttl_de_la_sesion_s": await cli.ttl_lease(sesion),
            "lock_con_el_mismo_token": bool(lock and lock.create_revision == token),
            "commit_con_token": commit.succeeded,
        })
        docker("start", contenedor)
        vueltas[-1]["segundos_hasta_reincorporarse"] = round(await esperar_miembro(contenedor), 2)
    informar("3_caida_de_cada_miembro_de_etcd", vueltas=vueltas,
             reconexiones_del_keepalive=mantenedor.reconexiones,
             sesion_viva_al_final=await cli.ttl_lease(sesion) > 0)
    await mantenedor.detener()
    await cli.revocar_lease(sesion)


async def escenario_3b(cli: ClienteEtcd) -> None:
    """Pérdida de la mayoría (2 de 3 miembros caídos) durante 8 s, menos que la sesión de 30 s."""
    sesion = await cli.conceder_lease(30)
    mantenedor = cli.mantener_vivo(sesion, 10)
    clave, token = await cli.bloquear(P + "locks/f4/b0", sesion, 10)
    caidos = [m for m in MIEMBROS][:2]
    for m in caidos:
        docker("stop", "-t", "0", m)
    inicio = time.monotonic()
    try:
        await cli.poner(P + "sin-mayoria", "x")
        escritura = "aceptada (inesperado)"
    except EtcdNoDisponible as e:
        escritura = f"rechazada tras {time.monotonic() - inicio:.1f} s (EtcdNoDisponible, el servicio responde NO_QUORUM)"
    except Exception as e:
        escritura = f"error inesperado {type(e).__name__}: {e}"
    restante = 8 - (time.monotonic() - inicio)
    if restante > 0:
        await asyncio.sleep(restante)
    for m in caidos:
        docker("start", m)
    vuelta = time.monotonic()
    while True:
        try:
            await cli.poner(P + "con-mayoria", "x")
            break
        except Exception:
            await asyncio.sleep(0.2)
    recuperacion = time.monotonic() - vuelta
    lock = await cli.obtener(clave)
    informar("3b_perdida_de_mayoria", miembros_detenidos=caidos, escritura_sin_mayoria=escritura,
             segundos_hasta_escribir_de_nuevo=round(recuperacion, 2),
             ttl_de_la_sesion_tras_recuperar_s=await cli.ttl_lease(sesion),
             lock_con_el_mismo_token=bool(lock and lock.create_revision == token))
    await mantenedor.detener()
    await cli.revocar_lease(sesion)


# ---------------------------------------------------------------------------------------
class ConcesorLocks:
    """Jerarquía de locks (archivo frente a bloques) tal como la aplicaría el líder.

    Un lock de bloque registra primero una "intención" atada a la sesión, pero solo si no hay
    un lock de archivo concedido ni en espera. El lock de archivo entra en la cola de etcd y
    después espera, con Watch, a que no quede ninguna intención de bloque. El registro se hace
    bajo un candado local por archivo, porque solo el líder concede locks.
    """

    def __init__(self, cli: ClienteEtcd):
        self.cli = cli
        self.decisor: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def esperar_vacio(self, prefijo: str, limite: float) -> None:
        if not await self.cli.esperar_prefijo_vacio(prefijo, max(0.0, limite - time.monotonic())):
            raise LockOcupado(prefijo)

    async def bloque(self, f: str, i: int, sesion: int, espera_s: float):
        limite = time.monotonic() + espera_s
        while True:
            async with self.decisor[f]:
                if await self.cli.contar(f"{P}locks/{f}/archivo/") == 0:
                    intencion = f"{P}locks/{f}/intencion/{sesion:x}-{i}"
                    await self.cli.poner(intencion, "", lease=sesion)
                    break
            await self.esperar_vacio(f"{P}locks/{f}/archivo/", limite)
        try:
            clave, token = await self.cli.bloquear(f"{P}locks/{f}/b{i}", sesion, max(0.1, limite - time.monotonic()))
        except LockOcupado:
            await self.cli.borrar_prefijo(intencion)
            raise
        return clave, token, intencion

    async def liberar_bloque(self, clave: bytes, intencion: str) -> None:
        await self.cli.desbloquear(clave)
        await self.cli.borrar_prefijo(intencion)

    async def archivo(self, f: str, sesion: int, espera_s: float):
        limite = time.monotonic() + espera_s
        async with self.decisor[f]:
            tarea = asyncio.create_task(self.cli.bloquear(f"{P}locks/{f}/archivo", sesion, espera_s))
            while not any(kv.lease == sesion for kv in await self.cli.rango(f"{P}locks/{f}/archivo/")):
                await asyncio.sleep(0.02)                      # su entrada ya está en la cola
        clave, token = await tarea
        try:
            await self.esperar_vacio(f"{P}locks/{f}/intencion/", limite)
        except LockOcupado:
            await self.cli.desbloquear(clave)
            raise
        return clave, token


async def escenario_4(cli: ClienteEtcd) -> None:
    concesor = ConcesorLocks(cli)
    sesiones = {n: await cli.conceder_lease(30) for n in "ABC"}
    eventos: list[tuple[float, str]] = []
    t0 = time.monotonic()

    def marca(texto):
        eventos.append((round(time.monotonic() - t0, 2), texto))

    a = await concesor.bloque("f2", 7, sesiones["A"], 10)
    marca("A obtiene el bloque 7")

    async def b():
        clave, _ = await concesor.archivo("f2", sesiones["B"], 10)
        marca("B obtiene el archivo entero")
        await asyncio.sleep(0.5)
        await cli.desbloquear(clave)
        marca("B libera el archivo")

    async def c():
        await asyncio.sleep(0.5)
        marca("C pide el bloque 3")
        clave, _, intencion = await concesor.bloque("f2", 3, sesiones["C"], 10)
        marca("C obtiene el bloque 3")
        await concesor.liberar_bloque(clave, intencion)

    tb, tc = asyncio.create_task(b()), asyncio.create_task(c())
    await asyncio.sleep(1.0)
    await concesor.liberar_bloque(a[0], a[2])
    marca("A libera el bloque 7")
    await asyncio.gather(tb, tc)

    orden = [t for _, t in eventos]
    correcto = (orden.index("A libera el bloque 7") < orden.index("B obtiene el archivo entero")
                < orden.index("B libera el archivo") < orden.index("C obtiene el bloque 3"))
    informar("4_jerarquia_de_locks", eventos=eventos, orden_correcto=correcto)
    for lease in sesiones.values():
        await cli.revocar_lease(lease)


# ---------------------------------------------------------------------------------------
async def escenario_5(cli: ClienteEtcd) -> None:
    f = P + "files/f3"
    manifiesto = {i: f"b-v1-{i}" for i in range(10)}
    await cli.poner(f, json.dumps({"version": 1}))
    await cli.poner(P + "manifests/f3/1", json.dumps(manifiesto))

    async def leer():
        kv = await cli.obtener(f)
        version = json.loads(kv.value)["version"]
        man = json.loads((await cli.obtener(P + f"manifests/f3/{version}")).value)
        return kv.mod_revision, version, {int(k): v for k, v in man.items()}

    async def commit(cambios: dict[int, tuple[str, str]], lectura) -> tuple[str, int]:
        """cambios: índice -> (base esperada, id nuevo). Reintenta si cambió otro bloque."""
        intentos = 0
        rev, version, man = lectura
        while True:
            intentos += 1
            for i, (base, _) in cambios.items():
                if man[i] != base:
                    return "VERSION_CONFLICT", intentos
            nuevo = dict(man) | {i: nuevo for i, (_, nuevo) in cambios.items()}
            r = await cli.txn(
                [cli.comparar_modificacion(f, rev)],
                [cli.op_poner(P + f"manifests/f3/{version + 1}", json.dumps(nuevo)),
                 cli.op_poner(f, json.dumps({"version": version + 1}))])
            if r.succeeded:
                return f"versión {version + 1}", intentos
            rev, version, man = await leer()

    l1, l2 = await leer(), await leer()                  # dos escritores leen la misma versión
    r1 = await commit({3: ("b-v1-3", "b-w1-3")}, l1)
    r2 = await commit({7: ("b-v1-7", "b-w2-7")}, l2)
    _, version, final = await leer()
    r3 = await commit({3: ("b-v1-3", "b-w3-3")}, (0, 0, {3: "b-v1-3"}))   # base vieja del bloque 3
    informar("5_commit_con_reintento", escritor_1=r1, escritor_2_bloque_distinto=r2,
             version_final=version, bloque_3=final[3], bloque_7=final[7],
             escritor_3_con_base_vieja=r3)


# ---------------------------------------------------------------------------------------
async def escenario_6(cli: ClienteEtcd) -> None:
    async def probar(n_comp: int, n_ops: int) -> str:
        try:
            r = await cli.txn([cli.comparar_no_existe(P + f"x/{i}") for i in range(n_comp)],
                              [cli.op_poner(P + f"y/{i}", "1") for i in range(n_ops)])
            return "ok" if r.succeeded else "comparación falsa"
        except grpc.aio.AioRpcError as e:
            return f"rechazada ({e.details()})"

    async def tamano(n_bytes: int) -> str:
        try:
            await cli.poner(P + "grande", b"x" * n_bytes)
            return "ok"
        except grpc.aio.AioRpcError as e:
            return f"rechazada ({e.details()})"

    informar("6_limites_de_etcd",
             txn_128_comparaciones_y_128_escrituras=await probar(128, 128),
             txn_129_comparaciones=await probar(129, 1),
             txn_129_escrituras=await probar(1, 129),
             peticion_de_1_4_MiB=await tamano(int(1.4 * 1024 * 1024)),
             peticion_de_1_6_MiB=await tamano(int(1.6 * 1024 * 1024)))


async def main() -> int:
    async with ClienteEtcd(DESTINO) as cli:
        await cli.esperar_listo()
        await cli.borrar_prefijo(P)
        await escenario_1_y_2(cli)
        await escenario_3(cli)
        await escenario_3b(cli)
        await escenario_4(cli)
        await escenario_5(cli)
        await escenario_6(cli)
        await cli.borrar_prefijo(P)
    (AQUI / "resultados.json").write_text(json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
