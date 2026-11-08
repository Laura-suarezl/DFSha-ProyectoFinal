"""Usuarios de DFSha: contraseñas con Argon2id, rol y cuota, guardados en etcd (D8).

Clave en etcd: /dfsha/users/<usuario> con el hash Argon2id, si es administrador, la cuota en
bytes y los bytes usados. La contraseña nunca se guarda, solo su hash (RFC 9106).
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from dfsha._gen.etcd.api.etcdserverpb import rpc_pb2 as rpc
from dfsha.metadata.etcd import ClienteEtcd

FORMATO_USUARIO = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")
_HASHER = PasswordHasher()                  # Argon2id con los parámetros recomendados por argon2-cffi


class UsuarioInvalido(ValueError):
    """El nombre no cumple el formato (minúsculas, números, guion o guion bajo, de 3 a 32)."""


@dataclass(frozen=True)
class Usuario:
    nombre: str
    admin: bool
    cuota_bytes: int
    usados_bytes: int


def validar_nombre(nombre: str) -> str:
    if not FORMATO_USUARIO.match(nombre or ""):
        raise UsuarioInvalido(nombre)
    return nombre


class RepositorioUsuarios:
    def __init__(self, cli: ClienteEtcd, prefijo: str):
        self.cli = cli
        self.prefijo = f"{prefijo}users/"

    def _clave(self, nombre: str) -> str:
        return self.prefijo + validar_nombre(nombre)

    async def crear(self, nombre: str, contrasena: str, admin: bool, cuota_bytes: int,
                    comparaciones: list[rpc.Compare] = ()) -> bool:
        """Crea el usuario. False si ya existía."""
        if len(contrasena) < 8:
            raise ValueError("la contraseña debe tener al menos 8 caracteres")
        hash_ = await asyncio.to_thread(_HASHER.hash, contrasena)
        valor = json.dumps({"hash": hash_, "admin": admin, "cuota": cuota_bytes, "usado": 0})
        return await self.cli.crear_si_no_existe(self._clave(nombre), valor, comparaciones=comparaciones)

    async def obtener(self, nombre: str) -> Usuario | None:
        try:
            kv = await self.cli.obtener(self._clave(nombre))
        except UsuarioInvalido:
            return None
        if kv is None:
            return None
        d = json.loads(kv.value)
        return Usuario(nombre, d["admin"], d["cuota"], d["usado"])

    async def verificar(self, nombre: str, contrasena: str) -> Usuario | None:
        """Usuario si la contraseña es correcta, None si no (sin decir si el usuario existe)."""
        try:
            kv = await self.cli.obtener(self._clave(nombre))
        except UsuarioInvalido:
            kv = None
        if kv is None:
            await asyncio.to_thread(_verificar_falso, contrasena)   # mismo costo, no revela si existe
            return None
        d = json.loads(kv.value)
        try:
            await asyncio.to_thread(_HASHER.verify, d["hash"], contrasena)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return None
        return Usuario(nombre, d["admin"], d["cuota"], d["usado"])

    async def fijar_cuota(self, nombre: str, cuota_bytes: int, comparaciones: list[rpc.Compare] = ()) -> bool:
        """Cambia la cuota. False si el usuario no existe o si falló una comparación extra.

        Si otro cambio del mismo usuario se cruzó (por ejemplo, un commit que actualizó el uso),
        vuelve a leer y reintenta.
        """
        clave = self._clave(nombre)
        kv = await self.cli.obtener(clave)
        while kv is not None:
            d = json.loads(kv.value)
            d["cuota"] = cuota_bytes
            r = await self.cli.txn([self.cli.comparar_modificacion(clave, kv.mod_revision), *comparaciones],
                                   [self.cli.op_poner(clave, json.dumps(d))])
            if r.succeeded:
                return True
            anterior, kv = kv, await self.cli.obtener(clave)
            if kv is not None and kv.mod_revision == anterior.mod_revision:
                return False                         # el usuario no cambió, falló la comparación extra
        return False


_HASH_FALSO = _HASHER.hash("contrasena-que-no-coincide")


def _verificar_falso(contrasena: str) -> None:
    try:
        _HASHER.verify(_HASH_FALSO, contrasena)
    except VerificationError:
        pass
