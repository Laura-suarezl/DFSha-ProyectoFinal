# DFSha

Sistema de archivos distribuido de alta disponibilidad, rendimiento y seguridad. Proyecto 1 de Sistemas Distribuidos (Arquitectura de nube y sistemas distribuidos), Universidad Pontificia Bolivariana, 2026-2, con el docente Álvaro Ospina Sanjuan.

**Equipo:** Laura Carolina Suárez Lozano y Tomás Castaño Peña.

**Arquitectura asignada:** opción 1, cliente/servidor con servicio distribuido entre servidores (S2S) dentro de una red privada (VPC de AWS Academy).

## Resumen

- **Plano de control.** Tres nodos de metadatos con un líder elegido por consenso (Raft, a través de etcd). Guardan el árbol de directorios, las versiones de cada archivo, los locks y la ubicación de los bloques.
- **Plano de datos.** Cuatro nodos de datos que guardan los archivos partidos en bloques de 16 MiB, cifrados por el cliente, con tres copias por bloque escritas en cadena entre servidores.
- **Cliente.** CLI (RF1 y RF2) y biblioteca (RF3) que solo conocen el nombre lógico del clúster y transfieren los bloques directamente con los nodos de datos.
- **Despliegue.** 9 máquinas de AWS en 3 zonas, con Docker, sin salida a internet en las subredes privadas, TLS en todos los canales y TLS mutuo entre servidores.

| Documento | Contenido |
|---|---|
| [docs/arquitectura.md](docs/arquitectura.md) | Contexto, componentes, despliegue y flujo de una escritura |
| [docs/diagramas/](docs/diagramas/) | Casos de uso, secuencias S0 a S6 y escenarios 1 y 3 |
| [docs/semantica.md](docs/semantica.md) | Qué admite write(), semántica de lock() y garantías comprobables |
| [docs/decisiones.md](docs/decisiones.md) | Registro de decisiones del equipo y cambios frente a los diseños individuales |
| [docs/api.md](docs/api.md) | Servicios gRPC, operaciones y errores |
| [docs/etcd-esquema.md](docs/etcd-esquema.md) | Claves en etcd, transacciones y locks |
| [docs/metadatos.md](docs/metadatos.md), [docs/nodos.md](docs/nodos.md), [docs/cliente.md](docs/cliente.md) | Funcionamiento de cada componente |
| [docs/seguridad.md](docs/seguridad.md) | Mecanismos de seguridad y tabla STRIDE |
| [docs/pruebas.md](docs/pruebas.md) | Pruebas automáticas, escenarios locales y experimentos en AWS |
| [docs/tls.md](docs/tls.md), [docs/configuracion.md](docs/configuracion.md) | CA interna, puertos y parámetros |
| [experimentos/resultados/resumen.md](experimentos/resultados/resumen.md) | Resultados de las pruebas en AWS |

## Estructura del repositorio

| Carpeta | Contenido |
|---|---|
| `proto/` | Contratos gRPC de DFSha y los .proto de etcd |
| `dfsha/common/` | Configuración, TLS, tokens, errores y código generado |
| `dfsha/metadata/` | Nodos de metadatos |
| `dfsha/datanode/` | Nodos de datos |
| `dfsha/client/` | Biblioteca y CLI del cliente |
| `scripts/` | Descarga de los .proto de etcd y generación del código Python |
| `prototipos/` | Prototipos de los riesgos técnicos, con sus resultados |
| `deploy/` | Imagen Docker, Compose local, CA interna (`deploy/certs/`) y scripts de AWS (`deploy/aws/`) |
| `tests/` | Pruebas unitarias y de integración |
| `experimentos/` | Experimentos en AWS, sus resultados y su análisis |
| `docs/` | Documentación y diagramas |

## Stack

Python 3.12, gRPC (grpcio), etcd 3.5, cryptography, argon2-cffi, PyJWT, boto3, typer, pytest, Docker y Docker Compose, AWS CLI. Detalle y justificación en [docs/decisiones.md](docs/decisiones.md).

## 1. Instalación

Requisitos: Python 3.12 o superior, Docker con Docker Compose y Git. Para AWS, además, AWS CLI v2 y Bash (en Windows, Git Bash).

