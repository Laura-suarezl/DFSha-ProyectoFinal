"""Nodo de metadatos: líder, usuarios, autenticación y administración (dfsha/metadata/).

Las pruebas de integración levantan 3 nodos de metadatos en este mismo proceso, cada uno con su
propio cliente de etcd y su servidor gRPC, sobre el etcd de prueba (variable DFSHA_PRUEBA_ETCD,
ver tests/test_etcd_cliente.py). Cada prueba usa un prefijo propio en etcd y lo borra al final.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import grpc
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

RAIZ = Path(__file__).resolve().parent.parent
subprocess.run([sys.executable, str(RAIZ / "scripts" / "generar_codigo.py")], check=True, capture_output=True)

from dfsha._gen import dfsha_pb2 as pb  # noqa: E402
from dfsha._gen import dfsha_pb2_grpc as pb_grpc  # noqa: E402
from dfsha.common.config import ErrorDeConfiguracion, Kms, cargar  # noqa: E402
from dfsha.common.errores import detalle_de, motivo_de  # noqa: E402
from dfsha.metadata.__main__ import construir, registrar_servicios  # noqa: E402
from dfsha.metadata.auditoria import ip_de  # noqa: E402
from dfsha.metadata.autenticacion import Autenticador, TokenInvalido  # noqa: E402
from dfsha.metadata.claves import (EnvoltorioArchivo, SecretosDelCluster, crear_clave_local,  # noqa: E402
                                   envoltorio_desde)
from dfsha.metadata.etcd import ClienteEtcd  # noqa: E402
from dfsha.metadata.servicio import DejoDeSerLider  # noqa: E402
from dfsha.metadata.usuarios import _HASHER, UsuarioInvalido, validar_nombre  # noqa: E402

DESTINO = os.environ.get("DFSHA_PRUEBA_ETCD")
integracion = pytest.mark.skipif(not DESTINO, reason="sin etcd de prueba (DFSHA_PRUEBA_ETCD)")
CONTRASENA_ADMIN = "clave-del-admin-123"


# ---------------------------------------------------------------- pruebas unitarias

@pytest.mark.parametrize("nombre", ["ana", "admin", "u_2", "eva-c", "a" * 32])
def test_nombres_validos(nombre):
    assert validar_nombre(nombre) == nombre


@pytest.mark.parametrize("nombre", ["", "ab", "Ana", "2ana", "ana/..", "ana b", "a" * 33, "../x"])
def test_nombres_invalidos(nombre):
    with pytest.raises(UsuarioInvalido):
        validar_nombre(nombre)


def test_las_contrasenas_se_guardan_con_argon2id():
    h = _HASHER.hash("una contraseña")
    assert h.startswith("$argon2id$")
    assert "una contraseña" not in h


def _autenticador(jwt_ttl_s: int = 3600, firma: Ed25519PrivateKey | None = None) -> Autenticador:
    secretos = SecretosDelCluster(firma or Ed25519PrivateKey.generate(), os.urandom(32))
    return Autenticador(None, secretos, "/dfsha/", jwt_ttl_s, 86400)


def test_jwt_ida_y_vuelta():
    a = _autenticador()
    token, vence_ms = a.emitir_jwt("ana", False)
    assert abs(vence_ms - (time.time() + 3600) * 1000) < 5000
    assert pyjwt.get_unverified_header(token)["alg"] == "EdDSA"
    identidad = a.identidad_de([("authorization", f"Bearer {token}")])
    assert identidad.usuario == "ana" and not identidad.admin


def test_jwt_rechaza_firma_de_otra_clave_alterado_o_vencido():
    a, otro = _autenticador(), _autenticador()
    token, _ = otro.emitir_jwt("ana", True)
    with pytest.raises(TokenInvalido):
        a.verificar_jwt(token)                              # firmado con otra clave
    bueno, _ = a.emitir_jwt("ana", False)
    cabecera, datos, firma = bueno.split(".")
    falso = pyjwt.utils.base64url_encode(json.dumps({**json.loads(pyjwt.utils.base64url_decode(datos)),
                                                     "adm": True}).encode()).decode()
    with pytest.raises(TokenInvalido):
        a.verificar_jwt(f"{cabecera}.{falso}.{firma}")      # se cambió el rol sin volver a firmar
    vencido, _ = _autenticador(jwt_ttl_s=-10, firma=a.secretos.firma_jwt).emitir_jwt("ana", False)
    with pytest.raises(TokenInvalido):
        a.verificar_jwt(vencido)
    with pytest.raises(TokenInvalido):
        a.identidad_de([("otra", "x")])                     # sin cabecera authorization


async def test_envoltorio_local(tmp_path):
    ruta = tmp_path / "kek.bin"
    crear_clave_local(ruta)
    env = envoltorio_desde(Kms(clave=f"archivo:{ruta}"))
    envuelto = await env.envolver(b"secreto")
    assert b"secreto" not in envuelto
    assert await env.desenvolver(envuelto) == b"secreto"
    otra = tmp_path / "otra.bin"
    crear_clave_local(otra)
    with pytest.raises(Exception):
        await EnvoltorioArchivo(str(otra)).desenvolver(envuelto)
    (tmp_path / "corta.bin").write_bytes(b"123")
    with pytest.raises(ErrorDeConfiguracion):
        EnvoltorioArchivo(str(tmp_path / "corta.bin"))


@pytest.mark.parametrize("peer, ip", [
    ("ipv4:10.0.1.5:51234", "10.0.1.5"),
    ("ipv6:[::1]:40000", "::1"),
    ("unix:/tmp/x", "unix:/tmp/x"),
])
def test_ip_de(peer, ip):
    assert ip_de(peer) == ip


def test_valores_nuevos_de_configuracion():
    cfg = cargar(entorno={})
    assert cfg.etcd.tls is True
    assert cfg.tiempos.token_renovacion_ttl_s == 86400
    assert cargar(entorno={"DFSHA_ETCD_TLS": "false"}).etcd.tls is False


# ---------------------------------------------------------------- integración

class Nodo:
    def __init__(self, nombre, nucleo, servidor, puerto):
        self.nombre, self.nucleo, self.servidor, self.puerto = nombre, nucleo, servidor, puerto
        self.canal = grpc.aio.insecure_channel(f"127.0.0.1:{puerto}")
        self.meta = pb_grpc.MetadataServiceStub(self.canal)
        self.admin = pb_grpc.AdminServiceStub(self.canal)

    async def apagar(self, renunciar: bool = True) -> None:
        await self.servidor.stop(0)
        if renunciar:
            await self.nucleo.elector.detener()
        else:                                    # caída abrupta, el lease vence solo
            self.nucleo.elector._tarea.cancel()
        await self.canal.close()


async def _lider(nodos, timeout_s: float = 15) -> Nodo:
    limite = time.monotonic() + timeout_s
    while time.monotonic() < limite:
        lideres = [n for n in nodos if n.nucleo.elector.es_lider]
        if len(lideres) == 1:
            return lideres[0]
        await asyncio.sleep(0.05)
    raise AssertionError("no hubo un único líder a tiempo")


@pytest.fixture
async def cluster(tmp_path):
    prefijo = f"/dfsha-pruebas/{uuid.uuid4().hex[:8]}/"
    kek = tmp_path / "kek.bin"
    crear_clave_local(kek)
    nodos, clientes = [], []
    for i in (1, 2, 3):
        cfg = cargar(entorno={"DFSHA_ETCD_PREFIJO": prefijo, "DFSHA_ETCD_TLS": "false", "DFSHA_TLS_HABILITADO": "false",
                              "DFSHA_KMS_CLAVE": f"archivo:{kek}", "DFSHA_NODO_NOMBRE": f"mds{i}.dfsha.internal"})
        cli = ClienteEtcd(DESTINO)
        await cli.esperar_listo(10)
        clientes.append(cli)
        nucleo = await construir(cfg, cli)
        nucleo.elector.iniciar()
        servidor = grpc.aio.server()
        registrar_servicios(servidor, nucleo)
        puerto = servidor.add_insecure_port("127.0.0.1:0")
        await servidor.start()
        nodos.append(Nodo(cfg.nodo.nombre, nucleo, servidor, puerto))
    await nodos[0].nucleo.usuarios.crear("admin", CONTRASENA_ADMIN, True, 0)
    try:
        yield nodos
    finally:
        for n in nodos:
            try:
                await n.apagar()
            except Exception:
                pass
        await clientes[0].borrar_prefijo(prefijo)
        for c in clientes:
            await c.cerrar()


def con_token(token: str):
    return (("authorization", f"Bearer {token}"),)


async def _error(llamada) -> grpc.aio.AioRpcError:
    with pytest.raises(grpc.aio.AioRpcError) as e:
        await llamada
    return e.value


@integracion
@pytest.mark.integracion
async def test_un_solo_lider_y_los_seguidores_lo_nombran(cluster):
    lider = await _lider(cluster)
    for n in cluster:
        if n is lider:
            continue
        e = await _error(n.meta.Login(pb.LoginRequest(username="admin", password=CONTRASENA_ADMIN)))
        assert e.code() == grpc.StatusCode.FAILED_PRECONDITION
        assert motivo_de(e) == "NOT_LEADER"
        assert detalle_de(e).leader == lider.nombre


@integracion
@pytest.mark.integracion
async def test_login_crea_sesion_y_renew_la_mantiene(cluster):
    lider = await _lider(cluster)
    r = await lider.meta.Login(pb.LoginRequest(username="admin", password=CONTRASENA_ADMIN))
    assert r.access_token and r.refresh_token and r.renew_interval_s == 10
    cli = lider.nucleo.cli
    assert 25 <= await cli.ttl_lease(int(r.session_id, 16)) <= 30
    assert await lider.nucleo.usuario_de_sesion(r.session_id) == "admin"
    renovada = await lider.meta.Renew(pb.RenewRequest(session_id=r.session_id), metadata=con_token(r.access_token))
    assert renovada.ttl_s == 30
    await cli.revocar_lease(int(r.session_id, 16))                 # la sesión vence
    e = await _error(lider.meta.Renew(pb.RenewRequest(session_id=r.session_id), metadata=con_token(r.access_token)))
    assert motivo_de(e) == "LEASE_EXPIRED"


@integracion
@pytest.mark.integracion
async def test_credenciales_incorrectas_no_revelan_si_el_usuario_existe(cluster):
    lider = await _lider(cluster)
    e1 = await _error(lider.meta.Login(pb.LoginRequest(username="admin", password="mala-contrasena")))
    e2 = await _error(lider.meta.Login(pb.LoginRequest(username="nadie", password="mala-contrasena")))
    for e in (e1, e2):
        assert e.code() == grpc.StatusCode.UNAUTHENTICATED and motivo_de(e) == "TOKEN_INVALID"
    assert e1.details() == e2.details()
    auditoria = await lider.nucleo.cli.rango(lider.nucleo.cfg.etcd.prefijo + "audit/")
    eventos = [json.loads(kv.value) for kv in auditoria]
    assert {"admin", "nadie"} <= {ev["usuario"] for ev in eventos if ev["resultado"] == "credenciales incorrectas"}
    assert all(ev["ip"] == "127.0.0.1" for ev in eventos)
    assert all(kv.lease for kv in auditoria)                       # vencen solas


@integracion
@pytest.mark.integracion
async def test_administracion_exige_rol_y_valida(cluster):
    lider = await _lider(cluster)
    admin = (await lider.meta.Login(pb.LoginRequest(username="admin", password=CONTRASENA_ADMIN))).access_token
    await lider.admin.CreateUser(pb.CreateUserRequest(username="ana", password="clave-de-ana", quota_bytes=10 ** 9),
                                 metadata=con_token(admin))
    ana = (await lider.meta.Login(pb.LoginRequest(username="ana", password="clave-de-ana"))).access_token

    e = await _error(lider.admin.CreateUser(pb.CreateUserRequest(username="eva", password="clave-de-eva"),
                                            metadata=con_token(ana)))
    assert e.code() == grpc.StatusCode.PERMISSION_DENIED and motivo_de(e) == "FORBIDDEN"
    e = await _error(lider.admin.CreateUser(pb.CreateUserRequest(username="eva", password="clave-de-eva")))
    assert motivo_de(e) == "TOKEN_INVALID"
    e = await _error(lider.admin.CreateUser(pb.CreateUserRequest(username="ana", password="otra-clave-1"),
                                            metadata=con_token(admin)))
    assert motivo_de(e) == "EXISTS"
    e = await _error(lider.admin.CreateUser(pb.CreateUserRequest(username="Ana!", password="otra-clave-1"),
                                            metadata=con_token(admin)))
    assert e.code() == grpc.StatusCode.INVALID_ARGUMENT
    e = await _error(lider.admin.CreateUser(pb.CreateUserRequest(username="eva", password="corta"),
                                            metadata=con_token(admin)))
    assert e.code() == grpc.StatusCode.INVALID_ARGUMENT

    await lider.admin.SetQuota(pb.SetQuotaRequest(username="ana", quota_bytes=5), metadata=con_token(admin))
    assert (await lider.nucleo.usuarios.obtener("ana")).cuota_bytes == 5
    e = await _error(lider.admin.SetQuota(pb.SetQuotaRequest(username="nadie", quota_bytes=5),
                                          metadata=con_token(admin)))
    assert motivo_de(e) == "NOT_FOUND"


@integracion
@pytest.mark.integracion
async def test_refresh_entrega_credenciales_nuevas_y_el_token_viejo_no_sirve(cluster):
    lider = await _lider(cluster)
    r = await lider.meta.Login(pb.LoginRequest(username="admin", password=CONTRASENA_ADMIN))
    r2 = await lider.meta.Refresh(pb.RefreshRequest(refresh_token=r.refresh_token))
    assert r2.refresh_token != r.refresh_token and r2.session_id != r.session_id
    assert lider.nucleo.autenticador.verificar_jwt(r2.access_token).admin
    e = await _error(lider.meta.Refresh(pb.RefreshRequest(refresh_token=r.refresh_token)))
    assert motivo_de(e) == "TOKEN_INVALID"
    claves = await lider.nucleo.cli.rango(lider.nucleo.cfg.etcd.prefijo + "refresh/")
    assert len(claves) == 1 and r2.refresh_token.encode() not in claves[0].key + claves[0].value


@integracion
@pytest.mark.integracion
async def test_los_tres_nodos_comparten_claves_envueltas(cluster):
    publicas = {n.nucleo.autenticador.secretos.verificacion_jwt.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw) for n in cluster}
    assert len(publicas) == 1
    assert len({n.nucleo.autenticador.secretos.hmac_bloques for n in cluster}) == 1
    lider = await _lider(cluster)
    firma = lider.nucleo.autenticador.secretos.firma_jwt.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    guardado = await lider.nucleo.cli.obtener(lider.nucleo.cfg.etcd.prefijo + "secrets/jwt-ed25519")
    assert firma not in guardado.value


@integracion
@pytest.mark.integracion
@pytest.mark.parametrize("renunciar", [True, False], ids=["renuncia", "caida"])
async def test_cambio_de_lider_conserva_sesiones_y_cerca_al_anterior(cluster, renunciar):
    lider = await _lider(cluster)
    r = await lider.meta.Login(pb.LoginRequest(username="admin", password=CONTRASENA_ADMIN))
    liderazgo_viejo = lider.nucleo.elector.liderazgo
    inicio = time.monotonic()
    await lider.apagar(renunciar=renunciar)
    restantes = [n for n in cluster if n is not lider]
    nuevo = await _lider(restantes)
    demora = time.monotonic() - inicio
    assert demora < (1 if renunciar else 8)
    # El JWT y la sesión de antes siguen sirviendo en el nuevo líder
    renovada = await nuevo.meta.Renew(pb.RenewRequest(session_id=r.session_id), metadata=con_token(r.access_token))
    assert renovada.ttl_s == 30
    # El líder anterior ya no puede escribir
    cli = nuevo.nucleo.cli
    viejo = await cli.txn([liderazgo_viejo.comparacion()], [cli.op_poner(nuevo.nucleo.cfg.etcd.prefijo + "x", "1")])
    assert not viejo.succeeded
    cluster.remove(lider)


@integracion
@pytest.mark.integracion
async def test_un_seguidor_no_escribe(cluster):
    lider = await _lider(cluster)
    seguidor = next(n for n in cluster if n is not lider)
    with pytest.raises(DejoDeSerLider):
        await seguidor.nucleo.abrir_sesion("admin")


@integracion
@pytest.mark.integracion
async def test_limite_de_peticiones_simultaneas(cluster):
    lider = await _lider(cluster)
    lider.nucleo.en_curso = lider.nucleo.cfg.limites.peticiones_simultaneas
    try:
        e = await _error(lider.meta.Login(pb.LoginRequest(username="admin", password=CONTRASENA_ADMIN)))
    finally:
        lider.nucleo.en_curso = 0
    assert e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED and motivo_de(e) == "OVERLOADED"
    assert detalle_de(e).retry_after_ms > 0
