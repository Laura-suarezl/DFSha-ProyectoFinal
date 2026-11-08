# Registro de decisiones de la fusión (Fase II)

Equipo: Laura Carolina Suárez Lozano y Tomás Castaño Peña. Arquitectura asignada: opción 1 (cliente/servidor S2S en red privada).

Regla de la fusión: en cada punto se adopta la opción que **menos simplifica los requisitos**, aunque eso signifique quedarse con un solo diseño, y la fusión debe ser coherente. Después de cada decisión se comprueba si alguna opción de los dos puntos siguientes queda descartada por compatibilidad.


| # | Punto | Decisión | Origen | Estado |
|---|---|---|---|---|
| 1 | Plano de control | 3 nodos de metadatos con líder por consenso (Raft vía etcd) | L | Decidido |
| 2 | Semántica de write() | Crear, reemplazar, anexar y modificar por bloque, con copy-on-write | L | Decidido |
| 3 | Publicación y versionado | Commit único por archivo verificado por avisos de los nodos de datos, manifiesto en una clave de etcd, op_id y retención por arriendos | L (adaptado a etcd) | Decidido |
| 4 | Concurrencia y lock() | Lock por bloque con cola FIFO, token de cerco y renovación por sesión, con primitivas de etcd | L (adaptado a etcd) | Decidido |
| 5 | Replicación y escritura en cadena | R = 3 en cadena, W = 2, verificación en cada nodo, cadena nueva ante fallas, tabla de ubicación con zonas, 4 nodos de datos, trozo configurable | L + T  | Decidido |
| 6 | Detección de fallos y recuperación | Latidos a los 3 nodos de metadatos (3 s, 10/30 s), órdenes en la respuesta, re-replicación, inventario, scrubbing, GC, Decommission. CheckBlockHealth como diagnóstico | L + T (diagnóstico) | Decidido |
| 7 | Descubrimiento y transparencia | Nombre lógico en zona privada de Route 53, NOT_LEADER con el nombre del líder, nodos devueltos por nombre | L | Decidido |
| 8 | Seguridad | Auth en los nodos de metadatos (Argon2id, JWT Ed25519), token por bloque HMAC, TLS 1.3 y mTLS por nombre, cifrado por el cliente y EBS, límite de concurrencia, auditoría | L + T (DoS, auditoría) | Decidido |
| 9 | Contrato de la API y errores | 4 servicios gRPC con una operación por acción y CheckBlockHealth, ChunkPayload de T con token, puertos 50051/50052, errores de L con LOCKED como ABORTED | L + T (mensaje, puertos, código) | Decidido |
| 10 | Despliegue en AWS | 3 zonas y subred pública, 9 EC2 (3 metadatos, 4 datos, 2 clientes), tamaños t3, EBS gp3 cifrado, NAT Gateway temporal, Docker Compose por máquina, script con AWS CLI | L + T (tamaños) | Decidido |
| 11 | Pruebas y métricas | Unión de E1 a E11 (L) y las pruebas de T, más E0 de acceso RF3, trozo 64 KB frente a 1 MB y desconexión por firewall | L + T + nueva | Decidido |
| 12 | Documentación | C4 de contexto (T), un diagrama de componentes en notación C4, despliegue, casos de uso y secuencias de L actualizados, STRIDE con amenazas de ambos, fuentes de diagramas en el repositorio | L + T (C4, STRIDE) | Decidido |

## D1. Plano de control

**Decisión.** Tres nodos de metadatos. Cada uno corre un miembro de etcd y el servicio de metadatos de DFSha. El servicio que gana la elección de etcd es el líder y atiende las escrituras, y los otros responden NOT_LEADER. El estado persistente (árbol, versiones, locks) vive en etcd y el estado blando (ubicación de bloques, latidos) en la memoria de cada nodo.

