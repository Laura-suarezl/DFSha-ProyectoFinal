"""El contrato de DFSha (proto/dfsha.proto) compila, tiene las operaciones acordadas y funciona por la red."""
import hashlib
import importlib
import subprocess
import sys
from pathlib import Path

import grpc
import pytest
from google.protobuf import descriptor_pb2

RAIZ = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module", autouse=True)
def codigo_generado():
    resultado = subprocess.run(
        [sys.executable, str(RAIZ / "scripts" / "generar_codigo.py")],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    importlib.invalidate_caches()


@pytest.fixture(scope="module")
def pb():
    pb2 = importlib.import_module("dfsha._gen.dfsha_pb2")
    pb2_grpc = importlib.import_module("dfsha._gen.dfsha_pb2_grpc")
    return pb2, pb2_grpc


SERVICIOS = {
    "MetadataService": {"Login", "Refresh", "Renew", "List", "Stat", "Mkdir", "Rmdir", "Remove", "Rename",
                        "Open", "GetBlockLocations", "AllocateBlocks", "Lock", "Unlock", "Commit", "Close",
                        "ReportBadBlock"},
    "AdminService": {"CreateUser", "SetQuota", "ListNodes", "Decommission"},
    "DataService": {"WriteBlock", "ReadBlock", "CheckBlockHealth"},
    "NodeService": {"Register", "Heartbeat", "BlockReceived", "BlockDeleted", "BlockReport"},
}


@pytest.mark.parametrize("servicio, operaciones", SERVICIOS.items())
def test_servicios_y_operaciones(pb, servicio, operaciones):
    pb2, pb2_grpc = pb
    descriptor = pb2.DESCRIPTOR.services_by_name[servicio]
    assert {m.name for m in descriptor.methods} == operaciones
    assert hasattr(pb2_grpc, f"{servicio}Stub")


def test_streaming_de_bloques(pb):
    pb2, _ = pb
    servicio = descriptor_pb2.ServiceDescriptorProto()
    pb2.DESCRIPTOR.services_by_name["DataService"].CopyToProto(servicio)
    metodos = {m.name: m for m in servicio.method}
    assert metodos["WriteBlock"].client_streaming and not metodos["WriteBlock"].server_streaming
    assert metodos["ReadBlock"].server_streaming and not metodos["ReadBlock"].client_streaming


def test_chunk_payload(pb):
    pb2, _ = pb
    campos = {f.name: f.number for f in pb2.ChunkPayload.DESCRIPTOR.fields}
    assert campos == {"block_id": 1, "sequence_number": 2, "data": 3, "is_last_chunk": 4,
                      "block_checksum": 5, "downstream_nodes": 6, "access_token": 7}


def test_motivos_de_error(pb):
    pb2, _ = pb
    motivos = {v.name.removeprefix("REASON_") for v in pb2.Reason.DESCRIPTOR.values}
    esperados = {"TOKEN_INVALID", "FORBIDDEN", "NOT_FOUND", "EXISTS", "DIR_NOT_EMPTY", "NOT_LEADER",
                 "VERSION_CONFLICT", "LEASE_EXPIRED", "LOCKED", "NO_QUORUM", "INSUFFICIENT_REPLICAS",
                 "QUOTA_EXCEEDED", "NO_SPACE", "OVERLOADED", "CHECKSUM_MISMATCH", "BAD_OFFSET"}
    assert esperados <= motivos


async def test_por_la_red_streaming_y_errores(pb):
    """Con un servidor gRPC real en un puerto local se prueban la escritura y lectura por streaming y el error con motivo."""
    pb2, pb2_grpc = pb
    guardado = {}

    class Datos(pb2_grpc.DataServiceServicer):
        async def WriteBlock(self, request_iterator, context):
            partes, cabecera = [], None
            async for trozo in request_iterator:
                cabecera = cabecera or trozo
                partes.append(trozo.data)
            bloque = b"".join(partes)
            if hashlib.sha256(bloque).hexdigest() != cabecera.block_checksum:
                detalle = pb2.ErrorDetail(reason=pb2.REASON_CHECKSUM_MISMATCH, block_id=cabecera.block_id)
                context.set_trailing_metadata((("dfsha-error-bin", detalle.SerializeToString()),))
                await context.abort(grpc.StatusCode.DATA_LOSS, "la huella no coincide")
            guardado[cabecera.block_id] = bloque
            return pb2.WriteResult(block_id=cabecera.block_id, stored_on=["dn1.dfsha.internal"])

        async def ReadBlock(self, request, context):
            bloque = guardado[request.block_id]
            for i in range(0, len(bloque), 1024):
                yield pb2.ChunkPayload(sequence_number=i // 1024, data=bloque[i:i + 1024],
                                       is_last_chunk=i + 1024 >= len(bloque))

    servidor = grpc.aio.server()
    pb2_grpc.add_DataServiceServicer_to_server(Datos(), servidor)
    puerto = servidor.add_insecure_port("127.0.0.1:0")
    await servidor.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{puerto}") as canal:
            stub = pb2_grpc.DataServiceStub(canal)
            datos = bytes(range(256)) * 20          # 5 KB en 5 trozos de 1 KB
            huella = hashlib.sha256(datos).hexdigest()

            def trozos(checksum):
                for n, i in enumerate(range(0, len(datos), 1024)):
                    if n == 0:
                        yield pb2.ChunkPayload(block_id="b-0000000000000001", sequence_number=0,
                                               data=datos[i:i + 1024], block_checksum=checksum,
                                               downstream_nodes=["dn2.dfsha.internal:50052"])
                    else:
                        yield pb2.ChunkPayload(sequence_number=n, data=datos[i:i + 1024],
                                               is_last_chunk=i + 1024 >= len(datos))

            resultado = await stub.WriteBlock(trozos(huella))
            assert resultado.block_id == "b-0000000000000001"

            leido = b"".join([t.data async for t in stub.ReadBlock(
                pb2.ReadBlockRequest(block_id="b-0000000000000001"))])
            assert leido == datos

            with pytest.raises(grpc.aio.AioRpcError) as error:
                await stub.WriteBlock(trozos("0" * 64))
            assert error.value.code() == grpc.StatusCode.DATA_LOSS
            crudo = dict(error.value.trailing_metadata())["dfsha-error-bin"]
            assert pb2.ErrorDetail.FromString(crudo).reason == pb2.REASON_CHECKSUM_MISMATCH
    finally:
        await servidor.stop(None)
