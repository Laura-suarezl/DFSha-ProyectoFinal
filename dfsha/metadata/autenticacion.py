"""JWT firmados con Ed25519 y tokens de renovación (D8, docs/metadatos.md).

- El JWT dura una hora (tiempos.jwt_ttl_s), lo firma el líder con la clave del clúster y lleva
  el usuario y si es administrador. Cualquier nodo de metadatos lo verifica con la clave pública.
- El token de renovación es un valor aleatorio que se entrega en Login. En etcd se guarda solo
  su huella SHA-256 (/dfsha/refresh/<huella>), atada a un lease de un día, así que un respaldo de
  etcd no permite usarlo. Cada Refresh lo cambia por uno nuevo, así un token robado y ya usado
  no sirve.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Sequence

import jwt

from dfsha._gen.etcd.api.etcdserverpb import rpc_pb2 as rpc
from dfsha.metadata.claves import SecretosDelCluster
from dfsha.metadata.etcd import ClienteEtcd

EMISOR = "dfsha"


class TokenInvalido(Exception):
    """JWT o token de renovación ausente, vencido, falso o de otro emisor."""


class ComparacionFallida(Exception):
    """No se cumplió una comparación extra de la transacción (en la práctica, la de liderazgo)."""


@dataclass(frozen=True)
class Identidad:
    usuario: str
    admin: bool


class Autenticador:
    def __init__(self, cli: ClienteEtcd, secretos: SecretosDelCluster, prefijo: str,
                 jwt_ttl_s: int, renovacion_ttl_s: int):
        self.cli = cli
        self.secretos = secretos
        self.prefijo_renovacion = f"{prefijo}refresh/"
        self.jwt_ttl_s = jwt_ttl_s
        self.renovacion_ttl_s = renovacion_ttl_s

    def emitir_jwt(self, usuario: str, admin: bool) -> tuple[str, int]:
        """(JWT, vencimiento en milisegundos desde 1970)."""
        ahora = int(time.time())
        vence = ahora + self.jwt_ttl_s
        token = jwt.encode({"iss": EMISOR, "sub": usuario, "adm": admin, "iat": ahora, "exp": vence,
                            "jti": uuid.uuid4().hex}, self.secretos.firma_jwt, algorithm="EdDSA")
        return token, vence * 1000

    def verificar_jwt(self, token: str) -> Identidad:
        try:
            datos = jwt.decode(token, self.secretos.verificacion_jwt, algorithms=["EdDSA"], issuer=EMISOR,
                               options={"require": ["exp", "iat", "sub", "iss"]})
        except jwt.PyJWTError as e:
            raise TokenInvalido(str(e)) from None
        return Identidad(datos["sub"], bool(datos.get("adm", False)))

    def identidad_de(self, metadatos) -> Identidad:
        """Identidad a partir de la cabecera authorization: Bearer <JWT> de una llamada gRPC."""
        for clave, valor in metadatos or ():
            if clave == "authorization" and valor.startswith("Bearer "):
                return self.verificar_jwt(valor.removeprefix("Bearer "))
        raise TokenInvalido("falta la cabecera authorization")

    async def emitir_renovacion(self, usuario: str, comparaciones: Sequence[rpc.Compare] = ()) -> str:
        """Crea un token de renovación. Con la comparación de liderazgo, solo lo crea el líder."""
        token = secrets.token_urlsafe(32)
        lease = await self.cli.conceder_lease(self.renovacion_ttl_s)
        if not await self.cli.crear_si_no_existe(self._clave(token), json.dumps({"usuario": usuario}),
                                                 lease, comparaciones):
            await self.cli.revocar_lease(lease)
            raise ComparacionFallida()
        return token

    async def rotar_renovacion(self, token: str, comparaciones: Sequence[rpc.Compare] = ()) -> tuple[str, str]:
        """Cambia un token de renovación por uno nuevo en una sola transacción.

        Devuelve (usuario, token nuevo). Cada token sirve una sola vez: si dos peticiones usan el
        mismo token a la vez, solo una lo consigue.
        """
        clave = self._clave(token) if token else None
        kv = await self.cli.obtener(clave) if clave else None
        if kv is None:
            raise TokenInvalido("token de renovación desconocido o vencido")
        usuario = json.loads(kv.value)["usuario"]
        nuevo = secrets.token_urlsafe(32)
        lease = await self.cli.conceder_lease(self.renovacion_ttl_s)
        r = await self.cli.txn(
            [self.cli.comparar_modificacion(clave, kv.mod_revision), *comparaciones],
            [self.cli.op_borrar(clave), self.cli.op_poner(self._clave(nuevo), json.dumps({"usuario": usuario}), lease)])
        if not r.succeeded:
            await self.cli.revocar_lease(lease)
            if await self.cli.obtener(clave) is None:
                raise TokenInvalido("token de renovación ya usado")
            raise ComparacionFallida()
        await self.cli.revocar_lease(kv.lease)       # el lease del token viejo ya no tiene claves
        return usuario, nuevo

    def _clave(self, token: str) -> str:
        return self.prefijo_renovacion + hashlib.sha256(token.encode("utf-8")).hexdigest()
