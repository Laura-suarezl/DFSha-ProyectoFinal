# Cliente

## Ruta de datos

`dfsha/client/datos.py` mueve los bloques entre el cliente y los nodos de datos (D5 y D8).

- **Cifrado.** Cada bloque se cifra con AES-256-GCM con la clave del archivo que entrega Open, un nonce nuevo y como datos asociados el id del archivo y el índice del bloque.
- **Dos huellas.** h es la huella del bloque en claro y H la del bloque cifrado. El nodo de datos verifica H al guardar. Al leer, el cliente verifica H, descifra, verifica h y al final la huella del archivo.
- **Paralelismo.** Los bloques viajan de a `almacenamiento.paralelismo` a la vez, directamente con los nodos de datos.
- **Fallas al leer.** Si una réplica no responde en `tiempos.timeout_replica_s` o su huella no coincide, se lee la siguiente y la mala se informa con ReportBadBlock.
- **Fallas al escribir.** Si la cadena guarda menos de W copias, se pide otra cadena para ese bloque con AllocateBlocks.

`subir` y `bajar` hacen el put y el get de un archivo completo con Open, AllocateBlocks, Commit y Close.

## Biblioteca

`dfsha/client/cliente.py` ofrece la clase `Cliente`.

- **Descubrimiento.** Solo conoce el nombre lógico del clúster. Sigue NOT_LEADER hasta el líder, lo guarda y, si deja de responder, vuelve a preguntar por el nombre lógico.
- **Sesión.** Login, renovación automática cada 10 s y Refresh del JWT antes de que venza.
- **RF1 y RF2.** ls, stat, mkdir, rmdir, rm, mv, put y get.
- **RF3.** `open(ruta, modo)` con los modos r, w, a y rw devuelve un archivo con read, write, append, lock, unlock, fsync y close. Lo escrito queda en un búfer por bloque y se publica en un solo commit con fsync o close. Escribir un bloque en modo rw toma su lock, y anexar toma el lock del final.

## CLI

`dfsha` (o `python -m dfsha.client`) tiene los comandos login, logout, pwd, cd, ls, stat, mkdir, rmdir, rm, mv, put, get y, para administradores, admin crear-usuario, admin cuota, admin nodos y admin retirar. login guarda el token de renovación en `~/.dfsha/sesion.json` y cada comando abre una sesión nueva con él.

En el entorno local la CLI corre en el contenedor `cliente`, dentro de la red, como en la máquina cliente de AWS.

```
docker compose -f deploy/compose/docker-compose.dev.yml exec cliente dfsha login admin
```

## Escenarios obligatorios

`deploy/compose/escenarios.py` ejecuta sobre el entorno local las operaciones de RF1 a RF3 y los escenarios 1 a 4, inyectando fallas con Docker.
