# Resultados de los experimentos en AWS

Despliegue de 9 EC2 t3.small en us-east-1 (3 zonas), R = 3 y W = 2, bloques de 16 MiB. La carga corre en las máquinas cliente y las fallas se inyectan con experimentos/ejecutar.py. Los tiempos están en segundos y el rendimiento en MiB/s. Cada punto tiene una repetición de calentamiento y 3 medidas.


## Correspondencia con las pruebas exigidas (5.2)

| Prueba exigida | Experimentos |
|---|---|
| Carga y recuperación | E1 |
| Acceso RF3 | E0 |
| Concurrencia | E7 |
| Replicación y fallos | E5, E6 y kill -9 durante una lectura |
| Consistencia | E4, E8 y réplica desconectada (escenario 2) |
| Seguridad | E9 |
| Rendimiento | E2, E3, E10, E11 y tamaño de trozo |

## E1. Carga y recuperación

| Tamaño | put (s) | get (s) | put MiB/s | get MiB/s | Hash idéntico | Bloques | Copias por bloque |
|---|---|---|---|---|---|---|---|
| 16MiB | 0.41 ± 0.02 (mín 0.40, máx 0.43) | 0.21 ± 0.00 (mín 0.21, máx 0.21) | 38.7 | 76.1 | sí | 1 | mín 3, máx 3 |
| 128MiB | 2.03 ± 0.10 (mín 1.92, máx 2.12) | 1.22 ± 0.01 (mín 1.21, máx 1.23) | 63.0 | 104.9 | sí | 8 | mín 3, máx 3 |
| 1024MiB | 16.85 ± 0.76 (mín 16.27, máx 17.72) | 9.03 ± 0.16 (mín 8.88, máx 9.20) | 60.8 | 113.3 | sí | 64 | mín 3, máx 3 |

Reparto de las copias de los 64 bloques del archivo de 1 GiB (RNF4): dn1 32, dn2 64, dn3 64, dn4 32.

## E0. Acceso RF3

| Comprobación | Resultado |
|---|---|
| read de un rango que cruza los bloques 6 y 7 | sí |
| tiempo del read (s) | 0.264 |
| lock del bloque 7 con token de cerco | sí |
| otro cliente no ve el write antes de close | sí |
| tiempo del close con commit (s) | 0.374 |
| otro cliente ve el write después de close | sí |
| el bloque vecino no cambia | sí |
| append de 30 bytes | sí |
| versión final del archivo | 3 |

## E7. Concurrencia (escritores repartidos entre los dos clientes)

En el mismo bloque, el lock serializa a los escritores. El que abrió antes del commit de otro recibe VERSION_CONFLICT, vuelve a abrir y reintenta, así que ningún cambio se pierde. En bloques distintos no hay conflictos y los escritores avanzan en paralelo.

| Caso | Escritores | Tiempo total (s) | Latencia por escritor (s) | Reintentos por conflicto | Cambios presentes al final |
|---|---|---|---|---|---|
| bloques distintos | 4 | 1.06 | 1.02 ± 0.03 (mín 0.98, máx 1.06) | 0 | 4 de 4 |
| bloques distintos | 8 | 1.93 | 1.84 ± 0.06 (mín 1.76, máx 1.93) | 0 | 8 de 8 |
| bloques distintos | 16 | 3.45 | 3.15 ± 0.34 (mín 2.28, máx 3.45) | 0 | 16 de 16 |
| mismo bloque | 4 | 5.12 | 3.18 ± 1.88 (mín 0.72, máx 5.12) | 6 | 4 de 4 |
| mismo bloque | 8 | 18.29 | 10.56 ± 6.50 (mín 0.82, máx 18.29) | 24 | 8 de 8 |
| mismo bloque | 16 | 70.70 | 45.23 ± 22.63 (mín 0.86, máx 70.70) | 120 | 16 de 16 |

## E11. Metadatos con varios clientes (stat y ls)

| Clientes | Operaciones/s | p50 (ms) | p95 (ms) | Muestras |
|---|---|---|---|---|
| 1 | 61 | 16.3 | 17.4 | 305 |
| 5 | 235 | 21.3 | 24.5 | 1173 |
| 10 | 279 | 36.1 | 40.7 | 1394 |
| 20 | 300 | 67.5 | 78.2 | 1502 |
| 50 | 348 | 146.3 | 170.2 | 1739 |

