# Prototipo de etcd en un clúster de 3 miembros

Prueba los riesgos del plano de control antes de construir los nodos de metadatos (D1, D3 y D4). Usa el mismo cliente que usarán los nodos de metadatos, `dfsha/metadata/etcd.py`, contra un clúster de etcd v3.5.35 de 3 miembros, el mismo tamaño que en AWS.

## Cómo ejecutarlo

```
docker compose -f prototipos/etcd-cluster/docker-compose.yml up -d
python prototipos/etcd-cluster/prueba_cluster.py
docker compose -f prototipos/etcd-cluster/docker-compose.yml down
```

El script necesita Docker en el mismo equipo, porque detiene y vuelve a arrancar miembros de etcd. Deja los resultados en `resultados.json`. Tarda poco más de un minuto.

Las pruebas de integración del cliente usan el mismo clúster.

```
DFSHA_PRUEBA_ETCD=ipv4:127.0.0.1:23791,127.0.0.1:23792,127.0.0.1:23793 pytest -m integracion
```

## Resultados

Cuatro corridas seguidas con los mismos resultados. Los tiempos son de la última, guardada en `resultados.json`.

| # | Escenario | Qué se esperaba | Resultado |
|---|---|---|---|
| 1 | Cae el líder del servicio de metadatos (deja de renovar su lease de 3 s) | Otro candidato asume solo | Nuevo líder en 2,81 s (entre 2,81 y 3,15 s en todas las corridas). Si renuncia de forma ordenada, en milisegundos |
| 2 | El líder caído intenta escribir | La comparación de liderazgo lo impide | Escritura del líder caído rechazada y la del vigente aceptada |
| 3 | Se detiene cada miembro de etcd por turno, también el líder de Raft | El servicio sigue y la sesión no se pierde | 0 operaciones fallidas en las tres caídas. El keep-alive se reconectó solo, la sesión siguió viva y el lock conservó su token |
| 3b | Se detienen 2 de los 3 miembros durante 8 s | Las escrituras se rechazan (sistema CP) y luego se recupera | Escritura rechazada a los 5,0 s con EtcdNoDisponible (NO_QUORUM). Al volver la mayoría se escribe al instante, con la sesión y el lock intactos |
| 4 | Bloque 7 ocupado por A, B pide el archivo entero y luego C pide el bloque 3 | B espera a A y C espera a B | A libera en 1,02 s, B obtiene el archivo en 1,02 s, B libera en 1,53 s y C obtiene el bloque en 1,54 s |
| 5 | Dos escritores leen la versión 1 y cambian bloques distintos (3 y 7), y un tercero tiene una base vieja del bloque 3 | Los dos primeros confirman sin perder cambios y el tercero recibe VERSION_CONFLICT | Versión 2 y versión 3 (el segundo reintentó una vez). La versión final tiene los dos cambios. El tercero recibe VERSION_CONFLICT |
| 6 | Límites de etcd | 128 operaciones por transacción y 1,5 MiB por petición | El límite es por lista: 128 comparaciones y 128 escrituras pasan, 129 se rechazan. 1,4 MiB pasa y 1,6 MiB se rechaza |

## Qué cambió en el diseño gracias al prototipo

- **La jerarquía de locks usa claves de intención.** Esperar con Watch a que el prefijo de los locks de bloque quede vacío no basta, porque entre la comprobación y la petición del lock puede colarse otro. Ahora cada lock de bloque registra una intención bajo un candado local del líder (ver docs/etcd-esquema.md).
- **Watch se abre desde la revisión siguiente a la del conteo.** En la primera versión se abría "desde ahora" y podía perderse el aviso de que el prefijo quedó vacío, lo que agregaba medio segundo de espera.
- **Plazo total por llamada a etcd.** En la primera versión cada reintento tenía su propio plazo, y sin mayoría una escritura tardaba 44 s en fallar, más que la sesión de 30 s. Ahora todo cabe en 5 s.
- **La caída de un miembro llega como UNAVAILABLE o como CANCELLED.** Las dos se tratan como falla del miembro. En el lock, CANCELLED también marca el fin de la espera normal, así que se distinguen por el tiempo transcurrido, y un corte antes de tiempo se reintenta con la misma entrada de la cola.
- **El límite de 128 operaciones es por lista.** Un commit puede modificar hasta 125 bloques con lock propio, no 120 como se había estimado.
