"""CLI de DFSha (RF1, RF2 y administración, docs/cliente.md).

Uso:  python -m dfsha.client <comando> ...   (o dfsha <comando> si el paquete está instalado)

login guarda el token de renovación en ~/.dfsha/sesion.json (solo para el usuario). Cada comando
lo usa para abrir una sesión nueva con Refresh, así no hace falta una sesión viva entre comandos.
La configuración (nombre del clúster, CA) sale de dfsha/common/config.py, como en los servidores.
"""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import typer
from google.protobuf import empty_pb2

from dfsha._gen import dfsha_pb2 as pb
from dfsha.client.cliente import Cliente, ErrorDfsha
from dfsha.common.config import cargar
from dfsha.common.tls import credenciales_canal

app = typer.Typer(help="Cliente de DFSha", no_args_is_help=True, add_completion=False)
admin = typer.Typer(help="Comandos de administración (rol administrador)", no_args_is_help=True)
app.add_typer(admin, name="admin")
ARCHIVO = Path(os.environ.get("DFSHA_SESION", Path.home() / ".dfsha" / "sesion.json"))


def _estado() -> dict:
    return json.loads(ARCHIVO.read_text(encoding="utf-8")) if ARCHIVO.exists() else {}


def _guardar(estado: dict) -> None:
    ARCHIVO.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVO.write_text(json.dumps(estado), encoding="utf-8")
    if os.name == "posix":
        os.chmod(ARCHIVO, 0o600)


def _nuevo_cliente() -> Cliente:
    cfg = cargar()
    return Cliente(cfg, credenciales_canal(cfg.tls) if cfg.tls.habilitado else None)


def _ejecutar(funcion):
    """Abre una sesión con el token guardado, ejecuta la función y guarda el token nuevo."""
    async def principal():
        estado = _estado()
        if not estado.get("renovacion"):
            raise ErrorDfsha("TOKEN_INVALID", "no hay sesión, ejecute login")
        c = _nuevo_cliente()
        try:
            await c.reanudar(estado["usuario"], estado["renovacion"])
            estado["renovacion"] = c.renovacion
            _guardar(estado)
            return await funcion(c, estado)
        finally:
            await c.cerrar()
    try:
        return asyncio.run(principal())
    except (ErrorDfsha, OSError) as e:
        typer.secho(f"error: {e}", fg="red", err=True)
        raise typer.Exit(1)


def _ruta(estado: dict, ruta: str | None) -> str:
    base = estado.get("cwd") or f"/{estado['usuario']}"
    if not ruta:
        return base
    return os.path.normpath(ruta if ruta.startswith("/") else f"{base}/{ruta}").replace("\\", "/")


