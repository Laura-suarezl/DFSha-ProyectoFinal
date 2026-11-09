"""Biblioteca del cliente de DFSha (RF1, RF2 y RF3, D2, D4 y D7, docs/cliente.md).

- Descubrimiento (D7). El cliente solo conoce el nombre lógico del clúster (dfsha.internal), que
  resuelve a los 3 nodos de metadatos. Si responde un seguidor, sigue NOT_LEADER hasta el líder y
  lo guarda. Si el líder no responde, vuelve a preguntar por el nombre lógico.
- Sesión. Login entrega JWT, token de renovación y sesión. Una tarea renueva la sesión cada 10 s y
  el JWT se renueva con Refresh antes de vencer.
- RF3 (D2). open devuelve un ArchivoAbierto con read, write, append, lock, unlock, fsync y close.
  Lo escrito queda en un búfer por bloque y se publica con fsync o close en un solo commit
  (copy-on-write, nunca se sobrescribe un bloque). Escribir un bloque en modo rw toma su lock.
"""
from __future__ import annotations

import asyncio
import secrets
import time

import grpc

from dfsha._gen import dfsha_pb2 as pb
from dfsha._gen import dfsha_pb2_grpc as pb_grpc
from dfsha.client.datos import (ErrorDatos, Transferencia, bajar, cifrar_bloque, descifrar_bloque, sha256,
                                subir)
from dfsha.common.config import OPCIONES_CANAL, Configuracion
from dfsha.common.errores import detalle_de, motivo_de

MODOS = {"r": pb.OPEN_MODE_READ, "w": pb.OPEN_MODE_WRITE, "a": pb.OPEN_MODE_APPEND, "rw": pb.OPEN_MODE_READ_WRITE}
REINTENTABLES = {"NOT_LEADER", "NO_QUORUM", "UNAVAILABLE", "OVERLOADED", "DEADLINE_EXCEEDED", "CANCELLED"}


class ErrorDfsha(Exception):
    def __init__(self, motivo: str, mensaje: str, error: grpc.aio.AioRpcError | None = None):
        super().__init__(f"{motivo}: {mensaje}")
        self.motivo, self.error = motivo, error


