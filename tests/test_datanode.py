"""Nodo de datos: almacén en disco (dfsha/datanode/almacen.py) y escritura en cadena (servicio.py)."""
import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import grpc
import pytest

RAIZ = Path(__file__).resolve().parent.parent
subprocess.run([sys.executable, str(RAIZ / "scripts" / "generar_codigo.py")], check=True, capture_output=True)

from dfsha._gen import dfsha_pb2 as pb  # noqa: E402
from dfsha._gen import dfsha_pb2_grpc as pb_grpc  # noqa: E402
from dfsha.common.errores import detalle_de, motivo_de  # noqa: E402
from dfsha.datanode.almacen import Almacen, HuellaIncorrecta, IdInvalido  # noqa: E402
from dfsha.datanode.servicio import ServicioDatos  # noqa: E402

ID = "b-00000000000000a1"
DATOS = os.urandom(3 * 1024 * 1024 + 123)          # 3 MiB y un poco, último trozo incompleto
HUELLA = hashlib.sha256(DATOS).hexdigest()
TROZO = 256 * 1024


# ----------------------------------------------------------------------------- almacén
def test_guardar_y_leer(tmp_path):
    almacen = Almacen(tmp_path)
    escritura = almacen.nueva_escritura(ID)
    escritura.escribir(DATOS[:1000])
    escritura.escribir(DATOS[1000:])
    assert escritura.confirmar(HUELLA) == len(DATOS)
    assert almacen.listar() == [ID]
    assert almacen.info(ID) == (HUELLA, len(DATOS))
    assert b"".join(almacen.leer(ID, TROZO)) == DATOS
    assert almacen.verificar(ID)
    assert not list(almacen.tmp.iterdir())


def test_huella_incorrecta_no_deja_nada(tmp_path):
    almacen = Almacen(tmp_path)
    escritura = almacen.nueva_escritura(ID)
    escritura.escribir(DATOS)
    with pytest.raises(HuellaIncorrecta):
        escritura.confirmar("0" * 64)
    assert almacen.listar() == []
    assert not list(almacen.tmp.iterdir())


def test_caida_a_mitad_deja_solo_el_temporal_y_se_limpia(tmp_path):
    almacen = Almacen(tmp_path)
    escritura = almacen.nueva_escritura(ID)
    escritura.escribir(DATOS[:5000])
    escritura._archivo.close()                       # el proceso muere: el sistema cierra sus archivos
    assert almacen.listar() == []
    assert Almacen(tmp_path).limpiar_temporales() == 1      # al volver a arrancar
    assert not list(almacen.tmp.iterdir())


def test_guardar_dos_veces_es_idempotente(tmp_path):
    almacen = Almacen(tmp_path)
    for _ in range(2):
        escritura = almacen.nueva_escritura(ID)
        escritura.escribir(DATOS)
        escritura.confirmar(HUELLA)
    assert almacen.listar() == [ID] and almacen.verificar(ID)


def test_revision_detecta_corrupcion(tmp_path):
    almacen = Almacen(tmp_path)
    escritura = almacen.nueva_escritura(ID)
    escritura.escribir(DATOS)
    escritura.confirmar(HUELLA)
    with open(almacen.bloques / ID, "r+b") as f:
        f.write(b"X")
    assert not almacen.verificar(ID)


@pytest.mark.parametrize("malo", ["../../etc/passwd", "b-123", "B-00000000000000A1", "", "b-00000000000000a1/x"])
def test_ids_invalidos(tmp_path, malo):
    with pytest.raises(IdInvalido):
        Almacen(tmp_path).nueva_escritura(malo)


# ----------------------------------------------------------------------------- cadena
@pytest.fixture
async def cadena(tmp_path):
    """Tres nodos de datos reales en puertos locales."""
    servidores, nodos = [], []
    for i in (1, 2, 3):
        almacen = Almacen(tmp_path / f"dn{i}")
        servicio = ServicioDatos(almacen, f"dn{i}", TROZO, timeout_reenvio_s=3)
        servidor = grpc.aio.server()
        pb_grpc.add_DataServiceServicer_to_server(servicio, servidor)
        puerto = servidor.add_insecure_port("127.0.0.1:0")
        await servidor.start()
        servidores.append((servidor, servicio))
        nodos.append((f"127.0.0.1:{puerto}", almacen))
    yield nodos
    for servidor, servicio in servidores:
        await servidor.stop(None)
        await servicio.cerrar()


