# DFSha

Sistema de archivos distribuido de alta disponibilidad, rendimiento y seguridad. Proyecto 1 de Sistemas Distribuidos (Arquitectura de nube y sistemas distribuidos), Universidad Pontificia Bolivariana, 2026-2, con el docente Álvaro Ospina Sanjuan.

**Equipo:** Laura Carolina Suárez Lozano y Tomás Castaño Peña.

**Arquitectura asignada:** opción 1, cliente/servidor con servicio distribuido entre servidores (S2S) dentro de una red privada (VPC de AWS Academy).


## Resumen de la arquitectura

- **Plano de control.** Tres nodos de metadatos con un líder elegido por consenso (Raft, a través de etcd). Guardan el árbol de directorios, las versiones de cada archivo, los locks y la ubicación de los bloques.
- **Plano de datos.** Cuatro nodos de datos que guardan los archivos partidos en bloques de 16 MB, con tres réplicas por bloque copiadas en cadena entre servidores.
- **Cliente.** CLI y biblioteca con las operaciones de RF1 a RF3. Cifra cada bloque antes de enviarlo y transfiere los bloques directamente con los nodos de datos.

Las decisiones, con sus alternativas y su justificación, están en [docs/decisiones.md](docs/decisiones.md).

## Estructura del repositorio

| Carpeta | Contenido |
|---|---|
| `proto/` | Contratos gRPC de DFSha y los .proto de etcd |
| `dfsha/common/` | Configuración, TLS, cifrado, errores y código generado |
| `dfsha/metadata/` | Nodos de metadatos |
| `dfsha/datanode/` | Nodos de datos |
| `dfsha/client/` | Biblioteca y CLI del cliente |
| `scripts/` | Descarga de los .proto de etcd y generación del código Python |
| `prototipos/` | Prototipos de los riesgos técnicos, con sus resultados |
| `deploy/` | Dockerfiles, Compose, CA interna (`deploy/certs/`) y scripts de AWS |
| `tests/` | Pruebas unitarias y de integración |
| `experiments/` | Experimentos E0 a E11 y sus resultados |
| `docs/` | Decisiones, plan, arquitectura, API y diagramas |

## Stack

Python 3.12, gRPC (grpcio), etcd 3.5, cryptography, argon2-cffi, PyJWT, boto3, typer, pytest, Docker y Docker Compose, AWS CLI. Detalle y justificación en [docs/decisiones.md](docs/decisiones.md).

## Entorno local con Docker

Para probar los servicios juntos en un solo computador hay un Compose de desarrollo con etcd y los nodos de datos, con TLS y los mismos nombres que en AWS. Cómo levantarlo y su prueba de humo están en [deploy/compose/README.md](deploy/compose/README.md).

## Instalación para desarrollo

```
python -m venv .venv
.venv\Scripts\activate          # en Linux o macOS: source .venv/bin/activate
pip install -e ".[dev]"
python scripts/generar_codigo.py   # genera dfsha/_gen/ desde proto/ (repetir si cambia un .proto)
pytest
```

El código generado no se versiona, así que hay que generarlo después de clonar. Los .proto de etcd ya están en `proto/etcd/`. Solo hace falta volver a descargarlos con `python scripts/vendor_etcd_protos.py` si se cambia la versión de etcd. El esquema de claves en etcd está en [docs/etcd-esquema.md](docs/etcd-esquema.md), la API de DFSha en [docs/api.md](docs/api.md) los parámetros de configuración en [docs/configuracion.md](docs/configuracion.md) y el TLS, la CA interna y los puertos en [docs/tls.md](docs/tls.md).

Las pruebas de integración necesitan un etcd en marcha y se saltan si no está configurado. Cómo ejecutarlas está en [prototipos/etcd-cluster/README.md](prototipos/etcd-cluster/README.md).

**Nota:** si el repositorio está dentro de una carpeta sincronizada (OneDrive, Dropbox, Google Drive), crea el entorno virtual fuera de ella, por ejemplo en `~/.venvs/dfsha`. La sincronización bloquea los archivos del entorno y la instalación falla.
