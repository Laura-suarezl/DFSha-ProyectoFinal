"""Espacio de nombres, Open, locks y commit (dfsha/metadata/archivos.py), sobre 3 nodos de metadatos y etcd.

Los nodos de datos se simulan en el registro de nodos del líder (registro y avisos BlockReceived).
"""
import hashlib
import os

import grpc
import pytest

from dfsha._gen import dfsha_pb2 as pb
from dfsha.common import tokens
from dfsha.common.errores import detalle_de, motivo_de
from test_metadatos import CONTRASENA_ADMIN, _error, _lider, cluster, con_token, integracion  # noqa: F401

pytestmark = [integracion, pytest.mark.integracion]
MIB = 1024 * 1024


async def sesion(lider, usuario="admin", clave=CONTRASENA_ADMIN):
    r = await lider.meta.Login(pb.LoginRequest(username=usuario, password=clave))
    return con_token(r.access_token), r.session_id


@pytest.fixture
async def lider(cluster):  # noqa: F811
    lider = await _lider(cluster)
    for i, zona in ((1, "a"), (2, "b"), (3, "c"), (4, "a")):
        lider.nucleo.nodos.registrar(f"dn{i}.dfsha.internal", zona, 100 * MIB, (100 - i) * MIB)
    return lider


def recibido(lider, bloque, huella, nodos=("dn1.dfsha.internal", "dn2.dfsha.internal")):
    for n in nodos:
        lider.nucleo.nodos.bloque_recibido(n, bloque, huella, MIB)


async def escribir(lider, meta, sid, ruta, cantidad=2):
    """Open en modo escritura, AllocateBlocks, avisos simulados y Commit. Devuelve (fd, bloques, open)."""
    o = await lider.meta.Open(pb.OpenRequest(path=ruta, mode=pb.OPEN_MODE_WRITE, session_id=sid), metadata=meta)
    a = await lider.meta.AllocateBlocks(pb.AllocateBlocksRequest(fd=o.fd, indices=list(range(cantidad))), metadata=meta)
    bloques = []
    for b in a.blocks:
        H = hashlib.sha256(b.block_id.encode()).hexdigest()
        recibido(lider, b.block_id, H)
        bloques.append(pb.CommittedBlock(index=b.index, new_block_id=b.block_id, cipher_checksum=H,
                                         plain_checksum=H, size=MIB, fencing_token=o.locks[0].fencing_token))
    c = await lider.meta.Commit(pb.CommitRequest(fd=o.fd, op_id=os.urandom(8).hex(), blocks=bloques,
                                                 new_size=cantidad * MIB, replace_all=True), metadata=meta)
    return o, a, c


async def test_espacio_de_nombres(lider):
    meta, _ = await sesion(lider)
    m = lider.meta
    await m.Mkdir(pb.PathRequest(path="/admin/docs"), metadata=meta)
    await m.Mkdir(pb.PathRequest(path="/admin/docs/sub"), metadata=meta)
    assert motivo_de(await _error(m.Mkdir(pb.PathRequest(path="/admin/docs"), metadata=meta))) == "EXISTS"
    assert motivo_de(await _error(m.Mkdir(pb.PathRequest(path="/otro/x"), metadata=meta))) == "FORBIDDEN"
    assert motivo_de(await _error(m.Mkdir(pb.PathRequest(path="/admin/no/x"), metadata=meta))) == "NOT_FOUND"
    assert motivo_de(await _error(m.Rmdir(pb.PathRequest(path="/admin/docs"), metadata=meta))) == "DIR_NOT_EMPTY"
    await m.Rename(pb.RenameRequest(source="/admin/docs/sub", destination="/admin/sub2"), metadata=meta)
    e = await _error(m.Rename(pb.RenameRequest(source="/admin/docs", destination="/admin/docs/dentro"), metadata=meta))
    assert e.code() == grpc.StatusCode.INVALID_ARGUMENT
    nombres = [x.name for x in (await m.List(pb.PathRequest(path="/admin"), metadata=meta)).entries]
    assert sorted(nombres) == ["docs", "sub2"]
    await m.Rmdir(pb.PathRequest(path="/admin/docs"), metadata=meta)
    assert (await m.Stat(pb.PathRequest(path="/admin/sub2"), metadata=meta)).entry.type == pb.ENTRY_TYPE_DIRECTORY