## E2. Lectura y escritura en paralelo (128 MiB)

| Caso | put (s) | get (s) | put MiB/s | get MiB/s | Hash idéntico |
|---|---|---|---|---|---|
| paralelismo_1 | 2.62 ± 0.01 (mín 2.60, máx 2.63) | 1.29 ± 0.01 (mín 1.28, máx 1.30) | 48.9 | 99.1 | sí |
| paralelismo_4 | 2.22 ± 0.02 (mín 2.21, máx 2.24) | 1.24 ± 0.01 (mín 1.23, máx 1.25) | 57.6 | 103.2 | sí |
| paralelismo_8 | 2.21 ± 0.17 (mín 2.10, máx 2.40) | 1.09 ± 0.01 (mín 1.09, máx 1.10) | 58.0 | 117.2 | sí |

## Tamaño de trozo del stream (128 MiB)

| Caso | put (s) | get (s) | put MiB/s | get MiB/s | Hash idéntico |
|---|---|---|---|---|---|
| 64KiB | 2.35 ± 0.04 (mín 2.30, máx 2.38) | 1.21 ± 0.03 (mín 1.18, máx 1.24) | 54.5 | 106.0 | sí |
| 1MiB | 1.99 ± 0.09 (mín 1.90, máx 2.08) | 1.07 ± 0.02 (mín 1.06, máx 1.09) | 64.2 | 119.7 | sí |

## E3. Factor de replicación (128 MiB)

| R | put (s) | get (s) | put MiB/s | get MiB/s | Hash idéntico |
|---|---|---|---|---|---|
| 3 (E1) | 2.03 ± 0.10 (mín 1.92, máx 2.12) | 1.22 ± 0.01 (mín 1.21, máx 1.23) | 63.0 | 104.9 | sí |
| 2 | 2.17 ± 0.15 (mín 2.00, máx 2.27) | 1.17 ± 0.08 (mín 1.08, máx 1.22) | 59.0 | 109.5 | sí |

La tercera copia casi no cuesta rendimiento: la cadena reenvía mientras recibe y el límite es la subida del cliente al primer nodo. R = 3 da tolerancia a una falla más por casi el mismo tiempo.

R = 3 es la configuración de E1. R = 1 no se admite (validación R >= 2, D5), se compara R = 2.

## E4. Réplica lenta (dn2 con 200 ms más de latencia, 128 MiB)

| Caso | put (s) | get (s) | put MiB/s | get MiB/s | Hash idéntico |
|---|---|---|---|---|---|
| Sin retraso (E1) | 2.03 ± 0.10 (mín 1.92, máx 2.12) | 1.22 ± 0.01 (mín 1.21, máx 1.23) | 63.0 | 104.9 | sí |
| dn2 +200 ms | 11.97 ± 2.59 (mín 9.06, máx 14.02) | 10.42 ± 0.80 (mín 9.53, máx 11.08) | 10.7 | 12.3 | sí |

**Interpretación.** Con W = 2, un bloque cuenta como escrito cuando lo tienen los dos primeros nodos de su cadena. La réplica lenta solo deja de afectar cuando queda tercera (A15). En unos dos tercios de las cadenas dn2 queda entre las dos primeras, así que la escritura en cadena la tiene en el camino crítico. Las lecturas también empeoran porque cliente2 está en la subred de dn2 y la cercanía se decide por subred, no por la latencia medida. La diferencia con lo previsto en la Fase I ("con W = 2 apenas cambia") viene de la escritura en cadena (D5). Las mejoras posibles son ordenar las réplicas por latencia medida y dejar a los nodos lentos al final de la cadena.

## E10. Costo del cifrado en el cliente (t3.small)

AES-256-GCM cifra a 616 MiB/s y SHA-256 calcula a 377 MiB/s, así que un bloque de 16 MiB se cifra en 26 ms y sus dos huellas en 85 ms.
 Con put de 128 MiB a 63.0 MiB/s, el cifrado y las huellas son una fracción pequeña del tiempo, que domina la red y la escritura en cadena.

## E9. Seguridad

| Caso | Resultado |
|---|---|
| token valido | aceptado |
| sin token | TOKEN_INVALID |
| token alterado | TOKEN_INVALID |
| otro usuario | FORBIDDEN |
| sin jwt | TOKEN_INVALID |

