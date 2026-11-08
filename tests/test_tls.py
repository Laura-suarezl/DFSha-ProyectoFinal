"""CA interna, certificados y TLS (deploy/certs/crear_certificados.py y dfsha/common/tls.py)."""
import asyncio
import hashlib
import os
import socket
import ssl
import subprocess
import sys
from pathlib import Path

import grpc
import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

RAIZ = Path(__file__).resolve().parent.parent
subprocess.run([sys.executable, str(RAIZ / "scripts" / "generar_codigo.py")], check=True, capture_output=True)

from dfsha._gen import dfsha_pb2 as pb  # noqa: E402
from dfsha._gen import dfsha_pb2_grpc as pb_grpc  # noqa: E402
from dfsha.common.config import ErrorDeConfiguracion, Tls, cargar  # noqa: E402
from dfsha.common.tls import credenciales_canal, credenciales_servidor, es_nodo, identidad_par  # noqa: E402
from dfsha.datanode.almacen import Almacen  # noqa: E402
from dfsha.datanode.servicio import ServicioDatos  # noqa: E402

SCRIPT = RAIZ / "deploy" / "certs" / "crear_certificados.py"


def crear(salida: Path, *extra: str) -> None:
    subprocess.run([sys.executable, str(SCRIPT), "--salida", str(salida), "--local", *extra],
                   check=True, capture_output=True)


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    salida = tmp_path_factory.mktemp("certs")
    crear(salida)
    return salida


def tls_de(certs: Path, nodo: str | None) -> Tls:
    if nodo is None:
        return Tls(ca=str(certs / "ca.crt"))
    return Tls(ca=str(certs / "ca.crt"), certificado=str(certs / f"{nodo}.crt"), llave=str(certs / f"{nodo}.key"))


# ----------------------------------------------------------------------------- certificados
def test_certificados_con_sus_nombres(certs):
    ca = x509.load_pem_x509_certificate((certs / "ca.crt").read_bytes())
    esperados = {"mds1": {"mds1.dfsha.internal", "dfsha.internal", "mds1", "localhost"},
                 "dn4": {"dn4.dfsha.internal", "dn4", "localhost"}}
    for nodo, nombres in esperados.items():
        cert = x509.load_pem_x509_certificate((certs / f"{nodo}.crt").read_bytes())
        alternativos = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert set(alternativos.get_values_for_type(x509.DNSName)) == nombres
        usos = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert {ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH} <= set(usos)
        cert.verify_directly_issued_by(ca)              # firmado por la CA interna
    assert {p.name for p in certs.glob("*.crt")} == {"ca.crt", "mds1.crt", "mds2.crt", "mds3.crt",
                                                      "dn1.crt", "dn2.crt", "dn3.crt", "dn4.crt"}


def test_la_ca_se_reutiliza(tmp_path):
    crear(tmp_path, "--datos", "1", "--metadatos", "1")
    ca = (tmp_path / "ca.crt").read_bytes()
    crear(tmp_path, "--datos", "2", "--metadatos", "1")
    assert (tmp_path / "ca.crt").read_bytes() == ca
    nuevo = x509.load_pem_x509_certificate((tmp_path / "dn2.crt").read_bytes())
    nuevo.verify_directly_issued_by(x509.load_pem_x509_certificate(ca))


@pytest.mark.skipif(os.name != "posix", reason="los permisos de archivo solo aplican en Linux y macOS")
def test_llaves_privadas_solo_para_el_dueno(certs):
    assert (certs / "dn1.key").stat().st_mode & 0o077 == 0


def test_faltan_certificados_da_un_error_claro(tmp_path):
    with pytest.raises(ErrorDeConfiguracion, match="no se pudo leer la llave del nodo"):
        credenciales_servidor(Tls(ca=str(tmp_path / "ca.crt"), llave=str(tmp_path / "x.key")))
    (tmp_path / "x.key").write_text("llave")
    with pytest.raises(ErrorDeConfiguracion, match=r"falta en la sección \[tls\] la ruta para el certificado del nodo"):
        credenciales_servidor(Tls(ca=str(tmp_path / "ca.crt"), llave=str(tmp_path / "x.key")))


def test_los_cuatro_puertos_deben_ser_distintos():
    with pytest.raises(ErrorDeConfiguracion, match="cuatro puertos"):
        cargar(entorno={"DFSHA_CLUSTER_PUERTO_DATOS_INTERNO": "50052"})
    assert cargar(entorno={}).cluster.puertos() == (50051, 50052, 50053, 50054)


@pytest.mark.parametrize("identidad, tipo, esperado", [
    ("dn2.dfsha.internal", "dn", True), ("dn12", "dn", True), ("mds1.dfsha.internal", "dn", False),
    ("mds3.dfsha.internal", "mds", True), ("dnx.dfsha.internal", "dn", False), (None, "dn", False),
])
def test_es_nodo(identidad, tipo, esperado):
    assert es_nodo(identidad, tipo) is esperado


# ----------------------------------------------------------------------------- puertos
class Identidad(pb_grpc.DataServiceServicer):
    """Responde con la identidad del otro extremo, para ver qué ve el servidor."""

    async def CheckBlockHealth(self, request, context):
        return pb.HealthCheckResponse(node=identidad_par(context) or "(sin certificado)")


