"""Arranca un nodo de datos con la configuración de dfsha/common/config.py.

Uso:  python -m dfsha.datanode [--escucha 0.0.0.0]

Toma de la configuración el nombre del nodo (nodo.nombre), su directorio de datos
(nodo.directorio_datos), sus dos puertos (cluster.puerto_datos para los clientes y
cluster.puerto_datos_interno para los otros nodos de datos), el TLS (sección [tls]) y el tamaño
de trozo para las lecturas (almacenamiento.tamano_trozo). Al arrancar borra los bloques a medias
que haya dejado una caída anterior (D6).

Con TLS habilitado, el puerto de clientes usa TLS y el interno TLS mutuo (docs/tls.md). Sin TLS
(solo para desarrollo, DFSHA_TLS_HABILITADO=false) ambos puertos van sin cifrar.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import grpc

from dfsha._gen import dfsha_pb2_grpc as pb_grpc
from dfsha.common.config import ErrorDeConfiguracion, cargar
from dfsha.common.tls import credenciales_canal, credenciales_servidor
from dfsha.datanode.almacen import Almacen
from dfsha.datanode.servicio import ServicioDatos

log = logging.getLogger("dfsha.datanode")


async def servir(escucha: str) -> None:
    cfg = cargar()
    if not cfg.nodo.nombre:
        raise ErrorDeConfiguracion("falta nodo.nombre (DFSHA_NODO_NOMBRE)")

    salida = credenciales_canal(cfg.tls, con_certificado=True) if cfg.tls.habilitado else None
    almacen = Almacen(cfg.nodo.directorio_datos)
    borrados = almacen.limpiar_temporales()
    servicio = ServicioDatos(almacen, cfg.nodo.nombre, cfg.almacenamiento.tamano_trozo,
                             credenciales_salida=salida)

    opciones = [("grpc.max_receive_message_length", 8 * 1024 * 1024)]
    servidor = grpc.aio.server(options=opciones)
    pb_grpc.add_DataServiceServicer_to_server(servicio, servidor)
    clientes = f"{escucha}:{cfg.cluster.puerto_datos}"
    interno = f"{escucha}:{cfg.cluster.puerto_datos_interno}"
    if cfg.tls.habilitado:
        servidor.add_secure_port(clientes, credenciales_servidor(cfg.tls))
        servidor.add_secure_port(interno, credenciales_servidor(cfg.tls, exigir_certificado_cliente=True))
        modo = "TLS en el puerto de clientes y TLS mutuo en el interno"
    else:
        servidor.add_insecure_port(clientes)
        servidor.add_insecure_port(interno)
        modo = "SIN TLS (solo para desarrollo)"
    await servidor.start()
    log.info("nodo %s escuchando en %s (clientes) y %s (interno), %s, datos en %s, %d temporales borrados al arrancar",
             cfg.nodo.nombre, clientes, interno, modo, cfg.nodo.directorio_datos, borrados)
    try:
        await servidor.wait_for_termination()
    finally:
        await servidor.stop(5)
        await servicio.cerrar()


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m dfsha.datanode")
    parser.add_argument("--escucha", default="0.0.0.0", help="interfaz en la que escucha (por defecto todas)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(servir(args.escucha))
    except ErrorDeConfiguracion as e:
        log.error("configuración inválida: %s", e)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
