"""Espacio de nombres y archivos en el líder de metadatos (RF1, RF3, D2, D3 y D4).

Implementa el esquema de docs/etcd-esquema.md. Toda escritura es una transacción de etcd con la
comparación de liderazgo. Los descriptores (fd) se guardan en etcd atados al lease de la sesión,
así sobreviven a un cambio de líder y desaparecen solos si la sesión vence.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import posixpath
import time

from dfsha._gen import dfsha_pb2 as pb
from dfsha.common import tokens
from dfsha.metadata.etcd import LockOcupado as LockOcupadoError

LEER, ESCRIBIR, ANEXAR, LEER_ESCRIBIR = pb.OPEN_MODE_READ, pb.OPEN_MODE_WRITE, pb.OPEN_MODE_APPEND, pb.OPEN_MODE_READ_WRITE
HUELLA_VACIA = hashlib.sha256(b"").hexdigest()


class ErrorDfsha(Exception):
    """Error con motivo propio (pb.REASON_*). Sin motivo, se responde INVALID_ARGUMENT."""

    def __init__(self, motivo: int | None, mensaje: str, **datos):
        super().__init__(mensaje)
        self.motivo, self.mensaje, self.datos = motivo, mensaje, datos


def _id(prefijo: str = "") -> str:
    return prefijo + os.urandom(8).hex()


def _ahora_ms() -> int:
    return int(time.time() * 1000)


def huella_archivo(bloques: list[dict]) -> str:
    return hashlib.sha256(b"".join(bytes.fromhex(b["h"]) for b in bloques)).hexdigest() if bloques else HUELLA_VACIA


def objetivo(t: pb.LockTarget) -> str:
    if t.HasField("block_index"):
        return f"b{t.block_index}"
    return "fin" if t.HasField("end_of_file") else "archivo"


class Archivos:
    def __init__(self, nucleo):
        self.n = nucleo
        self.cli = nucleo.cli
        self.p = nucleo.cfg.etcd.prefijo
        self._candados: dict[str, asyncio.Lock] = {}

    def k(self, *partes) -> str:
        return self.p + "/".join(str(x) for x in partes)

    # ------------------------------------------------------------------ transacciones
    async def _txn(self, comparaciones, operaciones) -> bool:
        liderazgo = self.n.elector.liderazgo
        r = await self.cli.txn([*self.n.comparaciones_lider(), *comparaciones], operaciones)
        if not r.succeeded:
            await self.n.comprobar_liderazgo(False, liderazgo)
        return r.succeeded

    async def _json(self, clave: str):
        kv = await self.cli.obtener(clave)
        return (json.loads(kv.value), kv) if kv else (None, None)

    # ------------------------------------------------------------------ rutas
    def partes(self, ruta: str, usuario: str) -> list[str]:
        if not ruta.startswith("/"):
            raise ErrorDfsha(None, "la ruta debe ser absoluta")
        comps = [c for c in posixpath.normpath(ruta).split("/") if c]
        if not comps or comps[0] != usuario:
            raise ErrorDfsha(pb.REASON_FORBIDDEN, "ruta fuera del espacio del usuario")
        if any(len(c) > 255 for c in comps):
            raise ErrorDfsha(None, "nombre de más de 255 caracteres")
        return comps[1:]

    async def raiz(self, usuario: str) -> str:
        """Id del directorio /usuario. Se crea la primera vez que se usa."""
        clave = self.k("ns", "root", usuario)
        entrada, _ = await self._json(clave)
        if entrada is None:
            nuevo = {"id": _id("d-"), "tipo": "d", "dueno": usuario, "modificado": _ahora_ms()}
            await self._txn([self.cli.comparar_no_existe(clave)], [
                self.cli.op_poner(clave, json.dumps(nuevo)),
                self.cli.op_poner(self.k("inodes", nuevo["id"]), json.dumps({"padre": "root", "nombre": usuario}))])
            entrada, _ = await self._json(clave)
        return entrada["id"]

    async def resolver(self, usuario: str, comps: list[str]):
        """(entrada, kv, id del padre, nombre) del último componente. entrada es None si no existe."""
        padre = "root"
        actual, kv = {"id": await self.raiz(usuario), "tipo": "d"}, None
        nombre = usuario
        for i, nombre in enumerate(comps):
            if actual["tipo"] != "d":
                raise ErrorDfsha(pb.REASON_NOT_FOUND, "un componente de la ruta no es un directorio",
                                 path="/" + "/".join([usuario, *comps[:i]]))
            padre = actual["id"]
            actual, kv = await self._json(self.k("ns", padre, nombre))
            if actual is None and i < len(comps) - 1:
                raise ErrorDfsha(pb.REASON_NOT_FOUND, "no existe", path="/" + "/".join([usuario, *comps[:i + 1]]))
        return actual, kv, padre, nombre

    async def _existente(self, usuario, ruta):
        comps = self.partes(ruta, usuario)
        entrada, kv, padre, nombre = await self.resolver(usuario, comps)
        if entrada is None:
            raise ErrorDfsha(pb.REASON_NOT_FOUND, "no existe", path=ruta)
        return comps, entrada, kv, padre, nombre

    # ------------------------------------------------------------------ RF1
    async def mkdir(self, usuario: str, ruta: str) -> None:
        comps = self.partes(ruta, usuario)
        if not comps:
            raise ErrorDfsha(pb.REASON_EXISTS, "ya existe", path=ruta)
        entrada, _, padre, nombre = await self.resolver(usuario, comps)
        if entrada is not None:
            raise ErrorDfsha(pb.REASON_EXISTS, "ya existe", path=ruta)
        nuevo = {"id": _id("d-"), "tipo": "d", "dueno": usuario, "modificado": _ahora_ms()}
        clave = self.k("ns", padre, nombre)
        if not await self._txn([self.cli.comparar_no_existe(clave)], [
                self.cli.op_poner(clave, json.dumps(nuevo)),
                self.cli.op_poner(self.k("inodes", nuevo["id"]), json.dumps({"padre": padre, "nombre": nombre}))]):
            raise ErrorDfsha(pb.REASON_EXISTS, "ya existe", path=ruta)

    async def _a_entrada(self, nombre: str, e: dict) -> pb.Entry:
        r = pb.Entry(name=nombre, type=pb.ENTRY_TYPE_DIRECTORY if e["tipo"] == "d" else pb.ENTRY_TYPE_FILE,
                     owner=e["dueno"], modified_at_unix_ms=e["modificado"])
        if e["tipo"] == "f":
            a, _ = await self._json(self.k("files", e["id"]))
            if a:
                r.size, r.version, r.modified_at_unix_ms = a["tamano"], a["version"], a["modificado"]
        return r

    async def listar(self, usuario: str, ruta: str) -> pb.ListResponse:
        comps = self.partes(ruta, usuario)
        entrada, _, _, nombre = await self.resolver(usuario, comps)
        if entrada is None:
            raise ErrorDfsha(pb.REASON_NOT_FOUND, "no existe", path=ruta)
        if entrada["tipo"] == "f":
            return pb.ListResponse(entries=[await self._a_entrada(nombre, entrada)])
        prefijo = self.k("ns", entrada["id"], "")
        hijos = await self.cli.rango(prefijo)
        return pb.ListResponse(entries=[await self._a_entrada(kv.key.decode()[len(prefijo):], json.loads(kv.value))
                                        for kv in hijos])

    async def stat(self, usuario: str, ruta: str) -> pb.FileInfo:
        comps = self.partes(ruta, usuario)
        entrada, _, _, nombre = await self.resolver(usuario, comps)
        if entrada is None:
            raise ErrorDfsha(pb.REASON_NOT_FOUND, "no existe", path=ruta)
        info = pb.FileInfo(entry=await self._a_entrada(nombre, entrada))
        if entrada["tipo"] == "f":
            a, _ = await self._json(self.k("files", entrada["id"]))
            info.file_checksum, info.block_size, info.replication = a["hash"], a["bloque"], a["r"]
        return info

    async def rmdir(self, usuario: str, ruta: str) -> None:
        comps, entrada, kv, padre, nombre = await self._existente(usuario, ruta)
        if not comps or entrada["tipo"] != "d":
            raise ErrorDfsha(None, "no es un directorio que se pueda borrar")
        if await self.cli.contar(self.k("ns", entrada["id"], "")):
            raise ErrorDfsha(pb.REASON_DIR_NOT_EMPTY, "el directorio no está vacío", path=ruta)
        if not await self._txn([self.cli.comparar_modificacion(kv.key, kv.mod_revision)],
                               [self.cli.op_borrar(kv.key), self.cli.op_borrar(self.k("inodes", entrada["id"]))]):
            raise ErrorDfsha(pb.REASON_NOT_FOUND, "cambió mientras se borraba", path=ruta)

    async def remove(self, usuario: str, ruta: str) -> None:
        """Borra el archivo si nadie tiene locks sobre él. Los bloques los borra el recolector."""
        _, entrada, kv, _, _ = await self._existente(usuario, ruta)
        if entrada["tipo"] != "f":
            raise ErrorDfsha(None, "es un directorio, use rmdir")
        fid = entrada["id"]
        for _ in range(5):
            if await self.cli.contar(self.k("locks", fid, "")):
                raise ErrorDfsha(pb.REASON_LOCKED, "el archivo tiene locks vigentes")
            archivo, kva = await self._json(self.k("files", fid))
            u, kvu = await self._json(self.k("users", usuario))
            u["usado"] = max(0, u["usado"] - archivo["tamano"])
            if await self._txn([self.cli.comparar_modificacion(kv.key, kv.mod_revision),
                                self.cli.comparar_modificacion(kva.key, kva.mod_revision),
                                self.cli.comparar_modificacion(kvu.key, kvu.mod_revision)],
                               [self.cli.op_borrar(kv.key), self.cli.op_borrar(self.k("inodes", fid)),
                                self.cli.op_borrar(kva.key), self.cli.op_poner(kvu.key, json.dumps(u))]):
                return
            _, entrada, kv, _, _ = await self._existente(usuario, ruta)
        raise ErrorDfsha(pb.REASON_NO_QUORUM, "demasiados cambios simultáneos", retry_after_ms=200)

    async def rename(self, usuario: str, origen: str, destino: str) -> None:
        comps_o, entrada, kv, _, _ = await self._existente(usuario, origen)
        comps_d = self.partes(destino, usuario)
        if not comps_o or not comps_d:
            raise ErrorDfsha(None, "no se puede mover el directorio raíz del usuario")
        existe, _, padre_d, nombre_d = await self.resolver(usuario, comps_d)
        if existe is not None:
            raise ErrorDfsha(pb.REASON_EXISTS, "el destino ya existe", path=destino)
        if entrada["tipo"] == "d":                 # no mover un directorio dentro de sí mismo
            actual = padre_d
            while actual != "root":
                if actual == entrada["id"]:
                    raise ErrorDfsha(None, "no se puede mover un directorio dentro de sí mismo")
                inodo, _ = await self._json(self.k("inodes", actual))
                actual = inodo["padre"] if inodo else "root"
        clave_d = self.k("ns", padre_d, nombre_d)
        if not await self._txn([self.cli.comparar_modificacion(kv.key, kv.mod_revision), self.cli.comparar_no_existe(clave_d)],
                               [self.cli.op_borrar(kv.key), self.cli.op_poner(clave_d, kv.value),
                                self.cli.op_poner(self.k("inodes", entrada["id"]),
                                                  json.dumps({"padre": padre_d, "nombre": nombre_d}))]):
            raise ErrorDfsha(pb.REASON_EXISTS, "el origen o el destino cambiaron", path=destino)

    # ------------------------------------------------------------------ descriptores
    async def _sesion(self, usuario: str, session_id: str) -> int:
        if await self.n.usuario_de_sesion(session_id) != usuario:
            raise ErrorDfsha(pb.REASON_LEASE_EXPIRED, "la sesión venció o no es de este usuario")
        return int(session_id, 16)

    async def descriptor(self, usuario: str, fd: str) -> dict:
        info, _ = await self._json(self.k("fds", fd)) if fd.startswith("fd-") else (None, None)
        if info is None:
            raise ErrorDfsha(pb.REASON_LEASE_EXPIRED, "descriptor cerrado o sesión vencida")
        if info["usuario"] != usuario:
            raise ErrorDfsha(pb.REASON_FORBIDDEN, "el descriptor es de otro usuario")
        return info

    def _ubicacion(self, b: dict, usuario: str) -> pb.BlockLocation:
        puerto = self.n.cfg.cluster.puerto_datos
        return pb.BlockLocation(
            index=b["i"], block_id=b["id"], cipher_checksum=b["H"], plain_checksum=b["h"], size=b["t"],
            replicas=[f"{n}:{puerto}" for n in self.n.nodos.ubicaciones(b["id"], b["H"])],
            access_token=tokens.emitir(self.n.autenticador.secretos.hmac_bloques, b["id"], tokens.LEER, usuario,
                                       self.n.cfg.tiempos.token_bloque_ttl_s))

    async def _crear_archivo(self, usuario: str, padre: str, nombre: str) -> None:
        cfg = self.n.cfg
        fid = _id("f-")
        clave = base64.b64encode(await self.n.envoltorio.envolver(os.urandom(32))).decode()
        ns = self.k("ns", padre, nombre)
        await self._txn([self.cli.comparar_no_existe(ns)], [
            self.cli.op_poner(ns, json.dumps({"id": fid, "tipo": "f", "dueno": usuario, "modificado": _ahora_ms()})),
            self.cli.op_poner(self.k("inodes", fid), json.dumps({"padre": padre, "nombre": nombre})),
            self.cli.op_poner(self.k("files", fid), json.dumps({
                "version": 0, "tamano": 0, "bloque": cfg.almacenamiento.tamano_bloque, "r": cfg.almacenamiento.replicas,
                "hash": HUELLA_VACIA, "clave": clave, "modificado": _ahora_ms()})),
            self.cli.op_poner(self.k("manifests", fid, 0), json.dumps({"bloques": []}))])

    async def abrir(self, usuario: str, ruta: str, modo: int, session_id: str) -> pb.OpenResponse:
        if modo not in (LEER, ESCRIBIR, ANEXAR, LEER_ESCRIBIR):
            raise ErrorDfsha(None, "modo de apertura inválido")
        lease = await self._sesion(usuario, session_id)
        comps = self.partes(ruta, usuario)
        if not comps:
            raise ErrorDfsha(None, "es un directorio")
        entrada, _, padre, nombre = await self.resolver(usuario, comps)
        if entrada is None:
            if modo in (LEER, LEER_ESCRIBIR):
                raise ErrorDfsha(pb.REASON_NOT_FOUND, "no existe", path=ruta)
            await self._crear_archivo(usuario, padre, nombre)
            entrada, _, _, _ = await self.resolver(usuario, comps)
        if entrada["tipo"] != "f":
            raise ErrorDfsha(None, "es un directorio")
        fid = entrada["id"]
        archivo, _ = await self._json(self.k("files", fid))
        manifiesto, _ = await self._json(self.k("manifests", fid, archivo["version"]))
        fd = _id("fd-")
        sesion = f"{lease:016x}"
        await self._txn([], [
            self.cli.op_poner(self.k("fds", fd), json.dumps({"archivo": fid, "usuario": usuario, "sesion": sesion,
                                                              "modo": modo, "version": archivo["version"]}), lease),
            self.cli.op_poner(self.k("readleases", fid, archivo["version"], sesion), "", lease)])
        respuesta = pb.OpenResponse(
            fd=fd, file_id=fid, version=archivo["version"], size=archivo["tamano"], block_size=archivo["bloque"],
            file_checksum=archivo["hash"], read_lease_s=self.n.cfg.tiempos.sesion_ttl_s,
            file_key=await self.n.envoltorio.desenvolver(base64.b64decode(archivo["clave"])),
            blocks=[self._ubicacion(b, usuario) for b in manifiesto["bloques"]])
        if modo == ESCRIBIR:
            try:
                respuesta.locks.append(await self._tomar(fid, sesion, lease, pb.LockTarget(whole_file=True),
                                                         self.n.cfg.tiempos.espera_lock_s, manifiesto))
            except ErrorDfsha:
                await self.cerrar(usuario, fd)
                raise
        return respuesta

    async def ubicaciones(self, usuario: str, fd: str, primero: int, ultimo: int) -> pb.BlockLocations:
        info = await self.descriptor(usuario, fd)
        manifiesto, _ = await self._json(self.k("manifests", info["archivo"], info["version"]))
        if manifiesto is None:
            raise ErrorDfsha(pb.REASON_NOT_FOUND, "la versión ya no existe")
        return pb.BlockLocations(blocks=[self._ubicacion(b, usuario) for b in manifiesto["bloques"]
                                         if primero <= b["i"] <= ultimo])

    async def asignar(self, usuario: str, fd: str, indices: list[int]) -> pb.AllocateBlocksResponse:
        cfg = self.n.cfg
        info = await self.descriptor(usuario, fd)
        if info["modo"] == LEER:
            raise ErrorDfsha(pb.REASON_FORBIDDEN, "el archivo se abrió solo para leer")
        if not 0 < len(indices) <= cfg.almacenamiento.bloques_por_asignacion:
            raise ErrorDfsha(None, f"se piden de 1 a {cfg.almacenamiento.bloques_por_asignacion} bloques por llamada")
        u, _ = await self._json(self.k("users", usuario))
        nuevo_uso = u["usado"] + len(indices) * cfg.almacenamiento.tamano_bloque
        if u["cuota"] and nuevo_uso > u["cuota"]:
            raise ErrorDfsha(pb.REASON_QUOTA_EXCEEDED, "se supera la cuota", used_bytes=u["usado"], limit_bytes=u["cuota"])
        cadena = self.n.nodos.elegir_cadena(cfg.almacenamiento.replicas)
        if len(cadena) < cfg.almacenamiento.confirmaciones:
            raise ErrorDfsha(pb.REASON_NO_SPACE, "no hay suficientes nodos de datos vivos")
        clave = self.n.autenticador.secretos.hmac_bloques
        respuesta, operaciones = pb.AllocateBlocksResponse(), []
        for j, indice in enumerate(indices):
            rotada = cadena[j % len(cadena):] + cadena[:j % len(cadena)]
            direcciones = [f"{rotada[0]}:{cfg.cluster.puerto_datos}"] + [f"{n}:{cfg.cluster.puerto_datos_interno}"
                                                                        for n in rotada[1:]]
            bid = _id("b-")
            respuesta.blocks.add(index=indice, block_id=bid, chain=direcciones, access_token=tokens.emitir(
                clave, bid, tokens.ESCRIBIR, usuario, cfg.tiempos.token_bloque_ttl_s))
            operaciones.append(self.cli.op_poner(self.k("uc", info["archivo"], fd, bid), json.dumps(
                {"i": indice, "cadena": rotada, "sesion": info["sesion"], "fecha": _ahora_ms()})))
        await self._txn([], operaciones)
        return respuesta

    # ------------------------------------------------------------------ locks (D4 y A5)
    def _candado(self, fid: str) -> asyncio.Lock:
        return self._candados.setdefault(fid, asyncio.Lock())

    async def _esperar_sin(self, prefijo: str, propio: str, limite: float) -> bool:
        """Espera a que bajo el prefijo no quede ninguna clave ajena (que no contenga `propio`)."""
        bucle = asyncio.get_running_loop()
        while True:
            if not [kv for kv in await self.cli.rango(prefijo) if propio not in kv.key.decode()]:
                return True
            if bucle.time() >= limite:
                return False
            await asyncio.sleep(0.05)

    async def _ocupado(self, nombre: str, fid: str) -> ErrorDfsha:
        dueños = await self.cli.rango(nombre + "/")
        if not dueños:
            return ErrorDfsha(pb.REASON_LOCKED, "se agotó la espera del lock")
        primero = min(dueños, key=lambda kv: kv.create_revision)
        ttl = await self.cli.ttl_lease(primero.lease)
        return ErrorDfsha(pb.REASON_LOCKED, "se agotó la espera del lock", lock_owner=f"{primero.lease:016x}",
                          lock_expires_at_unix_ms=_ahora_ms() + max(ttl, 0) * 1000)

    async def _tomar(self, fid: str, sesion: str, lease: int, t: pb.LockTarget, espera_s: float,
                     manifiesto: dict | None = None) -> pb.LockGrant:
        bucle = asyncio.get_running_loop()
        limite = bucle.time() + espera_s
        obj = objetivo(t)
        nombre = self.k("locks", fid, obj)
        archivo_prefijo, intenciones = self.k("locks", fid, "archivo", ""), self.k("locks", fid, "intencion", "")
        propio = f"{lease:x}"
        try:
            if obj == "archivo":
                async with self._candado(fid):
                    tarea = asyncio.ensure_future(self.cli.bloquear(nombre, lease, max(0.1, limite - bucle.time())))
                    while not tarea.done() and await self.cli.obtener(f"{nombre}/{propio}") is None:
                        await asyncio.sleep(0.01)  # ya en la cola: desde aquí ninguna intención nueva entra
                clave, token = await tarea
                if not await self._esperar_sin(intenciones, sesion, limite):
                    await self.cli.desbloquear(clave)
                    raise ErrorDfsha(pb.REASON_LOCKED, "hay locks de bloque vigentes en el archivo")
            else:
                while True:
                    async with self._candado(fid):
                        if not [kv for kv in await self.cli.rango(archivo_prefijo) if propio not in kv.key.decode()]:
                            await self.cli.poner(self.k("locks", fid, "intencion", f"{sesion}-{obj}"), "", lease)
                            break
                    if not await self._esperar_sin(archivo_prefijo, propio, limite):
                        raise await self._ocupado(self.k("locks", fid, "archivo"), fid)
                try:
                    clave, token = await self.cli.bloquear(nombre, lease, max(0.1, limite - bucle.time()))
                except Exception:
                    await self.cli.borrar_prefijo(self.k("locks", fid, "intencion", f"{sesion}-{obj}"))
                    raise
        except LockOcupadoError:
            raise await self._ocupado(nombre, fid) from None
        actual = ""
        if t.HasField("block_index"):
            if manifiesto is None:
                archivo, _ = await self._json(self.k("files", fid))
                manifiesto, _ = await self._json(self.k("manifests", fid, archivo["version"]))
            actual = next((b["id"] for b in manifiesto["bloques"] if b["i"] == t.block_index), "")
        return pb.LockGrant(target=t, fencing_token=token, current_block_id=actual,
                            expires_at_unix_ms=_ahora_ms() + self.n.cfg.tiempos.sesion_ttl_s * 1000)

    async def bloquear(self, usuario: str, fd: str, objetivos, espera_s: int) -> pb.LockResponse:
        info = await self.descriptor(usuario, fd)
        if info["modo"] == LEER:
            raise ErrorDfsha(pb.REASON_FORBIDDEN, "el archivo se abrió solo para leer")
        espera = min(espera_s or self.n.cfg.tiempos.espera_lock_s, self.n.cfg.tiempos.espera_lock_s)
        lease, concedidos = int(info["sesion"], 16), []
        try:
            for t in objetivos:
                concedidos.append(await self._tomar(info["archivo"], info["sesion"], lease, t, espera))
        except ErrorDfsha:
            await self.liberar(usuario, fd, [g.target for g in concedidos])
            raise
        return pb.LockResponse(grants=concedidos)

    async def liberar(self, usuario: str, fd: str, objetivos=()) -> None:
        info = await self.descriptor(usuario, fd)
        fid, sesion = info["archivo"], info["sesion"]
        propio = f"/{int(sesion, 16):x}"
        nombres = {objetivo(t) for t in objetivos}
        for kv in await self.cli.rango(self.k("locks", fid, "")):
            clave = kv.key.decode()
            resto = clave[len(self.k("locks", fid, "")):]
            obj = resto.split("/", 1)[0]
            if obj == "intencion":
                obj_int = resto.split("/", 1)[1]
                if obj_int.startswith(sesion + "-") and (not nombres or obj_int.split("-", 1)[1] in nombres):
                    await self.cli.borrar_prefijo(clave)
            elif clave.endswith(propio) and (not nombres or obj in nombres):
                await self.cli.desbloquear(kv.key)

    # ------------------------------------------------------------------ commit (D3)
    async def commit(self, usuario: str, fd: str, op_id: str, bloques, nuevo_tamano: int,
                     reemplazar: bool) -> pb.CommitResponse:
        cfg = self.n.cfg
        if not op_id:
            raise ErrorDfsha(None, "falta op_id")
        hecho, _ = await self._json(self.k("ops", op_id))
        if hecho:
            return pb.CommitResponse(version=hecho["version"], file_checksum=hecho["hash"])
        info = await self.descriptor(usuario, fd)
        fid, sesion = info["archivo"], info["sesion"]
        lease = int(sesion, 16)
        for b in bloques:                          # W copias verificadas por los avisos de los nodos
            if len(self.n.nodos.ubicaciones(b.new_block_id, b.cipher_checksum)) < cfg.almacenamiento.confirmaciones:
                raise ErrorDfsha(pb.REASON_INSUFFICIENT_REPLICAS, f"el bloque {b.index} no tiene suficientes copias",
                                 retry_after_ms=500)
        for _ in range(5):
            archivo, kva = await self._json(self.k("files", fid))
            if archivo is None:
                raise ErrorDfsha(pb.REASON_NOT_FOUND, "el archivo se borró")
            u, kvu = await self._json(self.k("users", usuario))
            manifiesto, _ = await self._json(self.k("manifests", fid, archivo["version"]))
            actual = {b["i"]: b for b in manifiesto["bloques"]}
            nuevo = {} if reemplazar else dict(actual)
            for b in bloques:
                base = actual.get(b.index)
                if not reemplazar and (b.base_block_id or base) and (base or {}).get("id") != b.base_block_id:
                    raise ErrorDfsha(pb.REASON_VERSION_CONFLICT, f"el bloque {b.index} cambió",
                                     block_index=b.index, current_block_id=(base or {}).get("id", ""))
                nuevo[b.index] = {"i": b.index, "id": b.new_block_id, "H": b.cipher_checksum, "h": b.plain_checksum,
                                  "t": b.size}
            lista = [nuevo[i] for i in sorted(nuevo)]
            if [b["i"] for b in lista] != list(range(len(lista))):
                raise ErrorDfsha(pb.REASON_BAD_OFFSET, "los bloques dejan un hueco en el archivo")
            mios = {kv.key.decode(): kv.create_revision for kv in await self.cli.rango(self.k("locks", fid, ""))
                    if kv.key.decode().endswith(f"/{lease:x}")}
            comparaciones = {}
            for b in bloques:
                candidatos = [self.k("locks", fid, o, f"{lease:x}") for o in (f"b{b.index}", "fin", "archivo")]
                clave = next((c for c in candidatos if mios.get(c) == b.fencing_token), None)
                if clave is None:
                    raise ErrorDfsha(pb.REASON_LEASE_EXPIRED, f"el lock del bloque {b.index} ya no es válido")
                comparaciones[clave] = self.cli.comparar_creacion(clave, b.fencing_token)
            usado = max(0, u["usado"] - archivo["tamano"] + nuevo_tamano)
            if u["cuota"] and usado > u["cuota"]:
                raise ErrorDfsha(pb.REASON_QUOTA_EXCEEDED, "se supera la cuota", used_bytes=u["usado"],
                                 limit_bytes=u["cuota"])
            version, huella = archivo["version"] + 1, huella_archivo(lista)
            u["usado"] = usado
            archivo.update(version=version, tamano=nuevo_tamano, hash=huella, modificado=_ahora_ms())
            lease_op = await self.cli.conceder_lease(cfg.tiempos.op_id_ttl_s)
            info_nueva = dict(info, version=version)
            if await self._txn(
                    [self.cli.comparar_modificacion(kva.key, kva.mod_revision),
                     self.cli.comparar_modificacion(kvu.key, kvu.mod_revision), *comparaciones.values()],
                    [self.cli.op_poner(self.k("manifests", fid, version), json.dumps({"bloques": lista})),
                     self.cli.op_poner(kva.key, json.dumps(archivo)),
                     self.cli.op_poner(kvu.key, json.dumps(u)),
                     self.cli.op_borrar_prefijo(self.k("uc", fid, fd, "")),
                     self.cli.op_poner(self.k("ops", op_id), json.dumps({"version": version, "hash": huella}), lease_op),
                     self.cli.op_poner(self.k("fds", fd), json.dumps(info_nueva), lease),
                     self.cli.op_poner(self.k("readleases", fid, version, sesion), "", lease),
                     self.cli.op_borrar(self.k("readleases", fid, info["version"], sesion))]):
                return pb.CommitResponse(version=version, file_checksum=huella)
            await self.cli.revocar_lease(lease_op)
            hecho, _ = await self._json(self.k("ops", op_id))
            if hecho:
                return pb.CommitResponse(version=hecho["version"], file_checksum=hecho["hash"])
        raise ErrorDfsha(pb.REASON_NO_QUORUM, "demasiados cambios simultáneos", retry_after_ms=200)

    async def cerrar(self, usuario: str, fd: str) -> None:
        try:
            info = await self.descriptor(usuario, fd)
        except ErrorDfsha:
            return                                 # ya cerrado o sesión vencida, Close no falla
        await self.liberar(usuario, fd)
        await self._txn([], [self.cli.op_borrar(self.k("readleases", info["archivo"], info["version"], info["sesion"])),
                             self.cli.op_borrar(self.k("fds", fd))])