class Cliente:
    def __init__(self, cfg: Configuracion, credenciales: grpc.ChannelCredentials | None,
                 destino_cluster: str | None = None, direcciones: dict[str, str] | None = None):
        self.cfg = cfg
        self.credenciales = credenciales
        puerto = cfg.cluster.puerto_metadatos
        self.destino_cluster = destino_cluster or f"dns:///{cfg.cluster.nombre}:{puerto}"
        self.direcciones = direcciones or {}       # nombre:puerto -> dirección, solo fuera de la red privada
        self._canales: dict[str, grpc.aio.Channel] = {}
        self._lider: str | None = None             # nombre:puerto del líder conocido
        self.transferencia = Transferencia(cfg, credenciales, self.direcciones)
        self.usuario = ""
        self.token = self.renovacion = self.sesion = ""
        self._vence_ms = 0
        self._renovador: asyncio.Task | None = None

    # ------------------------------------------------------------------ canales y líder
    def _canal(self, destino: str) -> grpc.aio.Channel:
        real = self.direcciones.get(destino, destino)
        if real not in self._canales:
            opciones = [("grpc.lb_policy_name", "round_robin"), *OPCIONES_CANAL]
            if destino == self.destino_cluster:       # el certificado de cada nodo lleva el nombre lógico
                opciones.append(("grpc.ssl_target_name_override", self.cfg.cluster.nombre))
            self._canales[real] = (grpc.aio.secure_channel(real, self.credenciales, options=opciones)
                                   if self.credenciales else grpc.aio.insecure_channel(real, options=opciones))
        return self._canales[real]

    def _stubs(self):
        canal = self._canal(self._lider or self.destino_cluster)
        return pb_grpc.MetadataServiceStub(canal), pb_grpc.AdminServiceStub(canal)

    @property
    def lider(self) -> str | None:
        return self._lider

    async def llamar(self, servicio: str, metodo: str, pedido, autenticado: bool = True, intentos: int = 8):
        """Llama al líder siguiendo NOT_LEADER y reintentando fallas transitorias con espera exponencial."""
        espera = 0.2
        for intento in range(intentos):
            meta, admin = self._stubs()
            stub = meta if servicio == "meta" else admin
            if autenticado:
                await self._jwt_vigente()
            try:
                return await getattr(stub, metodo)(pedido, metadata=self.cabecera() if autenticado else None,
                                                   timeout=30)
            except grpc.aio.AioRpcError as e:
                motivo = motivo_de(e)
                if motivo == "NOT_LEADER" and detalle_de(e).leader:
                    self._lider = f"{detalle_de(e).leader}:{self.cfg.cluster.puerto_metadatos}"
                    continue
                if motivo == "TOKEN_INVALID" and autenticado and self.renovacion and intento == 0:
                    await self._refrescar()
                    continue
                if motivo not in REINTENTABLES or intento == intentos - 1:
                    raise ErrorDfsha(motivo, e.details() or "", e) from None
                if motivo in ("UNAVAILABLE", "DEADLINE_EXCEEDED", "CANCELLED"):
                    self._lider = None                 # el líder cayó: volver al nombre lógico
                d = detalle_de(e)
                await asyncio.sleep(max(espera, (d.retry_after_ms / 1000) if d else 0))
                espera = min(espera * 2, 5.0)

    def cabecera(self):
        return (("authorization", f"Bearer {self.token}"),)

    # ------------------------------------------------------------------ sesión
    def _guardar(self, r: pb.LoginResponse) -> None:
        self.token, self.renovacion, self.sesion, self._vence_ms = (r.access_token, r.refresh_token, r.session_id,
                                                                    r.expires_at_unix_ms)

    async def login(self, usuario: str, contrasena: str) -> None:
        self.usuario = usuario
        self._guardar(await self.llamar("meta", "Login", pb.LoginRequest(username=usuario, password=contrasena),
                                        autenticado=False))
        self._iniciar_renovacion()

    async def reanudar(self, usuario: str, renovacion: str) -> None:
        """Sesión nueva a partir de un token de renovación guardado (lo usa la CLI entre comandos)."""
        self.usuario, self.renovacion = usuario, renovacion
        await self._refrescar()
        self._iniciar_renovacion()

    async def _refrescar(self) -> None:
        self._guardar(await self.llamar("meta", "Refresh", pb.RefreshRequest(refresh_token=self.renovacion),
                                        autenticado=False))

    async def _jwt_vigente(self) -> None:
        if self.renovacion and self._vence_ms - time.time() * 1000 < 60_000:
            await self._refrescar()

    def _iniciar_renovacion(self) -> None:
        if self._renovador is None:
            self._renovador = asyncio.create_task(self._renovar())

    async def _renovar(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.tiempos.renovacion_s)
            try:
                await self.llamar("meta", "Renew", pb.RenewRequest(session_id=self.sesion), intentos=3)
            except ErrorDfsha:
                pass                                   # la próxima operación lo notará

    async def cerrar(self) -> None:
        if self._renovador:
            self._renovador.cancel()
            await asyncio.gather(self._renovador, return_exceptions=True)
        await self.transferencia.cerrar()
        for c in self._canales.values():
            await c.close()

    # ------------------------------------------------------------------ RF1
    def ruta(self, ruta: str) -> str:
        return ruta if ruta.startswith("/") else f"/{self.usuario}/{ruta}"

    async def ls(self, ruta: str) -> list[pb.Entry]:
        return list((await self.llamar("meta", "List", pb.PathRequest(path=self.ruta(ruta)))).entries)

    async def stat(self, ruta: str) -> pb.FileInfo:
        return await self.llamar("meta", "Stat", pb.PathRequest(path=self.ruta(ruta)))

    async def mkdir(self, ruta: str) -> None:
        await self.llamar("meta", "Mkdir", pb.PathRequest(path=self.ruta(ruta)))

    async def rmdir(self, ruta: str) -> None:
        await self.llamar("meta", "Rmdir", pb.PathRequest(path=self.ruta(ruta)))

    async def rm(self, ruta: str) -> None:
        await self.llamar("meta", "Remove", pb.PathRequest(path=self.ruta(ruta)))

    async def mv(self, origen: str, destino: str) -> None:
        await self.llamar("meta", "Rename", pb.RenameRequest(source=self.ruta(origen), destination=self.ruta(destino)))

    # ------------------------------------------------------------------ RF2
    async def _con_lider(self, funcion):
        """Repite una operación completa si el líder cambió a mitad (put y get son repetibles)."""
        for intento in range(4):
            await self.llamar("meta", "Renew", pb.RenewRequest(session_id=self.sesion))   # ubica al líder
            meta, _ = self._stubs()
            try:
                return await funcion(meta)
            except grpc.aio.AioRpcError as e:
                if motivo_de(e) not in REINTENTABLES or intento == 3:
                    raise ErrorDfsha(motivo_de(e), e.details() or "", e) from None
                self._lider = None
                await asyncio.sleep(1)

    async def put(self, datos, ruta: str) -> pb.CommitResponse:
        """datos son bytes o una ruta local (Path), que se lee por bloques."""
        return await self._con_lider(lambda meta: subir(self.cfg, meta, self.cabecera(), self.sesion, self.ruta(ruta),
                                                        datos, self.transferencia))

    async def get(self, ruta: str, destino=None):
        """Devuelve el contenido, o lo escribe en destino (Path) bloque por bloque."""
        return await self._con_lider(lambda meta: bajar(self.cfg, meta, self.cabecera(), self.sesion, self.ruta(ruta),
                                                        self.transferencia, destino))

    # ------------------------------------------------------------------ RF3
    async def open(self, ruta: str, modo: str = "r") -> "ArchivoAbierto":
        r = await self.llamar("meta", "Open", pb.OpenRequest(path=self.ruta(ruta), mode=MODOS[modo],
                                                             session_id=self.sesion))
        return ArchivoAbierto(self, r, modo)


