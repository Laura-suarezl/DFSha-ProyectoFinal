"""MetadataService y AdminService del nodo de metadatos (D1, D4, D8 y D9, docs/metadatos.md).

Esta primera parte atiende el inicio de sesión, la renovación de la sesión y la administración
de usuarios. Las operaciones de archivos llegan en las siguientes etapas y por ahora responden
UNIMPLEMENTED (lo hace la clase base generada).

Reglas comunes:
- Solo el líder atiende (docs/api.md). Un seguidor responde NOT_LEADER con el nombre del líder.
- Toda escritura en etcd lleva la comparación de liderazgo. Si falla, el nodo dejó de ser líder
  y responde NOT_LEADER sin haber escrito nada.
- Si etcd no tiene mayoría, la llamada a etcd falla en 5 s y se responde NO_QUORUM.
"""
from __future__ import annotations

import functools
import json
import time

import grpc
from google.protobuf import empty_pb2

from dfsha._gen import dfsha_pb2 as pb
from dfsha._gen import dfsha_pb2_grpc as pb_grpc
from dfsha.common.config import Configuracion
from dfsha.common.errores import abortar
from dfsha.metadata.auditoria import Auditoria, ip_de
from dfsha.metadata.autenticacion import Autenticador, ComparacionFallida, Identidad, TokenInvalido
from dfsha.metadata.etcd import ClienteEtcd, EtcdNoDisponible
from dfsha.metadata.lider import Elector
from dfsha.metadata.usuarios import RepositorioUsuarios, UsuarioInvalido

ESPERA_SIN_QUORUM_MS = 1000
ESPERA_SOBRECARGA_MS = 200
MENSAJE_CREDENCIALES = "usuario o contraseña incorrectos"


class DejoDeSerLider(Exception):
    """La comparación de liderazgo de una transacción no se cumplió."""


class Nucleo:
    """Lo que comparten los servicios de un nodo de metadatos."""

    def __init__(self, cfg: Configuracion, cli: ClienteEtcd, elector: Elector, usuarios: RepositorioUsuarios,
                 autenticador: Autenticador, auditoria: Auditoria):
        self.cfg = cfg
        self.cli = cli
        self.elector = elector
        self.usuarios = usuarios
        self.autenticador = autenticador
        self.auditoria = auditoria
        self.prefijo_sesiones = f"{cfg.etcd.prefijo}sessions/"
        self.en_curso = 0                            # peticiones atendiéndose ahora mismo

    def comparaciones_lider(self) -> list:
        liderazgo = self.elector.liderazgo
        if liderazgo is None:
            raise DejoDeSerLider()
        return [liderazgo.comparacion()]

    async def comprobar_liderazgo(self, txn_ok: bool, liderazgo) -> None:
        """Si una transacción con comparación de liderazgo falló, averigua si fue por el liderazgo."""
        if txn_ok:
            return
        kv = await self.cli.obtener(liderazgo.clave)
        if kv is None or kv.create_revision != liderazgo.revision:
            raise DejoDeSerLider()

    async def abrir_sesion(self, usuario: str) -> int:
        """Crea el lease de la sesión y su clave. Devuelve el id de la sesión (el del lease)."""
        comparaciones = self.comparaciones_lider()
        lease = await self.cli.conceder_lease(self.cfg.tiempos.sesion_ttl_s)
        valor = json.dumps({"usuario": usuario, "inicio": int(time.time() * 1000)})
        clave = f"{self.prefijo_sesiones}{lease:016x}"
        r = await self.cli.txn(comparaciones, [self.cli.op_poner(clave, valor, lease)])
        if not r.succeeded:
            await self.cli.revocar_lease(lease)
            raise DejoDeSerLider()
        return lease

    async def usuario_de_sesion(self, session_id: str) -> str | None:
        try:
            lease = int(session_id, 16)
        except ValueError:
            return None
        kv = await self.cli.obtener(f"{self.prefijo_sesiones}{lease:016x}")
        return json.loads(kv.value)["usuario"] if kv else None


def atender(solo_admin: bool = False, autenticado: bool = True):
    """Envuelve un método de servicio con las reglas comunes.

    Aplica el límite de peticiones simultáneas (OVERLOADED), comprueba el liderazgo, verifica el
    JWT (y el rol administrador si hace falta) y traduce las fallas de etcd y del liderazgo a
    NO_QUORUM y NOT_LEADER.
    """
    def decorador(metodo):
        @functools.wraps(metodo)
        async def envoltura(self, request, context: grpc.aio.ServicerContext):
            nucleo: Nucleo = self.nucleo
            if nucleo.en_curso >= nucleo.cfg.limites.peticiones_simultaneas:
                await abortar(context, pb.REASON_OVERLOADED, "el nodo alcanzó su límite de peticiones simultáneas",
                              retry_after_ms=ESPERA_SOBRECARGA_MS)
            nucleo.en_curso += 1
            try:
                if not nucleo.elector.es_lider:
                    await _responder_no_lider(nucleo, context)
                identidad = None
                if autenticado:
                    try:
                        identidad = nucleo.autenticador.identidad_de(context.invocation_metadata())
                    except TokenInvalido:
                        await nucleo.auditoria.registrar(metodo.__name__, "", ip_de(context.peer()), "token inválido")
                        await abortar(context, pb.REASON_TOKEN_INVALID, "token ausente, vencido o inválido")
                    if solo_admin and not identidad.admin:
                        await nucleo.auditoria.registrar(metodo.__name__, identidad.usuario,
                                                         ip_de(context.peer()), "denegado, no es administrador")
                        await abortar(context, pb.REASON_FORBIDDEN, "la operación exige el rol administrador")
                return await metodo(self, request, context, identidad)
            except (DejoDeSerLider, ComparacionFallida):
                await _responder_no_lider(nucleo, context)
            except EtcdNoDisponible:
                await abortar(context, pb.REASON_NO_QUORUM, "no hay mayoría de nodos de metadatos",
                              retry_after_ms=ESPERA_SIN_QUORUM_MS)
            finally:
                nucleo.en_curso -= 1
        return envoltura
    return decorador