**Por qué.** Es la única opción que no simplifica RNF2. Un Master único (T) es un punto único de falla y es una simplificación que habría que declarar y hacer aprobar. Las opciones de respaldo con conmutación manual o con disco compartido siguen teniendo un punto único de falla. etcd es una implementación probada de Raft que se usa como componente.

**Ajustes de contrato que impone etcd**:
- Máximo 128 operaciones por transacción (--max-txn-ops). La lista de bloques de una versión va en una sola clave (manifiesto), no una clave por bloque.
- Máximo 1,5 MiB por petición (--max-request-bytes). Se declara como límite del tamaño del manifiesto.
- Cuota de 2 GB de la base de datos por defecto. Se declara.
- La revisión global de etcd solo crece y sirve como token de cerco para los locks.
- El cliente Python más usado de etcd (python-etcd3) no tiene mantenimiento activo.

**Despliegue.** 3 nodos de metadatos, 4 nodos de datos y 1 cliente suman 8 de las 9 EC2 permitidas.

**Compatibilidad con los puntos 2 y 3.** No descarta ninguna opción. La única condición nueva es el manifiesto en una sola clave.

## D2. Semántica de write()

**Decisión.** write() admite creación, reemplazo, anexado y modificación parcial por bloque. Modificar o anexar es leer el bloque, cambiarlo en un búfer local y subirlo como bloque nuevo con id nuevo (copy-on-write). Un bloque guardado nunca cambia. Lo escrito se hace visible al confirmar con close() o fsync() (close-to-open).

**Por qué.** Es la única opción que cubre las cuatro operaciones que nombra RF3 sin pedir aprobación. POSIX completo excede lo pedido y la modificación solo de bloques alineados es un recorte encubierto.

**Costo aceptado.** Un cambio pequeño reescribe un bloque de 16 MB, y los bloques viejos se conservan hasta que el recolector los borra.

**Compatibilidad con los puntos 3 y 4.**
- Punto 3: queda descartado el commit por bloque (T), porque un write() que cambia varios bloques los publicaría por separado y un lector podría ver una mezcla de versiones (rompe la atomicidad y el escenario 3). También quedan descartadas las marcas de generación (T), innecesarias porque una réplica vieja no puede existir.
- Punto 4: no descarta ninguna opción.

## D3. Publicación y versionado

**Decisión.** Una escritura existe para los demás solo cuando el líder confirma una nueva versión del archivo con un único commit.
- **Verificación por el servidor.** El líder publica solo si cada bloque nuevo tiene al menos 2 avisos BlockReceived de los propios nodos de datos con la huella esperada. No confía en lo que diga el cliente.
- **Manifiesto en etcd.** La lista de bloques (ids y huellas) de cada versión va en una sola clave. El commit es una transacción de etcd que cambia el manifiesto solo si no cambió desde que el líder lo leyó y si el lock sigue vigente.
- **Reintento interno por base de bloque.** Si otro escritor cambió bloques distintos del mismo archivo entre la lectura y la transacción, el líder relee el manifiesto, verifica que la base de cada bloque modificado sigue vigente y reintenta. Solo rechaza con VERSION_CONFLICT si la base de algún bloque propio cambió.
- **Idempotencia.** El op_id del commit se guarda en etcd con vencimiento. Un reintento tras la caída del líder devuelve el mismo resultado y no publica dos veces.
- **Versión anterior.** Se conserva mientras haya arriendos de lectura sobre ella, y después la borra el recolector (garantía G3).

**Por qué.** Cada pieza alternativa de T simplifica un requisito: confiar en el cliente debilita RNF6 y RNF3, no tener op_id permite aplicar un commit dos veces y liberar la versión vieja al instante rompe G3. Una clave por bloque en etcd choca con el límite de 128 operaciones por transacción.

**De T se conserva.** El esquema de archivo temporal y renombrado atómico, equivalente en ambos diseños (el nombre .tmp o .part se fija en el punto 9).

