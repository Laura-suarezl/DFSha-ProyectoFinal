# Seguridad y modelo de amenazas

Una sola tabla STRIDE con las amenazas de los dos diseños de la Fase I y las mitigaciones de D8, tal como quedaron implementadas (D12). La columna Evidencia remite a las pruebas que lo comprueban.

## Mecanismos

- **Autenticación.** Usuario y contraseña con hash Argon2id guardado en etcd. El líder entrega un JWT firmado con Ed25519, válido una hora, y un token de renovación que cambia en cada uso (`dfsha/metadata/autenticacion.py`).
- **Autorización.** Cada usuario solo accede a `/usuario`, comprobado en el servidor con la ruta normalizada. AdminService exige el rol administrador. Los nodos de datos exigen un token de acceso por bloque, firmado con HMAC, válido 10 minutos y atado al bloque y a la operación (`dfsha/common/tokens.py`).
- **Tránsito.** TLS 1.3 en todos los canales. TLS mutuo entre servidores, incluidos los miembros de etcd, con certificados de una CA interna emitidos por nombre ([docs/tls.md](tls.md)).
- **Reposo.** El cliente cifra cada bloque con AES-256-GCM con la clave del archivo, que se guarda envuelta con la clave maestra de AWS KMS. Los discos EBS están cifrados.
- **Red.** Subredes privadas sin salida a internet y grupos de seguridad que solo dejan a los clientes los puertos 50051 y 50052.
- **Disponibilidad.** Límite de peticiones simultáneas por servidor con respuesta OVERLOADED.
- **Auditoría.** Inicios de sesión, accesos denegados, borrados y acciones de administración, con fecha, IP, usuario y resultado (`dfsha/metadata/auditoria.py`).

## Tabla STRIDE

| Id | Categoría | Componente | Amenaza | Mitigación implementada | Evidencia |
|---|---|---|---|---|---|
| S1 | Suplantación | Nodos de metadatos | Un usuario se hace pasar por otro para leer sus archivos | JWT firmado con Ed25519 y verificado en cada llamada. La identidad sale del token, nunca de la petición | E9 (sin JWT y otro usuario) |
| S2 | Suplantación | Nodos de datos y NodeService | Una máquina se registra como nodo de datos o envía latidos falsos | Puertos internos con TLS mutuo. NodeService solo acepta certificados de nodos de datos y el nombre del certificado debe coincidir con el del nodo | tests/test_tls.py, tests/test_nodos.py |
| S3 | Suplantación | Inicio de sesión | Adivinar contraseñas por fuerza bruta | Argon2id hace costoso cada intento, el mensaje no revela si el usuario existe y el límite de peticiones acota la tasa. **No hay bloqueo por cuenta tras varios intentos fallidos**, que el diseño de la Fase I preveía. Queda como riesgo residual | tests/test_metadatos.py |
| T1 | Alteración | Nodos de datos | Se modifica o corrompe un bloque en disco | Huella SHA-256 del bloque cifrado verificada al guardar, al leer y en la revisión diaria. AES-GCM detecta cualquier cambio al descifrar. La copia dañada se descarta, se informa y se repone | kill -9 durante una lectura, tests/test_nodos.py |
| T2 | Alteración | Red | Se alteran mensajes en tránsito | TLS 1.3 con integridad en todos los canales | E9 |
| T3 | Alteración | Cliente | Un cliente modificado se salta validaciones | Todas las validaciones se repiten en los servidores: ruta, dueño, lock, versión base, copias y cuota | tests/test_archivos.py |
| R1 | Repudio | Espacio de nombres | Un usuario niega haber borrado archivos | Auditoría en etcd con fecha, IP, usuario, operación y resultado, replicada en 3 miembros. Dura 7 días y la puede borrar quien administre etcd: no es un registro inmutable | tests/test_metadatos.py |
| I1 | Fuga de información | Red | Alguien dentro de la VPC captura el tráfico | TLS 1.3 en todos los canales, TLS mutuo entre servidores | E9: la captura en los 4 nodos no contiene el texto en claro |
| I2 | Fuga de información | Discos | Se roba un disco o una instantánea EBS | Bloques cifrados por el cliente con una clave por archivo envuelta con KMS, y EBS cifrado | E9: ningún bloque en disco contiene el texto en claro |
| I3 | Fuga de información | Nodos de datos | Pedro pide un bloque de Ana conociendo su id | Token HMAC por bloque y operación. Sin token o con un token alterado, el nodo responde TOKEN_INVALID | E9 |
| I4 | Fuga de información | etcd | Se copia la base de etcd o un respaldo | Las claves del clúster y de archivo se guardan envueltas con KMS. Los tokens de renovación se guardan solo como huella | tests/test_metadatos.py |
| D1 | Denegación de servicio | Todos los servidores | Saturación de peticiones | Límite de peticiones simultáneas con OVERLOADED y espera sugerida. La re-replicación también está acotada para no agotar la memoria | E11, estabilidad de la serie |
| D2 | Denegación de servicio | Plano de control | Cae el líder o un miembro de etcd | 3 nodos en 3 zonas, elección automática y mayoría de etcd | E6 |
| E1 | Elevación de privilegios | Rutas | Escapar del espacio propio con `..` | Normalización de la ruta en el servidor antes de resolverla. Una ruta fuera de `/usuario` responde FORBIDDEN | E9 (otro usuario), tests/test_archivos.py |
| E2 | Elevación de privilegios | AdminService | Un usuario normal usa operaciones de administración | AdminService exige el rol administrador del JWT | tests/test_metadatos.py |

**Fuera de alcance (declarado).** Un administrador malicioso que controle a la vez un nodo de metadatos y uno de datos, nodos que mientan activamente (fallas bizantinas) y ataques de denegación de servicio a nivel de red, que en AWS mitigan los grupos de seguridad y el propio proveedor.