@pytest.fixture
async def servidor(certs):
    """Un servidor con los dos puertos de un nodo de datos (dn1)."""
    s = grpc.aio.server()
    pb_grpc.add_DataServiceServicer_to_server(Identidad(), s)
    clientes = s.add_secure_port("localhost:0", credenciales_servidor(tls_de(certs, "dn1")))
    interno = s.add_secure_port("localhost:0", credenciales_servidor(tls_de(certs, "dn1"), exigir_certificado_cliente=True))
    await s.start()
    yield clientes, interno
    await s.stop(None)


async def preguntar(puerto: int, credenciales) -> str:
    async with grpc.aio.secure_channel(f"localhost:{puerto}", credenciales) as canal:
        r = await pb_grpc.DataServiceStub(canal).CheckBlockHealth(pb.HealthCheckRequest(), timeout=5)
        return r.node


async def test_puerto_de_clientes_acepta_cliente_sin_certificado(certs, servidor):
    clientes, _ = servidor
    assert await preguntar(clientes, credenciales_canal(tls_de(certs, None))) == "(sin certificado)"


async def test_puerto_interno_rechaza_cliente_sin_certificado(certs, servidor):
    _, interno = servidor
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await preguntar(interno, credenciales_canal(tls_de(certs, None)))
    assert error.value.code() == grpc.StatusCode.UNAVAILABLE


async def test_puerto_interno_identifica_al_nodo(certs, servidor):
    _, interno = servidor
    identidad = await preguntar(interno, credenciales_canal(tls_de(certs, "dn2"), con_certificado=True))
    assert identidad == "dn2.dfsha.internal" and es_nodo(identidad, "dn")


async def test_puerto_interno_rechaza_certificado_de_otra_ca(tmp_path, certs, servidor):
    _, interno = servidor
    crear(tmp_path / "otra", "--datos", "1", "--metadatos", "0")
    intruso = Tls(ca=str(certs / "ca.crt"), certificado=str(tmp_path / "otra" / "dn1.crt"),
                  llave=str(tmp_path / "otra" / "dn1.key"))
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await preguntar(interno, credenciales_canal(intruso, con_certificado=True))
    assert error.value.code() == grpc.StatusCode.UNAVAILABLE


async def test_cliente_rechaza_servidor_de_otra_ca(tmp_path, servidor):
    clientes, _ = servidor
    crear(tmp_path / "otra", "--datos", "0", "--metadatos", "0")
    with pytest.raises(grpc.aio.AioRpcError):
        await preguntar(clientes, credenciales_canal(Tls(ca=str(tmp_path / "otra" / "ca.crt"))))


async def test_se_negocia_tls_13(certs, servidor):
    clientes, _ = servidor

    def version() -> str:
        contexto = ssl.create_default_context(cafile=str(certs / "ca.crt"))
        contexto.set_alpn_protocols(["h2"])
        with socket.create_connection(("localhost", clientes), timeout=5) as crudo:
            with contexto.wrap_socket(crudo, server_hostname="localhost") as s:
                return s.version()

    assert await asyncio.to_thread(version) == "TLSv1.3"


# ----------------------------------------------------------------------------- cadena
async def test_cadena_de_tres_con_tls_mutuo(certs, tmp_path):
    """El cliente entra por el puerto de clientes de dn1 y el reenvío va por los puertos internos."""
    servidores, puertos = [], []
    for i in (1, 2, 3):
        tls = tls_de(certs, f"dn{i}")
        servicio = ServicioDatos(Almacen(tmp_path / f"dn{i}"), f"dn{i}", 256 * 1024, timeout_reenvio_s=5,
                                 credenciales_salida=credenciales_canal(tls, con_certificado=True))
        s = grpc.aio.server()
        pb_grpc.add_DataServiceServicer_to_server(servicio, s)
        clientes = s.add_secure_port("localhost:0", credenciales_servidor(tls))
        interno = s.add_secure_port("localhost:0", credenciales_servidor(tls, exigir_certificado_cliente=True))
        await s.start()
        servidores.append((s, servicio))
        puertos.append((clientes, interno))
    try:
        datos = os.urandom(1024 * 1024 + 7)
        huella = hashlib.sha256(datos).hexdigest()

        def trozos():
            for n, i in enumerate(range(0, len(datos), 256 * 1024)):
                t = pb.ChunkPayload(sequence_number=n, data=datos[i:i + 256 * 1024],
                                    is_last_chunk=i + 256 * 1024 >= len(datos))
                if n == 0:
                    t.block_id, t.block_checksum = "b-00000000000000c1", huella
                    t.downstream_nodes.extend([f"localhost:{puertos[1][1]}", f"localhost:{puertos[2][1]}"])
                yield t

        async with grpc.aio.secure_channel(f"localhost:{puertos[0][0]}", credenciales_canal(tls_de(certs, None))) as canal:
            r = await pb_grpc.DataServiceStub(canal).WriteBlock(trozos())
        assert list(r.stored_on) == ["dn1", "dn2", "dn3"]
        for i in (1, 2, 3):
            assert Almacen(tmp_path / f"dn{i}").info("b-00000000000000c1") == (huella, len(datos))
    finally:
        for s, servicio in servidores:
            await s.stop(None)
            await servicio.cerrar()