**Compatibilidad con los puntos 4 y 5.**
- Punto 4: el commit debe comprobar el lock dentro de la transacción de etcd. Ambas opciones lo permiten, así que no se descarta nada.
- Punto 5: queda descartada la variante en que los nodos de datos solo responden por la cadena y no avisan al plano de control. Cada nodo de datos envía BlockReceived a los tres nodos de metadatos.

## D4. Concurrencia y lock()

**Decisión.**
- **Granularidad.** Lock exclusivo por bloque para escribir. Anexar bloquea el "final" del archivo. Reemplazar y borrar bloquean el archivo entero. Quien necesita varios bloques los pide en orden ascendente de índice (sin interbloqueos). Los lectores no bloquean, porque leen su instantánea.
- **Espera.** Cola en orden de llegada hasta 10 s, implementada con la receta de mutex de etcd (los que esperan se ordenan por la revisión con que crearon su clave). Si se supera la espera, el error es LOCKED con el dueño y el vencimiento.
- **Token de cerco.** Es la revisión de creación de la clave del lock en etcd, que solo crece. El commit comprueba dentro de su transacción que esa clave sigue existiendo con esa revisión. Si el lease venció, la clave ya no existe y el commit falla con LEASE_EXPIRED.
- **Renovación por sesión.** Cada sesión de cliente es un lease de etcd de 30 s, y todos sus locks y arriendos cuelgan de él. Renew(sesión), cada 10 s, es el keep-alive de ese lease.

**Ajuste por etcd.** Los leases viven en el clúster de etcd y sobreviven a un cambio de líder del servicio de metadatos, así que la "gracia de 30 s al cambiar de líder" del diseño L ya no hace falta para los locks. Se mantiene la regla de no borrar bloques viejos durante 60 s tras un cambio de líder, porque el recolector es propio.

**Por qué.** El lock por archivo (T) simplifica RNF5, porque dos escritores en bloques distintos se esperarían. Sin token de cerco, un proceso congelado puede confirmar después de perder su lock (simplifica RNF3). El rechazo inmediato con reintento aleatorio (T) no recorta un requisito, pero permite la inanición y hace menos predecible la prueba de concurrencia.

**Compatibilidad con los puntos 5 y 6.** No descarta ninguna opción. Por los puntos 1 y 3, el latido dirigido a un único Master (T) no encaja en el punto 6.

## D5. Replicación y escritura en cadena

**Decisión.**
- **Cadena.** 3 réplicas por bloque (R = 3, mínimo configurable 2). El cliente envía el bloque una vez al primer nodo, que lo reenvía al segundo mientras lo recibe, y este al tercero. Las confirmaciones vuelven por la cadena. Un bloque cuenta como escrito con al menos 2 copias confirmadas (W = 2).
- **Verificación en cada nodo.** Cada nodo escribe en un archivo temporal, verifica la huella de su copia, la renombra a definitiva y solo entonces envía BlockReceived a los tres nodos de metadatos.
- **Falla en la cadena.** El cliente pide una cadena nueva sin el nodo caído y reenvía el bloque completo con el mismo id (idempotente). Con 2 copias buenas sigue adelante y el líder repone la tercera en segundo plano.
- **Ubicación.** Tabla de ubicación en el líder: solo nodos vivos y con espacio, tres nodos distintos en al menos dos zonas, rotación del primer nodo y desempate por el menos ocupado.
- **Nodos de datos.** 4, para poder reponer las tres copias si cae uno.
- **Trozo del stream.** Configurable, 1 MB por defecto. La comparación 64 KB frente a 1 MB es una de las condiciones de la prueba de rendimiento.

**Por qué.** Que solo verifique el último nodo (T) deja copias dañadas sin detectar (integridad, RNF3). Abortar la subida si cae un nodo de la cadena (T) no tolera la falla (RNF2). Con 3 nodos de datos (T) no se puede reponer la tercera copia tras una caída (RNF2). La distribución sin reglas (T) no garantiza zonas distintas. El tamaño de trozo no afecta requisitos, solo rendimiento, así que se fusiona como parámetro y se mide.

