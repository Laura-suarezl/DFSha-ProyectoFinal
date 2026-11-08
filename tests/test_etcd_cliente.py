"""Cliente de etcd de los nodos de metadatos (dfsha/metadata/etcd.py).

Las pruebas de integración necesitan un etcd en marcha y se activan con la variable
DFSHA_PRUEBA_ETCD, por ejemplo con el clúster del prototipo:
    docker compose -f prototipos/etcd-cluster/docker-compose.yml up -d
    DFSHA_PRUEBA_ETCD=ipv4:127.0.0.1:23791,127.0.0.1:23792,127.0.0.1:23793 pytest -m integracion
"""
import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
subprocess.run([sys.executable, str(RAIZ / "scripts" / "generar_codigo.py")], check=True, capture_output=True)

from dfsha.metadata.etcd import ClienteEtcd, LockOcupado, fin_de_prefijo  # noqa: E402

DESTINO = os.environ.get("DFSHA_PRUEBA_ETCD")
integracion = pytest.mark.skipif(not DESTINO, reason="sin etcd de prueba (DFSHA_PRUEBA_ETCD)")


@pytest.mark.parametrize("prefijo, fin", [
    (b"/dfsha/locks/f1/", b"/dfsha/locks/f10"),
    (b"a", b"b"),
    (b"a\xff", b"b"),
    (b"\xff\xff", b"\0"),
])
def test_fin_de_prefijo(prefijo, fin):
    assert fin_de_prefijo(prefijo) == fin


@pytest.fixture
async def cli():
    async with ClienteEtcd(DESTINO) as c:
        await c.esperar_listo(10)
        yield c


@pytest.fixture
def prefijo():
    return f"/dfsha-pruebas/{uuid.uuid4().hex[:8]}/"


@integracion
@pytest.mark.integracion
async def test_lock_con_token_y_cerco(cli, prefijo):
    sesion = await cli.conceder_lease(30)
    try:
        clave, token = await cli.bloquear(prefijo + "locks/f/b7", sesion, 5)
        assert token > 0
        assert (await cli.txn([cli.comparar_creacion(clave, token)], [cli.op_poner(prefijo + "x", "1")])).succeeded
        await cli.revocar_lease(sesion)                        # la sesión vence
        assert not (await cli.txn([cli.comparar_creacion(clave, token)], [cli.op_poner(prefijo + "x", "2")])).succeeded
    finally:
        await cli.borrar_prefijo(prefijo)


@integracion
@pytest.mark.integracion
async def test_lock_ocupado_tras_la_espera(cli, prefijo):
    s1, s2 = await cli.conceder_lease(30), await cli.conceder_lease(30)
    try:
        await cli.bloquear(prefijo + "locks/f/b1", s1, 5)
        inicio = time.monotonic()
        with pytest.raises(LockOcupado):
            await cli.bloquear(prefijo + "locks/f/b1", s2, 1.0)
        assert 0.9 <= time.monotonic() - inicio < 3
        # etcd borra la entrada del que esperaba del lado del servidor, unos milisegundos después
        # de responder (menos de 30 ms medidos), así que se le da hasta 1 s
        limite = time.monotonic() + 1
        while await cli.contar(prefijo + "locks/f/b1/") != 1 and time.monotonic() < limite:
            await asyncio.sleep(0.01)
        assert await cli.contar(prefijo + "locks/f/b1/") == 1   # la entrada del que esperaba se limpió
    finally:
        await cli.revocar_lease(s1)
        await cli.revocar_lease(s2)
        await cli.borrar_prefijo(prefijo)


@integracion
@pytest.mark.integracion
async def test_esperar_prefijo_vacio(cli, prefijo):
    await cli.poner(prefijo + "intencion/a", "")
    assert not await cli.esperar_prefijo_vacio(prefijo + "intencion/", 0.3)

    async def borrar_luego():
        await asyncio.sleep(0.3)
        await cli.borrar_prefijo(prefijo + "intencion/")

    tarea = asyncio.create_task(borrar_luego())
    inicio = time.monotonic()
    assert await cli.esperar_prefijo_vacio(prefijo + "intencion/", 5)
    assert time.monotonic() - inicio < 1.5                    # despierta con el evento, no por sondeo
    await tarea


@integracion
@pytest.mark.integracion
async def test_eleccion_y_comparacion_de_liderazgo(cli, prefijo):
    l1, l2 = await cli.conceder_lease(5), await cli.conceder_lease(5)
    try:
        lider1 = await cli.postular(prefijo + "eleccion", l1, "mds1")
        segundo = asyncio.create_task(cli.postular(prefijo + "eleccion", l2, "mds2"))
        assert await cli.lider(prefijo + "eleccion") == "mds1"
        await cli.renunciar(lider1)
        lider2 = await asyncio.wait_for(segundo, 5)
        assert await cli.lider(prefijo + "eleccion") == "mds2"
        assert not (await cli.txn([lider1.comparacion()], [cli.op_poner(prefijo + "y", "1")])).succeeded
        assert (await cli.txn([lider2.comparacion()], [cli.op_poner(prefijo + "y", "2")])).succeeded
    finally:
        await cli.revocar_lease(l1)
        await cli.revocar_lease(l2)
        await cli.borrar_prefijo(prefijo)
