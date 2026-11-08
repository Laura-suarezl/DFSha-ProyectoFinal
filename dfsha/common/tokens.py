"""Tokens de acceso por bloque firmados con HMAC-SHA256 (D8).

El líder de metadatos los entrega en Open, GetBlockLocations y AllocateBlocks, y el nodo de datos
los verifica sin consultar a nadie, con la clave HMAC que recibe en Register por el canal de TLS
mutuo. Cada token sirve para un bloque, una operación ("r" leer o "w" escribir) y 10 minutos.

Formato: base64url(datos JSON) "." base64url(HMAC de los datos).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

LEER, ESCRIBIR = "r", "w"


class TokenBloqueInvalido(Exception):
    """Token ausente, mal formado, de otro bloque u operación, vencido o con firma falsa."""


def id_de_clave(clave: bytes) -> str:
    return hashlib.sha256(clave).hexdigest()[:8]


def emitir(clave: bytes, block_id: str, operacion: str, usuario: str, ttl_s: int, ahora: float | None = None) -> bytes:
    datos = json.dumps({"b": block_id, "o": operacion, "u": usuario, "e": int(ahora or time.time()) + ttl_s},
                       separators=(",", ":")).encode()
    firma = hmac.new(clave, datos, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(datos) + b"." + base64.urlsafe_b64encode(firma)


def verificar(clave: bytes, token: bytes, block_id: str, operacion: str, ahora: float | None = None) -> str:
    """Devuelve el usuario del token o lanza TokenBloqueInvalido."""
    try:
        datos_b64, firma_b64 = token.split(b".")
        datos, firma = base64.urlsafe_b64decode(datos_b64), base64.urlsafe_b64decode(firma_b64)
    except ValueError:
        raise TokenBloqueInvalido("token mal formado") from None
    if not hmac.compare_digest(firma, hmac.new(clave, datos, hashlib.sha256).digest()):
        raise TokenBloqueInvalido("firma inválida")
    d = json.loads(datos)
    if d["b"] != block_id or d["o"] != operacion:
        raise TokenBloqueInvalido("el token es de otro bloque u operación")
    if d["e"] < (ahora or time.time()):
        raise TokenBloqueInvalido("token vencido")
    return d["u"]
