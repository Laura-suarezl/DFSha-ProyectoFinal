"""Ruta de datos del cliente (D5 y D8, docs/cliente.md).

- Cada bloque se cifra con AES-256-GCM con la clave del archivo, un nonce nuevo y como datos
  asociados el id del archivo y el índice del bloque, así un bloque no se puede mover de lugar.
- Dos huellas por bloque: h del bloque en claro y H del bloque cifrado. El nodo de datos verifica
  H, y el cliente verifica H al leer, descifra y verifica h.
- Los bloques viajan en paralelo (almacenamiento.paralelismo) directamente con los nodos de datos.
- Al leer, si una réplica no responde en tiempo.timeout_replica_s o su huella no coincide, se pasa a
  la siguiente y la mala se informa con ReportBadBlock.
- Al escribir, si la cadena guarda menos de W copias, se pide una cadena nueva para ese bloque.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets

import grpc
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from dfsha._gen import dfsha_pb2 as pb
from dfsha._gen import dfsha_pb2_grpc as pb_grpc
from dfsha.common.config import Configuracion
from dfsha.common.errores import detalle_de, motivo_de


class ErrorDatos(Exception):
    """No se pudo escribir o leer un bloque con ninguna réplica o cadena."""


def sha256(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def _aad(file_id: str, indice: int) -> bytes:
    return f"{file_id}:{indice}".encode()


def cifrar_bloque(clave: bytes, file_id: str, indice: int, plano: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(clave).encrypt(nonce, plano, _aad(file_id, indice))


def descifrar_bloque(clave: bytes, file_id: str, indice: int, cifrado: bytes) -> bytes:
    try:
        return AESGCM(clave).decrypt(cifrado[:12], cifrado[12:], _aad(file_id, indice))
    except InvalidTag:
        raise ErrorDatos(f"el bloque {indice} no se pudo descifrar (alterado o fuera de lugar)") from None


class Transferencia:
    """Envía y recibe bloques con los nodos de datos, con un canal reutilizable por nodo."""

    def __init__(self, cfg: Configuracion, credenciales: grpc.ChannelCredentials | None,
                 direcciones: dict[str, str] | None = None):
        self.cfg = cfg
        self.credenciales = credenciales
        self.direcciones = direcciones or {}     # nombre:puerto -> dirección real (pruebas y anfitrión)
        self._canales: dict[str, grpc.aio.Channel] = {}
        self.malas: list[pb.BadBlockReport] = []

    def _stub(self, destino: str) -> pb_grpc.DataServiceStub:
        real = self.direcciones.get(destino, destino)
        if real not in self._canales:
            self._canales[real] = (grpc.aio.secure_channel(real, self.credenciales) if self.credenciales
                                   else grpc.aio.insecure_channel(real))
        return pb_grpc.DataServiceStub(self._canales[real])

    async def cerrar(self) -> None:
        for c in self._canales.values():
            await c.close()

    def _trozos(self, block_id: str, cifrado: bytes, cadena: list[str], token: bytes):
        tam = self.cfg.almacenamiento.tamano_trozo
        partes = [cifrado[i:i + tam] for i in range(0, len(cifrado), tam)] or [b""]
        for n, parte in enumerate(partes):
            t = pb.ChunkPayload(sequence_number=n, data=parte, is_last_chunk=n == len(partes) - 1)
            if n == 0:
                t.block_id, t.block_checksum, t.access_token = block_id, sha256(cifrado), token
                t.downstream_nodes.extend(cadena)
            yield t

    async def escribir(self, asignado: pb.AllocatedBlock, cifrado: bytes) -> list[str]:
        """Escribe por la cadena asignada. Devuelve los nodos que guardaron y verificaron su copia."""
        try:
            r = await self._stub(asignado.chain[0]).WriteBlock(
                self._trozos(asignado.block_id, cifrado, list(asignado.chain[1:]), asignado.access_token),
                timeout=60)
            return list(r.stored_on)
        except grpc.aio.AioRpcError:
            return []

    async def leer(self, ubicacion: pb.BlockLocation) -> bytes:
        """Bloque cifrado y verificado (H), probando las réplicas en orden."""
        for replica in ubicacion.replicas:
            try:
                trozos = self._stub(replica).ReadBlock(pb.ReadBlockRequest(
                    block_id=ubicacion.block_id, access_token=ubicacion.access_token),
                    timeout=self.cfg.tiempos.timeout_replica_s + ubicacion.size / (8 * 1024 * 1024))
                datos = b"".join([t.data async for t in trozos])
            except grpc.aio.AioRpcError as e:
                self.malas.append(pb.BadBlockReport(block_id=ubicacion.block_id, node=replica,
                                                    reason=f"sin respuesta ({motivo_de(e)})"))
                continue
            if sha256(datos) == ubicacion.cipher_checksum:
                return datos
            self.malas.append(pb.BadBlockReport(block_id=ubicacion.block_id, node=replica, reason="huella distinta"))
        raise ErrorDatos(f"ninguna réplica del bloque {ubicacion.index} respondió con la huella correcta")


async def _con_limite(n: int, corrutinas):
    semaforo = asyncio.Semaphore(n)

    async def una(c):
        async with semaforo:
            return await c
    return await asyncio.gather(*(una(c) for c in corrutinas))


async def subir(cfg: Configuracion, meta: pb_grpc.MetadataServiceStub, token, sesion: str, ruta: str,
                datos: bytes, transferencia: Transferencia) -> pb.CommitResponse:
    """Crea o reemplaza un archivo completo (put): Open en modo escritura, bloques en paralelo y Commit."""
    abierto = await meta.Open(pb.OpenRequest(path=ruta, mode=pb.OPEN_MODE_WRITE, session_id=sesion), metadata=token)
    try:
        tam, w = abierto.block_size, cfg.almacenamiento.confirmaciones
        partes = [datos[i:i + tam] for i in range(0, len(datos), tam)]
        lock = abierto.locks[0].fencing_token
        confirmados: list[pb.CommittedBlock] = []

        async def uno(indice: int, plano: bytes, asignado: pb.AllocatedBlock) -> pb.CommittedBlock:
            cifrado = cifrar_bloque(abierto.file_key, abierto.file_id, indice, plano)
            for _ in range(3):
                if len(await transferencia.escribir(asignado, cifrado)) >= w:
                    return pb.CommittedBlock(index=indice, new_block_id=asignado.block_id, cipher_checksum=sha256(cifrado),
                                             plain_checksum=sha256(plano), size=len(plano), fencing_token=lock)
                asignado = (await meta.AllocateBlocks(pb.AllocateBlocksRequest(fd=abierto.fd, indices=[indice]),
                                                      metadata=token)).blocks[0]   # otra cadena
            raise ErrorDatos(f"el bloque {indice} no alcanzó {w} copias")

        lote = cfg.almacenamiento.bloques_por_asignacion
        for i in range(0, len(partes), lote):
            indices = list(range(i, min(i + lote, len(partes))))
            asignados = (await meta.AllocateBlocks(pb.AllocateBlocksRequest(fd=abierto.fd, indices=indices),
                                                   metadata=token)).blocks
            confirmados += await _con_limite(cfg.almacenamiento.paralelismo,
                                             [uno(a.index, partes[a.index], a) for a in asignados])
        pedido = pb.CommitRequest(fd=abierto.fd, op_id=secrets.token_hex(8), blocks=confirmados,
                                  new_size=len(datos), replace_all=True)
        for _ in range(10):                       # los avisos BlockReceived llegan al líder en paralelo
            try:
                return await meta.Commit(pedido, metadata=token)
            except grpc.aio.AioRpcError as e:
                if motivo_de(e) != "INSUFFICIENT_REPLICAS":
                    raise
                await asyncio.sleep(detalle_de(e).retry_after_ms / 1000)
        raise ErrorDatos("el líder no vio las copias de todos los bloques")
    finally:
        await meta.Close(pb.CloseRequest(fd=abierto.fd), metadata=token)


async def bajar(cfg: Configuracion, meta: pb_grpc.MetadataServiceStub, token, sesion: str, ruta: str,
                transferencia: Transferencia) -> bytes:
    """Lee un archivo completo (get), verificando cada bloque y la huella del archivo."""
    abierto = await meta.Open(pb.OpenRequest(path=ruta, mode=pb.OPEN_MODE_READ, session_id=sesion), metadata=token)
    try:
        async def uno(b: pb.BlockLocation) -> bytes:
            plano = descifrar_bloque(abierto.file_key, abierto.file_id, b.index, await transferencia.leer(b))
            if sha256(plano) != b.plain_checksum:
                raise ErrorDatos(f"el bloque {b.index} no coincide con su huella en claro")
            return plano

        bloques = sorted(abierto.blocks, key=lambda b: b.index)
        partes = await _con_limite(cfg.almacenamiento.paralelismo, [uno(b) for b in bloques])
        huella = sha256(b"".join(bytes.fromhex(b.plain_checksum) for b in bloques))
        if huella != abierto.file_checksum:
            raise ErrorDatos("la huella del archivo no coincide")
        for mala in transferencia.malas:
            try:
                await meta.ReportBadBlock(mala, metadata=token)
            except grpc.aio.AioRpcError:
                pass
        transferencia.malas.clear()
        return b"".join(partes)
    finally:
        await meta.Close(pb.CloseRequest(fd=abierto.fd), metadata=token)
