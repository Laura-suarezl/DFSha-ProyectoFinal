"""Claves del clúster: la de firma de los JWT y la HMAC de los tokens de acceso por bloque (D8).

Se generan una sola vez, la primera vez que arranca un nodo de metadatos, y se guardan en etcd
(/dfsha/secrets/<nombre>) envueltas con la clave maestra. Así los tres nodos de metadatos firman
y verifican con las mismas claves, y quien lea etcd o un respaldo de su disco no las obtiene sin
acceso a la clave maestra.

La clave maestra está en AWS KMS (kms.clave = alias o id de la clave, comprobado desde una EC2
con LabInstanceProfile). Para desarrollo local, kms.clave = "archivo:<ruta>" usa una clave de
32 bytes guardada en un archivo, que se crea con: python -m dfsha.metadata crear-clave-local <ruta>
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from dfsha.common.config import ErrorDeConfiguracion, Kms
from dfsha.metadata.etcd import ClienteEtcd

NOMBRE_JWT = "jwt-ed25519"
NOMBRE_HMAC = "hmac-bloques"
_AAD = b"dfsha-secreto-del-cluster"


class EnvoltorioArchivo:
    """Envuelve con AES-256-GCM usando una clave en un archivo. Solo para desarrollo local."""

    def __init__(self, ruta: str):
        try:
            clave = Path(ruta).read_bytes()
        except OSError as e:
            raise ErrorDeConfiguracion(f"no se pudo leer la clave maestra local {ruta}: {e.strerror}") from None
        if len(clave) != 32:
            raise ErrorDeConfiguracion(f"la clave maestra local {ruta} debe tener 32 bytes")
        self._aes = AESGCM(clave)

    async def envolver(self, datos: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aes.encrypt(nonce, datos, _AAD)

    async def desenvolver(self, envuelto: bytes) -> bytes:
        return self._aes.decrypt(envuelto[:12], envuelto[12:], _AAD)


class EnvoltorioKms:
    """Envuelve con AWS KMS usando las credenciales del rol de la instancia."""

    def __init__(self, clave: str, region: str):
        import boto3                                     # solo hace falta en AWS
        from botocore.config import Config
        # Con plazos cortos, si KMS no responde el arranque falla y el contenedor se reinicia, en vez
        # de quedarse esperando para siempre
        self._kms = boto3.client("kms", region_name=region, config=Config(
            connect_timeout=5, read_timeout=10, retries={"max_attempts": 3}))
        self._clave = clave

    async def envolver(self, datos: bytes) -> bytes:
        r = await asyncio.to_thread(self._kms.encrypt, KeyId=self._clave, Plaintext=datos,
                                    EncryptionContext={"uso": "dfsha-secreto-del-cluster"})
        return r["CiphertextBlob"]

    async def desenvolver(self, envuelto: bytes) -> bytes:
        r = await asyncio.to_thread(self._kms.decrypt, CiphertextBlob=envuelto,
                                    EncryptionContext={"uso": "dfsha-secreto-del-cluster"})
        return r["Plaintext"]


def envoltorio_desde(kms: Kms):
    if kms.clave.startswith("archivo:"):
        return EnvoltorioArchivo(kms.clave.removeprefix("archivo:"))
    return EnvoltorioKms(kms.clave, kms.region)


def crear_clave_local(ruta: str | os.PathLike) -> None:
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(os.urandom(32))
    if os.name == "posix":
        os.chmod(ruta, 0o600)


@dataclass(frozen=True)
class SecretosDelCluster:
    firma_jwt: Ed25519PrivateKey
    hmac_bloques: bytes

    @property
    def verificacion_jwt(self) -> Ed25519PublicKey:
        return self.firma_jwt.public_key()


async def _obtener_o_crear(cli: ClienteEtcd, envoltorio, clave: str, generar) -> bytes:
    """Lee un secreto de etcd o, si no existe, lo crea. Si dos nodos lo crean a la vez, gana uno."""
    kv = await cli.obtener(clave)
    if kv is None:
        await cli.crear_si_no_existe(clave, await envoltorio.envolver(generar()))
        kv = await cli.obtener(clave)
    return await envoltorio.desenvolver(kv.value)


async def cargar_secretos(cli: ClienteEtcd, envoltorio, prefijo: str) -> SecretosDelCluster:
    from cryptography.hazmat.primitives import serialization

    def nueva_firma() -> bytes:
        return Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())

    firma = await _obtener_o_crear(cli, envoltorio, f"{prefijo}secrets/{NOMBRE_JWT}", nueva_firma)
    hmac = await _obtener_o_crear(cli, envoltorio, f"{prefijo}secrets/{NOMBRE_HMAC}", lambda: os.urandom(32))
    return SecretosDelCluster(Ed25519PrivateKey.from_private_bytes(firma), hmac)
