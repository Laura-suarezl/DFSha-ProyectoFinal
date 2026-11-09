# Semántica de acceso y garantías

Aclaraciones de qué admite write(), documentacion de la semántica de lock() y formulacion las garantías de consistencia de forma comprobable. Este documento lo hace para DFSha tal como está implementado.

## Operaciones (RF3)

`open(ruta, modo)` devuelve un archivo abierto con read, write, append, lock, unlock, fsync y close (`dfsha/client/cliente.py`).

| Modo | write() admite | Lock que toma |
|---|---|---|
| `r` | Nada, solo lectura | Ninguno |
| `w` | **Crear** o **reemplazar**. El archivo queda solo con lo escrito | El del archivo entero, al abrir |
| `a` | **Anexar** al final | El del final del archivo, al escribir |
| `rw` | **Modificar por bloque** cualquier rango y anexar | El de cada bloque que se escribe, al escribir |

- **Granularidad.** Se lee y escribe cualquier rango de bytes. El sistema trabaja internamente con bloques de 16 MiB, y modificar unos bytes reescribe solo los bloques tocados, que se guardan como bloques nuevos (copy-on-write, nunca se sobrescribe un bloque).
- **Huecos.** No se admite escribir más allá del final del archivo (BAD_OFFSET).
- **Publicación.** Lo escrito queda en un búfer del cliente y se publica en un solo commit con fsync() o close().

## Semántica de lock()

- **Objetivos.** Un bloque, el final del archivo o el archivo entero. El lock del archivo entero excluye a los demás del mismo archivo.
- **Cola.** Los pedidos esperan en orden de llegada hasta 10 s. Si se agota la espera, la respuesta es LOCKED con el dueño actual y su vencimiento.
- **Vida.** Los locks cuelgan de la sesión, que dura 30 s y se renueva cada 10 s. Si el cliente muere, sus locks vencen solos.
- **Token de cerco.** Cada lock tiene un número que solo crece (la revisión de etcd). El commit lo comprueba, así un cliente que perdió su lock sin saberlo no puede publicar (LEASE_EXPIRED).
- **Implícito y explícito.** write() toma solo el lock que necesita. lock() y unlock() permiten tomarlos antes, por ejemplo para varios bloques a la vez.

## Garantías

| Garantía | Enunciado comprobable | Cómo se cumple | Evidencia |
|---|---|---|---|
| G1 Confirmación | close() o fsync() responden OK solo cuando cada bloque nuevo está en al menos 2 nodos de datos con su huella verificada y la versión nueva está en al menos 2 de los 3 miembros de etcd | El commit exige 2 avisos BlockReceived por bloque y se escribe con una transacción de etcd, que necesita mayoría | E0, E1, escenario 2 |
| G2 Visibilidad | Todo open() que empiece después de ese OK ve esa versión o una posterior, incluido el mismo cliente | Open lee la versión vigente desde el líder, que lee etcd | E0 |
| G3 Instantánea | Quien leyó ve completa la versión que había al abrir, aunque otro confirme cambios mientras tanto | Cada open() crea un arriendo de lectura atado a la sesión, y el recolector no borra una versión con arriendos | E0 (el otro cliente no ve el cambio antes de close) |
| G4 Atomicidad | Lo no confirmado nunca es visible, ni archivos a medias ni mezclas de versiones | Un solo commit por archivo publica el manifiesto completo | E8, escenario 3 |
| G5 Integridad | Todo bloque entregado coincide con su huella. Si no, se descarta y se lee otra réplica | Huella cifrada H, etiqueta AES-GCM y huella en claro h, verificadas en el cliente, y revisión diaria en los nodos | kill -9 durante una lectura, E1 |
| G6 Monotonía | La versión de un archivo solo crece y un cliente no ve una versión anterior a otra que ya vio | La versión sale de etcd, que solo avanza | E0, E7 |

**Escrituras concurrentes.** Dos escritores del mismo bloque se ordenan por el lock. Si el segundo había abierto antes del commit del primero, su commit recibe VERSION_CONFLICT, porque su versión base ya no es la vigente, y debe volver a abrir. Ningún cambio se pierde (E7 con 16 escritores, escenario 1). La integridad no reemplaza esta coordinación: las dos funcionan juntas.

## Límites declarados

| Límite | Valor | Origen |
|---|---|---|
| Bloques modificados por commit con lock propio | 125 | 128 operaciones por transacción de etcd (A1) |
| Tamaño de un archivo | Unos 120 GB | 1,5 MiB por petición de etcd para el manifiesto |
| Réplica lenta entre las dos primeras de la cadena | Frena la escritura | Escritura en cadena (A15, E4) |
