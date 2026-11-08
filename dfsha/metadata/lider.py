"""Elección del líder de los nodos de metadatos (D1, docs/etcd-esquema.md).

Cada nodo de metadatos compite en la elección de etcd con su propio lease de pocos segundos,
renovado continuamente. El que gana es el líder y atiende las escrituras. Si el líder cae, su
lease vence y otro candidato asume solo. Si un nodo pierde su lease (por ejemplo, porque quedó
aislado), deja de considerarse líder y vuelve a competir.

Aunque un nodo creyera ser líder sin serlo, no podría escribir: toda transacción de escritura
incluye la comparación de liderazgo (Liderazgo.comparacion()).
"""
from __future__ import annotations

import asyncio
import logging

import grpc

from dfsha.metadata.etcd import ClienteEtcd, EtcdNoDisponible, Liderazgo

log = logging.getLogger(__name__)


class Elector:
    def __init__(self, cli: ClienteEtcd, nombre_eleccion: str, nombre_nodo: str, ttl_s: int):
        self.cli = cli
        self.nombre_eleccion = nombre_eleccion
        self.nombre_nodo = nombre_nodo
        self.ttl_s = ttl_s
        self.liderazgo: Liderazgo | None = None
        self.cambios = 0                       # veces que este nodo llegó a ser líder
        self._lease: int | None = None
        self._tarea: asyncio.Task | None = None
        self._es_lider = asyncio.Event()

    @property
    def es_lider(self) -> bool:
        return self.liderazgo is not None

    def iniciar(self) -> None:
        self._tarea = asyncio.create_task(self._bucle(), name=f"eleccion-{self.nombre_nodo}")

    async def esperar_liderazgo(self, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(self._es_lider.wait(), timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    async def lider_actual(self) -> str | None:
        """Nombre del nodo líder, o None si no hay líder en este momento."""
        if self.liderazgo is not None:
            return self.nombre_nodo
        try:
            return await self.cli.lider(self.nombre_eleccion)
        except (EtcdNoDisponible, grpc.aio.AioRpcError):
            return None

    async def _bucle(self) -> None:
        while True:
            mantenedor = None
            campana = None
            try:
                self._lease = await self.cli.conceder_lease(self.ttl_s)
                mantenedor = self.cli.mantener_vivo(self._lease, max(0.5, self.ttl_s / 3))
                campana = asyncio.create_task(self.cli.postular(self.nombre_eleccion, self._lease, self.nombre_nodo))
                perdido = asyncio.create_task(mantenedor.perdido.wait())
                hecho, _ = await asyncio.wait({campana, perdido}, return_when=asyncio.FIRST_COMPLETED)
                if campana in hecho:
                    self.liderazgo = campana.result()
                    self.cambios += 1
                    self._es_lider.set()
                    log.info("%s es el líder de metadatos (revisión %d)", self.nombre_nodo, self.liderazgo.revision)
                    await perdido
                    log.warning("%s perdió el liderazgo: su lease venció", self.nombre_nodo)
                else:
                    campana.cancel()
            except asyncio.CancelledError:
                raise
            except (EtcdNoDisponible, grpc.aio.AioRpcError) as e:
                log.warning("%s no pudo competir por el liderazgo (%s), reintenta", self.nombre_nodo, e)
                await asyncio.sleep(1)
            finally:
                self.liderazgo = None
                self._es_lider.clear()
                if campana and not campana.done():
                    campana.cancel()
                if mantenedor:
                    await mantenedor.detener()

    async def detener(self) -> None:
        """Renuncia de forma ordenada, así otro nodo asume en milisegundos y no al vencer el lease."""
        liderazgo, lease = self.liderazgo, self._lease
        if self._tarea:
            self._tarea.cancel()
            try:
                await self._tarea
            except asyncio.CancelledError:
                pass
        try:
            if liderazgo:
                await self.cli.renunciar(liderazgo)
            if lease:
                await self.cli.revocar_lease(lease)
        except (EtcdNoDisponible, grpc.aio.AioRpcError):
            pass
