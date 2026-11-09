"""Recuperación en el líder de metadatos (D3, D6, docs/nodos.md).

Cada tiempos.recuperacion_s el líder hace un ciclo con cuatro tareas.
1. Re-replicación. Cada bloque referenciado (versión vigente de cada archivo o versión con arriendo
   de lectura) debe tener R copias en nodos no muertos que no estén en retiro. Un nodo sospechoso
   todavía cuenta, así un corte breve no provoca copias innecesarias (D6, se re-replica a los 30 s). Si tiene menos, ordena a
   un nodo que lo tenga copiarlo a otros, elegidos como una cadena nueva.
2. Recolector de manifiestos. Borra los manifiestos que no son la versión vigente y que nadie lee.
   Borra los bloques en construcción de más de una hora que nunca se confirmaron.
3. Recolector de bloques. Ordena borrar los bloques que ningún manifiesto ni bloque en construcción
   referencia. Solo después de tiempos.gracia_recolector_s como líder, para tener los inventarios.
4. Retiro planificado (Decommission). Un nodo en retiro no cuenta como copia, así que sus bloques se
   copian a otros. Cuando todos tienen R copias fuera de él, se le ordena borrarlos.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from dfsha._gen import dfsha_pb2 as pb
from dfsha.metadata.etcd import EtcdNoDisponible

log = logging.getLogger(__name__)
UNA_HORA_MS = 3600 * 1000
COPIAS_POR_NODO_Y_CICLO = 8     # no inundar a un nodo con órdenes de copia (memoria y red)


class Recuperacion:
    def __init__(self, nucleo):
        self.n = nucleo
        self.cli = nucleo.cli
        self.p = nucleo.cfg.etcd.prefijo
        self._pendientes: dict[str, float] = {}     # bloque -> hasta cuándo no repetir la orden de copia
        self._lider_desde: float | None = None
        self._tarea: asyncio.Task | None = None

    def iniciar(self) -> None:
        self._tarea = asyncio.create_task(self._bucle())

    async def detener(self) -> None:
        if self._tarea:
            self._tarea.cancel()
            await asyncio.gather(self._tarea, return_exceptions=True)

    async def _bucle(self) -> None:
        while True:
            await asyncio.sleep(self.n.cfg.tiempos.recuperacion_s)
            try:
                await self.ciclo()
            except EtcdNoDisponible:
                pass
            except Exception:
                log.exception("falló el ciclo de recuperación")

    async def _rango(self, *partes) -> list:
        return await self.cli.rango(self.p + "/".join(partes))

    def _resto(self, kv, *partes) -> list[str]:
        return kv.key.decode()[len(self.p + "/".join(partes)):].split("/")

    async def ciclo(self, gracia_s: float | None = None) -> dict:
        """Un ciclo completo. Devuelve un resumen (para pruebas y registros)."""
        resumen = {"copias": 0, "borrados": 0, "manifiestos": 0, "perdidos": 0}
        if not self.n.elector.es_lider:
            self._lider_desde = None
            return resumen
        ahora = time.monotonic()
        self._lider_desde = self._lider_desde or ahora
        gracia = self.n.cfg.tiempos.gracia_recolector_s if gracia_s is None else gracia_s
        registro, cfg = self.n.nodos, self.n.cfg
        registro.retirando = {self._resto(kv, "decommission/")[0] for kv in await self._rango("decommission/")}

        vigentes = {self._resto(kv, "files/")[0]: json.loads(kv.value)["version"] for kv in await self._rango("files/")}
        leidas = {tuple(self._resto(kv, "readleases/")[:2]) for kv in await self._rango("readleases/")}
        referenciados: dict[str, str] = {}
        borrar_manifiestos = []
        for kv in await self._rango("manifests/"):
            fid, version = self._resto(kv, "manifests/")[:2]
            if vigentes.get(fid) == int(version) or (fid, version) in leidas:
                for b in json.loads(kv.value)["bloques"]:
                    referenciados[b["id"]] = b["H"]
            else:
                borrar_manifiestos.append(kv.key)
        en_construccion, borrar_uc = set(), []
        ahora_ms = int(time.time() * 1000)
        for kv in await self._rango("uc/"):
            info = json.loads(kv.value)
            if ahora_ms - info["fecha"] > UNA_HORA_MS:
                borrar_uc.append(kv.key)
            else:
                en_construccion.add(self._resto(kv, "uc/")[2])
        if borrar_manifiestos or borrar_uc:
            ops = [self.cli.op_borrar(k) for k in borrar_manifiestos + borrar_uc][:100]
            await self.cli.txn(self.n.comparaciones_lider(), ops)
            resumen["manifiestos"] = len(borrar_manifiestos)

        # 1 y 4: re-replicación, contando las copias en nodos no muertos que no se están retirando
        r = cfg.almacenamiento.replicas
        ordenadas: dict[str, int] = {}
        for bloque, huella in referenciados.items():
            todas = registro.ubicaciones(bloque, huella)
            buenas = [n for n in todas if registro.estado(n) not in (pb.NODE_STATE_DEAD, pb.NODE_STATE_DECOMMISSIONING)]
            if len(buenas) >= r or self._pendientes.get(bloque, 0) > ahora:
                continue
            origen = (buenas or [n for n in todas if registro.estado(n) != pb.NODE_STATE_DEAD] or [None])[0]
            if origen is None:
                resumen["perdidos"] += 1
                log.error("bloque %s sin ninguna copia disponible", bloque)
                continue
            if ordenadas.get(origen, 0) >= COPIAS_POR_NODO_Y_CICLO:
                continue                              # el siguiente ciclo sigue con el resto
            destinos = registro.elegir_cadena(r - len(buenas), excluir=set(todas))
            if destinos:
                ordenadas[origen] = ordenadas.get(origen, 0) + 1
                registro.encolar(origen, pb.NodeCommand(replicate=pb.ReplicateCommand(
                    block_id=bloque, block_checksum=huella, targets=destinos)))
                self._pendientes[bloque] = ahora + 60
                resumen["copias"] += 1

        # 3 y 4: borrar huérfanos y vaciar los nodos en retiro
        con_gracia = ahora - self._lider_desde >= gracia
        for nodo in list(registro.nodos.values()):
            borrar = []
            for bloque in list(nodo.bloques):
                if bloque in en_construccion:
                    continue
                if bloque not in referenciados:
                    if con_gracia:
                        borrar.append(bloque)
                elif nodo.nombre in registro.retirando:
                    fuera = [n for n in registro.ubicaciones(bloque, referenciados[bloque])
                             if registro.estado(n) == pb.NODE_STATE_ALIVE]
                    if len(fuera) >= r:
                        borrar.append(bloque)
            if borrar:
                registro.encolar(nodo.nombre, pb.NodeCommand(remove=pb.DeleteCommand(block_ids=borrar)))
                resumen["borrados"] += len(borrar)
        return resumen