def trozos(cadena_restante, huella=HUELLA, datos=DATOS, block_id=ID):
    for n, i in enumerate(range(0, len(datos), TROZO)):
        t = pb.ChunkPayload(sequence_number=n, data=datos[i:i + TROZO], is_last_chunk=i + TROZO >= len(datos))
        if n == 0:
            t.block_id, t.block_checksum = block_id, huella
            t.downstream_nodes.extend(cadena_restante)
        yield t


async def escribir(nodos_destino, **kw):
    async with grpc.aio.insecure_channel(nodos_destino[0]) as canal:
        return await pb_grpc.DataServiceStub(canal).WriteBlock(trozos(nodos_destino[1:], **kw))


async def test_cadena_de_tres_guarda_tres_copias_identicas(cadena):
    direcciones = [d for d, _ in cadena]
    resultado = await escribir(direcciones)
    assert list(resultado.stored_on) == ["dn1", "dn2", "dn3"]
    for direccion, almacen in cadena:
        assert almacen.info(ID) == (HUELLA, len(DATOS))
        async with grpc.aio.insecure_channel(direccion) as canal:
            leido = b"".join([t.data async for t in pb_grpc.DataServiceStub(canal).ReadBlock(
                pb.ReadBlockRequest(block_id=ID))])
        assert leido == DATOS


async def test_huella_incorrecta_en_la_cadena(cadena):
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await escribir([d for d, _ in cadena], huella="0" * 64)
    assert error.value.code() == grpc.StatusCode.DATA_LOSS
    assert motivo_de(error.value) == "CHECKSUM_MISMATCH"
    assert detalle_de(error.value).block_id == ID
    # Los nodos 2 y 3 detectan la huella por su cuenta y limpian en paralelo: se les da un momento.
    for _ in range(40):
        if all(a.listar() == [] and not list(a.tmp.iterdir()) for _, a in cadena):
            break
        await asyncio.sleep(0.05)
    for _, almacen in cadena:
        assert almacen.listar() == [] and not list(almacen.tmp.iterdir())


async def test_siguiente_nodo_inalcanzable_deja_las_copias_que_se_pudieron(cadena):
    (d1, _), (d2, _), _ = cadena
    resultado = await escribir([d1, d2, "127.0.0.1:1"])    # el tercero no existe
    assert list(resultado.stored_on) == ["dn1", "dn2"]      # W = 2 se cumple


async def test_reenvio_por_otra_cadena_con_el_mismo_id(cadena):
    (d1, a1), _, (d3, a3) = cadena
    primera = await escribir([d1, "127.0.0.1:1"])          # el segundo nodo cayó
    assert list(primera.stored_on) == ["dn1"]
    segunda = await escribir([d1, d3])                       # el cliente reenvía sin ese nodo
    assert list(segunda.stored_on) == ["dn1", "dn3"]
    assert a1.verificar(ID) and a3.verificar(ID)


async def test_leer_un_bloque_que_no_existe(cadena):
    async with grpc.aio.insecure_channel(cadena[0][0]) as canal:
        with pytest.raises(grpc.aio.AioRpcError) as error:
            async for _ in pb_grpc.DataServiceStub(canal).ReadBlock(pb.ReadBlockRequest(block_id="b-00000000000000ff")):
                pass
    assert error.value.code() == grpc.StatusCode.NOT_FOUND and motivo_de(error.value) == "NOT_FOUND"


async def test_diagnostico(cadena):
    direccion, almacen = cadena[0]
    await escribir([direccion])
    with open(almacen.bloques / ID, "r+b") as f:
        f.write(b"X")
    async with grpc.aio.insecure_channel(direccion) as canal:
        r = await pb_grpc.DataServiceStub(canal).CheckBlockHealth(pb.HealthCheckRequest())
    assert r.node == "dn1"
    assert [(b.block_id, b.present, b.checksum_ok) for b in r.blocks] == [(ID, True, False)]


async def test_trozo_fuera_de_orden(cadena):
    def desordenados():
        t = list(trozos([]))
        t[2].sequence_number = 7
        yield from t
    async with grpc.aio.insecure_channel(cadena[0][0]) as canal:
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await pb_grpc.DataServiceStub(canal).WriteBlock(desordenados())
    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert cadena[0][1].listar() == [] and not list(cadena[0][1].tmp.iterdir())
