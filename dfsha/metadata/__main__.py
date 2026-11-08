"""Arranca un nodo de metadatos o prepara el clúster (docs/metadatos.md).

Uso:
    python -m dfsha.metadata [servir] [--escucha 0.0.0.0]
    python -m dfsha.metadata crear-admin --usuario admin [--cuota 0]
    python -m dfsha.metadata crear-clave-local <ruta>

servir (la opción por defecto) conecta con etcd, carga o crea las claves del clúster, compite por
el liderazgo y atiende MetadataService y AdminService en cluster.puerto_metadatos con TLS. Al
recibir SIGTERM (docker stop) renuncia al liderazgo, así otro nodo asume en milisegundos.

crear-admin escribe el primer administrador directamente en etcd, porque AdminService exige ya
tener uno. Toma la contraseña de la variable DFSHA_CONTRASENA o la pide por la terminal.

crear-clave-local crea la clave maestra de desarrollo (kms.clave = "archivo:<ruta>"). En AWS la
clave maestra está en KMS y este paso no hace falta.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import signal
import sys

import grpc

from dfsha._gen import dfsha_pb2_grpc as pb_grpc
from dfsha.common.config import Configuracion, ErrorDeConfiguracion, cargar
from dfsha.common.tls import credenciales_canal, credenciales_servidor
from dfsha.metadata.auditoria import Auditoria
from dfsha.metadata.autenticacion import Autenticador
from dfsha.metadata.claves import cargar_secretos, crear_clave_local, envoltorio_desde
from dfsha.metadata.etcd import ClienteEtcd, EtcdNoDisponible
from dfsha.metadata.lider import Elector
from dfsha.metadata.nodos import ServicioNodos
from dfsha.metadata.servicio import Nucleo, ServicioAdministracion, ServicioMetadatos
from dfsha.metadata.usuarios import RepositorioUsuarios, UsuarioInvalido

log = logging.getLogger("dfsha.metadata")
ELECCION = "election/metadata"


def cliente_etcd(cfg: Configuracion) -> ClienteEtcd:
    credenciales = credenciales_canal(cfg.tls, con_certificado=True) if cfg.etcd.tls else None
    return ClienteEtcd(cfg.etcd.destino, credenciales, cfg.etcd.timeout_s)


async def _con_reintentos(que: str, corrutina_fabrica, intentos: int = 30):
    """Repite una operación inicial mientras etcd no responde (por ejemplo, al arrancar todo a la vez)."""
    for intento in range(1, intentos + 1):
        try:
            return await corrutina_fabrica()
        except (EtcdNoDisponible, grpc.aio.AioRpcError) as e:
            if intento == intentos:
                raise
            log.info("%s: etcd aún no responde (%s), intento %d de %d", que, e, intento, intentos)
            await asyncio.sleep(2)


async def construir(cfg: Configuracion, cli: ClienteEtcd) -> Nucleo:
    prefijo = cfg.etcd.prefijo
    envoltorio = envoltorio_desde(cfg.kms)
    secretos = await _con_reintentos("cargar las claves del clúster", lambda: cargar_secretos(cli, envoltorio, prefijo))
    elector = Elector(cli, prefijo + ELECCION, cfg.nodo.nombre, cfg.tiempos.lider_ttl_s)
    autenticador = Autenticador(cli, secretos, prefijo, cfg.tiempos.jwt_ttl_s, cfg.tiempos.token_renovacion_ttl_s)
    return Nucleo(cfg, cli, elector, RepositorioUsuarios(cli, prefijo), autenticador,
                  Auditoria(cli, prefijo, cfg.tiempos.auditoria_ttl_s), envoltorio=envoltorio)


def registrar_servicios(servidor: grpc.aio.Server, nucleo: Nucleo) -> None:
    pb_grpc.add_MetadataServiceServicer_to_server(ServicioMetadatos(nucleo), servidor)
    pb_grpc.add_AdminServiceServicer_to_server(ServicioAdministracion(nucleo), servidor)


def servicio_nodos(nucleo: Nucleo) -> ServicioNodos:
    cfg = nucleo.cfg
    return ServicioNodos(nucleo.nodos, nucleo.elector, nucleo.autenticador.secretos, cfg.tiempos,
                         exigir_identidad=cfg.tls.habilitado)


async def servir(escucha: str) -> None:
    cfg = cargar()
    if not cfg.nodo.nombre:
        raise ErrorDeConfiguracion("falta nodo.nombre (DFSHA_NODO_NOMBRE)")
    cli = cliente_etcd(cfg)
    nucleo = await construir(cfg, cli)
    nucleo.elector.iniciar()

    servidor = grpc.aio.server()
    registrar_servicios(servidor, nucleo)
    # NodeService va en un servidor aparte: un servicio por puerto (docs/tls.md)
    interno = grpc.aio.server()
    pb_grpc.add_NodeServiceServicer_to_server(servicio_nodos(nucleo), interno)
    direccion = f"{escucha}:{cfg.cluster.puerto_metadatos}"
    direccion_interna = f"{escucha}:{cfg.cluster.puerto_metadatos_interno}"
    if cfg.tls.habilitado:
        servidor.add_secure_port(direccion, credenciales_servidor(cfg.tls))
        interno.add_secure_port(direccion_interna, credenciales_servidor(cfg.tls, exigir_certificado_cliente=True))
        modo = "TLS, y TLS mutuo en el interno"
    else:
        servidor.add_insecure_port(direccion)
        interno.add_insecure_port(direccion_interna)
        modo = "SIN TLS (solo para desarrollo)"
    await servidor.start()
    await interno.start()
    try:                                     # docker stop envía SIGTERM: renuncia en orden
        asyncio.get_running_loop().add_signal_handler(
            signal.SIGTERM, lambda: (asyncio.ensure_future(interno.stop(5)), asyncio.ensure_future(servidor.stop(5))))
    except NotImplementedError:              # Windows no tiene add_signal_handler
        pass
    log.info("nodo de metadatos %s escuchando en %s y %s con %s, etcd en %s (%s)", cfg.nodo.nombre, direccion,
             direccion_interna, modo,
             cfg.etcd.destino, "TLS mutuo" if cfg.etcd.tls else "sin TLS")
    try:
        await servidor.wait_for_termination()
    finally:
        await servidor.stop(5)
        await interno.stop(5)
        await nucleo.elector.detener()
        await cli.cerrar()


async def crear_admin(usuario: str, cuota: int) -> int:
    cfg = cargar()
    contrasena = os.environ.get("DFSHA_CONTRASENA") or getpass.getpass(f"Contraseña para {usuario}: ")
    async with cliente_etcd(cfg) as cli:
        repo = RepositorioUsuarios(cli, cfg.etcd.prefijo)
        try:
            creado = await _con_reintentos("crear el administrador",
                                           lambda: repo.crear(usuario, contrasena, True, cuota))
        except UsuarioInvalido:
            log.error("nombre de usuario inválido: minúsculas, números, - o _, de 3 a 32, empieza por letra")
            return 2
        except ValueError as e:
            log.error("%s", e)
            return 2
    if not creado:
        log.info("el usuario %s ya existía, no se cambió", usuario)
        return 1
    log.info("administrador %s creado", usuario)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m dfsha.metadata")
    sub = parser.add_subparsers(dest="orden")
    p_servir = sub.add_parser("servir", help="atiende MetadataService y AdminService (por defecto)")
    p_servir.add_argument("--escucha", default="0.0.0.0", help="interfaz en la que escucha (por defecto todas)")
    p_admin = sub.add_parser("crear-admin", help="crea el primer administrador directamente en etcd")
    p_admin.add_argument("--usuario", required=True)
    p_admin.add_argument("--cuota", type=int, default=0, help="cuota en bytes, 0 es sin límite")
    p_clave = sub.add_parser("crear-clave-local", help="crea la clave maestra de desarrollo")
    p_clave.add_argument("ruta")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        if args.orden == "crear-clave-local":
            crear_clave_local(args.ruta)
            log.info("clave maestra local creada en %s", args.ruta)
            return 0
        if args.orden == "crear-admin":
            return asyncio.run(crear_admin(args.usuario, args.cuota))
        asyncio.run(servir(getattr(args, "escucha", "0.0.0.0")))
    except ErrorDeConfiguracion as e:
        log.error("configuración inválida: %s", e)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