**De T se conserva.** El trozo de 64 KB, como condición experimental.

**Compatibilidad con los puntos 6 y 7.**
- Punto 6: el líder necesita saber qué nodos están vivos y su espacio, y poder ordenar copias. Como hay tres nodos de metadatos (D1) y los avisos van a los tres (D3), queda descartado el latido dirigido a un único Master (T).
- Punto 7: no descarta nada.
- Aviso para el punto 10: la regla de "al menos dos zonas" requiere subredes en varias zonas de disponibilidad.

## D6. Detección de fallos y recuperación

**Decisión.**
- **El nodo informa.** Cada nodo de datos envía un latido cada 3 s a los tres nodos de metadatos, con su espacio libre y su carga. Avisa en el momento con BlockReceived o BlockDeleted y envía su inventario completo (BlockReport) al arrancar y cada hora. Las órdenes del líder (copiar o borrar bloques) viajan en la respuesta al latido, así que el líder nunca abre conexiones hacia los nodos.
- **Estados.** Vivo con latido en los últimos 10 s. Sospechoso entre 10 y 30 s sin latido (no recibe bloques nuevos y pasa al final de las listas de lectura). Muerto pasados 30 s, y entonces se re-replica.
- **Recuperación.** Re-replicación que prioriza los bloques con una sola copia, con límite de copias simultáneas. Inventario para borrar lo que sobra. Revisión diaria de bloques (scrubbing) y tratamiento de réplicas corruptas. Recolector de basura. Retiro planificado (Decommission). Limpieza de temporales al reiniciar.
- **Cliente.** No espera la detección: si una réplica no responde en 3 s, lee la siguiente.
- **Nodos de metadatos.** Su caída la resuelven etcd y la elección de líder (D1).
- **Diagnóstico.** CheckBlockHealth (T) se conserva como operación de diagnóstico de los nodos de datos, no como mecanismo de detección.

**Por qué.** Que el líder pregunte a cada nodo escala peor y deja sin información a un líder nuevo. Sin umbrales definidos (T) no hay una garantía comprobable de cuándo se repara. T no especifica re-replicación, recolección ni corrupción.

**De T se conserva.** CheckBlockHealth como diagnóstico y la prueba de kill -9 durante una lectura (punto 11).

**Compatibilidad con los puntos 7 y 8.**
- Punto 7: los nodos de datos y los clientes deben llegar a los tres nodos de metadatos, así que queda descartado configurar una sola dirección del Master (T).
- Punto 8: no descarta nada.

## D7. Descubrimiento y transparencia

**Decisión.**
- **Plano de control.** El cliente y los nodos de datos solo conocen el nombre lógico dfsha.internal (puerto en configuración, nunca en el código). Una zona privada de Route 53 lo resuelve a los tres nodos de metadatos. Si responde un seguidor, devuelve NOT_LEADER con el nombre del líder, y el cliente lo guarda en caché. Los nodos de datos se registran resolviendo el mismo nombre.
- **Bloques.** El líder devuelve en Open o GetBlockLocations los nombres de los nodos (dnN.dfsha.internal) ordenados por cercanía. Si un nodo no responde o el bloque se movió, el cliente vuelve a preguntar.
- **Transparencias.** Acceso, ubicación, migración y replicación, fallas y concurrencia, como en el diseño L.

**Por qué.** Una sola dirección del Master (T) quedó descartada en D6. Una lista de IPs en configuración sigue siendo una dirección fija en el cliente (simplifica RNF7). Un balanceador funciona pero no conoce al líder, es otra pieza y no está verificado en el laboratorio. Preguntarle a etcd traslada el mismo problema y expone etcd. Cloud Map no está permitido. Devolver nombres en lugar de IPs (T) permite certificados TLS por nombre y sobrevive a cambios de IP.

