# API de DFSha

El contrato está en [proto/dfsha.proto](../proto/dfsha.proto) (gRPC con Protocol Buffers 3, paquete `dfsha.v1`). El código Python se genera con `python scripts/generar_codigo.py`.

## Servicios y puertos

| Servicio | Dónde corre | Puerto | Quién lo llama |
|---|---|---|---|
| MetadataService | Los 3 nodos de metadatos (solo el líder atiende) | 50051 (TLS) | Cliente |
| AdminService | Los 3 nodos de metadatos (solo el líder atiende) | 50051 (TLS) | Administrador |
| NodeService | Los 3 nodos de metadatos | 50053 (TLS mutuo) | Nodos de datos |
| DataService | Cada nodo de datos | 50052 (TLS) | Cliente |
| DataService | Cada nodo de datos | 50054 (TLS mutuo) | Otros nodos de datos (cadena y copias de reposición) |

Todos los canales usan TLS 1.3. Entre servidores el TLS es mutuo, así que cada nodo presenta un certificado de la CA interna emitido para su nombre DNS. Como gRPC para Python no permite pedir el certificado del cliente de forma opcional, el tráfico entre servidores va por puertos internos propios (50053 y 50054), que los grupos de seguridad cierran a los clientes. Los nodos se nombran siempre por nombre (`mdsN.dfsha.internal`, `dnN.dfsha.internal`), nunca por IP. La CA y los certificados se describen en [docs/tls.md](tls.md). La relación entre nodos de datos y de metadatos está en [docs/nodos.md](nodos.md).

## Autenticación en cada llamada

- **MetadataService y AdminService.** Cada llamada, salvo Login y Refresh, lleva la cabecera `authorization: Bearer <JWT>`. El JWT dura una hora y lo firma el líder con Ed25519. AdminService exige además el rol administrador.
- **DataService.** Cada escritura y lectura lleva en el mensaje un token de acceso por bloque, firmado con HMAC y válido 10 minutos, que el líder entrega en Open, GetBlockLocations o AllocateBlocks. El nodo de datos lo verifica sin consultar al líder.
- **NodeService y DataService en el puerto interno.** El otro nodo se identifica con su certificado de TLS mutuo. NodeService solo acepta certificados de nodos de datos (`dnN`).

## MetadataService

| Operación | Qué hace | Errores propios |
|---|---|---|
| Login | Valida usuario y contraseña (Argon2id) y entrega el JWT, el token de renovación y una sesión de 30 s | TOKEN_INVALID |
| Refresh | Entrega un JWT nuevo, una sesión nueva y un token de renovación nuevo. El token usado deja de servir | TOKEN_INVALID |
| Renew | Renueva la sesión y con ella todos sus locks y arriendos de lectura. Se llama cada 10 s | TOKEN_INVALID, LEASE_EXPIRED |
| List, Stat | Listan un directorio o describen una entrada | NOT_FOUND, FORBIDDEN |
| Mkdir, Rmdir | Crean o borran un directorio | EXISTS, DIR_NOT_EMPTY, NOT_FOUND, FORBIDDEN |
| Remove | Borra un archivo si nadie tiene locks sobre él. Los lectores con arriendo siguen leyendo la versión que abrieron y el recolector borra los bloques después | NOT_FOUND, LOCKED, FORBIDDEN |
| Rename | Mueve o renombra en una sola operación atómica | NOT_FOUND, EXISTS, FORBIDDEN |
| Open | Abre un archivo y devuelve el descriptor, la versión vista, la clave del archivo, el arriendo de lectura (dura lo mismo que la sesión, 30 s, y se renueva con Renew) y la ubicación de los bloques. En modo escritura incluye el lock del archivo entero | NOT_FOUND, LOCKED, FORBIDDEN |
| GetBlockLocations | Refresca las réplicas y los tokens de un rango de bloques | NOT_FOUND |
| AllocateBlocks | Asigna ids nuevos y una cadena de 3 nodos de datos a hasta 8 bloques | QUOTA_EXCEEDED, NO_SPACE |
| Lock, Unlock | Toman o liberan locks de bloques, del final del archivo o del archivo entero, con espera en cola de hasta 10 s | LOCKED |
| Commit | Publica una versión nueva si cada bloque tiene al menos 2 copias verificadas y los locks siguen vigentes | VERSION_CONFLICT, LEASE_EXPIRED, INSUFFICIENT_REPLICAS |
| Close | Libera los locks y el arriendo de lectura del descriptor | Ninguno |
| ReportBadBlock | Avisa que una réplica no respondió o tenía la huella equivocada | Ninguno |