async def test_escritura_completa_y_lectura(lider):
    meta, sid = await sesion(lider)
    o, a, c = await escribir(lider, meta, sid, "/admin/f.bin")
    assert len(o.locks) == 1 and o.locks[0].target.whole_file and len(o.file_key) == 32
    assert a.blocks[0].chain[0].endswith(":50052") and all(x.endswith(":50054") for x in a.blocks[0].chain[1:])
    assert len({x.split(":")[0] for x in a.blocks[0].chain}) == 3
    assert c.version == 1
    await lider.meta.Close(pb.CloseRequest(fd=o.fd), metadata=meta)
    leer = await lider.meta.Open(pb.OpenRequest(path="/admin/f.bin", mode=pb.OPEN_MODE_READ, session_id=sid), metadata=meta)
    assert leer.version == 1 and leer.size == 2 * MIB and leer.file_key == o.file_key
    assert leer.file_checksum == c.file_checksum and len(leer.blocks) == 2
    b0 = leer.blocks[0]
    assert sorted(b0.replicas) == ["dn1.dfsha.internal:50052", "dn2.dfsha.internal:50052"]
    clave = lider.nucleo.autenticador.secretos.hmac_bloques
    assert tokens.verificar(clave, b0.access_token, b0.block_id, tokens.LEER) == "admin"
    st = await lider.meta.Stat(pb.PathRequest(path="/admin/f.bin"), metadata=meta)
    assert st.entry.size == 2 * MIB and st.entry.version == 1
    assert (await lider.nucleo.usuarios.obtener("admin")).usados_bytes == 2 * MIB
    e = await _error(lider.meta.AllocateBlocks(pb.AllocateBlocksRequest(fd=leer.fd, indices=[2]), metadata=meta))
    assert motivo_de(e) == "FORBIDDEN"                                  # abierto para leer


async def test_commit_exige_copias_y_es_idempotente(lider):
    meta, sid = await sesion(lider)
    o = await lider.meta.Open(pb.OpenRequest(path="/admin/g", mode=pb.OPEN_MODE_WRITE, session_id=sid), metadata=meta)
    a = await lider.meta.AllocateBlocks(pb.AllocateBlocksRequest(fd=o.fd, indices=[0]), metadata=meta)
    b = a.blocks[0]
    bloque = pb.CommittedBlock(index=0, new_block_id=b.block_id, cipher_checksum="aa" * 32, plain_checksum="bb" * 32,
                               size=10, fencing_token=o.locks[0].fencing_token)
    pedido = pb.CommitRequest(fd=o.fd, op_id="op-1", blocks=[bloque], new_size=10, replace_all=True)
    recibido(lider, b.block_id, "aa" * 32, nodos=["dn1.dfsha.internal"])
    e = await _error(lider.meta.Commit(pedido, metadata=meta))
    assert motivo_de(e) == "INSUFFICIENT_REPLICAS" and detalle_de(e).retry_after_ms > 0
    recibido(lider, b.block_id, "aa" * 32)
    assert (await lider.meta.Commit(pedido, metadata=meta)).version == 1
    assert (await lider.meta.Commit(pedido, metadata=meta)).version == 1   # reintento con el mismo op_id


async def test_modificacion_por_bloque_y_conflicto(lider):
    meta, sid = await sesion(lider)
    o, _, _ = await escribir(lider, meta, sid, "/admin/h")
    await lider.meta.Close(pb.CloseRequest(fd=o.fd), metadata=meta)
    rw = await lider.meta.Open(pb.OpenRequest(path="/admin/h", mode=pb.OPEN_MODE_READ_WRITE, session_id=sid), metadata=meta)
    g = (await lider.meta.Lock(pb.LockRequest(fd=rw.fd, targets=[pb.LockTarget(block_index=1)]), metadata=meta)).grants[0]
    assert g.current_block_id == rw.blocks[1].block_id and g.fencing_token > 0
    a = await lider.meta.AllocateBlocks(pb.AllocateBlocksRequest(fd=rw.fd, indices=[1]), metadata=meta)
    nuevo = a.blocks[0].block_id
    recibido(lider, nuevo, "cc" * 32)
    malo = pb.CommittedBlock(index=1, new_block_id=nuevo, base_block_id="b-0000000000000000", cipher_checksum="cc" * 32,
                             plain_checksum="cc" * 32, size=MIB, fencing_token=g.fencing_token)
    e = await _error(lider.meta.Commit(pb.CommitRequest(fd=rw.fd, op_id="x1", blocks=[malo], new_size=2 * MIB),
                                       metadata=meta))
    assert motivo_de(e) == "VERSION_CONFLICT" and detalle_de(e).current_block_id == g.current_block_id
    malo.base_block_id = g.current_block_id
    c = await lider.meta.Commit(pb.CommitRequest(fd=rw.fd, op_id="x2", blocks=[malo], new_size=2 * MIB), metadata=meta)
    assert c.version == 2
    loc = await lider.meta.GetBlockLocations(pb.BlockRangeRequest(fd=rw.fd, first_index=0, last_index=1), metadata=meta)
    assert [b.block_id for b in loc.blocks] == [rw.blocks[0].block_id, nuevo]     # el bloque 0 no cambió


