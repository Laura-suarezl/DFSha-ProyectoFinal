"""Estado de los nodos de datos y NodeService del lado de los metadatos (D5, D6, docs/nodos.md).

Este estado es blando: vive en la memoria de cada uno de los 3 nodos de metadatos y se reconstruye
con los registros, latidos, avisos e inventarios de los nodos de datos, que van a los tres. Así un
nuevo líder ya conoce la ubicación de cada bloque sin leer nada de etcd.

- Estado de un nodo por la edad de su último latido: vivo, sospechoso (10 s) y muerto (30 s).
- Ubicación de cada bloque: qué nodos tienen una copia y con qué huella.
- Órdenes pendientes por nodo (copiar o borrar bloques), que el líder entrega en la respuesta al latido.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import grpc
from google.protobuf import empty_pb2

from dfsha._gen import dfsha_pb2 as pb
from dfsha._gen import dfsha_pb2_grpc as pb_grpc
from dfsha.common.config import Tiempos
from dfsha.common.errores import abortar
from dfsha.common.tls import identidad_par
from dfsha.common.tokens import id_de_clave
from dfsha.metadata.auditoria import ip_de

log = logging.getLogger(__name__)


@dataclass
class EstadoNodo:
    nombre: str
    zona: str = ""
    ip: str = ""                        # dirección desde la que se registró (para ordenar por cercanía)
    capacidad: int = 0
    libre: int = 0
    usado: int = 0
    transferencias: int = 0
    ultimo_latido: float = 0.0          # reloj monotónico
    ultimo_latido_ms: int = 0           # fecha, para mostrarla
    bloques: dict[str, tuple[str, int]] = field(default_factory=dict)   # id -> (huella, tamaño)


class RegistroNodos:
    def __init__(self, tiempos: Tiempos, reloj=time.monotonic):
        self.tiempos = tiempos
        self.reloj = reloj
        self.nodos: dict[str, EstadoNodo] = {}
        self._ordenes: dict[str, list[pb.NodeCommand]] = {}
        self.retirando: set[str] = set()

    # ------------------------------------------------------------ avisos de los nodos
    def registrar(self, nombre: str, zona: str, capacidad: int, libre: int, ip: str = "") -> None:
        n = self.nodos.setdefault(nombre, EstadoNodo(nombre))
        n.zona, n.capacidad, n.libre, n.ip = zona, capacidad, libre, ip
        self._marcar(n)

    def latido(self, nombre: str, libre: int, usado: int, transferencias: int) -> bool:
        """False si el nodo no está registrado (por ejemplo, porque este nodo de metadatos reinició)."""
        n = self.nodos.get(nombre)
        if n is None:
            return False
        n.libre, n.usado, n.transferencias = libre, usado, transferencias
        self._marcar(n)
        return True

    def _marcar(self, n: EstadoNodo) -> None:
        n.ultimo_latido = self.reloj()
        n.ultimo_latido_ms = int(time.time() * 1000)

    def bloque_recibido(self, nodo: str, block_id: str, huella: str, tamano: int) -> None:
        if nodo in self.nodos:
            self.nodos[nodo].bloques[block_id] = (huella, tamano)

    def bloque_borrado(self, nodo: str, block_id: str) -> None:
        if nodo in self.nodos:
            self.nodos[nodo].bloques.pop(block_id, None)

    def inventario(self, nodo: str, bloques: dict[str, tuple[str, int]]) -> None:
        if nodo in self.nodos:
            self.nodos[nodo].bloques = dict(bloques)

    # ------------------------------------------------------------ consultas
    def estado(self, nombre: str) -> int:
        if nombre in self.retirando:
            return pb.NODE_STATE_DECOMMISSIONING
        edad = self.reloj() - self.nodos[nombre].ultimo_latido
        if edad > self.tiempos.muerto_s:
            return pb.NODE_STATE_DEAD
        if edad > self.tiempos.sospechoso_s:
            return pb.NODE_STATE_SUSPECT
        return pb.NODE_STATE_ALIVE

    def vivos(self) -> list[EstadoNodo]:
        return [n for n in self.nodos.values() if self.estado(n.nombre) == pb.NODE_STATE_ALIVE]

    def ubicaciones(self, block_id: str, huella: str | None = None) -> list[str]:
        """Nodos no muertos que tienen el bloque (con esa huella, si se indica)."""
        return [n.nombre for n in self.nodos.values()
                if block_id in n.bloques and (huella is None or n.bloques[block_id][0] == huella)
                and self.estado(n.nombre) != pb.NODE_STATE_DEAD]

    def ordenar(self, nombres: list[str], ip_cliente: str = "") -> list[str]:
        """Réplicas para leer: vivos antes que sospechosos (D6), y entre ellos primero los de la misma
        subred que el cliente (cercanía, D7) y los menos cargados."""
        red = ip_cliente.rsplit(".", 1)[0] if ip_cliente.count(".") == 3 else None

        def clave(nombre: str):
            n = self.nodos.get(nombre)
            if n is None:
                return (2, 1, 0, nombre)
            cerca = red is not None and n.ip.rsplit(".", 1)[0] == red
            return (0 if self.estado(nombre) == pb.NODE_STATE_ALIVE else 1, 0 if cerca else 1, n.transferencias, nombre)
        return sorted(nombres, key=clave)

    def elegir_cadena(self, cantidad: int, excluir: set[str] = frozenset(), minimo: int = 1) -> list[str]:
        """Hasta `cantidad` nodos vivos, primero de zonas distintas y con más espacio libre (D5).

        Si hay menos de `minimo` nodos vivos se suman los sospechosos: un nodo sospechoso puede estar
        solo atrasado con sus latidos por la carga, y no está muerto (D6).
        """
        disponibles = self.vivos()
        if len([n for n in disponibles if n.nombre not in excluir]) < minimo:
            disponibles = [n for n in self.nodos.values() if self.estado(n.nombre) == pb.NODE_STATE_SUSPECT] + disponibles
        candidatos = sorted((n for n in disponibles if n.nombre not in excluir),
                            key=lambda n: (-n.libre, n.transferencias, n.nombre))
        elegidos, zonas = [], set()
        for n in candidatos:                       # una por zona primero
            if n.zona not in zonas:
                elegidos.append(n)
                zonas.add(n.zona)
        elegidos += [n for n in candidatos if n not in elegidos]
        return [n.nombre for n in elegidos[:cantidad]]

    # ------------------------------------------------------------ órdenes
    def encolar(self, nodo: str, orden: pb.NodeCommand) -> None:
        self._ordenes.setdefault(nodo, []).append(orden)

    def tomar_ordenes(self, nodo: str) -> list[pb.NodeCommand]:
        return self._ordenes.pop(nodo, [])


class ServicioNodos(pb_grpc.NodeServiceServicer):
    """NodeService en el puerto interno 50053. Lo atienden los 3 nodos de metadatos.

    Con TLS, el puerto exige certificado y el nodo solo puede hablar en su propio nombre.
    Solo la respuesta del líder lleva órdenes.
    """

    def __init__(self, registro: RegistroNodos, elector, secretos, tiempos: Tiempos, exigir_identidad: bool):
        self.registro = registro
        self.elector = elector
        self.secretos = secretos
        self.tiempos = tiempos
        self.exigir_identidad = exigir_identidad

    async def _comprobar(self, context, nodo: str) -> None:
        if self.exigir_identidad and identidad_par(context) != nodo:
            await abortar(context, pb.REASON_FORBIDDEN, f"el certificado no corresponde a {nodo}")

    def _respuesta(self, nodo: str) -> pb.HeartbeatResponse:
        if not self.elector.es_lider:
            return pb.HeartbeatResponse(from_leader=False)
        return pb.HeartbeatResponse(from_leader=True, commands=self.registro.tomar_ordenes(nodo))

    async def Register(self, request, context):
        await self._comprobar(context, request.node)
        self.registro.registrar(request.node, request.zone, request.capacity_bytes, request.free_bytes,
                                ip_de(context.peer()))
        log.info("nodo de datos %s registrado (zona %s)", request.node, request.zone)
        clave = self.secretos.hmac_bloques
        return pb.RegisterResponse(heartbeat_interval_s=self.tiempos.latido_s, block_token_key=clave,
                                   block_token_key_id=id_de_clave(clave))

    async def Heartbeat(self, request, context):
        await self._comprobar(context, request.node)
        if not self.registro.latido(request.node, request.free_bytes, request.used_bytes, request.active_transfers):
            await abortar(context, pb.REASON_NOT_FOUND, "nodo no registrado, debe registrarse de nuevo")
        return self._respuesta(request.node)

    async def BlockReceived(self, request, context):
        await self._comprobar(context, request.node)
        self.registro.bloque_recibido(request.node, request.block_id, request.block_checksum, request.size)
        return empty_pb2.Empty()

    async def BlockDeleted(self, request, context):
        await self._comprobar(context, request.node)
        self.registro.bloque_borrado(request.node, request.block_id)
        return empty_pb2.Empty()

    async def BlockReport(self, request, context):
        await self._comprobar(context, request.node)
        if request.node not in self.registro.nodos:
            await abortar(context, pb.REASON_NOT_FOUND, "nodo no registrado, debe registrarse de nuevo")
        self.registro.inventario(request.node, {b.block_id: (b.block_checksum, b.size) for b in request.blocks})
        return self._respuesta(request.node)


def lista_de_nodos(registro: RegistroNodos, lider: str) -> pb.NodeList:
    respuesta = pb.NodeList(leader=lider or "")
    for n in sorted(registro.nodos.values(), key=lambda n: n.nombre):
        respuesta.nodes.add(name=n.nombre, zone=n.zona, state=registro.estado(n.nombre), free_bytes=n.libre,
                            used_bytes=n.usado, block_count=len(n.bloques), last_heartbeat_unix_ms=n.ultimo_latido_ms)
    return respuesta
