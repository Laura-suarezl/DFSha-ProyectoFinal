# Esquema de claves en etcd

etcd guarda el estado persistente del plano de control (D1). Este documento define qué clave guarda cada cosa, quién la escribe y cómo se combinan en transacciones. El estado blando (latidos, ubicación de cada bloque y carga de los nodos) no va en etcd, vive en la memoria de cada nodo de metadatos y se reconstruye con los avisos de los nodos de datos (D6).

## Convenciones

- Todas las claves empiezan con `/dfsha/`.
- Los valores son JSON en UTF-8, para poder leerlos con `etcdctl get` al depurar.
- Los identificadores de archivos, directorios y bloques son cadenas aleatorias de 16 caracteres hexadecimales. Los de bloque llevan el prefijo `b-`.
- Solo el líder del servicio de metadatos escribe en etcd. Toda transacción de escritura incluye la **comparación de liderazgo** (ver abajo), así un nodo que perdió el liderazgo sin enterarse no puede escribir.

## Claves

| Clave | Valor | Tipo | Ciclo de vida |
|---|---|---|---|
| `/dfsha/election/metadata` | Nombre del nodo líder (lo crea la API Election de etcd) | Elección | Atada al lease del líder, de 3 s |
| `/dfsha/config` | Parámetros del clúster: tamaño de bloque, R, W y tamaño de trozo | Persistente | Se crea al iniciar el clúster |
| `/dfsha/users/<usuario>` | Hash Argon2id, rol, cuota en bytes y bytes usados | Persistente | Lo crea el administrador |
| `/dfsha/secrets/<nombre>` | Claves de firma (JWT Ed25519 y HMAC de los tokens de bloque) envueltas con KMS | Persistente | Se crean una vez al iniciar el clúster |
| `/dfsha/ns/<id del padre>/<nombre>` | Entrada del árbol: id, tipo (directorio o archivo), dueño, fechas | Persistente | mkdir, put, rm, rmdir, mv |
| `/dfsha/inodes/<id>` | Índice inverso: id del padre, nombre, tipo y dueño | Persistente | Igual que la entrada del árbol |
| `/dfsha/files/<id del archivo>` | Puntero a la versión vigente: versión, tamaño, tamaño de bloque, R, hash del archivo y clave del archivo envuelta con KMS | Persistente | Cambia en cada commit |
| `/dfsha/manifests/<id del archivo>/<versión>` | Manifiesto de la versión: lista de bloques con índice, id, huella cifrada, huella en claro y tamaño | Persistente | Se crea en el commit y lo borra el recolector |
| `/dfsha/uc/<id del archivo>/<descriptor>/<id del bloque>` | Bloque en construcción: índice, cadena asignada, sesión y fecha | Persistente | Se crea en AllocateBlocks. El commit borra de una vez todo el prefijo de su descriptor, y el recolector lo borra pasada una hora si nunca se confirmó |
| `/dfsha/sessions/<id de sesión>` | Usuario y fecha de inicio | Atada al lease de la sesión (30 s) | Desaparece sola si la sesión deja de renovarse |
| `/dfsha/locks/<id del archivo>/<objetivo>/<lease>` | La crea la API Lock de etcd | Atada al lease de la sesión | Unlock o vencimiento de la sesión |
| `/dfsha/readleases/<id del archivo>/<versión>/<id de sesión>` | Vacío | Atada al lease de la sesión | Close o vencimiento de la sesión |
| `/dfsha/ops/<op_id>` | Resultado del commit ya aplicado | Atada a un lease propio de 1 h | Vence sola |
| `/dfsha/decommission/<nodo>` | Fecha de inicio del retiro | Persistente | Hasta que el nodo queda vacío |
| `/dfsha/audit/<fecha>-<id aleatorio>` | Fecha, IP, usuario, operación y resultado | Atada a un lease de 7 días | Vence sola |

El objetivo de un lock es `b<índice>` para un bloque, `fin` para anexar y `archivo` para reemplazar o borrar.

La raíz del espacio de cada usuario es la entrada `/dfsha/ns/root/<usuario>`.

## Sesión, locks y token de cerco (D4)

1. Al iniciar sesión, el líder crea un lease de etcd de 30 s y la clave `/dfsha/sessions/<id>` atada a él. El id de sesión es el id del lease. Renew(sesión), cada 10 s, es el keep-alive de ese lease.
2. Para tomar un lock, el líder llama a la API Lock de etcd con el nombre `/dfsha/locks/<archivo>/<objetivo>` y el lease de la sesión del cliente. etcd ordena a los que esperan por la revisión con que llegaron (cola en orden de llegada) y devuelve la clave del lock.
3. La revisión de creación de esa clave es el **token de cerco**. Solo crece, porque las revisiones de etcd solo crecen.
4. Si la espera supera los 10 s, el líder cancela la llamada y responde ABORTED con el motivo LOCKED. Se comprobó con etcd v3.5.35 que, al cancelar, la entrada del que esperaba desaparece sola de la cola.
5. Si la sesión deja de renovarse, el lease vence y etcd borra la sesión, sus locks y sus arriendos de lectura a la vez.

