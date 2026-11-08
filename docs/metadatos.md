# Nodos de metadatos

Los tres nodos de metadatos forman el plano de control (D1). Cada uno corre junto a un miembro de etcd, compite en la elección de líder y atiende MetadataService y AdminService en el puerto 50051 con TLS. Solo el líder atiende las peticiones y escribe en etcd. Los otros dos remiten al cliente al líder.

Esta parte cubre la elección del líder, los usuarios, el inicio de sesión, la sesión con su lease, las claves del clúster, la administración de usuarios y la auditoría. Las operaciones de archivos (List, Open, Lock, Commit y las demás) responden UNIMPLEMENTED hasta que se implementen.

## Módulos

| Archivo | Qué hace |
|---|---|
| `dfsha/metadata/etcd.py` | Cliente de etcd (KV, transacciones, leases, Lock, Election y Watch) |
| `dfsha/metadata/lider.py` | Elección del líder con su lease y renuncia ordenada |
| `dfsha/metadata/usuarios.py` | Usuarios con Argon2id, rol y cuota |
| `dfsha/metadata/autenticacion.py` | JWT firmados con Ed25519 y tokens de renovación |
| `dfsha/metadata/claves.py` | Claves del clúster envueltas con la clave maestra (KMS o archivo local) |
| `dfsha/metadata/auditoria.py` | Registro de auditoría en etcd |
| `dfsha/metadata/servicio.py` | MetadataService y AdminService, con las reglas comunes de cada llamada |
| `dfsha/metadata/__main__.py` | Arranque del nodo y preparación del clúster |

## Elección del líder

Cada nodo pide a etcd un lease de 3 s (`tiempos.lider_ttl_s`), lo renueva cada segundo y compite en la elección `/dfsha/election/metadata` con su nombre DNS como valor. El que gana es el líder y guarda la clave y la revisión de su candidatura, que usa en la comparación de liderazgo de cada escritura (docs/etcd-esquema.md).

- **Caída del líder.** Su lease vence a los 3 s y otro candidato asume solo.
- **Parada ordenada.** Con SIGTERM (`docker stop`) el líder renuncia y revoca su lease, así otro asume en milisegundos.
- **Líder aislado.** Si un nodo pierde su lease, deja de considerarse líder y vuelve a competir. Aunque creyera seguir siéndolo, la comparación de liderazgo rechazaría sus escrituras.

## Usuarios

Cada usuario es una clave `/dfsha/users/<usuario>` con el hash Argon2id de su contraseña, si es administrador, su cuota en bytes y los bytes que usa. Una cuota de 0 significa sin límite.

- **Nombres.** Minúsculas, números, guion y guion bajo, de 3 a 32 caracteres, empezando por letra. Así el nombre sirve como raíz de su espacio (`/usuario`) sin riesgo de rutas como `..`.
- **Contraseñas.** Al menos 8 caracteres. Se guarda solo el hash Argon2id con los parámetros recomendados por argon2-cffi (RFC 9106). El cálculo corre en un hilo aparte para no detener al servidor.
- **Respuesta igual.** Un usuario inexistente y una contraseña equivocada dan el mismo error y cuestan lo mismo, así no se puede averiguar qué usuarios existen.

## Inicio de sesión

**Login.** El líder verifica la contraseña y entrega cuatro cosas.
1. Un JWT firmado con Ed25519, válido una hora (`tiempos.jwt_ttl_s`), con el usuario y su rol.
2. Un token de renovación aleatorio, válido un día (`tiempos.token_renovacion_ttl_s`).
3. Una sesión, que es un lease de etcd de 30 s (`tiempos.sesion_ttl_s`) con la clave `/dfsha/sessions/<id>`. El id de la sesión es el id del lease en hexadecimal.
4. El intervalo de renovación, 10 s (`tiempos.renovacion_s`).

**Renew.** Cada 10 s el cliente renueva la sesión con su JWT. El líder comprueba que la sesión sea de ese usuario y renueva el lease. Con él se mantienen vivos los locks y los arriendos de lectura de la sesión (D4). Si la sesión ya venció responde LEASE_EXPIRED, porque sus locks se perdieron y la aplicación debe saberlo.

**Refresh.** Con el token de renovación el líder entrega un JWT nuevo, una sesión nueva y un token de renovación nuevo, y el anterior deja de servir. El cambio es una sola transacción de etcd, así que si dos peticiones usan el mismo token solo una lo consigue.

En etcd el token de renovación se guarda solo como su huella SHA-256 (`/dfsha/refresh/<huella>`), así que leer etcd o un respaldo suyo no permite usarlo.

## Claves del clúster

