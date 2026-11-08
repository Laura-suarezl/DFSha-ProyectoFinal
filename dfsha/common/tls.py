"""Credenciales TLS de gRPC a partir de la configuración (D8, docs/tls.md).

gRPC para Python no permite que un mismo puerto pida certificado de cliente de forma opcional:
o lo exige a todos o no lo pide a nadie. Por eso cada servidor escucha en dos puertos.
- Puerto de clientes (50051 o 50052). TLS, el cliente verifica al servidor y no presenta certificado.
- Puerto interno (50053 o 50054). TLS mutuo, ambos lados presentan un certificado de la CA interna.

Uso típico en un servidor:
    servidor.add_secure_port(f"0.0.0.0:{cfg.cluster.puerto_datos}", credenciales_servidor(cfg.tls))
    servidor.add_secure_port(f"0.0.0.0:{cfg.cluster.puerto_datos_interno}",
                             credenciales_servidor(cfg.tls, exigir_certificado_cliente=True))
    canal_hacia_otro_nodo = grpc.aio.secure_channel(destino, credenciales_canal(cfg.tls, con_certificado=True))
"""
from __future__ import annotations

from pathlib import Path

import grpc

from dfsha.common.config import ErrorDeConfiguracion, Tls


def _leer(ruta: str, que: str) -> bytes:
    if not ruta:
        raise ErrorDeConfiguracion(f"falta en la sección [tls] la ruta para {que}")
    try:
        return Path(ruta).read_bytes()
    except OSError as e:
        raise ErrorDeConfiguracion(f"no se pudo leer {que} en {ruta}: {e.strerror}") from None


def credenciales_servidor(tls: Tls, exigir_certificado_cliente: bool = False) -> grpc.ServerCredentials:
    """Credenciales de un puerto de servidor. Con exigir_certificado_cliente, el puerto es de TLS mutuo."""
    return grpc.ssl_server_credentials(
        [(_leer(tls.llave, "la llave del nodo"), _leer(tls.certificado, "el certificado del nodo"))],
        root_certificates=_leer(tls.ca, "el certificado de la CA") if exigir_certificado_cliente else None,
        require_client_auth=exigir_certificado_cliente,
    )


def credenciales_canal(tls: Tls, con_certificado: bool = False) -> grpc.ChannelCredentials:
    """Credenciales para conectarse a un servidor. Entre servidores, con_certificado=True (TLS mutuo)."""
    if con_certificado:
        return grpc.ssl_channel_credentials(
            root_certificates=_leer(tls.ca, "el certificado de la CA"),
            private_key=_leer(tls.llave, "la llave del nodo"),
            certificate_chain=_leer(tls.certificado, "el certificado del nodo"),
        )
    return grpc.ssl_channel_credentials(root_certificates=_leer(tls.ca, "el certificado de la CA"))


def identidad_par(context: grpc.aio.ServicerContext) -> str | None:
    """Primer nombre DNS del certificado del otro extremo, o None si no presentó certificado.

    Solo hay certificado del otro extremo en los puertos internos (TLS mutuo).
    """
    datos = context.auth_context()
    nombres = datos.get("x509_subject_alternative_name") or datos.get("x509_common_name") or []
    return nombres[0].decode("utf-8") if nombres else None


def es_nodo(identidad: str | None, tipo: str) -> bool:
    """True si la identidad es de un nodo del tipo dado ("dn" o "mds"), por ejemplo dn2.dfsha.internal."""
    if not identidad:
        return False
    corto = identidad.split(".", 1)[0]
    return corto.startswith(tipo) and corto[len(tipo):].isdigit()