**Jerarquía de locks.** El lock `archivo` excluye a los locks de bloque y al de `fin` del mismo archivo. Como solo el líder concede locks, la jerarquía la aplica él. Antes de conceder un lock de bloque o de `fin` comprueba que no haya un lock `archivo` concedido ni en espera, y al conceder un lock `archivo` espera, observando el prefijo con Watch, a que no quede ningún lock de bloque ni de `fin`.

## Comparación de liderazgo

Cada nodo de metadatos compite en la elección `/dfsha/election/metadata` con su propio lease de 3 s, renovado cada segundo. Al ganar, guarda la clave y la revisión de su candidatura. Toda transacción de escritura del líder compara que la revisión de creación de su clave de líder sea igual a la que guardó al ganar.

Si perdió el liderazgo (por ejemplo, porque se quedó aislado y su lease venció), su clave ya no existe y la transacción falla sin escribir nada.

## Transacción del commit (D3)

El cliente envía el commit con el op_id, el descriptor, los bloques nuevos y la base de cada bloque modificado. El líder sigue estos pasos.

1. Busca `/dfsha/ops/<op_id>`. Si existe, devuelve ese resultado sin repetir nada.
2. Verifica en su memoria que cada bloque nuevo tenga al menos 2 avisos BlockReceived con la huella esperada.
3. Lee `/dfsha/files/<archivo>` y el manifiesto vigente, y comprueba que la base de cada bloque modificado sigue siendo la vigente.
4. Arma el manifiesto nuevo y ejecuta una sola transacción, con estas dos partes.
   - **Comparaciones:** la de liderazgo, que las revisiones de modificación de `/dfsha/files/<archivo>` y de `/dfsha/users/<usuario>` sean las que leyó, y que cada lock usado siga existiendo con su token.
   - **Si todo se cumple:** escribe `/dfsha/manifests/<archivo>/<versión nueva>`, actualiza `/dfsha/files/<archivo>`, actualiza el uso de cuota del usuario, borra con una sola operación el prefijo `/dfsha/uc/<archivo>/<descriptor>/` y escribe `/dfsha/ops/<op_id>` con el resultado.
5. Si la transacción falla solo porque `/dfsha/files/<archivo>` o el uso de cuota cambiaron (otro escritor confirmó bloques distintos u otro archivo), vuelve al paso 3. Si falla porque un lock ya no existe, responde ABORTED con el motivo LEASE_EXPIRED. Si la base de un bloque propio cambió, responde ABORTED con VERSION_CONFLICT.

El momento en que esa transacción se confirma es el único punto de commit.

## Límites que impone etcd (declarados)

| Límite de etcd (valor por defecto) | Consecuencia en DFSha |
|---|---|
| 128 operaciones por transacción (`--max-txn-ops`) | Un commit tiene 8 operaciones fijas (3 comparaciones y 5 escrituras) más una comparación por lock. Un mismo commit puede modificar hasta unos 120 bloques con lock propio (cerca de 1,9 GB). Para cambios mayores, el cliente confirma por partes con fsync(), y un reemplazo completo usa un solo lock `archivo` |
| 1,5 MiB por petición (`--max-request-bytes`) | Cada bloque ocupa unos 200 bytes en el manifiesto, así que un manifiesto admite unos 7800 bloques (cerca de 120 GB por archivo) |
| 2 GB de base de datos (`--quota-backend-bytes`) | Suficiente para los metadatos del proyecto. Se activa la compactación periódica del historial (`--auto-compaction-mode=periodic`, `--auto-compaction-retention=1h`) |

## Contrato con etcd en el código

- Los .proto oficiales de etcd están en `proto/etcd/`, copiados de la versión v3.5.35 sin las anotaciones de gogoproto ni de google.api, que no cambian el formato de los mensajes. Los genera `scripts/vendor_etcd_protos.py`.
- El código Python se genera con `scripts/generar_codigo.py` en `dfsha/_gen/` (no se versiona).
- DFSha usa estos servicios de etcd, KV (lecturas, escrituras y transacciones), Lease (sesiones y leases propios), Watch (esperas sobre prefijos), Lock (cola de locks) y Election (líder de metadatos).
