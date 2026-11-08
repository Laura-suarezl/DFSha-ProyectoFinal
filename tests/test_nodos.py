"""Tokens de acceso, estado de los nodos y NodeService (dfsha/common/tokens.py, dfsha/metadata/nodos.py,
dfsha/datanode/latidos.py).

Las pruebas de integración no necesitan etcd: levantan 3 NodeService (uno hace de líder) y 2 nodos
de datos reales en este proceso, sin TLS.
"""
import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import grpc
import pytest

RAIZ = Path(__file__).resolve().parent.parent
subprocess.run([sys.executable, str(RAIZ / "scripts" / "generar_codigo.py")], check=True, capture_output=True)

from dfsha._gen import dfsha_pb2 as pb  # noqa: E402
from dfsha._gen import dfsha_pb2_grpc as pb_grpc  # noqa: E402
from dfsha.common import tokens  # noqa: E402
from dfsha.common.config import Tiempos, cargar  # noqa: E402
from dfsha.common.errores import motivo_de  # noqa: E402
from dfsha.datanode.almacen import Almacen  # noqa: E402
from dfsha.datanode.latidos import ClienteMetadatos  # noqa: E402
from dfsha.datanode.servicio import ServicioDatos  # noqa: E402
from dfsha.metadata.nodos import RegistroNodos, ServicioNodos, lista_de_nodos  # noqa: E402

CLAVE = os.urandom(32)
DATOS = os.urandom(300 * 1024)
HUELLA = hashlib.sha256(DATOS).hexdigest()
ID = "b-00000000000000b1"


# ---------------------------------------------------------------- unitarias
def test_tokens_de_bloque():
    t = tokens.emitir(CLAVE, ID, tokens.LEER, "ana", 600)
    assert tokens.verificar(CLAVE, t, ID, tokens.LEER) == "ana"
    casos = [
        (os.urandom(32), t, ID, tokens.LEER),                        # otra clave
        (CLAVE, t, "b-00000000000000b2", tokens.LEER),              # otro bloque
        (CLAVE, t, ID, tokens.ESCRIBIR),                             # otra operación
        (CLAVE, tokens.emitir(CLAVE, ID, tokens.LEER, "ana", -1), ID, tokens.LEER),   # vencido
        (CLAVE, b"basura", ID, tokens.LEER),
        (CLAVE, t.replace(b".", b"x."), ID, tokens.LEER),           # datos alterados
    ]
    for clave, token, bloque, op in casos:
        with pytest.raises(tokens.TokenBloqueInvalido):
            tokens.verificar(clave, token, bloque, op)


def test_estados_por_edad_del_latido():
    ahora = [0.0]
    r = RegistroNodos(Tiempos(), reloj=lambda: ahora[0])
    r.registrar("dn1", "a", 100, 50)
    assert r.estado("dn1") == pb.NODE_STATE_ALIVE
    ahora[0] = 11
    assert r.estado("dn1") == pb.NODE_STATE_SUSPECT
    ahora[0] = 31
    assert r.estado("dn1") == pb.NODE_STATE_DEAD
    r.bloque_recibido("dn1", ID, HUELLA, 1)
    assert r.ubicaciones(ID) == []                                  # un nodo muerto no cuenta
    assert r.latido("dn1", 50, 50, 0) and r.ubicaciones(ID, HUELLA) == ["dn1"]
    assert not r.latido("dn9", 0, 0, 0)


def test_cadena_prefiere_zonas_distintas_y_espacio_libre():
    r = RegistroNodos(Tiempos())
    for nombre, zona, libre in [("dn1", "a", 10), ("dn2", "a", 90), ("dn3", "b", 50), ("dn4", "c", 5)]:
        r.registrar(nombre, zona, 100, libre)
    assert r.elegir_cadena(3) == ["dn2", "dn3", "dn4"]
    assert r.elegir_cadena(4) == ["dn2", "dn3", "dn4", "dn1"]
    assert r.elegir_cadena(3, excluir={"dn2"}) == ["dn3", "dn1", "dn4"]


# ---------------------------------------------------------------- integración
class Metadatos:
    def __init__(self, lider: bool):
        self.registro = RegistroNodos(Tiempos())
        self.elector = SimpleNamespace(es_lider=lider)
        self.servidor = grpc.aio.server()
        servicio = ServicioNodos(self.registro, self.elector, SimpleNamespace(hmac_bloques=CLAVE), Tiempos(), False)
        pb_grpc.add_NodeServiceServicer_to_server(servicio, self.servidor)
        self.destino = f"127.0.0.1:{self.servidor.add_insecure_port('127.0.0.1:0')}"


class Datos:
    def __init__(self, nombre, raiz, destinos):
        self.nombre = nombre
        cfg = cargar(entorno={"DFSHA_NODO_NOMBRE": nombre, "DFSHA_NODO_ZONA": f"z-{nombre}"})
        self.almacen = Almacen(raiz / nombre)
        self.servicio = ServicioDatos(self.almacen, nombre, 64 * 1024, timeout_reenvio_s=3, limite_peticiones=4)
        self.servidor = grpc.aio.server()
        pb_grpc.add_DataServiceServicer_to_server(self.servicio, self.servidor)
        self.destino = f"127.0.0.1:{self.servidor.add_insecure_port('127.0.0.1:0')}"
        self.cliente = ClienteMetadatos(cfg, self.almacen, self.servicio, None, destinos=destinos)
        self.canal = grpc.aio.insecure_channel(self.destino)
        self.stub = pb_grpc.DataServiceStub(self.canal)


