"""DataService del nodo de datos: escritura en cadena, lectura y diagnóstico (D5, D6 y D9).

WriteBlock recibe el bloque en trozos y, mientras lo guarda, reenvía cada trozo al siguiente
nodo de la cadena (downstream_nodes), que llega con su puerto interno. Con TLS habilitado, el
reenvío presenta el certificado del nodo (TLS mutuo, docs/tls.md). Cada nodo verifica su propia copia antes de renombrarla a
definitiva. La respuesta indica qué nodos de la cadena guardaron y verificaron su copia
(stored_on), y el bloque cuenta como escrito con al menos W de ellas (D5).

Si el siguiente nodo cae o deja de responder, este nodo termina de guardar su copia y responde
solo con las copias que sí quedaron. El cliente decide si le bastan (W) o si reenvía el bloque
completo por otra cadena con el mismo id, lo que es seguro porque guardar dos veces el mismo
bloque deja el mismo contenido.

Seguridad y carga (D8):
- Cada escritura y lectura de un cliente trae un token de acceso por bloque (dfsha/common/tokens.py)
  que se verifica con la clave HMAC recibida en Register. Otro nodo de datos, identificado por su
  certificado en el puerto interno (cadena y copias), no necesita token.
- Si el nodo ya atiende el máximo de peticiones simultáneas, responde OVERLOADED.

Al guardar o borrar un bloque se llama a al_guardar o al_borrar, que avisan a los 3 nodos de
metadatos (dfsha/datanode/latidos.py).
"""
from __future__ import annotations

import asyncio
import logging

import grpc

from dfsha._gen import dfsha_pb2 as pb
from dfsha._gen import dfsha_pb2_grpc as pb_grpc
from dfsha.common import tokens
from dfsha.common.errores import abortar, motivo_de
from dfsha.common.tls import es_nodo, identidad_par
from dfsha.datanode.almacen import Almacen, BloqueNoEncontrado, HuellaIncorrecta, IdInvalido, validar_id

log = logging.getLogger(__name__)
_FIN = object()


class Reenvio:
    """Reenvía los trozos de un bloque al siguiente nodo de la cadena mientras llegan.

    Cada envío espera a la vez dos cosas: que haya lugar en la cola (contrapresión, pocos trozos
    en memoria) o que la llamada al siguiente nodo termine. Si el siguiente nodo falla, se deja
    de reenviar al instante, sin quedarse esperando una cola que nadie lee, y este nodo sigue
    guardando su copia.
    """

    def __init__(self, canal: grpc.aio.Channel, siguiente: str, timeout_s: float, nodos_restantes: int):
        self.siguiente = siguiente
        self.timeout_s = timeout_s
        # El siguiente nodo puede tener que esperar a su vez a los que vienen detrás.
        self.timeout_resultado_s = timeout_s * (nodos_restantes + 1)
        self.fallo: str | None = None
        self._cola: asyncio.Queue = asyncio.Queue(maxsize=4)
        stub = pb_grpc.DataServiceStub(canal)
        self._llamada = stub.WriteBlock(self._trozos())
        self._resultado = asyncio.ensure_future(self._esperar_resultado())

    async def _trozos(self):
        while (trozo := await self._cola.get()) is not _FIN:
            yield trozo

    async def _esperar_resultado(self) -> pb.WriteResult:
        return await self._llamada

    async def _poner(self, elemento) -> bool:
        """Pone un trozo en la cola. False si la llamada terminó o no hubo lugar a tiempo."""
        poner = asyncio.ensure_future(self._cola.put(elemento))
        hecho, _ = await asyncio.wait({poner, self._resultado}, timeout=self.timeout_s,
                                      return_when=asyncio.FIRST_COMPLETED)
        if poner in hecho:
            return True
        poner.cancel()
        if self._resultado in hecho:
            self.fallo = self._describir(self._resultado.exception()) if self._resultado.exception() \
                else f"{self.siguiente} terminó antes de recibir todo el bloque"
        else:
            self.fallo = f"{self.siguiente} dejó de recibir"
            self.cancelar()
        return False

    async def enviar(self, trozo: pb.ChunkPayload) -> None:
        if not self.fallo:
            await self._poner(trozo)

    async def terminar(self) -> list[str]:
        """Espera la respuesta del siguiente nodo y devuelve los nodos que guardaron su copia."""
        if not self.fallo and await self._poner(_FIN):
            try:
                resultado = await asyncio.wait_for(asyncio.shield(self._resultado), self.timeout_resultado_s)
                return list(resultado.stored_on)
            except (asyncio.TimeoutError, grpc.aio.AioRpcError) as e:
                self.fallo = self._describir(e)
                self.cancelar()
        log.warning("cadena cortada después de este nodo: %s", self.fallo)
        return []

    def cancelar(self) -> None:
        self._llamada.cancel()
        self._resultado.cancel()

    def _describir(self, e: BaseException | None) -> str:
        if isinstance(e, grpc.aio.AioRpcError):
            return f"{self.siguiente} respondió {motivo_de(e)}"
        return f"{self.siguiente} no respondió a tiempo"


