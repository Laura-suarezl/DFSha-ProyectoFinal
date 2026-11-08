"""Cliente asíncrono de etcd para los nodos de metadatos (D1, D3 y D4, docs/etcd-esquema.md).

Es una capa delgada sobre los stubs generados desde los .proto oficiales de etcd. Habla con los
tres miembros del clúster a través de un solo destino gRPC (por ejemplo dns:///dfsha.internal:2379,
o ipv4:127.0.0.1:23791,127.0.0.1:23792 en pruebas) con balanceo round_robin, así que si un miembro
cae las llamadas siguientes van a los otros.

Qué ofrece:
- KV y transacciones con comparación por revisión (commit del manifiesto, comparación de liderazgo).
- Leases con keep-alive que se reconecta solo (sesiones de clientes y candidatura a líder).
- Lock del lado del servidor, que devuelve la clave del lock y su token de cerco.
- Election del lado del servidor (líder de metadatos) y Watch sobre un prefijo.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Iterable, Sequence

import grpc

from dfsha._gen.etcd.api.etcdserverpb import rpc_pb2 as rpc
from dfsha._gen.etcd.api.etcdserverpb import rpc_pb2_grpc as rpc_grpc
from dfsha._gen.etcd.api.mvccpb import kv_pb2
from dfsha._gen.etcd.api.v3electionpb import v3election_pb2 as eleccion
from dfsha._gen.etcd.api.v3electionpb import v3election_pb2_grpc as eleccion_grpc
from dfsha._gen.etcd.api.v3lockpb import v3lock_pb2 as lock
from dfsha._gen.etcd.api.v3lockpb import v3lock_pb2_grpc as lock_grpc

log = logging.getLogger(__name__)

KeyValue = kv_pb2.KeyValue
# Si cae el miembro al que está conectado el canal, la llamada termina con UNAVAILABLE o con
# CANCELLED (el stream se reinicia). Nunca cancelamos llamadas unarias a propósito, así que en
# ambos casos se trata de una falla del miembro y la siguiente llamada irá a otro.
_REINTENTABLES = {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.CANCELLED}
_ESPERAS = (0.2, 0.4, 0.8, 1.6, 3.2, 5.0)      # espera exponencial hasta 5 s (D9)


class EtcdNoDisponible(Exception):
    """No se pudo completar una llamada a etcd ni reintentándola (sin mayoría o sin red)."""


class LockOcupado(Exception):
    """Se agotó la espera en la cola del lock."""


class LeasePerdido(Exception):
    """El lease venció o fue revocado."""


def _b(x: str | bytes) -> bytes:
    return x.encode("utf-8") if isinstance(x, str) else x


def fin_de_prefijo(prefijo: str | bytes) -> bytes:
    """Clave final (exclusiva) del rango que cubre todas las claves con ese prefijo."""
    p = bytearray(_b(prefijo))
    for i in range(len(p) - 1, -1, -1):
        if p[i] < 0xFF:
            p[i] += 1
            return bytes(p[: i + 1])
    return b"\0"


@dataclass(frozen=True)
class Liderazgo:
    """Candidatura ganadora. revision sirve para la comparación de liderazgo en cada escritura."""
    nombre: bytes
    clave: bytes
    revision: int
    lease: int

    def comparacion(self) -> rpc.Compare:
        return ClienteEtcd.comparar_creacion(self.clave, self.revision)

    def _a_proto(self) -> eleccion.LeaderKey:
        return eleccion.LeaderKey(name=self.nombre, key=self.clave, rev=self.revision, lease=self.lease)


class MantenedorLease:
    """Mantiene vivo un lease con keep-alive periódico y se reconecta si el stream se corta."""

    def __init__(self, cliente: "ClienteEtcd", lease: int, intervalo_s: float):
        self.lease = lease
        self.intervalo_s = intervalo_s
        self.perdido = asyncio.Event()
        self.ultimo_ttl: int | None = None
        self.reconexiones = 0
        self._cliente = cliente
        self._tarea = asyncio.create_task(self._bucle(), name=f"keepalive-{lease:x}")

    async def _bucle(self) -> None:
        while not self.perdido.is_set():
            try:
                async def pedidos():
                    while True:
                        yield rpc.LeaseKeepAliveRequest(ID=self.lease)
                        await asyncio.sleep(self.intervalo_s)

                async for respuesta in self._cliente._lease.LeaseKeepAlive(pedidos()):
                    if respuesta.TTL <= 0:
                        log.warning("lease %x perdido", self.lease)
                        self.perdido.set()
                        return
                    self.ultimo_ttl = respuesta.TTL
            except asyncio.CancelledError:
                raise
            except grpc.aio.AioRpcError as e:
                self.reconexiones += 1
                log.info("keep-alive del lease %x interrumpido (%s), reconectando", self.lease, e.code().name)
                await asyncio.sleep(min(1.0, self.intervalo_s))

    async def detener(self) -> None:
        self._tarea.cancel()
        try:
            await self._tarea
        except asyncio.CancelledError:
            pass


class ClienteEtcd:
    def __init__(self, destino: str, credenciales: grpc.ChannelCredentials | None = None,
                 timeout_s: float = 5.0):
        opciones = [
            ("grpc.lb_policy_name", "round_robin"),
            ("grpc.keepalive_time_ms", 10_000),
            ("grpc.keepalive_timeout_ms", 5_000),
            ("grpc.keepalive_permit_without_calls", 1),
        ]
        if credenciales is None:
            self._canal = grpc.aio.insecure_channel(destino, options=opciones)
        else:
            self._canal = grpc.aio.secure_channel(destino, credenciales, options=opciones)
        self.timeout_s = timeout_s
        self._kv = rpc_grpc.KVStub(self._canal)
        self._lease = rpc_grpc.LeaseStub(self._canal)
        self._watch = rpc_grpc.WatchStub(self._canal)
        self._lock = lock_grpc.LockStub(self._canal)
        self._eleccion = eleccion_grpc.ElectionStub(self._canal)

    async def __aenter__(self) -> "ClienteEtcd":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.cerrar()

    async def cerrar(self) -> None:
        await self._canal.close()

    async def esperar_listo(self, timeout_s: float = 30.0) -> None:
        await asyncio.wait_for(self._canal.channel_ready(), timeout_s)

    async def _llamar(self, metodo, pedido, reintentar: bool = True, timeout_s: float | None = None):
        """Llama a etcd con un plazo total (reintentos incluidos).

        Solo reintenta operaciones idempotentes, y solo si falló el miembro conectado. Si se agota
        el plazo, lanza EtcdNoDisponible: así el lado sin mayoría recibe el error enseguida.
        En una transacción no se sabe si se aplicó, por eso las transacciones nunca se reintentan.
        """
        plazo = timeout_s or self.timeout_s
        bucle = asyncio.get_running_loop()
        limite = bucle.time() + plazo
        esperas = iter(_ESPERAS if reintentar else ())
        while True:
            restante = limite - bucle.time()
            if restante <= 0:
                raise EtcdNoDisponible(f"sin respuesta de etcd en {plazo} s")
            try:
                return await metodo(pedido, timeout=restante)
            except grpc.aio.AioRpcError as e:
                codigo = e.code()
                if codigo == grpc.StatusCode.DEADLINE_EXCEEDED:
                    raise EtcdNoDisponible(f"sin respuesta de etcd en {plazo} s") from e
                if codigo not in _REINTENTABLES:
                    raise
                espera = next(esperas, None)
                if espera is None or espera >= limite - bucle.time():
                    raise EtcdNoDisponible(e.details() or codigo.name) from e
                await asyncio.sleep(espera)

    # ------------------------------------------------------------------ KV
    async def obtener(self, clave: str | bytes) -> KeyValue | None:
        r = await self._llamar(self._kv.Range, rpc.RangeRequest(key=_b(clave)))
        return r.kvs[0] if r.kvs else None

    async def rango(self, prefijo: str | bytes, limite: int = 0) -> list[KeyValue]:
        r = await self._llamar(self._kv.Range, rpc.RangeRequest(
            key=_b(prefijo), range_end=fin_de_prefijo(prefijo), limit=limite))
        return list(r.kvs)

    async def contar(self, prefijo: str | bytes) -> int:
        return (await self.contar_con_revision(prefijo))[0]

    async def contar_con_revision(self, prefijo: str | bytes) -> tuple[int, int]:
        """Cantidad de claves del prefijo y revisión del clúster en que se contó.

        Para esperar un cambio sin perder eventos, se vigila desde revision + 1.
        """
        r = await self._llamar(self._kv.Range, rpc.RangeRequest(
            key=_b(prefijo), range_end=fin_de_prefijo(prefijo), count_only=True))
        return r.count, r.header.revision

    async def esperar_prefijo_vacio(self, prefijo: str | bytes, timeout_s: float) -> bool:
        """Espera, con Watch, a que no quede ninguna clave con el prefijo. False si se agota el plazo."""
        limite = asyncio.get_running_loop().time() + timeout_s
        cantidad, revision = await self.contar_con_revision(prefijo)
        while cantidad > 0:
            restante = limite - asyncio.get_running_loop().time()
            if restante <= 0:
                return False
            vigia = self.vigilar(prefijo, desde_revision=revision + 1).__aiter__()
            try:
                evento = await asyncio.wait_for(vigia.__anext__(), restante)
                revision = max(revision, evento.kv.mod_revision)
            except asyncio.TimeoutError:
                pass
            finally:
                await vigia.aclose()
            cantidad, revision = await self.contar_con_revision(prefijo)
        return True

    async def poner(self, clave: str | bytes, valor: str | bytes, lease: int = 0) -> int:
        r = await self._llamar(self._kv.Put, rpc.PutRequest(key=_b(clave), value=_b(valor), lease=lease))
        return r.header.revision

    async def borrar_prefijo(self, prefijo: str | bytes) -> int:
        r = await self._llamar(self._kv.DeleteRange, rpc.DeleteRangeRequest(
            key=_b(prefijo), range_end=fin_de_prefijo(prefijo)))
        return r.deleted

    async def txn(self, comparaciones: Sequence[rpc.Compare], si_cumple: Sequence[rpc.RequestOp],
                  si_no: Sequence[rpc.RequestOp] = ()) -> rpc.TxnResponse:
        """Transacción atómica. No se reintenta sola: si etcd cae a mitad, no se sabe si se aplicó."""
        return await self._llamar(self._kv.Txn, rpc.TxnRequest(
            compare=list(comparaciones), success=list(si_cumple), failure=list(si_no)), reintentar=False)

    # Constructores de comparaciones y operaciones para txn
    @staticmethod
    def comparar_modificacion(clave: str | bytes, revision: int) -> rpc.Compare:
        """La clave no cambió desde la revisión leída (0 = la clave no existe)."""
        return rpc.Compare(key=_b(clave), target=rpc.Compare.MOD, result=rpc.Compare.EQUAL, mod_revision=revision)

    @staticmethod
    def comparar_creacion(clave: str | bytes, revision: int) -> rpc.Compare:
        """La clave existe y fue creada en esa revisión (token de cerco o liderazgo)."""
        return rpc.Compare(key=_b(clave), target=rpc.Compare.CREATE, result=rpc.Compare.EQUAL,
                           create_revision=revision)

    @staticmethod
    def comparar_no_existe(clave: str | bytes) -> rpc.Compare:
        return rpc.Compare(key=_b(clave), target=rpc.Compare.CREATE, result=rpc.Compare.EQUAL, create_revision=0)

    @staticmethod
    def op_poner(clave: str | bytes, valor: str | bytes, lease: int = 0) -> rpc.RequestOp:
        return rpc.RequestOp(request_put=rpc.PutRequest(key=_b(clave), value=_b(valor), lease=lease))

    @staticmethod
    def op_borrar(clave: str | bytes) -> rpc.RequestOp:
        return rpc.RequestOp(request_delete_range=rpc.DeleteRangeRequest(key=_b(clave)))

    @staticmethod
    def op_borrar_prefijo(prefijo: str | bytes) -> rpc.RequestOp:
        return rpc.RequestOp(request_delete_range=rpc.DeleteRangeRequest(
            key=_b(prefijo), range_end=fin_de_prefijo(prefijo)))

    # ------------------------------------------------------------------ leases
    async def conceder_lease(self, ttl_s: int) -> int:
        r = await self._llamar(self._lease.LeaseGrant, rpc.LeaseGrantRequest(TTL=ttl_s))
        return r.ID

    async def revocar_lease(self, lease: int) -> None:
        try:
            await self._llamar(self._lease.LeaseRevoke, rpc.LeaseRevokeRequest(ID=lease))
        except grpc.aio.AioRpcError as e:
            if e.code() != grpc.StatusCode.NOT_FOUND:
                raise

    async def ttl_lease(self, lease: int) -> int:
        """Segundos que le quedan al lease, o -1 si ya no existe."""
        r = await self._llamar(self._lease.LeaseTimeToLive, rpc.LeaseTimeToLiveRequest(ID=lease))
        return r.TTL

    def mantener_vivo(self, lease: int, intervalo_s: float) -> MantenedorLease:
        return MantenedorLease(self, lease, intervalo_s)

    async def renovar_lease(self, lease: int) -> int:
        """Renueva un lease una sola vez (lo que hace cada Renew de un cliente). Devuelve el TTL
        nuevo, o 0 si el lease ya había vencido."""
        async def uno():
            yield rpc.LeaseKeepAliveRequest(ID=lease)

        try:
            async for respuesta in self._lease.LeaseKeepAlive(uno(), timeout=self.timeout_s):
                return max(respuesta.TTL, 0)
        except grpc.aio.AioRpcError as e:
            if e.code() in _REINTENTABLES or e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                raise EtcdNoDisponible(e.details() or e.code().name) from e
            raise
        return 0

    async def crear_si_no_existe(self, clave: str | bytes, valor: str | bytes, lease: int = 0,
                                 comparaciones: Sequence[rpc.Compare] = ()) -> bool:
        """Crea la clave solo si no existe (y si se cumplen las comparaciones extra). True si la creó."""
        r = await self.txn([self.comparar_no_existe(clave), *comparaciones], [self.op_poner(clave, valor, lease)])
        return r.succeeded

    # ------------------------------------------------------------------ lock (D4)
    async def bloquear(self, nombre: str | bytes, lease: int, espera_s: float) -> tuple[bytes, int]:
        """Toma el lock en la cola de etcd. Devuelve (clave del lock, token de cerco).

        etcd termina la espera agotada con DEADLINE_EXCEEDED o con CANCELLED, y CANCELLED también
        aparece si cae el miembro conectado. Se distinguen por el tiempo: un corte antes del final
        del plazo es una falla del miembro y se reintenta con el tiempo restante. Reintentar es
        seguro, porque etcd reutiliza la entrada de la cola de la misma sesión (misma clave).
        """
        bucle = asyncio.get_running_loop()
        limite = bucle.time() + espera_s
        while True:
            restante = limite - bucle.time()
            if restante <= 0:
                raise LockOcupado(_b(nombre).decode())
            try:
                r = await self._lock.Lock(lock.LockRequest(name=_b(nombre), lease=lease), timeout=restante)
                break
            except grpc.aio.AioRpcError as e:
                codigo = e.code()
                agotado = limite - bucle.time() <= 0.25
                if codigo in (grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.CANCELLED) and agotado:
                    raise LockOcupado(_b(nombre).decode()) from e
                if codigo in _REINTENTABLES or codigo == grpc.StatusCode.DEADLINE_EXCEEDED:
                    await asyncio.sleep(0.2)
                    continue
                raise
        kv = await self.obtener(r.key)
        if kv is None:
            raise LeasePerdido(f"el lock {r.key!r} desapareció apenas concedido")
        return r.key, kv.create_revision

    async def desbloquear(self, clave: bytes) -> None:
        await self._llamar(self._lock.Unlock, lock.UnlockRequest(key=clave))

    # ------------------------------------------------------------------ elección (D1)
    async def postular(self, nombre: str | bytes, lease: int, valor: str | bytes) -> Liderazgo:
        """Compite por el liderazgo. La llamada vuelve solo cuando este candidato es el líder."""
        r = await self._eleccion.Campaign(eleccion.CampaignRequest(name=_b(nombre), lease=lease, value=_b(valor)))
        lk = r.leader
        return Liderazgo(lk.name, lk.key, lk.rev, lk.lease)

    async def lider(self, nombre: str | bytes) -> str | None:
        try:
            r = await self._llamar(self._eleccion.Leader, eleccion.LeaderRequest(name=_b(nombre)))
        except grpc.aio.AioRpcError as e:
            if "no leader" in (e.details() or ""):
                return None
            raise
        return r.kv.value.decode("utf-8")

    async def renunciar(self, liderazgo: Liderazgo) -> None:
        await self._llamar(self._eleccion.Resign, eleccion.ResignRequest(leader=liderazgo._a_proto()))

    async def observar_lider(self, nombre: str | bytes) -> AsyncIterator[str]:
        async for r in self._eleccion.Observe(eleccion.LeaderRequest(name=_b(nombre))):
            yield r.kv.value.decode("utf-8")

    # ------------------------------------------------------------------ watch
    async def vigilar(self, prefijo: str | bytes, desde_revision: int = 0) -> AsyncIterator[kv_pb2.Event]:
        """Eventos (PUT o DELETE) sobre las claves del prefijo, desde una revisión dada."""
        crear = rpc.WatchRequest(create_request=rpc.WatchCreateRequest(
            key=_b(prefijo), range_end=fin_de_prefijo(prefijo), start_revision=desde_revision))

        async def pedidos():
            yield crear
            await asyncio.Event().wait()     # mantiene abierto el stream

        async for r in self._watch.Watch(pedidos()):
            for evento in r.events:
                yield evento


def operaciones(*grupos: Iterable[rpc.RequestOp]) -> list[rpc.RequestOp]:
    """Aplana listas de operaciones para pasarlas a txn."""
    return [op for grupo in grupos for op in grupo]