**Requisitos del entorno.** Zona privada de Route 53 (permitida en el Learner Lab) y VPC con enableDnsSupport y enableDnsHostnames activados.

**Alternativa en reserva.** DNS interno de Docker Swarm, si Route 53 diera problemas. Depende del punto 10.

**Compatibilidad con los puntos 8 y 9.**
- Punto 8: no descarta opciones, pero los certificados TLS deben emitirse por nombre (dfsha.internal, mdsN.dfsha.internal, dnN.dfsha.internal).
- Punto 9: no descarta nada. La API debe incluir NOT_LEADER con el nombre del líder y la operación para refrescar ubicaciones.

## D8. Seguridad

**Decisión.**
- **Autenticación.** Vive en los nodos de metadatos. Usuarios y hash Argon2id guardados en etcd. El líder emite un JWT de una hora firmado con Ed25519 (firma asimétrica), más un token de renovación. Cada llamada lleva el JWT en sus cabeceras.
- **Autorización.** Cada usuario tiene su espacio /usuario y cada entrada guarda su dueño. El líder comprueba en cada operación que la ruta normalizada esté dentro del espacio del usuario. Hay un rol administrador sin acceso a contenidos y cuotas por usuario. Los nodos de datos exigen un token de acceso por bloque firmado con HMAC (ids, operación, usuario, vencimiento de 10 min).
- **Tránsito.** TLS 1.3 entre cliente y servidores y TLS mutuo entre servidores (también entre miembros de etcd), con una CA interna que emite certificados por nombre (D7).
- **Reposo.** El cliente cifra cada bloque con AES-256-GCM (clave por archivo custodiada por los metadatos y envuelta con una clave maestra, nonce nuevo por bloque, AAD con archivo e índice), más volúmenes EBS cifrados.
- **Denegación de servicio (de T).** Límite de peticiones simultáneas en cada servidor y rechazo controlado con RESOURCE_EXHAUSTED.
- **Auditoría (fusión).** Registro de inicios de sesión, accesos denegados y borrados (L) con fecha, IP, usuario y operación (T).

**Por qué.** Un servicio Auth externo (T) es un punto único de falla si no se replica. La firma asimétrica evita que quien verifica pueda fabricar tokens. El cifrado de disco solo no es una simplificación para el profesor, pero el cifrado por el cliente da una evidencia de almacenamiento protegido fácil de mostrar y es coherente con las dos huellas por bloque de D3 y D5. Declarar la denegación de servicio fuera de alcance (L) simplificaba más que la mitigación de T.

**De T se conserva.** Mitigación de denegación de servicio, campos de auditoría y el análisis STRIDE (punto 12).

**Verificado en el laboratorio (2026-11-06).** Se puede crear una clave KMS propia (customer managed, simétrica) y cifrar con ella, y existen las claves gestionadas aws/ebs y aws/s3. La clave maestra (KEK) va en una clave KMS propia. El 2026-11-07 se comprobó desde una EC2 con LabInstanceProfile (rol LabRole) que Encrypt, Decrypt y GenerateDataKey funcionan, así que el servicio de metadatos puede envolver y desenvolver las claves de archivo con KMS sin credenciales de consola.

**Compatibilidad con los puntos 9 y 10.**
- Punto 9: no descarta nada. El contrato debe llevar el JWT en las cabeceras, el token por bloque en WriteBlock y ReadBlock, y la clave del archivo en la respuesta de Open.
- Punto 10: no descarta nada. Requiere CA interna, EBS cifrado y un lugar para la clave maestra.

## D9. Contrato de la API y errores