async def test_locks_en_cola_jerarquia_y_close(lider):
    meta, sid_a = await sesion(lider)
    _, sid_b = await sesion(lider)
    o, _, _ = await escribir(lider, meta, sid_a, "/admin/k")
    await lider.meta.Close(pb.CloseRequest(fd=o.fd), metadata=meta)
    fa = (await lider.meta.Open(pb.OpenRequest(path="/admin/k", mode=pb.OPEN_MODE_READ_WRITE, session_id=sid_a),
                                metadata=meta)).fd
    fb = (await lider.meta.Open(pb.OpenRequest(path="/admin/k", mode=pb.OPEN_MODE_READ_WRITE, session_id=sid_b),
                                metadata=meta)).fd
    await lider.meta.Lock(pb.LockRequest(fd=fa, targets=[pb.LockTarget(block_index=0)]), metadata=meta)
    e = await _error(lider.meta.Lock(pb.LockRequest(fd=fb, targets=[pb.LockTarget(block_index=0)], wait_s=1),
                                     metadata=meta))
    assert motivo_de(e) == "LOCKED" and detalle_de(e).lock_owner == sid_a
    await lider.meta.Lock(pb.LockRequest(fd=fb, targets=[pb.LockTarget(block_index=1)], wait_s=1), metadata=meta)
    e = await _error(lider.meta.Lock(pb.LockRequest(fd=fb, targets=[pb.LockTarget(whole_file=True)], wait_s=1),
                                     metadata=meta))
    assert motivo_de(e) == "LOCKED"                                     # A tiene un lock de bloque
    await lider.meta.Close(pb.CloseRequest(fd=fa), metadata=meta)       # libera el bloque 0
    g = await lider.meta.Lock(pb.LockRequest(fd=fb, targets=[pb.LockTarget(block_index=0)], wait_s=1), metadata=meta)
    assert g.grants[0].fencing_token > 0


async def test_sesion_vencida_invalida_el_commit(lider):
    meta, sid = await sesion(lider)
    o = await lider.meta.Open(pb.OpenRequest(path="/admin/v", mode=pb.OPEN_MODE_WRITE, session_id=sid), metadata=meta)
    a = await lider.meta.AllocateBlocks(pb.AllocateBlocksRequest(fd=o.fd, indices=[0]), metadata=meta)
    recibido(lider, a.blocks[0].block_id, "dd" * 32)
    await lider.nucleo.cli.revocar_lease(int(sid, 16))
    e = await _error(lider.meta.Commit(pb.CommitRequest(fd=o.fd, op_id="v1", new_size=1, replace_all=True, blocks=[
        pb.CommittedBlock(index=0, new_block_id=a.blocks[0].block_id, cipher_checksum="dd" * 32, plain_checksum="dd" * 32,
                          size=1, fencing_token=o.locks[0].fencing_token)]), metadata=meta))
    assert motivo_de(e) == "LEASE_EXPIRED"


async def test_cuota_y_borrado(lider):
    meta, sid = await sesion(lider)
    await lider.nucleo.usuarios.fijar_cuota("admin", MIB)
    o = await lider.meta.Open(pb.OpenRequest(path="/admin/q", mode=pb.OPEN_MODE_WRITE, session_id=sid), metadata=meta)
    e = await _error(lider.meta.AllocateBlocks(pb.AllocateBlocksRequest(fd=o.fd, indices=[0]), metadata=meta))
    assert motivo_de(e) == "QUOTA_EXCEEDED" and detalle_de(e).limit_bytes == MIB
    assert motivo_de(await _error(lider.meta.Remove(pb.PathRequest(path="/admin/q"), metadata=meta))) == "LOCKED"
    await lider.meta.Close(pb.CloseRequest(fd=o.fd), metadata=meta)
    await lider.meta.Remove(pb.PathRequest(path="/admin/q"), metadata=meta)
    assert motivo_de(await _error(lider.meta.Stat(pb.PathRequest(path="/admin/q"), metadata=meta))) == "NOT_FOUND"