```
git clone https://github.com/Laura-suarezl/DFSha-ProyectoFinal.git
cd DFSha-ProyectoFinal
python -m venv .venv
.venv\Scripts\activate             # en Linux o macOS: source .venv/bin/activate
pip install -e ".[dev]"
python scripts/generar_codigo.py   # genera dfsha/_gen/ desde proto/ (no se versiona)
```

**Nota:** si el repositorio está dentro de una carpeta sincronizada (OneDrive, Dropbox, Google Drive), crea el entorno virtual fuera de ella, por ejemplo en `~/.venvs/dfsha`. La sincronización bloquea los archivos del entorno y la instalación falla.

## 2. Pruebas

```
pytest                                                            # pruebas unitarias
docker compose -f prototipos/etcd-cluster/docker-compose.yml up -d
DFSHA_PRUEBA_ETCD=ipv4:127.0.0.1:23791,127.0.0.1:23792,127.0.0.1:23793 pytest    # con integración
docker compose -f prototipos/etcd-cluster/docker-compose.yml down -v
```

Sin la variable `DFSHA_PRUEBA_ETCD` las pruebas de integración se saltan. Con ella se prueban los 3 nodos de metadatos sobre un etcd de 3 miembros.

## 3. Entorno local completo

Un Docker Compose levanta en un solo equipo los 3 miembros de etcd, los 3 nodos de metadatos, los 4 nodos de datos y una máquina cliente, con TLS y los mismos nombres que en AWS. Sirve para desarrollar, no reemplaza el despliegue distribuido.

```
python deploy/certs/crear_certificados.py --local
python -m dfsha.metadata crear-clave-local deploy/certs/out/kek-dev.bin
docker compose -f deploy/compose/docker-compose.dev.yml up -d --build
python deploy/compose/prueba_humo.py        # prueba de punta a punta
python deploy/compose/escenarios.py         # RF1 a RF3 y escenarios obligatorios 1 a 4
docker compose -f deploy/compose/docker-compose.dev.yml down -v
```

Detalle en [deploy/compose/README.md](deploy/compose/README.md).

## 4. Despliegue en AWS Academy

Con las credenciales del laboratorio en `~/.aws/credentials`, desde Bash.

```
bash deploy/aws/crear_infraestructura.sh    # VPC, subredes, endpoints, grupos, Route 53, KMS, bucket, llave SSH
bash deploy/aws/subir_artefactos.sh         # imagen, certificados y Compose por rol al bucket privado
bash deploy/aws/crear_instancias.sh         # las 9 EC2, que se configuran solas al arrancar
bash deploy/aws/ssh.sh mds1 "sudo docker exec -e DFSHA_CONTRASENA='<contraseña>' dfsha-metadatos-1 python -m dfsha.metadata crear-admin --usuario admin"
```

Para actualizar después de un cambio, `subir_artefactos.sh` y `redesplegar.sh`. Para borrar todo, `destruir.sh`. Detalle en [deploy/aws/README.md](deploy/aws/README.md).

## 5. Uso de la CLI

En el entorno local la CLI corre en el contenedor `cliente`, y en AWS en las máquinas cliente (`sudo docker compose -f /opt/dfsha/compose.yml exec cliente ...`).

```
dfsha login admin
dfsha admin crear-usuario ana --cuota 10000000000
dfsha login ana
dfsha mkdir videos
dfsha put viaje.mp4 videos/viaje.mp4
dfsha ls videos
dfsha get videos/viaje.mp4 copia.mp4
dfsha mv videos/viaje.mp4 videos/paseo.mp4
dfsha rm videos/paseo.mp4
dfsha admin nodos
```

Las operaciones de RF3 (open, read, write, append, lock, fsync y close) se usan desde la biblioteca, `dfsha.client.cliente.Cliente`. Ejemplos en [deploy/compose/escenarios.py](deploy/compose/escenarios.py) y [experimentos/carga.py](experimentos/carga.py).

## 6. Experimentos

```
python experimentos/ejecutar.py     # sobre el despliegue de AWS, inyecta las fallas por SSH
python experimentos/analizar.py     # escribe experimentos/resultados/resumen.md
```

La correspondencia con las pruebas mínimas del enunciado y los resultados están en [experimentos/resultados/resumen.md](experimentos/resultados/resumen.md).