**Decisión.**
- **Protocolo.** gRPC con TLS en todos los canales (ambos diseños).
- **Servicios.** MetadataService (sesión, directorios, Open, GetBlockLocations, AllocateBlocks, Lock, Unlock, Renew, Commit, Close, ReportBadBlock), AdminService (usuarios, cuotas, estado de nodos, Decommission), DataService (WriteBlock y ReadBlock por streaming, más CheckBlockHealth de diagnóstico) y NodeService (Register, Heartbeat, BlockReceived, BlockReport), atendido por los tres nodos de metadatos. Una operación por acción.
- **Mensaje de trozos.** ChunkPayload de T (block_id, sequence_number, data, is_last_chunk, block_checksum, downstream_nodes), con el token de acceso por bloque agregado (D8). Tamaño del trozo configurable (D5).
- **Puertos.** Plano de control y administración en 50051, nodos de datos en 50052, etcd en 2379 (clientes) y 2380 (pares).
- **Errores.** Tabla de L. LOCKED pasa de DEADLINE_EXCEEDED a ABORTED, junto con VERSION_CONFLICT y LEASE_EXPIRED, porque DEADLINE_EXCEEDED también lo genera gRPC cuando vence el plazo del propio cliente y no se podrían distinguir.
- **Reintentos.** Solo operaciones idempotentes o con op_id, con espera exponencial hasta 5 s.

**Por qué.** CommitBlock, AcquireLease por archivo y SendHeartbeat a un único Master (T) quedaron descartados por D3, D4 y D6. Una operación genérica para el espacio de nombres (T) no simplifica requisitos, pero hace genéricos los mensajes, los errores y la autorización. El ChunkPayload de T está mejor especificado que el de L. Los puertos 50051 y 50052 son la convención de gRPC.

**De T se conserva.** ChunkPayload, puertos, CheckBlockHealth y el uso de ABORTED para el lock ocupado.

**Compatibilidad con los puntos 10 y 11.** No descarta nada. Los grupos de seguridad se abren en 50051, 50052, 2379 y 2380. CheckBlockHealth queda disponible como evidencia en las pruebas.

## D10. Despliegue en AWS Academy

**Decisión.**
- **Red.** VPC propia con enableDnsSupport y enableDnsHostnames, tres subredes privadas en tres zonas de disponibilidad (un nodo de metadatos por zona) y una subred pública para el cliente que hace de bastión.
- **Máquinas (9 de 9).** 3 nodos de metadatos con etcd, 4 nodos de datos y 2 clientes en zonas distintas. El segundo cliente sirve para la prueba de concurrencia con dos clientes reales y para medir rendimiento agregado.
- **Tamaños (de T, ajustados).** t3.small para los nodos de metadatos, t3.micro para los nodos de datos y t3.small o t3.medium para los clientes. EBS gp3 de 20 GB cifrado en los nodos de datos.
- **Salida a Internet para instalar.** NAT Gateway temporal en la subred pública, creado para instalar y borrado después, porque se cobra por hora y por datos procesados. Opcional: una AMI base con Docker y las imágenes ya instaladas, para que las instancias nuevas arranquen sin volver a crear el NAT.
- **Ejecución.** Docker Compose en cada máquina, un archivo por rol, lanzados con un script de despliegue por SSH a través del bastión. Un docker-compose local con todo en un PC sirve solo para desarrollar.
- **Infraestructura.** Script con AWS CLI y pasos manuales documentados como respaldo. Las IP privadas se conservan al detener e iniciar las instancias, así que los registros de Route 53 siguen siendo válidos.
- **Grupos de seguridad.** 50051 solo desde clientes y nodos de datos, 2379 y 2380 solo entre nodos de metadatos, 50052 solo desde clientes y otros nodos de datos, SSH solo al bastión desde la IP del administrador.

**Por qué.** Una sola subred en una zona (T) simplifica RNF2 a nivel de zona y es incompatible con la regla de ubicación de D5. Juntar roles en una máquina ya no hace falta con 9 EC2. Docker Swarm agrega una red y un DNS propios encima de Route 53 y de los certificados por nombre (D7).

**De T se conserva.** Los tamaños de las instancias y del volumen EBS, y la idea del cliente como bastión.

