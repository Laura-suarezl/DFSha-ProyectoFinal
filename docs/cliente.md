# Cliente

## Ruta de datos

`dfsha/client/datos.py` mueve los bloques entre el cliente y los nodos de datos (D5 y D8).

- **Cifrado.** Cada bloque se cifra con AES-256-GCM con la clave del archivo que entrega Open, un nonce nuevo y como datos asociados el id del archivo y el índice del bloque.
- **Dos huellas.** h es la huella del bloque en claro y H la del bloque cifrado. El nodo de datos verifica H al guardar. Al leer, el cliente verifica H, descifra, verifica h y al final la huella del archivo.
- **Paralelismo.** Los bloques viajan de a `almacenamiento.paralelismo` a la vez, directamente con los nodos de datos.
- **Fallas al leer.** Si una réplica no responde en `tiempos.timeout_replica_s` o su huella no coincide, se lee la siguiente y la mala se informa con ReportBadBlock.
- **Fallas al escribir.** Si la cadena guarda menos de W copias, se pide otra cadena para ese bloque con AllocateBlocks.

`subir` y `bajar` hacen el put y el get de un archivo completo con Open, AllocateBlocks, Commit y Close.