async def _responder_no_lider(nucleo: Nucleo, context) -> None:
    lider = await nucleo.elector.lider_actual()
    if lider is None or lider == nucleo.elector.nombre_nodo:
        await abortar(context, pb.REASON_NO_QUORUM, "elección de líder en curso",
                      retry_after_ms=ESPERA_SIN_QUORUM_MS)
    await abortar(context, pb.REASON_NOT_LEADER, "este nodo no es el líder", leader=lider)


class ServicioMetadatos(pb_grpc.MetadataServiceServicer):
    def __init__(self, nucleo: Nucleo):
        self.nucleo = nucleo

    async def _entregar_credenciales(self, usuario: str, admin: bool, renovacion: str) -> pb.LoginResponse:
        n = self.nucleo
        sesion = await n.abrir_sesion(usuario)
        token, vence_ms = n.autenticador.emitir_jwt(usuario, admin)
        return pb.LoginResponse(access_token=token, refresh_token=renovacion, expires_at_unix_ms=vence_ms,
                                session_id=f"{sesion:016x}", renew_interval_s=n.cfg.tiempos.renovacion_s)

    @atender(autenticado=False)
    async def Login(self, request, context, _identidad):
        n = self.nucleo
        ip = ip_de(context.peer())
        usuario = await n.usuarios.verificar(request.username, request.password)
        if usuario is None:
            await n.auditoria.registrar("Login", request.username[:64], ip, "credenciales incorrectas")
            await abortar(context, pb.REASON_TOKEN_INVALID, MENSAJE_CREDENCIALES)
        renovacion = await n.autenticador.emitir_renovacion(usuario.nombre, n.comparaciones_lider())
        respuesta = await self._entregar_credenciales(usuario.nombre, usuario.admin, renovacion)
        await n.auditoria.registrar("Login", usuario.nombre, ip, "ok")
        return respuesta

    @atender(autenticado=False)
    async def Refresh(self, request, context, _identidad):
        """Entrega un JWT y una sesión nuevos, y cambia el token de renovación por uno nuevo."""
        n = self.nucleo
        ip = ip_de(context.peer())
        try:
            nombre, renovacion = await n.autenticador.rotar_renovacion(request.refresh_token, n.comparaciones_lider())
        except TokenInvalido:
            await n.auditoria.registrar("Refresh", "", ip, "token de renovación inválido")
            await abortar(context, pb.REASON_TOKEN_INVALID, "token de renovación desconocido o vencido")
        usuario = await n.usuarios.obtener(nombre)
        if usuario is None:                          # el usuario se borró después del Login
            await abortar(context, pb.REASON_TOKEN_INVALID, "token de renovación desconocido o vencido")
        respuesta = await self._entregar_credenciales(usuario.nombre, usuario.admin, renovacion)
        await n.auditoria.registrar("Refresh", usuario.nombre, ip, "ok")
        return respuesta

    @atender()
    async def Renew(self, request, context, identidad: Identidad):
        n = self.nucleo
        if await n.usuario_de_sesion(request.session_id) != identidad.usuario:
            await abortar(context, pb.REASON_LEASE_EXPIRED, "la sesión venció o no es de este usuario")
        ttl = await n.cli.renovar_lease(int(request.session_id, 16))
        if ttl <= 0:
            await abortar(context, pb.REASON_LEASE_EXPIRED, "la sesión venció")
        return pb.RenewResponse(ttl_s=ttl)


class ServicioAdministracion(pb_grpc.AdminServiceServicer):
    def __init__(self, nucleo: Nucleo):
        self.nucleo = nucleo

    @atender(solo_admin=True)
    async def CreateUser(self, request, context, identidad: Identidad):
        n = self.nucleo
        liderazgo = n.elector.liderazgo
        try:
            creado = await n.usuarios.crear(request.username, request.password, request.admin,
                                            request.quota_bytes, n.comparaciones_lider())
        except UsuarioInvalido:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                                "nombre de usuario inválido (minúsculas, números, - o _, de 3 a 32, empieza por letra)")
        except ValueError as e:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
        if not creado:
            await n.comprobar_liderazgo(False, liderazgo)
            await abortar(context, pb.REASON_EXISTS, "el usuario ya existe", path=request.username)
        await n.auditoria.registrar(f"CreateUser {request.username}", identidad.usuario,
                                    ip_de(context.peer()), "ok")
        return empty_pb2.Empty()

    @atender(solo_admin=True)
    async def SetQuota(self, request, context, identidad: Identidad):
        n = self.nucleo
        liderazgo = n.elector.liderazgo
        try:
            cambiado = await n.usuarios.fijar_cuota(request.username, request.quota_bytes, n.comparaciones_lider())
        except UsuarioInvalido:
            cambiado = False
        if not cambiado:
            await n.comprobar_liderazgo(False, liderazgo)
            await abortar(context, pb.REASON_NOT_FOUND, "el usuario no existe", path=request.username)
        await n.auditoria.registrar(f"SetQuota {request.username} {request.quota_bytes}", identidad.usuario,
                                    ip_de(context.peer()), "ok")
        return empty_pb2.Empty()