Los tres nodos firman y verifican con las mismas claves, la de firma de los JWT (Ed25519) y la HMAC de los tokens de acceso por bloque. El primer nodo que arranca las genera y las guarda en `/dfsha/secrets/<nombre>` envueltas con la clave maestra. Si dos nodos arrancan a la vez, una transacción hace que gane uno y el otro use la clave ya guardada.

- **En AWS.** La clave maestra es una clave propia de KMS (`kms.clave`, por ejemplo `alias/dfsha-kek`). El nodo usa las credenciales del rol de la instancia (LabInstanceProfile) y envuelve con el contexto de cifrado `uso=dfsha-secreto-del-cluster`.
- **En desarrollo local.** `kms.clave = "archivo:<ruta>"` usa una clave de 32 bytes guardada en un archivo, que se crea con `python -m dfsha.metadata crear-clave-local <ruta>`. Envuelve con AES-256-GCM.

## Administración

AdminService exige un JWT con rol administrador. Por ahora ofrece CreateUser y SetQuota, y ListNodes y Decommission se agregan con el monitor de nodos.

El primer administrador no puede crearse con AdminService, porque haría falta otro administrador. Se crea directamente en etcd con `crear-admin`.

## Auditoría

Se registran los inicios de sesión (correctos y fallidos), los Refresh, los tokens inválidos, los accesos denegados y las acciones de administración. Cada registro es una clave `/dfsha/audit/<fecha>-<id aleatorio>` con la fecha, la IP del cliente, el usuario, la operación y el resultado, atada a un lease de 7 días (`tiempos.auditoria_ttl_s`) que se reutiliza durante una hora. Registrar nunca hace fallar la operación auditada. Si etcd no responde, el evento va al log del nodo.

## Reglas comunes de cada llamada

Todas las operaciones pasan por las mismas comprobaciones, en este orden.
1. **Límite de peticiones simultáneas.** Si el nodo ya atiende `limites.peticiones_simultaneas` peticiones, responde OVERLOADED con `retry_after_ms` (D8).
2. **Liderazgo.** Un seguidor responde NOT_LEADER con el nombre del líder. Si en ese momento no hay líder (elección en curso o sin mayoría de etcd), responde NO_QUORUM con `retry_after_ms`.
3. **JWT.** Salvo Login y Refresh, la llamada debe traer `authorization: Bearer <JWT>` válido. Si falta o es inválido responde TOKEN_INVALID. AdminService exige además el rol administrador, y si no lo tiene responde FORBIDDEN.
4. **Escritura con comparación de liderazgo.** Si la comparación falla, el nodo dejó de ser líder sin enterarse y responde NOT_LEADER sin haber escrito nada.
5. **etcd sin mayoría.** La llamada a etcd falla en 5 s y se responde NO_QUORUM.

## Arranque

```
python -m dfsha.metadata                                    # servir, la opción por defecto
python -m dfsha.metadata crear-admin --usuario admin        # contraseña en DFSHA_CONTRASENA o por la terminal
python -m dfsha.metadata crear-clave-local deploy/certs/out/kek-dev.bin
```

El nodo toma su nombre de `nodo.nombre`, el destino de etcd de `etcd.destino` y su certificado de la sección `[tls]`. Con `etcd.tls` habilitado usa el certificado del nodo para hablar con etcd con TLS mutuo. Los parámetros están en [docs/configuracion.md](configuracion.md) y el entorno local con los tres nodos en [deploy/compose/README.md](../deploy/compose/README.md).

## Comportamiento comprobado

Pruebas de `tests/test_metadatos.py` con 3 nodos de metadatos en un mismo proceso sobre un etcd de 3 miembros, repetidas 5 veces con el mismo resultado, y el entorno local de Docker Compose.

| Situación | Comportamiento |
|---|---|
| Arranque de los 3 nodos | Hay un único líder y los seguidores responden NOT_LEADER con su nombre |
| Credenciales equivocadas | Mismo error para usuario inexistente y contraseña equivocada, y ambos quedan en la auditoría con la IP |
| Usuario sin rol administrador | CreateUser responde FORBIDDEN |
| Refresh con un token ya usado | TOKEN_INVALID |
| Renew de una sesión vencida | LEASE_EXPIRED |
| Parada ordenada del líder | Otro asume en menos de 1 s (15 ms en el entorno local con `docker stop`) |
| Caída abrupta del líder | Otro asume en unos 3,5 s (3,65 s en el entorno local con `docker kill`) |
| Cambio de líder | El JWT y la sesión de antes siguen sirviendo en el nuevo líder, y el líder anterior ya no puede escribir |
| Caen 2 de los 3 miembros de etcd | Las peticiones responden NO_QUORUM en 5,3 s |
| Cae la máquina del líder (su nodo de metadatos y su miembro de etcd) | Otro nodo asume y, al volver la máquina, se suma como seguidor |
| Límite de peticiones alcanzado | OVERLOADED con `retry_after_ms` |