@pytest.fixture
async def entorno(tmp_path):
    mds = [Metadatos(lider=i == 0) for i in range(3)]
    for m in mds:
        await m.servidor.start()
    dns = [Datos(f"dn{i}", tmp_path, [m.destino for m in mds]) for i in (1, 2)]
    for d in dns:
        await d.servidor.start()
        await d.cliente.registrar(espera_s=5)
    yield mds, dns
    for d in dns:
        await d.cliente.detener()
        await d.canal.close()
        await d.servidor.stop(None)
        await d.servicio.cerrar()
    for m in mds:
        await m.servidor.stop(None)


def trozos(token: bytes, cadena=()):
    yield pb.ChunkPayload(block_id=ID, block_checksum=HUELLA, data=DATOS, is_last_chunk=True,
                          access_token=token, downstream_nodes=list(cadena))


async def esperar(condicion, timeout_s=3):
    for _ in range(int(timeout_s / 0.02)):
        if condicion():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_registro_entrega_la_clave_y_los_tres_conocen_al_nodo(entorno):
    mds, (dn1, dn2) = entorno
    assert dn1.servicio.clave_tokens == CLAVE
    assert all({"dn1", "dn2"} <= set(m.registro.nodos) for m in mds)
    assert mds[0].registro.nodos["dn1"].zona == "z-dn1"
    lista = lista_de_nodos(mds[0].registro, "mds1")
    assert [n.state for n in lista.nodes] == [pb.NODE_STATE_ALIVE] * 2 and lista.leader == "mds1"


async def test_tokens_en_el_nodo_de_datos_y_aviso_a_los_tres(entorno):
    mds, (dn1, dn2) = entorno
    e = await _error(dn1.stub.WriteBlock(trozos(b"")))
    assert motivo_de(e) == "TOKEN_INVALID"
    escribir = tokens.emitir(CLAVE, ID, tokens.ESCRIBIR, "ana", 600)
    r = await dn1.stub.WriteBlock(trozos(escribir, [dn2.destino]))      # sin certificado, dn2 no lo ve como nodo
    assert list(r.stored_on) == ["dn1", "dn2"]
    assert await esperar(lambda: all(m.registro.ubicaciones(ID, HUELLA) == ["dn1", "dn2"] for m in mds))
    e = await _error(_leer(dn1.stub, escribir))                         # token de escritura no sirve para leer
    assert motivo_de(e) == "TOKEN_INVALID"
    assert await _leer(dn1.stub, tokens.emitir(CLAVE, ID, tokens.LEER, "ana", 600)) == DATOS


async def test_ordenes_de_copia_y_borrado_del_lider(entorno):
    mds, (dn1, dn2) = entorno
    dn1.servicio.clave_tokens = dn2.servicio.clave_tokens = None        # sin TLS no hay identidad de nodo
    await dn1.stub.WriteBlock(trozos(b""))
    lider, seguidor = mds[0], mds[1]
    orden = pb.NodeCommand(replicate=pb.ReplicateCommand(block_id=ID, block_checksum=HUELLA, targets=[dn2.destino]))
    seguidor.registro.encolar("dn1", orden)                             # un seguidor nunca entrega órdenes
    lider.registro.encolar("dn1", orden)
    await dn1.cliente.latido()
    assert await esperar(lambda: dn2.almacen.existe(ID))
    assert seguidor.registro._ordenes["dn1"]                            # quedó sin entregar
    assert await esperar(lambda: lider.registro.ubicaciones(ID) == ["dn1", "dn2"])
    lider.registro.encolar("dn2", pb.NodeCommand(remove=pb.DeleteCommand(block_ids=[ID])))
    await dn2.cliente.latido()
    assert await esperar(lambda: not dn2.almacen.existe(ID))
    assert await esperar(lambda: all(m.registro.ubicaciones(ID) == ["dn1"] for m in mds))


async def test_revision_borra_la_copia_danada_y_avisa(entorno):
    mds, (dn1, _) = entorno
    dn1.servicio.clave_tokens = None
    await dn1.stub.WriteBlock(trozos(b""))
    assert await esperar(lambda: mds[0].registro.ubicaciones(ID) == ["dn1"])
    with open(dn1.almacen.bloques / ID, "r+b") as f:
        f.write(b"X")
    assert await dn1.cliente.revisar() == [ID]
    assert not dn1.almacen.existe(ID)
    assert await esperar(lambda: all(m.registro.ubicaciones(ID) == [] for m in mds))


async def test_metadatos_reiniciado_recupera_el_estado_con_el_inventario(entorno):
    mds, (dn1, _) = entorno
    dn1.servicio.clave_tokens = None
    await dn1.stub.WriteBlock(trozos(b""))
    assert await esperar(lambda: mds[2].registro.ubicaciones(ID) == ["dn1"])
    mds[2].registro.nodos.clear()                                       # perdió su estado blando
    await dn1.cliente.latido()                                          # responde NOT_FOUND
    await dn1.cliente.latido()                                          # se registra de nuevo con inventario
    assert mds[2].registro.ubicaciones(ID) == ["dn1"]


async def test_limite_de_peticiones(entorno):
    _, (dn1, _) = entorno
    dn1.servicio.en_curso = dn1.servicio.limite_peticiones
    e = await _error(_leer(dn1.stub, b""))
    dn1.servicio.en_curso = 0
    assert e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED and motivo_de(e) == "OVERLOADED"


async def _leer(stub, token):
    return b"".join([t.data async for t in stub.ReadBlock(pb.ReadBlockRequest(block_id=ID, access_token=token))])


async def _error(llamada):
    with pytest.raises(grpc.aio.AioRpcError) as e:
        await llamada
    return e.value