Cualquier operación que llegue a un seguidor responde NOT_LEADER con el nombre del líder. Si no hay líder en ese momento (elección en curso) o no hay mayoría de nodos de metadatos, responde NO_QUORUM. El funcionamiento interno está en [docs/metadatos.md](metadatos.md).

## AdminService

| Operación | Qué hace |
|---|---|
| CreateUser | Crea un usuario con su contraseña (al menos 8 caracteres), su rol y su cuota (0 es sin límite). Un nombre o una contraseña inválidos responden INVALID_ARGUMENT, y un usuario existente EXISTS |
| SetQuota | Cambia la cuota de un usuario. Si no existe responde NOT_FOUND |
| ListNodes | Muestra el estado de cada nodo de datos (vivo, sospechoso, muerto o en retiro) y quién es el líder |
| Decommission | Inicia el retiro planificado de un nodo de datos, que copia sus bloques antes de apagarlo |

## DataService

**WriteBlock (streaming del cliente).** El bloque cifrado viaja en trozos `ChunkPayload`, de 1 MB por defecto.
1. El primer trozo lleva el id del bloque, la huella SHA-256 del bloque completo, los nodos restantes de la cadena y el token de acceso.
2. Los trozos siguientes solo llevan el número de secuencia y los datos, y el último marca `is_last_chunk`.
3. Cada nodo escribe lo que recibe en un archivo temporal y reenvía cada trozo al siguiente nodo de la cadena mientras lo recibe. La cadena que entrega AllocateBlocks trae el primer nodo con su puerto de clientes (50052) y los siguientes con el puerto interno (50054), así que el reenvío entre nodos va siempre por TLS mutuo.
4. Al terminar, cada nodo verifica la huella de su copia, la renombra a definitiva y avisa con BlockReceived a los 3 nodos de metadatos.
5. La respuesta `WriteResult` vuelve por la cadena e indica qué nodos guardaron y verificaron su copia. El bloque cuenta como escrito con al menos 2. Cada nodo espera la respuesta del siguiente solo un momento después de terminar su copia (`tiempos.gracia_cadena_s`, multiplicado por los nodos que quedan detrás). Si no llega, responde sin él y le sigue enviando en segundo plano, así una réplica lenta no frena la escritura. Si el cliente recibe menos de 2 copias, pide otra cadena con AllocateBlocks, que evita al nodo que falló.

**ReadBlock (streaming del servidor).** Devuelve el bloque cifrado en trozos. El cliente verifica la huella cifrada, descifra y verifica la huella en claro.

**CheckBlockHealth.** Diagnóstico. Indica qué bloques tiene el nodo y si su huella coincide.

## NodeService

| Operación | Cuándo | Qué hace |
|---|---|---|
| Register | Al arrancar el nodo de datos | Lo da de alta y le entrega el intervalo de latido (3 s) y la clave para verificar los tokens de acceso |
| Heartbeat | Cada 3 s, a los 3 nodos de metadatos | Informa espacio y carga. La respuesta del líder trae las órdenes de copiar o borrar bloques, y los seguidores responden sin órdenes |
| BlockReceived, BlockDeleted | En el momento | Avisan que el nodo guardó o borró un bloque |
| BlockReport | Al arrancar y cada hora | Inventario completo. La respuesta trae las órdenes para corregir diferencias |

## Errores

Cada error usa un código de estado estándar de gRPC y, en los metadatos de cierre de la llamada, la clave `dfsha-error-bin` con un `ErrorDetail` serializado. Ese mensaje lleva el motivo propio de DFSha y los datos que ayudan a reaccionar.

