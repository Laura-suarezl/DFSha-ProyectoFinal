"""Relación del nodo de datos con los 3 nodos de metadatos (D6, docs/nodos.md).

- Register al arrancar en los tres. La respuesta trae la clave HMAC de los tokens de acceso.
- Heartbeat cada 3 s a los tres, con espacio libre, usado y transferencias en curso. Solo la
  respuesta del líder trae órdenes, que se ejecutan aquí (copiar un bloque a otros nodos o borrarlo).
- BlockReceived y BlockDeleted a los tres en cuanto se guarda o borra un bloque.
- BlockReport (inventario completo) al registrarse y cada hora.
- Revisión diaria de huellas (scrubbing). Una copia dañada se borra y se avisa con BlockDeleted,
  así el líder ve una copia menos y ordena una nueva.

Si un nodo de metadatos responde que no conoce a este nodo (porque reinició y perdió su estado
blando), el nodo vuelve a registrarse en él y le envía el inventario.
"""
from __future__ import annotations

import asyncio
import logging
import shutil

import grpc

from dfsha._gen import dfsha_pb2 as pb
from dfsha._gen import dfsha_pb2_grpc as pb_grpc
from dfsha.common.config import Configuracion
from dfsha.common.errores import motivo_de
from dfsha.datanode.almacen import Almacen
from dfsha.datanode.servicio import ServicioDatos

log = logging.getLogger(__name__)
PLAZO_S = 2.0


