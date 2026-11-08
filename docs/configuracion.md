# Configuración

Ningún nombre de nodo, puerto, ruta ni tiempo va escrito en el código de los servicios (RNF7). Todos salen de `dfsha/common/config.py`, que combina tres fuentes en este orden de prioridad (la última gana).

1. **Valores por defecto**, que son los de [docs/decisiones.md](decisiones.md).
2. **Un archivo TOML**, indicado con la variable `DFSHA_CONFIG`.
3. **Variables de entorno** `DFSHA_<SECCION>_<CLAVE>`, por ejemplo `DFSHA_TIEMPOS_SESION_TTL_S=30`. Es la forma que usa Docker Compose en cada máquina.

Los tamaños se pueden escribir en bytes o con unidad (`16MiB`, `64KiB`). Al cargar, la configuración se valida y una combinación incoherente (por ejemplo W mayor que R) detiene el servicio con un mensaje que dice qué valor corregir.

```
from dfsha.common.config import cargar
cfg = cargar()
cfg.almacenamiento.tamano_bloque, cfg.tiempos.sesion_ttl_s
```

## Parámetros

| Sección y clave | Por defecto | Qué es | Decisión |
|---|---|---|---|
| `cluster.nombre` | `dfsha.internal` | Nombre lógico que resuelve a los 3 nodos de metadatos | D7 |
| `cluster.puerto_metadatos` | 50051 | MetadataService y AdminService, para clientes, con TLS | D9 |
| `cluster.puerto_datos` | 50052 | DataService, para clientes, con TLS | D9 |
| `cluster.puerto_metadatos_interno` | 50053 | NodeService, solo nodos de datos, con TLS mutuo | D8 |
| `cluster.puerto_datos_interno` | 50054 | DataService entre nodos de datos (cadena y copias), con TLS mutuo | D8 |
| `etcd.destino` | `dns:///dfsha.internal:2379` | Destino gRPC con los 3 miembros de etcd | D1 |
| `etcd.prefijo` | `/dfsha/` | Prefijo de todas las claves | Esquema de etcd |
| `etcd.timeout_s` | 5 | Plazo total de cada llamada a etcd, reintentos incluidos | D1 |
| `etcd.tls` | `true` | TLS mutuo con etcd usando el certificado del nodo | D8 |
| `tls.habilitado` | `true` | TLS 1.3 y TLS mutuo entre servidores | D8 |
| `tls.ca`, `tls.certificado`, `tls.llave` | `/etc/dfsha/certs/ca.crt`, vacío, vacío | Certificado de la CA interna y el propio del nodo (ver [docs/tls.md](tls.md)) | D8 |
| `almacenamiento.tamano_bloque` | 16 MiB | Tamaño de bloque | D5 |
| `almacenamiento.replicas` | 3 | R, mínimo 2 | D5 |
| `almacenamiento.confirmaciones` | 2 | W, copias verificadas para dar un bloque por escrito | D5 |
| `almacenamiento.tamano_trozo` | 1 MiB | Trozo del stream de bloques, menor que el límite de 4 MiB de gRPC | D5 |
| `almacenamiento.bloques_por_asignacion` | 8 | Bloques por llamada a AllocateBlocks | D9 |
| `tiempos.sesion_ttl_s` | 30 | Vida de la sesión, que también cubre locks y arriendos de lectura | D4 |
| `tiempos.renovacion_s` | 10 | Intervalo de Renew, como máximo la mitad de la sesión | D4 |
| `tiempos.lider_ttl_s` | 3 | Lease de la candidatura a líder de metadatos | D1 |
| `tiempos.espera_lock_s` | 10 | Espera máxima en la cola de un lock | D4 |
| `tiempos.latido_s`, `sospechoso_s`, `muerto_s` | 3, 10, 30 | Latido de los nodos de datos y umbrales de detección | D6 |
| `tiempos.timeout_replica_s` | 3 | Plazo antes de leer la siguiente réplica | D6 |
| `tiempos.gracia_recolector_s` | 60 | Tiempo sin borrar bloques viejos tras un cambio de líder | D4 |
| `tiempos.op_id_ttl_s` | 3600 | Vida del resultado de un commit para reintentos | D3 |
| `tiempos.jwt_ttl_s` | 3600 | Vida del JWT | D8 |
| `tiempos.token_renovacion_ttl_s` | 86400 | Vida del token de renovación (1 día) | D8 |
| `tiempos.token_bloque_ttl_s` | 600 | Vida del token de acceso por bloque | D8 |
| `tiempos.auditoria_ttl_s` | 604800 | Vida de un registro de auditoría (7 días) | D8 |
| `limites.peticiones_simultaneas` | 64 | Límite por servidor antes de responder OVERLOADED | D8 |
| `kms.clave`, `kms.region` | `alias/dfsha-kek`, `us-east-1` | Clave maestra que envuelve las claves del clúster y las de archivo. `archivo:<ruta>` usa una clave local de desarrollo (ver [docs/metadatos.md](metadatos.md)) | D8 |
| `nodo.nombre` | vacío | Nombre DNS propio del nodo, por ejemplo `dn1.dfsha.internal` o `mds1.dfsha.internal` | D7 |
| `nodo.zona` | vacío | Zona de disponibilidad del nodo | D5 |
| `nodo.directorio_datos` | `/var/lib/dfsha` | Dónde guarda bloques un nodo de datos | D5 |

## Reglas de validación

- R es al menos 2 y W está entre 1 y R.
- El trozo es positivo, no supera el bloque y queda por debajo del límite de 4 MiB por mensaje de gRPC.
- La renovación es como máximo la mitad de la vida de la sesión, para que un mensaje perdido no la haga vencer.
- Se cumple latido < sospechoso < muerto.
- El lease del líder es de al menos 2 s.
- Los cuatro puertos (metadatos, datos y sus dos internos) son distintos.
- El prefijo de etcd empieza y termina con `/`.
- Una clave o sección desconocida en el archivo TOML es un error, para no aceptar en silencio un nombre mal escrito.

## Ejemplo de archivo para un nodo de datos

```toml
[cluster]
nombre = "dfsha.internal"

[nodo]
nombre = "dn2.dfsha.internal"
zona = "us-east-1b"
directorio_datos = "/var/lib/dfsha"

[tls]
ca = "/etc/dfsha/certs/ca.crt"
certificado = "/etc/dfsha/certs/dn2.crt"
llave = "/etc/dfsha/certs/dn2.key"
```

El mismo nodo, configurado solo con variables de entorno (por ejemplo en Docker Compose), queda así.

```
DFSHA_NODO_NOMBRE=dn2.dfsha.internal
DFSHA_NODO_ZONA=us-east-1b
DFSHA_TLS_CERTIFICADO=/etc/dfsha/certs/dn2.crt
DFSHA_TLS_LLAVE=/etc/dfsha/certs/dn2.key
```
