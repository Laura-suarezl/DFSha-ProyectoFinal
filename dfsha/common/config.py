"""Configuración de DFSha (ver docs/configuracion.md).

Ningún nombre, puerto, ruta ni tiempo va escrito en el código de los servicios (RNF7). Todo sale
de aquí, combinando tres fuentes en este orden de prioridad (la última gana):

1. Los valores por defecto de esta clase, que son los de docs/decisiones.md.
2. Un archivo TOML, indicado con la variable DFSHA_CONFIG o con el parámetro ruta.
3. Variables de entorno DFSHA_<SECCION>_<CLAVE>, por ejemplo DFSHA_TIEMPOS_SESION_TTL_S=30.

Uso:
    from dfsha.common.config import cargar
    cfg = cargar()
    cfg.cluster.nombre, cfg.tiempos.sesion_ttl_s, cfg.almacenamiento.tamano_bloque
"""
from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

KIB, MIB, GIB = 1024, 1024 ** 2, 1024 ** 3
LIMITE_MENSAJE_GRPC = 4 * MIB          # tamaño máximo por defecto de un mensaje gRPC


class ErrorDeConfiguracion(ValueError):
    """La configuración tiene un valor inválido o incoherente."""


@dataclass(frozen=True)
class Cluster:
    nombre: str = "dfsha.internal"     # nombre lógico, resuelve a los 3 nodos de metadatos (D7)
    puerto_metadatos: int = 50051      # MetadataService, AdminService y NodeService (D9)
    puerto_datos: int = 50052          # DataService (D9)


@dataclass(frozen=True)
class Etcd:
    destino: str = "dns:///dfsha.internal:2379"   # destino gRPC con los 3 miembros (D1)
    prefijo: str = "/dfsha/"
    timeout_s: float = 5.0             # plazo de cada llamada a etcd


@dataclass(frozen=True)
class Tls:
    habilitado: bool = True            # TLS 1.3, y TLS mutuo entre servidores (D8)
    ca: str = "/etc/dfsha/certs/ca.crt"
    certificado: str = ""              # certificado propio del nodo (vacío en el cliente)
    llave: str = ""


@dataclass(frozen=True)
class Almacenamiento:
    tamano_bloque: int = 16 * MIB      # D5
    replicas: int = 3                  # R
    confirmaciones: int = 2            # W
    tamano_trozo: int = 1 * MIB        # trozo del stream, configurable (D5)
    bloques_por_asignacion: int = 8    # bloques por llamada a AllocateBlocks


@dataclass(frozen=True)
class Tiempos:
    sesion_ttl_s: int = 30             # lease de la sesión (D4)
    renovacion_s: int = 10             # Renew(sesión)
    lider_ttl_s: int = 3               # lease de la candidatura a líder de metadatos
    espera_lock_s: int = 10            # espera máxima en la cola del lock
    latido_s: int = 3                  # latido de los nodos de datos (D6)
    sospechoso_s: int = 10
    muerto_s: int = 30
    timeout_replica_s: int = 3         # el cliente pasa a la siguiente réplica
    gracia_recolector_s: int = 60      # sin borrar bloques viejos tras un cambio de líder
    op_id_ttl_s: int = 3600            # vida del resultado de un commit (D3)
    jwt_ttl_s: int = 3600              # D8
    token_bloque_ttl_s: int = 600      # token de acceso por bloque (D8)
    auditoria_ttl_s: int = 7 * 24 * 3600


@dataclass(frozen=True)
class Limites:
    peticiones_simultaneas: int = 64   # mitigación de denegación de servicio (D8)


@dataclass(frozen=True)
class Kms:
    clave: str = "alias/dfsha-kek"     # clave maestra que envuelve las claves de archivo (D8)
    region: str = "us-east-1"


@dataclass(frozen=True)
class Nodo:
    nombre: str = ""                   # nombre DNS propio, por ejemplo dn1.dfsha.internal
    zona: str = ""                     # zona de disponibilidad
    directorio_datos: str = "/var/lib/dfsha"