class ClienteMetadatos:
    def __init__(self, cfg: Configuracion, almacen: Almacen, servicio: ServicioDatos,
                 credenciales: grpc.ChannelCredentials | None, destinos: list[str] | None = None):
        self.cfg = cfg
        self.nombre = cfg.nodo.nombre
        self.almacen = almacen
        self.servicio = servicio
        puerto = cfg.cluster.puerto_metadatos_interno
        destinos = destinos or [f"{n.strip()}:{puerto}" for n in cfg.cluster.nodos_metadatos.split(",") if n.strip()]
        self._canales = {d: (grpc.aio.secure_channel(d, credenciales) if credenciales else grpc.aio.insecure_channel(d))
                         for d in destinos}
        self._stubs = {d: pb_grpc.NodeServiceStub(c) for d, c in self._canales.items()}
        self._sin_registro = set(destinos)
        self._tareas: list[asyncio.Task] = []
        self._en_curso: set[str] = set()       # bloques con una orden de copia en marcha
        servicio.al_guardar = self.avisar_guardado
        servicio.al_borrar = self.avisar_borrado

    # ------------------------------------------------------------ espacio
    def _espacio(self) -> tuple[int, int, int]:
        uso = shutil.disk_usage(self.almacen.raiz)
        return uso.total, uso.free, uso.used

    def _inventario(self) -> list[pb.StoredBlock]:
        bloques = []
        for block_id in self.almacen.listar():
            try:
                huella, tamano = self.almacen.info(block_id)
                bloques.append(pb.StoredBlock(block_id=block_id, block_checksum=huella, size=tamano))
            except KeyError:
                pass                                # se borró mientras se listaba
        return bloques

    # ------------------------------------------------------------ registro
    async def _registrar_en(self, destino: str) -> pb.RegisterResponse | None:
        total, libre, _ = self._espacio()
        try:
            r = await self._stubs[destino].Register(pb.RegisterRequest(
                node=self.nombre, zone=self.cfg.nodo.zona, capacity_bytes=total, free_bytes=libre), timeout=PLAZO_S)
            informe = await self._stubs[destino].BlockReport(pb.BlockReportRequest(
                node=self.nombre, blocks=self._inventario()), timeout=PLAZO_S * 5)
        except grpc.aio.AioRpcError as e:
            log.debug("registro en %s falló: %s", destino, motivo_de(e))
            return None
        self._sin_registro.discard(destino)
        self.servicio.clave_tokens = r.block_token_key
        self._atender(informe)
        return r

    async def registrar(self, espera_s: float | None = None) -> pb.RegisterResponse:
        """Se registra en todos los que respondan. Vuelve cuando al menos uno respondió."""
        bucle = asyncio.get_running_loop()
        limite = None if espera_s is None else bucle.time() + espera_s
        while True:
            respuestas = await asyncio.gather(*(self._registrar_en(d) for d in list(self._sin_registro)))
            buenas = [r for r in respuestas if r]
            if buenas or len(self._sin_registro) < len(self._stubs):
                log.info("%s registrado en %d de %d nodos de metadatos", self.nombre,
                         len(self._stubs) - len(self._sin_registro), len(self._stubs))
                return buenas[0] if buenas else pb.RegisterResponse()
            if limite is not None and bucle.time() > limite:
                raise TimeoutError("ningún nodo de metadatos respondió al registro")
            log.info("esperando a los nodos de metadatos para registrarse")
            await asyncio.sleep(1)

    # ------------------------------------------------------------ avisos
    async def _a_todos(self, metodo: str, pedido) -> None:
        async def uno(destino):
            try:
                await getattr(self._stubs[destino], metodo)(pedido, timeout=PLAZO_S)
            except grpc.aio.AioRpcError:
                pass                               # el inventario periódico corrige lo que se pierda
        await asyncio.gather(*(uno(d) for d in self._stubs))

    async def avisar_guardado(self, block_id: str, huella: str, tamano: int) -> None:
        await self._a_todos("BlockReceived", pb.BlockNotice(node=self.nombre, block_id=block_id,
                                                            block_checksum=huella, size=tamano))

    async def avisar_borrado(self, block_id: str) -> None:
        await self._a_todos("BlockDeleted", pb.BlockNotice(node=self.nombre, block_id=block_id))

    # ------------------------------------------------------------ latidos y órdenes
    async def latido(self) -> None:
        for destino in list(self._sin_registro):
            await self._registrar_en(destino)
        _, libre, usado = self._espacio()
        pedido = pb.HeartbeatRequest(node=self.nombre, free_bytes=libre, used_bytes=usado,
                                     active_transfers=self.servicio.en_curso)

        async def uno(destino):
            try:
                self._atender(await self._stubs[destino].Heartbeat(pedido, timeout=PLAZO_S))
            except grpc.aio.AioRpcError as e:
                if motivo_de(e) == "NOT_FOUND":
                    self._sin_registro.add(destino)
        await asyncio.gather(*(uno(d) for d in self._stubs if d not in self._sin_registro))

    def _atender(self, respuesta: pb.HeartbeatResponse) -> None:
        if respuesta.from_leader:
            for orden in respuesta.commands:
                asyncio.ensure_future(self.ejecutar(orden))

    async def ejecutar(self, orden: pb.NodeCommand) -> None:
        if orden.HasField("remove"):
            for block_id in orden.remove.block_ids:
                await self.servicio.borrar(block_id)
            log.info("orden de borrado ejecutada: %s", list(orden.remove.block_ids))
        elif orden.HasField("replicate"):
            await self.copiar(orden.replicate)

    async def copiar(self, orden: pb.ReplicateCommand) -> list[str]:
        """Envía una copia del bloque en cadena a los nodos indicados, por sus puertos internos."""
        if orden.block_id in self._en_curso or not orden.targets or not self.almacen.existe(orden.block_id):
            return []
        self._en_curso.add(orden.block_id)
        try:
            huella, _ = self.almacen.info(orden.block_id)
            if orden.block_checksum and huella != orden.block_checksum:
                log.warning("orden de copia de %s con otra huella, no se copia", orden.block_id)
                return []
            puerto = self.cfg.cluster.puerto_datos_interno
            destinos = [t if ":" in t else f"{t}:{puerto}" for t in orden.targets]
            tamano_trozo = self.cfg.almacenamiento.tamano_trozo

            def trozos():
                datos = list(self.almacen.leer(orden.block_id, tamano_trozo)) or [b""]
                for i, d in enumerate(datos):
                    t = pb.ChunkPayload(sequence_number=i, data=d, is_last_chunk=i == len(datos) - 1)
                    if i == 0:
                        t.block_id, t.block_checksum = orden.block_id, huella
                        t.downstream_nodes.extend(destinos[1:])
                    yield t

            r = await pb_grpc.DataServiceStub(self.servicio._canal(destinos[0])).WriteBlock(trozos(), timeout=60)
            log.info("bloque %s copiado a %s", orden.block_id, list(r.stored_on))
            return list(r.stored_on)
        except grpc.aio.AioRpcError as e:
            log.warning("copia de %s a %s falló: %s", orden.block_id, list(orden.targets), motivo_de(e))
            return []
        finally:
            self._en_curso.discard(orden.block_id)

    # ------------------------------------------------------------ revisión e inventario
    async def revisar(self) -> list[str]:
        """Verifica la huella de cada bloque. Borra y avisa los dañados. Devuelve sus ids."""
        danados = []
        for block_id in self.almacen.listar():
            if not await asyncio.to_thread(self.almacen.verificar, block_id):
                danados.append(block_id)
                log.warning("bloque %s dañado en %s, se borra para que el líder ordene otra copia", block_id, self.nombre)
                await self.servicio.borrar(block_id)
        return danados

    async def enviar_inventario(self) -> None:
        pedido = pb.BlockReportRequest(node=self.nombre, blocks=self._inventario())

        async def uno(destino):
            try:
                self._atender(await self._stubs[destino].BlockReport(pedido, timeout=PLAZO_S * 5))
            except grpc.aio.AioRpcError as e:
                if motivo_de(e) == "NOT_FOUND":
                    self._sin_registro.add(destino)
        await asyncio.gather(*(uno(d) for d in self._stubs if d not in self._sin_registro))

    # ------------------------------------------------------------ ciclo de vida
    def iniciar(self) -> None:
        t = self.cfg.tiempos
        for intervalo, accion in ((t.latido_s, self.latido), (t.inventario_s, self.enviar_inventario),
                                  (t.revision_s, self.revisar)):
            self._tareas.append(asyncio.create_task(self._cada(intervalo, accion)))

    async def _cada(self, intervalo_s: float, accion) -> None:
        while True:
            await asyncio.sleep(intervalo_s)
            try:
                await accion()
            except Exception:
                log.exception("falló %s", accion.__name__)

    async def detener(self) -> None:
        for t in self._tareas:
            t.cancel()
        await asyncio.gather(*self._tareas, return_exceptions=True)
        for c in self._canales.values():
            await c.close()