| Nodo | Captura de tráfico (bytes, apariciones del texto en claro) | Disco (bloques, bloques con el texto en claro) |
|---|---|---|
| dn1 | 24 0 | 1134 0 |
| dn2 | 56798 0 | 1754 0 |
| dn3 | 16743 0 | 1758 0 |
| dn4 | 39388 0 | 1132 0 |

Puertos vistos desde la máquina cliente (los internos los cierran los grupos de seguridad): mds1.dfsha.internal:50053 cerrado, mds1.dfsha.internal:2379 cerrado, dn1.dfsha.internal:50054 cerrado, dn1.dfsha.internal:50052 abierto, mds1.dfsha.internal:50051 abierto.

## E5. Caída de un nodo de datos

- **Con 4 nodos.** Cae dn2. put y get de 128 MiB después de la caída en 1.97 ± 0.00 (mín 1.97, máx 1.97) s y 1.24 ± 0.00 (mín 1.24, máx 1.24) s, hash idéntico: sí. Las copias vuelven a 3 en 19.6 s (copias finales [3, 3, 3, 3, 3, 3, 3, 3]).
- **Con 3 nodos.** dn1 apagado y cae dn2. put y get de 64 MiB con 2 nodos vivos: hash idéntico sí. Copias del archivo anterior: [2, 2, 2, 2, 2, 2, 2, 2] (sin un tercer nodo vivo no se puede volver a 3).

## kill -9 durante una lectura

Con kill -9 a dn2 a los 4 s, put y get de 1 GiB en 18.39 ± 0.00 (mín 18.39, máx 18.39) s y 10.84 ± 0.00 (mín 10.84, máx 10.84) s, hash idéntico: sí.

## E6. Caída del líder de metadatos

Con docker kill al líder (mds1), el mayor hueco sin respuesta de un cliente que hace stat cada 50 ms fue de 3.07 s (0 llamadas fallidas). Líderes vistos por el cliente: mds1, mds2. Al volver, el nodo caído recibió el registro de 4 de 4 nodos de datos en 25 s.

## Réplica desconectada durante una escritura (escenario 2)

Con un firewall que bloquea los puertos de datos de dn3, el put de 64 MiB terminó en 21.76 ± 0.00 (mín 21.76, máx 21.76) s con copias por bloque [3, 3, 2, 3] (W = 2), hash idéntico: sí. 30 s después de quitar el bloqueo, copias [3, 3, 3, 3].

**Interpretación.** El firewall descarta los paquetes sin responder, así que el nodo anterior de la cadena solo detecta la desconexión cuando vence su plazo de reenvío (10 s). Después el cliente pide otra cadena sin dn3. La escritura es correcta pero lenta. Un plazo de reenvío más corto la aceleraría, a costa de más falsas alarmas con la red cargada.

## E8. Escritor muerto a mitad de un put

Antes: archivo de 64 MiB. Un escritor que reemplazaba el archivo por uno de 1 GiB murió con kill -9. Después: versión 1, tamaño 67108864 bytes, copias [3, 3, 3, 3]. El archivo sigue en la versión anterior y la versión incompleta nunca se publicó.

## Estabilidad de los nodos durante la serie

| Nodo | Al empezar | Al terminar |
|---|---|---|
| mds1 | reinicios=0 memoria=57.73MiB oom_desde_arranque=0 | reinicios=0 memoria=54.73MiB oom_desde_arranque=0 |
| mds2 | reinicios=0 memoria=56.48MiB oom_desde_arranque=0 | reinicios=0 memoria=53.2MiB oom_desde_arranque=0 |
| mds3 | reinicios=0 memoria=56.54MiB oom_desde_arranque=0 | reinicios=0 memoria=53.11MiB oom_desde_arranque=0 |
| dn1 | reinicios=0 memoria=34.29MiB oom_desde_arranque=2 | reinicios=0 memoria=68.2MiB oom_desde_arranque=2 |
| dn2 | reinicios=0 memoria=42.39MiB oom_desde_arranque=8 | reinicios=0 memoria=169MiB oom_desde_arranque=8 |
| dn3 | reinicios=0 memoria=27.43MiB oom_desde_arranque=30 | reinicios=0 memoria=245.8MiB oom_desde_arranque=30 |
| dn4 | reinicios=0 memoria=25.91MiB oom_desde_arranque=0 | reinicios=0 memoria=58MiB oom_desde_arranque=0 |
