# Pruebas

DFSha se prueba en tres niveles: pruebas automáticas con pytest, escenarios sobre el entorno local de Docker Compose y experimentos sobre el despliegue de AWS. Los resultados de AWS están en [experimentos/resultados/resumen.md](../experimentos/resultados/resumen.md).

## 1. Pruebas automáticas

| Archivo | Qué prueba | Necesita etcd |
|---|---|---|
| `tests/test_contrato_etcd.py`, `tests/test_contrato_dfsha.py` | Que los .proto compilan y el contrato tiene lo que la API documenta | No |
| `tests/test_config.py` | Carga y validación de la configuración | No |
| `tests/test_tls.py` | CA interna, TLS, TLS mutuo e identidad de los nodos | No |
| `tests/test_datanode.py` | Almacén de bloques, escritura en cadena, huellas y fallas de la cadena | No |
| `tests/test_nodos.py` | Tokens de acceso, estado de los nodos, NodeService, órdenes, revisión diaria y réplica lenta | No |
| `tests/test_cliente_datos.py` | Cifrado por bloque y cambio de réplica al leer | No |
| `tests/test_etcd_cliente.py` | Cliente de etcd: transacciones, leases, Lock y Election | Sí |
| `tests/test_metadatos.py` | Líder, sesión, usuarios, JWT, cambio de líder y límite de peticiones | Sí |
| `tests/test_archivos.py` | Espacio de nombres, Open, locks, commit, conflictos y cuota | Sí |
| `tests/test_recuperacion.py` | Re-replicación, recolector, Decommission y ReportBadBlock | Sí |

Cómo ejecutarlas está en el [README](../README.md#2-pruebas).

## 2. Entorno local

| Script | Qué comprueba |
|---|---|
| `deploy/compose/prueba_humo.py` | Sesión, NOT_LEADER, administración, escritura en cadena con token, commit, lectura desde las 3 réplicas, put y get con la biblioteca y puertos internos cerrados |
| `deploy/compose/escenarios.py` | RF1 a RF3 y los escenarios obligatorios 1 a 4, con fallas inyectadas con Docker |

## 3. Experimentos en AWS

`experimentos/ejecutar.py` corre la carga en las máquinas cliente (`experimentos/carga.py`) e inyecta las fallas por SSH. Cada punto de tiempo tiene una repetición de calentamiento y 3 medidas (A14). Antes de cada experimento deja los 9 servicios arriba y sin fallas, y al final registra la estabilidad de cada nodo.

| Prueba exigida (5.2) | Experimento | Fallas inyectadas | Qué demuestra |
|---|---|---|---|
| Carga y recuperación | E1: put y get de 16 MiB, 128 MiB y 1 GiB | Ninguna | Hash idéntico, 3 copias por bloque y bloques repartidos en los 4 nodos |
| Acceso RF3 | E0: read de un rango, lock, write de 100 bytes en el bloque 7, append y close | Ninguna | Lo escrito solo se ve tras close y el bloque vecino no cambia |
| Concurrencia | E7: 4, 8 y 16 escritores desde los dos clientes, en el mismo bloque y en bloques distintos | Ninguna | Ningún cambio se pierde. En el mismo bloque se serializan con el lock y VERSION_CONFLICT |
| Replicación y fallos | E5: cae un nodo de datos con 4 y con 3 nodos. E6: cae el líder. kill -9 durante una lectura de 1 GiB | docker kill, docker stop, kill -9 | Lecturas y escrituras correctas, copias repuestas, cambio de líder y re-registro de los nodos |
| Consistencia | E4: réplica con 200 ms más de latencia. E8: escritor muerto a mitad de un put. Réplica desconectada por firewall | tc netem, kill -9, nftables | Nunca se publica una versión incompleta y el bloque cuenta con W = 2 |
| Seguridad | E9: token válido, ausente y alterado, otro usuario, sin JWT, captura de tráfico, disco y puertos | tcpdump | Acceso denegado donde corresponde, sin texto en claro en la red ni en disco |
| Rendimiento | E2 (paralelismo 1, 4 y 8), E3 (R = 2 y R = 3), E10 (costo del cifrado), E11 (1 a 50 clientes) y tamaño de trozo (64 KiB y 1 MiB) | Ninguna | Latencia y rendimiento comparados en varias condiciones |

## Limitaciones encontradas

- **Réplica lenta.** Con W = 2, una réplica lenta entre las dos primeras de la cadena frena la escritura (E4, A15). La Fase I preveía que apenas cambiaría.
- **Réplica desconectada sin respuesta.** Si un nodo descarta los paquetes sin responder, la escritura espera el plazo de reenvío de 10 s antes de pedir otra cadena.
- **Copias de más.** Cuando un nodo caído vuelve después de la re-replicación, sus copias quedan como copias de más y no se recortan.
- **Fuerza bruta.** No hay bloqueo por cuenta tras varios intentos fallidos ([docs/seguridad.md](seguridad.md)).
