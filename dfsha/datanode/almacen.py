"""Almacén de bloques en el disco de un nodo de datos (D5 y D6).

Estructura bajo el directorio de datos del nodo:
    tmp/<id>.<aleatorio>.part   bloque que se está recibiendo (nunca cuenta como guardado)
    blocks/<id>                 bloque completo, verificado y definitivo
    blocks/<id>.meta            huella SHA-256 y tamaño del bloque

Un bloque pasa de tmp/ a blocks/ solo si llegó completo y con la huella esperada, con un
renombrado atómico después de forzar la escritura a disco. Si el nodo cae a mitad de un
bloque, queda solo el .part, que se borra al arrancar. Así nunca se publica un bloque
incompleto (escenario 3).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Iterator

FORMATO_ID = re.compile(r"^b-[0-9a-f]{16}$")


class IdInvalido(ValueError):
    """El id de bloque no tiene el formato b-xxxxxxxxxxxxxxxx (protege contra rutas maliciosas)."""


class HuellaIncorrecta(Exception):
    """El bloque recibido no coincide con la huella esperada."""

    def __init__(self, block_id: str, esperada: str, obtenida: str):
        super().__init__(f"{block_id}: huella {obtenida} distinta de la esperada {esperada}")
        self.block_id, self.esperada, self.obtenida = block_id, esperada, obtenida


class BloqueNoEncontrado(KeyError):
    """El nodo no tiene ese bloque."""


def validar_id(block_id: str) -> str:
    if not FORMATO_ID.match(block_id or ""):
        raise IdInvalido(repr(block_id))
    return block_id


def _sincronizar_directorio(ruta: Path) -> None:
    """Fuerza a disco la entrada del renombrado (solo existe en Linux y macOS)."""
    if os.name != "posix":
        return
    fd = os.open(ruta, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Escritura:
    """Un bloque que se está recibiendo. Se confirma con confirmar() o se descarta con descartar()."""

    def __init__(self, almacen: "Almacen", block_id: str):
        self.block_id = validar_id(block_id)
        self._almacen = almacen
        self._temporal = almacen.tmp / f"{block_id}.{uuid.uuid4().hex[:8]}.part"
        self._archivo = open(self._temporal, "wb")
        self._huella = hashlib.sha256()
        self._confirmando = False
        self.tamano = 0

    def escribir(self, datos: bytes) -> None:
        self._archivo.write(datos)
        self._huella.update(datos)
        self.tamano += len(datos)

    def confirmar(self, huella_esperada: str) -> int:
        """Verifica la huella, fuerza a disco y renombra a definitivo. Devuelve el tamaño.

        Desde que empieza, la confirmación decide sola el destino del temporal (renombrarlo si la
        huella coincide, borrarlo si no), aunque mientras tanto se pida descartar la escritura.
        """
        self._confirmando = True
        self._archivo.flush()
        os.fsync(self._archivo.fileno())
        self._archivo.close()
        obtenida = self._huella.hexdigest()
        if obtenida != huella_esperada:
            self._temporal.unlink(missing_ok=True)
            raise HuellaIncorrecta(self.block_id, huella_esperada, obtenida)
        definitivo = self._almacen.bloques / self.block_id
        os.replace(self._temporal, definitivo)          # atómico; si ya existía igual, queda igual
        meta = definitivo.with_suffix(".meta")
        meta_tmp = self._almacen.tmp / f"{self.block_id}.{uuid.uuid4().hex[:8]}.meta"
        meta_tmp.write_text(json.dumps({"checksum": obtenida, "size": self.tamano}), encoding="utf-8")
        os.replace(meta_tmp, meta)
        _sincronizar_directorio(self._almacen.bloques)
        return self.tamano

    def descartar(self) -> None:
        if self._confirmando:
            return
        if not self._archivo.closed:
            self._archivo.close()
        self._temporal.unlink(missing_ok=True)


class Almacen:
    def __init__(self, raiz: str | os.PathLike):
        self.raiz = Path(raiz)
        self.bloques = self.raiz / "blocks"
        self.tmp = self.raiz / "tmp"
        self.bloques.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def limpiar_temporales(self) -> int:
        """Borra los bloques a medias que dejó una caída. Se llama al arrancar el nodo."""
        borrados = 0
        for parcial in list(self.tmp.glob("*.part")) + list(self.tmp.glob("*.meta")):
            parcial.unlink(missing_ok=True)
            borrados += 1
        return borrados

    def nueva_escritura(self, block_id: str) -> Escritura:
        return Escritura(self, block_id)

    def existe(self, block_id: str) -> bool:
        return (self.bloques / validar_id(block_id)).is_file()

    def info(self, block_id: str) -> tuple[str, int]:
        """(huella, tamaño) guardados del bloque. Si falta el .meta, se recalcula la huella."""
        ruta = self.bloques / validar_id(block_id)
        if not ruta.is_file():
            raise BloqueNoEncontrado(block_id)
        meta = ruta.with_suffix(".meta")
        if meta.is_file():
            datos = json.loads(meta.read_text(encoding="utf-8"))
            return datos["checksum"], datos["size"]
        return self._calcular_huella(ruta), ruta.stat().st_size

    def verificar(self, block_id: str) -> bool:
        """True si el contenido en disco coincide con la huella guardada (revisión de bloques)."""
        esperada, _ = self.info(block_id)
        return self._calcular_huella(self.bloques / block_id) == esperada

    def leer(self, block_id: str, tamano_trozo: int) -> Iterator[bytes]:
        ruta = self.bloques / validar_id(block_id)
        if not ruta.is_file():
            raise BloqueNoEncontrado(block_id)
        with open(ruta, "rb") as f:
            while trozo := f.read(tamano_trozo):
                yield trozo

    def listar(self) -> list[str]:
        return sorted(p.name for p in self.bloques.iterdir() if FORMATO_ID.match(p.name))

    def borrar(self, block_id: str) -> bool:
        ruta = self.bloques / validar_id(block_id)
        existia = ruta.is_file()
        ruta.unlink(missing_ok=True)
        ruta.with_suffix(".meta").unlink(missing_ok=True)
        return existia

    @staticmethod
    def _calcular_huella(ruta: Path) -> str:
        h = hashlib.sha256()
        with open(ruta, "rb") as f:
            while trozo := f.read(1024 * 1024):
                h.update(trozo)
        return h.hexdigest()
