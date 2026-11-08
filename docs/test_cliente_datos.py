"""Ruta de datos del cliente (dfsha/client/datos.py) con nodos de datos reales en este proceso."""
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
from dfsha.client.datos import ErrorDatos, Transferencia, cifrar_bloque, descifrar_bloque, sha256  # noqa: E402
from dfsha.common.config import cargar  # noqa: E402
from dfsha.datanode.almacen import Almacen  # noqa: E402
from dfsha.datanode.servicio import ServicioDatos  # noqa: E402

CLAVE = os.urandom(32)
CFG = cargar(entorno={"DFSHA_ALMACENAMIENTO_TAMANO_TROZO": "64KiB", "DFSHA_TIEMPOS_TIMEOUT_REPLICA_S": "1"})


def test_cifrado_por_bloque_con_datos_asociados():
    c = cifrar_bloque(CLAVE, "f-1", 3, b"hola")
    assert b"hola" not in c and descifrar_bloque(CLAVE, "f-1", 3, c) == b"hola"
    assert cifrar_bloque(CLAVE, "f-1", 3, b"hola") != c                   # nonce nuevo cada vez
    for archivo, indice in (("f-1", 4), ("f-2", 3)):                       # bloque movido de lugar
        with pytest.raises(ErrorDatos):
            descifrar_bloque(CLAVE, archivo, indice, c)


@pytest.fixture
async def nodos(tmp_path):
    lista = []
    for i in (1, 2, 3):
        almacen = Almacen(tmp_path / f"dn{i}")
        servicio = ServicioDatos(almacen, f"dn{i}", 64 * 1024, timeout_reenvio_s=3)
        servidor = grpc.aio.server()
        pb_grpc.add_DataServiceServicer_to_server(servicio, servidor)
        puerto = servidor.add_insecure_port("127.0.0.1:0")
        await servidor.start()
        lista.append((f"127.0.0.1:{puerto}", almacen, servidor, servicio))
    yield lista
    for _, _, servidor, servicio in lista:
        await servidor.stop(None)
        await servicio.cerrar()


async def test_escritura_en_cadena_y_lectura_con_cambio_de_replica(nodos):
    t = Transferencia(CFG, None)
    plano = os.urandom(300 * 1024)
    cifrado = cifrar_bloque(CLAVE, "f-1", 0, plano)
    asignado = pb.AllocatedBlock(index=0, block_id="b-00000000000000c1", chain=[n[0] for n in nodos])
    assert await t.escribir(asignado, cifrado) == ["dn1", "dn2", "dn3"]
    # dn2 tiene la copia dañada y la primera réplica no existe
    with open(nodos[1][1].bloques / asignado.block_id, "r+b") as f:
        f.write(b"X")
    ubic = pb.BlockLocation(index=0, block_id=asignado.block_id, cipher_checksum=sha256(cifrado),
                            replicas=["127.0.0.1:1", nodos[1][0], nodos[2][0]], size=len(cifrado))
    assert descifrar_bloque(CLAVE, "f-1", 0, await t.leer(ubic)) == plano
    assert [(m.node, m.reason.split(" ")[0]) for m in t.malas] == [("127.0.0.1:1", "sin"), (nodos[1][0], "huella")]
    ubic.replicas[:] = ["127.0.0.1:1"]
    with pytest.raises(ErrorDatos):
        await t.leer(ubic)
    await t.cerrar()


async def test_cadena_cortada_deja_menos_copias(nodos):
    t = Transferencia(CFG, None)
    asignado = pb.AllocatedBlock(index=0, block_id="b-00000000000000c2", chain=[nodos[0][0], "127.0.0.1:1"])
    assert await t.escribir(asignado, b"x" * 1000) == ["dn1"]             # el cliente pedirá otra cadena
    asignado.chain[:] = ["127.0.0.1:1"]
    assert await t.escribir(asignado, b"x") == []
    await t.cerrar()
