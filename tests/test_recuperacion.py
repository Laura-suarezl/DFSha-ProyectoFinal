"""Re-replicación, recolector, Decommission y ReportBadBlock (dfsha/metadata/recuperacion.py), con etcd."""
import pytest
from google.protobuf import empty_pb2

from dfsha._gen import dfsha_pb2 as pb
from dfsha.metadata.recuperacion import Recuperacion
from test_archivos import escribir, lider, recibido, sesion  # noqa: F401
from test_metadatos import cluster, integracion  # noqa: F401

pytestmark = [integracion, pytest.mark.integracion]


def ordenes(lider, nodo, tipo):
    return [o for o in lider.nucleo.nodos._ordenes.get(nodo, []) if o.HasField(tipo)]


async def test_re_replicacion_hasta_r_copias(lider):  # noqa: F811
    meta, sid = await sesion(lider)
    o, a, _ = await escribir(lider, meta, sid, "/admin/r", cantidad=1)   # copias en dn1 y dn2
    rec = Recuperacion(lider.nucleo)
    assert (await rec.ciclo())["copias"] == 1
    orden = ordenes(lider, "dn1.dfsha.internal", "replicate")[0].replicate
    assert orden.block_id == a.blocks[0].block_id and list(orden.targets) == ["dn3.dfsha.internal"]
    assert (await rec.ciclo())["copias"] == 0                              # no repite la orden enseguida
    lider.nucleo.nodos._ordenes.clear()
    rec._pendientes.clear()
    lider.nucleo.nodos.nodos["dn2.dfsha.internal"].ultimo_latido = -1000   # dn2 muerto
    await rec.ciclo()
    assert len(ordenes(lider, "dn1.dfsha.internal", "replicate")[0].replicate.targets) == 2


async def test_recolector_de_huerfanos_y_manifiestos(lider):  # noqa: F811
    meta, sid = await sesion(lider)
    for _ in range(3):                                                     # versiones 1, 2 y 3
        o, a, c = await escribir(lider, meta, sid, "/admin/m", cantidad=1)
        await lider.meta.Close(pb.CloseRequest(fd=o.fd), metadata=meta)
        for n in ("dn3.dfsha.internal",):
            recibido(lider, a.blocks[0].block_id, "x", nodos=[n])
    lider.nucleo.nodos.bloque_recibido("dn4.dfsha.internal", "b-00000000000000ff", "y", 1)
    rec = Recuperacion(lider.nucleo)
    r = await rec.ciclo()
    assert r["borrados"] == 0                                              # huérfanos: sin gracia todavía
    assert r["manifiestos"] >= 3                                           # versiones 0, 1 y 2, nadie las lee
    await rec.ciclo(gracia_s=0)
    assert "b-00000000000000ff" in ordenes(lider, "dn4.dfsha.internal", "remove")[-1].remove.block_ids
    fid = (await lider.meta.Stat(pb.PathRequest(path="/admin/m"), metadata=meta)).entry
    assert fid.version == 3


async def test_decommission_copia_y_luego_vacia(lider):  # noqa: F811
    meta, sid = await sesion(lider)
    _, a, _ = await escribir(lider, meta, sid, "/admin/d", cantidad=1)
    bloque = a.blocks[0].block_id
    await lider.admin.Decommission(pb.DecommissionRequest(node="dn1.dfsha.internal"), metadata=meta)
    estados = {n.name: n.state for n in (await lider.admin.ListNodes(empty_pb2.Empty(), metadata=meta)).nodes}
    assert estados["dn1.dfsha.internal"] == pb.NODE_STATE_DECOMMISSIONING
    rec = Recuperacion(lider.nucleo)
    await rec.ciclo()
    destinos = ordenes(lider, "dn2.dfsha.internal", "replicate")[0].replicate.targets
    assert "dn1.dfsha.internal" not in destinos and len(destinos) == 2
    assert not ordenes(lider, "dn1.dfsha.internal", "remove")              # todavía es una copia necesaria
    huella = lider.nucleo.nodos.nodos["dn1.dfsha.internal"].bloques[bloque][0]
    recibido(lider, bloque, huella, nodos=list(destinos))
    await rec.ciclo()
    assert bloque in ordenes(lider, "dn1.dfsha.internal", "remove")[0].remove.block_ids


async def test_report_bad_block_quita_la_copia(lider):  # noqa: F811
    meta, sid = await sesion(lider)
    _, a, _ = await escribir(lider, meta, sid, "/admin/b", cantidad=1)
    bloque = a.blocks[0].block_id
    await lider.meta.ReportBadBlock(pb.BadBlockReport(block_id=bloque, node="dn2.dfsha.internal:50052",
                                                      reason="huella distinta"), metadata=meta)
    assert lider.nucleo.nodos.ubicaciones(bloque) == ["dn1.dfsha.internal"]
    assert bloque in ordenes(lider, "dn2.dfsha.internal", "remove")[0].remove.block_ids
