"""CA interna de DFSha y certificados de los nodos (D7 y D8, docs/tls.md).

Crea una autoridad de certificación propia y, con ella, un certificado por nodo emitido para su
nombre DNS. Los nodos de metadatos llevan además el nombre lógico del clúster, porque los
clientes se conectan a él. Todos los certificados sirven como servidor y como cliente, porque
entre servidores el TLS es mutuo (también entre los miembros de etcd).

Uso:
    python deploy/certs/crear_certificados.py                      # 3 de metadatos y 4 de datos
    python deploy/certs/crear_certificados.py --local              # añade nombres para pruebas locales
    python deploy/certs/crear_certificados.py --metadatos 3 --datos 5 --salida otra/carpeta

Si la CA ya existe en la carpeta de salida se reutiliza, así se pueden emitir certificados para
nodos nuevos sin invalidar los anteriores. Las llaves privadas quedan solo en la carpeta de
salida, que está en .gitignore.
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

AQUI = Path(__file__).resolve().parent
VIGENCIA_CA = datetime.timedelta(days=730)
VIGENCIA_NODO = datetime.timedelta(days=365)


def guardar(ruta: Path, datos: bytes, privada: bool = False) -> None:
    ruta.write_bytes(datos)
    if privada and os.name == "posix":
        os.chmod(ruta, 0o600)


def pem_llave(llave) -> bytes:
    return llave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())


def pem_certificado(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def crear_o_cargar_ca(salida: Path, dominio: str):
    llave_ca, cert_ca = salida / "ca.key", salida / "ca.crt"
    if llave_ca.exists() and cert_ca.exists():
        llave = serialization.load_pem_private_key(llave_ca.read_bytes(), password=None)
        return llave, x509.load_pem_x509_certificate(cert_ca.read_bytes()), False
    llave = ec.generate_private_key(ec.SECP256R1())
    nombre = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DFSha"),
                        x509.NameAttribute(NameOID.COMMON_NAME, f"CA interna de {dominio}")])
    ahora = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(nombre).issuer_name(nombre).public_key(llave.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(ahora - datetime.timedelta(minutes=5))
            .not_valid_after(ahora + VIGENCIA_CA)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(llave.public_key()), critical=False)
            .sign(llave, hashes.SHA256()))
    guardar(llave_ca, pem_llave(llave), privada=True)
    guardar(cert_ca, pem_certificado(cert))
    return llave, cert, True


def emitir(llave_ca, cert_ca, nombre: str, dns: list[str], ips: list[str]):
    llave = ec.generate_private_key(ec.SECP256R1())
    ahora = datetime.datetime.now(datetime.timezone.utc)
    alternativos = [x509.DNSName(d) for d in dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DFSha"),
                                     x509.NameAttribute(NameOID.COMMON_NAME, nombre)]))
            .issuer_name(cert_ca.subject).public_key(llave.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(ahora - datetime.timedelta(minutes=5))
            .not_valid_after(ahora + VIGENCIA_NODO)
            .add_extension(x509.SubjectAlternativeName(alternativos), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, key_cert_sign=False,
                                         crl_sign=False, content_commitment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                                  ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(llave_ca.public_key()), critical=False)
            .sign(llave_ca, hashes.SHA256()))
    return llave, cert


def nodos(dominio: str, metadatos: int, datos: int, local: bool) -> dict[str, tuple[list[str], list[str]]]:
    """Nombre de archivo -> (nombres DNS, IPs) de cada certificado."""
    resultado = {}
    for i in range(1, metadatos + 1):
        dns = [f"mds{i}.{dominio}", dominio]
        resultado[f"mds{i}"] = (dns + ([f"mds{i}", "localhost"] if local else []), ["127.0.0.1"] if local else [])
    for i in range(1, datos + 1):
        dns = [f"dn{i}.{dominio}"]
        resultado[f"dn{i}"] = (dns + ([f"dn{i}", "localhost"] if local else []), ["127.0.0.1"] if local else [])
    return resultado


def main() -> int:
    parser = argparse.ArgumentParser(description="CA interna de DFSha y certificados de los nodos")
    parser.add_argument("--dominio", default="dfsha.internal")
    parser.add_argument("--metadatos", type=int, default=3)
    parser.add_argument("--datos", type=int, default=4)
    parser.add_argument("--salida", type=Path, default=AQUI / "out")
    parser.add_argument("--local", action="store_true",
                        help="añade el nombre corto, localhost y 127.0.0.1 (Docker Compose local y pruebas)")
    args = parser.parse_args()

    args.salida.mkdir(parents=True, exist_ok=True)
    llave_ca, cert_ca, nueva = crear_o_cargar_ca(args.salida, args.dominio)
    print(f"CA {'creada' if nueva else 'reutilizada'}: {args.salida / 'ca.crt'}")
    for nombre, (dns, ips) in nodos(args.dominio, args.metadatos, args.datos, args.local).items():
        llave, cert = emitir(llave_ca, cert_ca, nombre, dns, ips)
        guardar(args.salida / f"{nombre}.key", pem_llave(llave), privada=True)
        guardar(args.salida / f"{nombre}.crt", pem_certificado(cert))
        print(f"  {nombre}.crt  {', '.join(dns + ips)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