**Verificado en el laboratorio (2026-11-06, us-east-1).** NAT Gateway público con IP elástica (disponible en unos 2 minutos), creación de VPC, tablas de rutas e IP elásticas, y creación de AMI (disponible en unos 90 segundos). Tipos permitidos de nano a large en cualquier familia (t2, t3, t4g, m5, c5, c6i, r5, m6g), xlarge o mayor denegado, solo On-Demand, solo us-east-1 y us-west-2, máximo 9 instancias en marcha y 32 vCPU. El despliegue usa 18 vCPU (todas las t3 de micro a medium tienen 2 vCPU). KMS en D8.

**Compatibilidad con los puntos 11 y 12.** No descarta nada. Habilita la prueba de concurrencia con dos clientes en máquinas distintas. El diagrama de despliegue debe actualizarse.

## D11. Pruebas y métricas

**Decisión.** Unión de los experimentos de ambos diseños, con una prueba nueva para el hueco que ninguno cubría.

| Prueba exigida (5.2) | Cómo se cubre | Origen |
|---|---|---|
| Carga y recuperación | E1 con 16 MB, 128 MB y 1 GB, reparto en varios nodos y hash idéntico | L + T |
| Acceso RF3 | E0 (nueva): open, read de un rango, write de 100 bytes en el bloque 7, append, lock y close, con el resultado esperado y visible solo tras close() | Nueva |
| Concurrencia | E7: 2, 4 y 8 escritores en el mismo bloque y en bloques distintos, desde los dos clientes | L |
| Replicación y fallos | E5 (caída de un nodo de datos con 3 y 4 nodos), E6 (caída del líder) y kill -9 durante una lectura | L + T |
| Consistencia | E4 (réplica con +200 ms), E8 (escritor muerto a mitad de un put) y réplica desconectada durante una escritura (escenario 2), con CheckBlockHealth como evidencia | L + T |
| Seguridad | E9: token válido, ausente y de otro usuario, captura de tráfico y bloque cifrado en el disco | L |
| Rendimiento | E2 (lectura paralela), E3 (R = 1 frente a R = 3), E10 (costo del cifrado), E11 (metadatos con 1, 5, 10, 20 y 50 clientes) y trozo de 64 KB frente a 1 MB | L + T |

- **Inyección de fallas.** kill -9 (T), docker stop y detener la instancia (L), tc netem para retrasar una réplica (L) y una regla de firewall para desconectarla sin apagarla (nueva).
- **Método.** Una repetición de calentamiento y 5 medidas por punto, con media, desviación estándar, p50 y p95 (L).
- **Evidencia.** Cada prueba es un script repetible que guarda sus registros, hashes y tiempos.

**Por qué.** Más pruebas no recortan requisitos, así que la unión es la opción que menos simplifica. Ninguno de los dos diseños tenía una prueba explícita de acceso RF3, que es una de las siete mínimas.

**De T se conserva.** kill -9 durante la lectura, la verificación de integridad de punta a punta, los niveles 5 y 20 de clientes y la comparación R = 1 frente a R = 3.

**Compatibilidad con el punto 12.** No descarta nada. El informe debe incluir esta tabla de correspondencia con la sección 5.2.

## D12. Documentación

**Decisión.**
- **Vistas.** Contexto en C4 nivel 1 (T), actualizado sin servicio Auth externo. Un único diagrama de componentes con las conexiones A a F de L en notación C4 nivel 2, actualizado con etcd y los puertos 50051 y 50052. Despliegue de L actualizado con D10. Casos de uso y secuencias S0 a S6 y escenarios 1 y 3 de L, actualizados con D3, D4 y D9.
- **Amenazas.** Una sola tabla STRIDE con las amenazas de ambos diseños y las mitigaciones de D8.
- **Fuentes.** El código fuente de todos los diagramas (scripts de L y fuentes de los C4 de T) va en docs/diagramas/ del repositorio.
- **Informe.** Matriz comparativa breve, arquitectura acordada, cambios frente a cada diseño individual (columna "Origen" de este registro), APIs y algoritmos, despliegue, pruebas con la tabla de D11, resultados, limitaciones y tabla de contribuciones. El Word sigue las reglas de estilo de Laura y el README va en Markdown.
- **Contribuciones.** Commits con la cuenta de cada integrante y tabla de quién hizo qué en el informe y el README.