| Código gRPC | Motivo | Cuándo | Datos en ErrorDetail |
|---|---|---|---|
| UNAUTHENTICATED | TOKEN_INVALID | JWT o token de acceso ausente, vencido o falso | Ninguno |
| PERMISSION_DENIED | FORBIDDEN | Ruta fuera del espacio del usuario | Ninguno |
| NOT_FOUND | NOT_FOUND | La ruta no existe | path |
| ALREADY_EXISTS | EXISTS | Crear algo que ya existe | path |
| FAILED_PRECONDITION | DIR_NOT_EMPTY | Borrar un directorio con contenido | path |
| FAILED_PRECONDITION | NOT_LEADER | La petición llegó a un seguidor | leader |
| ABORTED | VERSION_CONFLICT | La base de algún bloque modificado ya no es la vigente | block_index, current_block_id |
| ABORTED | LEASE_EXPIRED | Commit con un lock que ya venció, o Renew de una sesión vencida | Ninguno |
| ABORTED | LOCKED | Se agotó la espera del lock | lock_owner, lock_expires_at_unix_ms |
| UNAVAILABLE | NO_QUORUM | No hay mayoría de nodos de metadatos, o hay una elección de líder en curso | retry_after_ms |
| UNAVAILABLE | INSUFFICIENT_REPLICAS | Algún bloque tiene menos de 2 copias verificadas | retry_after_ms |
| RESOURCE_EXHAUSTED | QUOTA_EXCEEDED | Se supera la cuota del usuario | used_bytes, limit_bytes |
| RESOURCE_EXHAUSTED | NO_SPACE | No hay espacio en el clúster | Ninguno |
| RESOURCE_EXHAUSTED | OVERLOADED | El servidor alcanzó su límite de peticiones simultáneas | retry_after_ms |
| DATA_LOSS | CHECKSUM_MISMATCH | La huella de un bloque no coincide | block_id |
| INVALID_ARGUMENT | BAD_OFFSET | Escribir más allá del final del archivo (no se admiten huecos) | Ninguno |

Un argumento mal formado que no tiene motivo propio (por ejemplo, un nombre de usuario con mayúsculas) responde INVALID_ARGUMENT sin ErrorDetail.

LOCKED usa ABORTED y no DEADLINE_EXCEEDED, porque gRPC también genera DEADLINE_EXCEEDED cuando vence el plazo del propio cliente, y así el cliente no confunde los dos casos.

**Reintentos.** Solo se reintentan las operaciones idempotentes o con op_id, con espera exponencial de 0,2 s, 0,4 s y 0,8 s hasta un máximo de 5 s. Ante NOT_LEADER, el cliente repite la llamada en el líder indicado. Ante ABORTED, la aplicación decide si vuelve a empezar la operación completa.

## Ejemplos de mensajes

Ana abre su video para leerlo (formato de texto de Protocol Buffers).

```
Open  > path: "/ana/videos/viaje.mp4"  mode: OPEN_MODE_READ  session_id: "694da1b87d62da07"
Open  < fd: "fd-812"  file_id: "f-3a91c0de55b2e017"  version: 1  size: 1073741824
        block_size: 16777216  file_checksum: "9f2c..."  file_key: <32 bytes>  read_lease_s: 30
        blocks { index: 0  block_id: "b-7f31a0c4e2d95b16"  cipher_checksum: "a1..."  plain_checksum: "c4..."
                 size: 16777216  replicas: "dn1.dfsha.internal:50052"  replicas: "dn2.dfsha.internal:50052"
                 replicas: "dn3.dfsha.internal:50052"  access_token: <token> }
        blocks { ... 63 bloques más ... }
```

Una aplicación de Ana modifica 100 bytes del bloque 7.

```
Lock      > fd: "fd-815"  targets { block_index: 7 }  wait_s: 10
Lock      < grants { target { block_index: 7 }  fencing_token: 41  current_block_id: "b-5e10c2a9f0d34e71"
                     expires_at_unix_ms: 1794150030000 }

AllocateBlocks > fd: "fd-815"  indices: 7
AllocateBlocks < blocks { index: 7  block_id: "b-9c02e7b1a4f86d30"
                          chain: "dn4.dfsha.internal:50052"  chain: "dn1.dfsha.internal:50054"
                          chain: "dn2.dfsha.internal:50054"  access_token: <token> }

Commit    > fd: "fd-815"  op_id: "c-5531"  new_size: 1073741824
            blocks { index: 7  new_block_id: "b-9c02e7b1a4f86d30"  base_block_id: "b-5e10c2a9f0d34e71"
                     cipher_checksum: "44..."  plain_checksum: "d0..."  size: 16777216  fencing_token: 41 }
Commit    < version: 2  file_checksum: "71..."
```

Respuestas de error.

```
Commit  < ABORTED            dfsha-error-bin { reason: REASON_VERSION_CONFLICT  block_index: 7
                                               current_block_id: "b-c771d03e9a5f2b84" }
Lock    < ABORTED            dfsha-error-bin { reason: REASON_LOCKED  lock_owner: "694da1b87d62da07"
                                               lock_expires_at_unix_ms: 1794150048000 }
Mkdir   < FAILED_PRECONDITION dfsha-error-bin { reason: REASON_NOT_LEADER  leader: "mds2.dfsha.internal" }
List    < PERMISSION_DENIED  dfsha-error-bin { reason: REASON_FORBIDDEN }
```