class ServicioDatos(pb_grpc.DataServiceServicer):
    def __init__(self, almacen: Almacen, nombre: str, tamano_trozo: int,
                 timeout_reenvio_s: float = 10.0,
                 credenciales_salida: grpc.ChannelCredentials | None = None,
                 limite_peticiones: int = 64):
        self.almacen = almacen
        self.clave_tokens: bytes | None = None   # sin clave (desarrollo sin metadatos) no se verifica
        self.limite_peticiones = limite_peticiones
        self.en_curso = 0
        self.al_guardar = None                   # async (block_id, huella, tamaño)
        self.al_borrar = None                    # async (block_id)
        self.nombre = nombre
        self.tamano_trozo = tamano_trozo
        self.timeout_reenvio_s = timeout_reenvio_s
        self._credenciales = credenciales_salida
        self._canales: dict[str, grpc.aio.Channel] = {}

    def _canal(self, destino: str) -> grpc.aio.Channel:
        """Canal reutilizable hacia otro nodo de datos (uno por destino)."""
        if destino not in self._canales:
            if self._credenciales is None:
                self._canales[destino] = grpc.aio.insecure_channel(destino)
            else:
                self._canales[destino] = grpc.aio.secure_channel(destino, self._credenciales)
        return self._canales[destino]

    async def cerrar(self) -> None:
        for canal in self._canales.values():
            await canal.close()

    async def _admitir(self, context) -> None:
        if self.en_curso >= self.limite_peticiones:
            await abortar(context, pb.REASON_OVERLOADED, "el nodo alcanzó su límite de peticiones simultáneas",
                          retry_after_ms=200)
        self.en_curso += 1

    async def _autorizar(self, context, token: bytes, block_id: str, operacion: str) -> None:
        if self.clave_tokens is None or es_nodo(identidad_par(context), "dn"):
            return
        try:
            tokens.verificar(self.clave_tokens, token, block_id, operacion)
        except (tokens.TokenBloqueInvalido, KeyError, ValueError) as e:
            await abortar(context, pb.REASON_TOKEN_INVALID, f"token de acceso inválido: {e}")

    def _aviso(self, funcion, *args) -> None:
        if funcion:
            asyncio.ensure_future(funcion(*args))

    async def borrar(self, block_id: str) -> None:
        if await asyncio.to_thread(self.almacen.borrar, block_id):
            self._aviso(self.al_borrar, block_id)

    async def WriteBlock(self, request_iterator, context):
        await self._admitir(context)
        try:
            return await self._escribir(request_iterator, context)
        finally:
            self.en_curso -= 1

    async def _escribir(self, request_iterator, context):
        entrada = request_iterator.__aiter__()
        try:
            primero = await entrada.__anext__()
        except StopAsyncIteration:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "stream vacío")
        await self._autorizar(context, primero.access_token, primero.block_id, tokens.ESCRIBIR)
        try:
            escritura = self.almacen.nueva_escritura(primero.block_id)
        except IdInvalido:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"id de bloque inválido {primero.block_id!r}")

        reenvio = None
        if primero.downstream_nodes:
            siguiente, *resto = primero.downstream_nodes
            reenvio = Reenvio(self._canal(siguiente), siguiente, self.timeout_reenvio_s, len(resto))

        trozo, secuencia = primero, 0
        try:
            while True:
                if trozo.sequence_number != secuencia:
                    raise ValueError(f"trozo {trozo.sequence_number} fuera de orden, se esperaba {secuencia}")
                escritura.escribir(trozo.data)
                if reenvio:
                    if secuencia == 0:     # el siguiente nodo recibe la cadena restante
                        reenviado = pb.ChunkPayload()
                        reenviado.CopyFrom(trozo)
                        del reenviado.downstream_nodes[:]
                        reenviado.downstream_nodes.extend(resto)
                        await reenvio.enviar(reenviado)
                    else:
                        await reenvio.enviar(trozo)
                if trozo.is_last_chunk:
                    break
                trozo = await entrada.__anext__()
                secuencia += 1
            await asyncio.to_thread(escritura.confirmar, primero.block_checksum)
        except HuellaIncorrecta as e:
            if reenvio:
                reenvio.cancelar()
            await abortar(context, pb.REASON_CHECKSUM_MISMATCH, str(e), block_id=e.block_id)
        except (StopAsyncIteration, ValueError) as e:
            escritura.descartar()
            if reenvio:
                reenvio.cancelar()
            mensaje = "el stream terminó sin el último trozo" if isinstance(e, StopAsyncIteration) else str(e)
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, mensaje)
        except BaseException:
            escritura.descartar()        # el cliente o el nodo anterior cortó la conexión
            if reenvio:
                reenvio.cancelar()
            raise

        self._aviso(self.al_guardar, primero.block_id, primero.block_checksum, escritura.tamano)
        guardado_en = [self.nombre]
        if reenvio:
            guardado_en += await reenvio.terminar()
        log.info("bloque %s guardado (%d bytes), copias en %s", primero.block_id, escritura.tamano, guardado_en)
        return pb.WriteResult(block_id=primero.block_id, stored_on=guardado_en)

    async def ReadBlock(self, request, context):
        await self._admitir(context)
        try:
            await self._autorizar(context, request.access_token, request.block_id, tokens.LEER)
            async for trozo in self._leer(request, context):
                yield trozo
        finally:
            self.en_curso -= 1

    async def _leer(self, request, context):
        try:
            huella, tamano = self.almacen.info(request.block_id)
        except (BloqueNoEncontrado, IdInvalido):
            await abortar(context, pb.REASON_NOT_FOUND, f"el nodo no tiene el bloque {request.block_id}",
                          block_id=request.block_id)
        if tamano == 0:
            yield pb.ChunkPayload(block_id=request.block_id, block_checksum=huella, is_last_chunk=True)
            return
        enviados, secuencia = 0, 0
        for datos in self.almacen.leer(request.block_id, self.tamano_trozo):
            enviados += len(datos)
            trozo = pb.ChunkPayload(sequence_number=secuencia, data=datos, is_last_chunk=enviados >= tamano)
            if secuencia == 0:
                trozo.block_id, trozo.block_checksum = request.block_id, huella
            yield trozo
            secuencia += 1

    async def CheckBlockHealth(self, request, context):
        ids = list(request.block_ids) or self.almacen.listar()
        respuesta = pb.HealthCheckResponse(node=self.nombre)
        for block_id in ids:
            try:
                validar_id(block_id)
                presente = self.almacen.existe(block_id)
            except IdInvalido:
                presente = False
            if not presente:
                respuesta.blocks.add(block_id=block_id, present=False)
                continue
            _, tamano = self.almacen.info(block_id)
            correcto = await asyncio.to_thread(self.almacen.verificar, block_id)
            respuesta.blocks.add(block_id=block_id, present=True, checksum_ok=correcto, size=tamano)
        return respuesta