**Por qué.** Más vistas no recortan nada, pero dos diagramas de componentes distintos serían incoherentes, por eso se fusionan en uno. STRIDE (T) da una estructura más sistemática a la tabla de amenazas de L.

**De T se conserva.** C4 de contexto, la notación C4 del diagrama de componentes y el análisis STRIDE.

## Revisión de coherencia del conjunto

- etcd (D1) sostiene el manifiesto y el commit (D3), los locks, la cola y el token de cerco (D4), y los usuarios (D8).
- Los nombres en Route 53 (D7) son la base de los certificados (D8) y de la zona privada del despliegue (D10).
- La regla de dos zonas (D5) se cumple con las tres zonas de D10.
- Los latidos e inventarios a los tres nodos de metadatos (D6) los atiende NodeService (D9).
- Los puertos de D9 son los que abren los grupos de seguridad de D10.
- La prueba con dos clientes (D11) usa la novena EC2 de D10.

**Simplificaciones que requieren aprobación del profesor.** Ninguna. Se evitaron el Master único (RNF2) y el write() sin modificación parcial (RF3), y el cifrado de disco no se considera simplificación.

**Límites a declarar.** Los de etcd (128 operaciones por transacción, 1,5 MiB por petición y 2 GB de cuota por defecto) y los de los diseños de origen (lectura de archivos menores de 16 MB desde un solo nodo, entre otros).

**Verificado en el laboratorio.** NAT Gateway, creación de AMI, tipos de instancia y KMS (ver D8 y D10). KMS también funciona desde una EC2 con LabInstanceProfile (comprobado el 2026-11-07). No queda nada pendiente de verificar.

## Stack (confirmado por el equipo el 2026-11-07)

| Parte | Herramienta |
|---|---|
| Lenguaje | Python 3.12 en todos los componentes (imagen python:3.12-slim) |
| RPC | grpcio y grpcio-tools, servidores con grpc.aio |
| Consenso y almacén de metadatos | etcd 3.5 (imagen oficial), cliente generado desde los .proto oficiales de etcd (KV, Lease, Lock y Election del lado del servidor) |
| Criptografía | cryptography (AES-GCM, Ed25519, HMAC, CA interna) y hashlib (SHA-256) |
| Contraseñas y tokens | argon2-cffi y PyJWT con EdDSA |
| KMS | boto3 con el rol de la instancia (LabInstanceProfile) |
| CLI | typer, sobre una biblioteca de cliente con open, read, write, append, lock, fsync y close |
| Pruebas | pytest, más scripts de experimentos con tc netem e iptables |
| Despliegue | Docker y Docker Compose por máquina, scripts con AWS CLI |

**Por qué.** Python es el lenguaje del laboratorio de gRPC del curso, así que ambos pueden explicar todo el código. Su punto débil, un cliente de etcd sin mantenimiento (python-etcd3), se evita generando el cliente desde los .proto oficiales de etcd, que además ofrecen Lock (cola por revisión y token de cerco) y Election del lado del servidor. Go era técnicamente mejor por su cliente oficial de etcd, pero no es el lenguaje del curso. Java pesa en una t3.micro, Node no aporta ventaja y mezclar lenguajes complica la defensa. REST con FastAPI no se usa porque D9 eligió gRPC por el streaming y el contrato.

**A comprobar en el prototipo.** Que la entrada de la cola de Lock se limpie al cancelar la espera de 10 s, y la compilación de los .proto de etcd con sus dependencias (gogo.proto y annotations.proto).