class ArchivoAbierto:
    def __init__(self, cliente: Cliente, abierto: pb.OpenResponse, modo: str):
        self.c, self.modo = cliente, modo
        self.fd, self.file_id, self.clave = abierto.fd, abierto.file_id, abierto.file_key
        self.tam_bloque = abierto.block_size
        self.version = abierto.version
        self.tamano = 0 if modo == "w" else abierto.size
        self.bloques = {} if modo == "w" else {b.index: b for b in abierto.blocks}
        self._sucios: dict[int, bytearray] = {}
        self._cache: dict[int, bytes] = {}
        self._locks: dict[str, int] = {}           # objetivo -> token de cerco
        self._reemplazo = modo == "w"              # w deja el archivo con solo lo escrito, aunque sea nada
        for g in abierto.locks:
            self._locks["archivo"] = g.fencing_token

    # ---------------------------------------------------------------- lectura
    async def _plano(self, i: int) -> bytes:
        if i in self._sucios:
            return bytes(self._sucios[i])
        if i not in self.bloques:
            return b""
        if i not in self._cache:
            b = self.bloques[i]
            plano = descifrar_bloque(self.clave, self.file_id, i, await self.c.transferencia.leer(b))
            if sha256(plano) != b.plain_checksum:
                raise ErrorDatos(f"el bloque {i} no coincide con su huella en claro")
            self._cache[i] = plano
        return self._cache[i]

    async def read(self, desplazamiento: int, cantidad: int) -> bytes:
        if self.modo == "a":
            raise ErrorDfsha("FORBIDDEN", "el modo anexar no permite leer")
        fin = min(self.tamano, desplazamiento + cantidad)
        partes, pos = [], desplazamiento
        while pos < fin:
            i, o = divmod(pos, self.tam_bloque)
            n = min(fin - pos, self.tam_bloque - o)
            partes.append((await self._plano(i))[o:o + n])
            pos += n
        return b"".join(partes)

    # ---------------------------------------------------------------- locks
    async def lock(self, objetivos: list[int] | str, espera_s: int = 10) -> list[pb.LockGrant]:
        """Toma locks de bloques (lista de índices), del final ("fin") o del archivo entero ("archivo")."""
        if isinstance(objetivos, str):
            pedidos = [pb.LockTarget(end_of_file=True) if objetivos == "fin" else pb.LockTarget(whole_file=True)]
        else:
            pedidos = [pb.LockTarget(block_index=i) for i in sorted(objetivos)]
        r = await self.c.llamar("meta", "Lock", pb.LockRequest(fd=self.fd, targets=pedidos, wait_s=espera_s))
        for g in r.grants:
            t = g.target
            nombre = f"b{t.block_index}" if t.HasField("block_index") else ("fin" if t.HasField("end_of_file") else "archivo")
            self._locks[nombre] = g.fencing_token
        return list(r.grants)

    async def unlock(self) -> None:
        if self._sucios:
            raise ErrorDfsha("LOCKED", "hay cambios sin publicar, use fsync antes de liberar los locks")
        await self.c.llamar("meta", "Unlock", pb.UnlockRequest(fd=self.fd))
        self._locks.clear()

    def _token(self, i: int) -> int | None:
        return self._locks.get("archivo") or self._locks.get(f"b{i}") or self._locks.get("fin")

    # ---------------------------------------------------------------- escritura
    async def write(self, desplazamiento: int, datos: bytes) -> int:
        if self.modo == "r":
            raise ErrorDfsha("FORBIDDEN", "el archivo se abrió solo para leer")
        if desplazamiento > self.tamano:
            raise ErrorDfsha("BAD_OFFSET", "no se admiten huecos más allá del final del archivo")
        if self.modo == "a" and desplazamiento != self.tamano:
            raise ErrorDfsha("BAD_OFFSET", "en modo anexar solo se escribe al final")
        pos, fin = desplazamiento, desplazamiento + len(datos)
        while pos < fin:
            i, o = divmod(pos, self.tam_bloque)
            n = min(fin - pos, self.tam_bloque - o)
            if self._token(i) is None:                 # lock implícito (D4)
                await self.lock("fin" if self.modo == "a" or i >= len(self.bloques) else [i])
            if i not in self._sucios:
                self._sucios[i] = bytearray(await self._plano(i))
            buf = self._sucios[i]
            if len(buf) < o:
                raise ErrorDfsha("BAD_OFFSET", "hueco dentro del bloque")
            buf[o:o + n] = datos[pos - desplazamiento:pos - desplazamiento + n]
            pos += n
        self.tamano = max(self.tamano, fin)
        return len(datos)

    async def append(self, datos: bytes) -> int:
        return await self.write(self.tamano, datos)

    async def fsync(self) -> int:
        """Publica los cambios en un solo commit. Devuelve la versión nueva."""
        if not self._sucios and not self._reemplazo:
            return self.version
        cfg, c = self.c.cfg, self.c
        indices = sorted(self._sucios)
        confirmados = []
        for k in range(0, len(indices), cfg.almacenamiento.bloques_por_asignacion):
            lote = indices[k:k + cfg.almacenamiento.bloques_por_asignacion]
            asignados = (await c.llamar("meta", "AllocateBlocks", pb.AllocateBlocksRequest(fd=self.fd, indices=lote))).blocks
            confirmados += await asyncio.gather(*(self._escribir_bloque(a) for a in asignados))
        pedido = pb.CommitRequest(fd=self.fd, op_id=secrets.token_hex(8), blocks=confirmados, new_size=self.tamano,
                                  replace_all=self.modo == "w")
        for intento in range(10):
            try:
                r = await c.llamar("meta", "Commit", pedido)
                break
            except ErrorDfsha as e:
                if e.motivo != "INSUFFICIENT_REPLICAS" or intento == 9:
                    raise
                await asyncio.sleep(0.5)
        self.version = r.version
        loc = await c.llamar("meta", "GetBlockLocations", pb.BlockRangeRequest(fd=self.fd, first_index=0,
                                                                                last_index=2 ** 31))
        self.bloques = {b.index: b for b in loc.blocks}
        self._cache.update({i: bytes(self._sucios[i]) for i in indices})
        self._sucios.clear()
        if self.modo == "w":
            self.modo, self._reemplazo = "rw", False   # después del primer commit se sigue modificando
        return self.version

    async def _escribir_bloque(self, a: pb.AllocatedBlock) -> pb.CommittedBlock:
        plano = bytes(self._sucios[a.index])
        cifrado = cifrar_bloque(self.clave, self.file_id, a.index, plano)
        for _ in range(5):
            if len(await self.c.transferencia.escribir(a, cifrado)) >= self.c.cfg.almacenamiento.confirmaciones:
                base = self.bloques[a.index].block_id if a.index in self.bloques and self.modo != "w" else ""
                return pb.CommittedBlock(index=a.index, new_block_id=a.block_id, base_block_id=base,
                                         cipher_checksum=sha256(cifrado), plain_checksum=sha256(plano),
                                         size=len(plano), fencing_token=self._token(a.index))
            await asyncio.sleep(0.5)                   # que lleguen al líder los avisos de las copias que sí quedaron
            a = (await self.c.llamar("meta", "AllocateBlocks",
                                     pb.AllocateBlocksRequest(fd=self.fd, indices=[a.index]))).blocks[0]
        raise ErrorDatos(f"el bloque {a.index} no alcanzó las copias necesarias")

    async def close(self) -> None:
        try:
            await self.fsync()
        finally:
            await self.c.llamar("meta", "Close", pb.CloseRequest(fd=self.fd))