@app.command()
def login(usuario: str):
    """Inicia sesión (contraseña en DFSHA_CONTRASENA o por la terminal)."""
    contrasena = os.environ.get("DFSHA_CONTRASENA") or getpass.getpass("Contraseña: ")

    async def principal():
        c = _nuevo_cliente()
        try:
            await c.login(usuario, contrasena)
            _guardar({"usuario": usuario, "renovacion": c.renovacion, "cwd": f"/{usuario}"})
            return c.lider
        finally:
            await c.cerrar()
    try:
        lider = asyncio.run(principal())
    except ErrorDfsha as e:
        typer.secho(f"error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"sesión iniciada como {usuario} (líder {lider or 'por nombre lógico'})")


@app.command()
def logout():
    """Borra el token guardado."""
    ARCHIVO.unlink(missing_ok=True)


@app.command()
def pwd():
    typer.echo(_ruta(_estado(), None))


@app.command()
def cd(ruta: str):
    async def f(c, e):
        destino = _ruta(e, ruta)
        if (await c.stat(destino)).entry.type != pb.ENTRY_TYPE_DIRECTORY:
            raise ErrorDfsha("NOT_FOUND", f"{destino} no es un directorio")
        e["cwd"] = destino
        _guardar(e)
    _ejecutar(f)


@app.command()
def ls(ruta: str = typer.Argument(None)):
    async def f(c, e):
        for x in await c.ls(_ruta(e, ruta)):
            tipo = "d" if x.type == pb.ENTRY_TYPE_DIRECTORY else "-"
            fecha = datetime.fromtimestamp(x.modified_at_unix_ms / 1000).strftime("%Y-%m-%d %H:%M")
            typer.echo(f"{tipo} {x.size:>12} v{x.version:<4} {fecha} {x.name}")
    _ejecutar(f)


@app.command()
def stat(ruta: str):
    async def f(c, e):
        i = await c.stat(_ruta(e, ruta))
        typer.echo(f"{i.entry.name}  tamaño {i.entry.size}  versión {i.entry.version}  dueño {i.entry.owner}  "
                   f"bloque {i.block_size}  réplicas {i.replication}  huella {i.file_checksum}")
    _ejecutar(f)


@app.command()
def mkdir(ruta: str):
    _ejecutar(lambda c, e: c.mkdir(_ruta(e, ruta)))


@app.command()
def rmdir(ruta: str):
    _ejecutar(lambda c, e: c.rmdir(_ruta(e, ruta)))


@app.command()
def rm(ruta: str):
    _ejecutar(lambda c, e: c.rm(_ruta(e, ruta)))


@app.command()
def mv(origen: str, destino: str):
    _ejecutar(lambda c, e: c.mv(_ruta(e, origen), _ruta(e, destino)))


@app.command()
def put(local: Path, remoto: str = typer.Argument(None)):
    """Sube un archivo local (RF2)."""
    async def f(c, e):
        destino = _ruta(e, remoto or local.name)
        r = await c.put(local.read_bytes(), destino)
        typer.echo(f"{destino} versión {r.version} huella {r.file_checksum}")
    _ejecutar(f)


@app.command()
def get(remoto: str, local: Path = typer.Argument(None)):
    """Descarga un archivo (RF2)."""
    async def f(c, e):
        origen = _ruta(e, remoto)
        datos = await c.get(origen)
        destino = local or Path(origen.rsplit("/", 1)[-1])
        destino.write_bytes(datos)
        typer.echo(f"{origen} -> {destino} ({len(datos)} bytes)")
    _ejecutar(f)


@admin.command("crear-usuario")
def crear_usuario(usuario: str, cuota: int = typer.Option(0, help="bytes, 0 es sin límite"),
                  administrador: bool = typer.Option(False, "--admin")):
    contrasena = os.environ.get("DFSHA_CONTRASENA_NUEVA") or getpass.getpass(f"Contraseña para {usuario}: ")
    _ejecutar(lambda c, e: c.llamar("admin", "CreateUser", pb.CreateUserRequest(
        username=usuario, password=contrasena, admin=administrador, quota_bytes=cuota)))


@admin.command()
def cuota(usuario: str, bytes_: int = typer.Argument(..., metavar="BYTES")):
    _ejecutar(lambda c, e: c.llamar("admin", "SetQuota", pb.SetQuotaRequest(username=usuario, quota_bytes=bytes_)))


@admin.command()
def nodos():
    async def f(c, e):
        r = await c.llamar("admin", "ListNodes", empty_pb2.Empty())
        typer.echo(f"líder de metadatos: {r.leader}")
        for n in r.nodes:
            estado = pb.NodeState.Name(n.state).removeprefix("NODE_STATE_")
            typer.echo(f"{n.name:<22} {n.zone:<12} {estado:<16} bloques {n.block_count:<6} libre {n.free_bytes}")
    _ejecutar(f)


@admin.command()
def retirar(nodo: str):
    """Retiro planificado de un nodo de datos (Decommission)."""
    _ejecutar(lambda c, e: c.llamar("admin", "Decommission", pb.DecommissionRequest(node=nodo)))


def main() -> None:
    app(prog_name="dfsha")


if __name__ == "__main__":
    sys.exit(main())