@dataclass(frozen=True)
class Configuracion:
    cluster: Cluster = field(default_factory=Cluster)
    etcd: Etcd = field(default_factory=Etcd)
    tls: Tls = field(default_factory=Tls)
    almacenamiento: Almacenamiento = field(default_factory=Almacenamiento)
    tiempos: Tiempos = field(default_factory=Tiempos)
    limites: Limites = field(default_factory=Limites)
    kms: Kms = field(default_factory=Kms)
    nodo: Nodo = field(default_factory=Nodo)

    def validar(self) -> "Configuracion":
        a, t = self.almacenamiento, self.tiempos
        reglas = [
            (a.replicas >= 2, "almacenamiento.replicas debe ser al menos 2"),
            (1 <= a.confirmaciones <= a.replicas, "almacenamiento.confirmaciones debe estar entre 1 y replicas"),
            (0 < a.tamano_trozo <= LIMITE_MENSAJE_GRPC - 64 * KIB,
             "almacenamiento.tamano_trozo debe ser positivo y menor que el límite de 4 MiB de gRPC"),
            (a.tamano_trozo <= a.tamano_bloque, "almacenamiento.tamano_trozo no puede superar tamano_bloque"),
            (a.bloques_por_asignacion >= 1, "almacenamiento.bloques_por_asignacion debe ser al menos 1"),
            (0 < t.renovacion_s * 2 <= t.sesion_ttl_s,
             "tiempos.renovacion_s debe ser como máximo la mitad de sesion_ttl_s"),
            (0 < t.latido_s < t.sospechoso_s < t.muerto_s,
             "tiempos debe cumplir latido_s < sospechoso_s < muerto_s"),
            (t.lider_ttl_s >= 2, "tiempos.lider_ttl_s debe ser al menos 2"),
            (self.cluster.puerto_metadatos != self.cluster.puerto_datos,
             "cluster.puerto_metadatos y cluster.puerto_datos deben ser distintos"),
            (self.etcd.prefijo.startswith("/") and self.etcd.prefijo.endswith("/"),
             "etcd.prefijo debe empezar y terminar con /"),
        ]
        for cumple, mensaje in reglas:
            if not cumple:
                raise ErrorDeConfiguracion(mensaje)
        return self


_TAMANO = re.compile(r"^\s*(\d+)\s*(B|KIB|MIB|GIB)?\s*$", re.I)
_UNIDADES = {None: 1, "B": 1, "KIB": KIB, "MIB": MIB, "GIB": GIB}


def _convertir(valor: Any, tipo: type, nombre: str) -> Any:
    """Convierte un valor del TOML o del entorno al tipo del campo."""
    try:
        if tipo is bool:
            if isinstance(valor, bool):
                return valor
            texto = str(valor).strip().lower()
            if texto in ("1", "true", "si", "sí", "yes"):
                return True
            if texto in ("0", "false", "no"):
                return False
            raise ValueError(valor)
        if tipo is int:
            if isinstance(valor, int) and not isinstance(valor, bool):
                return valor
            m = _TAMANO.match(str(valor))          # admite tamaños como "16MiB" o "64KiB"
            if not m:
                raise ValueError(valor)
            return int(m.group(1)) * _UNIDADES[m.group(2).upper() if m.group(2) else None]
        if tipo is float:
            return float(valor)
        return str(valor)
    except ValueError:
        raise ErrorDeConfiguracion(f"{nombre}: valor inválido {valor!r}") from None


def _construir(clase: type, datos: Mapping[str, Any], seccion: str) -> Any:
    campos = {f.name: f for f in dataclasses.fields(clase)}
    desconocidas = set(datos) - set(campos)
    if desconocidas:
        raise ErrorDeConfiguracion(f"claves desconocidas en [{seccion}]: {sorted(desconocidas)}")
    tipos = {n: {"str": str, "int": int, "float": float, "bool": bool}[f.type] for n, f in campos.items()}
    return clase(**{n: _convertir(v, tipos[n], f"{seccion}.{n}") for n, v in datos.items()})


def cargar(ruta: str | os.PathLike | None = None, entorno: Mapping[str, str] | None = None) -> Configuracion:
    """Carga la configuración combinando valores por defecto, archivo TOML y variables de entorno."""
    entorno = os.environ if entorno is None else entorno
    ruta = ruta or entorno.get("DFSHA_CONFIG")
    archivo: dict[str, dict[str, Any]] = {}
    if ruta:
        with open(Path(ruta), "rb") as f:
            archivo = tomllib.load(f)

    secciones = {f.name: f for f in dataclasses.fields(Configuracion)}
    desconocidas = set(archivo) - set(secciones)
    if desconocidas:
        raise ErrorDeConfiguracion(f"secciones desconocidas: {sorted(desconocidas)}")

    valores = {}
    for nombre, campo in secciones.items():
        datos = dict(archivo.get(nombre, {}))
        for clave in (f.name for f in dataclasses.fields(campo.default_factory)):
            variable = f"DFSHA_{nombre}_{clave}".upper()
            if variable in entorno:
                datos[clave] = entorno[variable]
        valores[nombre] = _construir(campo.default_factory, datos, nombre)
    return Configuracion(**valores).validar()
