# Prototipo de la escritura en cadena entre nodos de datos

Prueba los riesgos del plano de datos antes de construir el nodo de datos completo (D5 y D6). Usa el mismo código que usará el nodo de datos, `dfsha/datanode/`, con tres nodos en **procesos separados** que se comunican por la red y se configuran solo con variables de entorno.

## Cómo ejecutarlo

```
python prototipos/cadena-datanodes/prueba_cadena.py
```

El script arranca tres procesos `python -m dfsha.datanode` en los puertos 50061, 50062 y 50063 de 127.0.0.1, cada uno con su propio directorio de datos temporal, ejecuta los escenarios, detiene los procesos y borra los datos. Deja los resultados en `resultados.json`. Tarda menos de un minuto.

Un nodo de datos se arranca a mano de la misma forma.

```
DFSHA_NODO_NOMBRE=dn1 DFSHA_NODO_DIRECTORIO_DATOS=/tmp/dn1 DFSHA_TLS_HABILITADO=false python -m dfsha.datanode
```

## Resultados

Tres corridas seguidas con la versión final, con los mismos resultados en los escenarios de fallas. Los tiempos son de la última, guardada en `resultados.json`.

| # | Escenario | Qué se esperaba | Resultado |
|---|---|---|---|
| 1 | Bloque de 16 MiB por la cadena de 3 nodos | Tres copias idénticas | Copias en los 3 nodos y lectura idéntica (misma huella) desde cada uno |
| 2 | Rendimiento con un bloque de 16 MiB (1 de calentamiento y 5 medidas) | Medir el costo de la cadena frente a un nodo, y el trozo de 1 MiB frente al de 64 KiB | Un nodo con trozo de 1 MiB, 201,4 MB/s (desviación 23,2). Cadena de 3 con trozo de 1 MiB, 135,0 MB/s (desviación 5,1). Cadena de 3 con trozo de 64 KiB, 98,0 MB/s (desviación 3,2) |
| 3 | El último nodo muere (kill) al enviar el trozo 10 de 16 | Quedan 2 copias (W = 2) y el bloque a medias nunca se publica | Copias en dn1 y dn2. dn3 dejó un .part que se borró al reiniciar, sin publicar el bloque |
| 4 | El nodo del medio muere al enviar el trozo 10 de 16 | Queda 1 copia y el cliente reenvía el bloque completo por otra cadena con el mismo id | Primera escritura con 1 copia (dn1). El reenvío por dn1 y dn3 dejó 2 copias correctas, y dn1 aceptó guardar otra vez el mismo bloque. El .part de dn2 se borró al reiniciar |
| 5 | Huella incorrecta | Ningún nodo publica el bloque ni deja temporales | DATA_LOSS con motivo CHECKSUM_MISMATCH, sin bloque ni temporales en ningún nodo |

**Cómo leer el rendimiento.** Aquí los tres nodos y el cliente comparten un solo computador, así que cada byte se procesa tres veces en el mismo procesador y la cadena rinde un 67 % de un nodo solo. En AWS cada nodo tiene su propia máquina y el límite pasa a ser la red, que es donde la cadena muestra su ventaja (el cliente envía cada bloque una sola vez). Esa medición es el experimento E3. El trozo de 1 MiB rindió más que el de 64 KiB, lo que respalda el valor por defecto de D5, y la comparación se repite en AWS como parte de los experimentos de rendimiento.

## Qué cambió en el diseño gracias al prototipo

- **Un nodo caído no arrastra al anterior.** En la primera versión, si el último nodo no respondía, el del medio se quedaba esperando a poner trozos en una cola que nadie leía, y el primero terminaba dándolo también por caído. Quedaba 1 copia en vez de 2. Ahora cada envío espera a la vez a la cola y a la conexión, y si la conexión falla se deja de reenviar al instante.
- **La respuesta de la cadena se espera según su longitud.** Cada nodo espera la respuesta del siguiente con un plazo proporcional a los nodos que faltan, porque el siguiente puede estar esperando a su vez a los de atrás.
- **La confirmación decide el destino del temporal.** Si el nodo anterior corta la conexión mientras un nodo está confirmando su copia, la confirmación termina sola. Renombra el bloque si la huella coincide y lo borra si no, sin chocar con un borrado simultáneo.
- **El id del bloque se valida** contra el formato `b-` y 16 caracteres hexadecimales antes de usarlo como nombre de archivo, porque llega por la red (D8).
