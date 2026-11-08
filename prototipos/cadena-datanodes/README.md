# Prototipo de la escritura en cadena entre nodos de datos

Prueba los riesgos del plano de datos antes de construir el nodo de datos completo (D5, D6 y D8). Usa el mismo código que usará el nodo de datos, `dfsha/datanode/`, con tres nodos en **procesos separados** que se comunican por la red, se configuran solo con variables de entorno y usan TLS con certificados de una CA interna.

## Cómo ejecutarlo

```
python prototipos/cadena-datanodes/prueba_cadena.py
python prototipos/cadena-datanodes/prueba_cadena.py --sin-tls
```

El script crea una CA interna en una carpeta temporal (con `deploy/certs/crear_certificados.py --local`) y arranca tres procesos `python -m dfsha.datanode`. Cada nodo escucha en dos puertos de 127.0.0.1, el de clientes (50061, 50062 y 50063) con TLS y el interno (50071, 50072 y 50073) con TLS mutuo. El cliente entra por el puerto de clientes del primer nodo y el bloque sigue por los puertos internos. Al terminar detiene los procesos y borra los datos y los certificados. Deja los resultados en `resultados.json`, o en `resultados-sin-tls.json` con `--sin-tls`. Cada corrida tarda menos de un minuto.

Un nodo de datos se arranca a mano de la misma forma.

```
DFSHA_NODO_NOMBRE=dn1 DFSHA_NODO_DIRECTORIO_DATOS=/tmp/dn1 DFSHA_TLS_CA=certs/ca.crt DFSHA_TLS_CERTIFICADO=certs/dn1.crt DFSHA_TLS_LLAVE=certs/dn1.key python -m dfsha.datanode --sin-metadatos
```

## Resultados

Dos pares de corridas seguidas (con y sin TLS), con los mismos resultados en los escenarios de fallas y de seguridad. Los tiempos son de las últimas, guardadas en `resultados.json` y `resultados-sin-tls.json`.

| # | Escenario | Qué se esperaba | Resultado |
|---|---|---|---|
| 1 | Bloque de 16 MiB por la cadena de 3 nodos | Tres copias idénticas | Copias en los 3 nodos y lectura idéntica (misma huella) desde cada uno |
| 2 | Rendimiento con un bloque de 16 MiB (1 de calentamiento y 5 medidas) | Medir el costo de la cadena frente a un nodo, el trozo de 1 MiB frente al de 64 KiB y el costo de TLS | Ver la tabla siguiente |
| 3 | El último nodo muere (kill) al enviar el trozo 10 de 16 | Quedan 2 copias (W = 2) y el bloque a medias nunca se publica | Copias en dn1 y dn2. dn3 dejó un .part que se borró al reiniciar, sin publicar el bloque |
| 4 | El nodo del medio muere al enviar el trozo 10 de 16 | Queda 1 copia y el cliente reenvía el bloque completo por otra cadena con el mismo id | Primera escritura con 1 copia (dn1). El reenvío por dn1 y dn3 dejó 2 copias correctas, y dn1 aceptó guardar otra vez el mismo bloque. El .part de dn2 se borró al reiniciar |
| 5 | Huella incorrecta | Ningún nodo publica el bloque ni deja temporales | DATA_LOSS con motivo CHECKSUM_MISMATCH, sin bloque ni temporales en ningún nodo |
| 6 | Quién puede entrar por cada puerto | Solo nodos con certificado de la CA en el puerto interno, y solo conexiones con TLS en el de clientes | Nodo legítimo en el puerto interno, aceptado. Cliente sin certificado en el puerto interno, rechazado (UNAVAILABLE). Certificado de otra CA en el puerto interno, rechazado (UNAVAILABLE). Conexión sin TLS al puerto de clientes, rechazado (UNAVAILABLE). Cliente con TLS en el puerto de clientes, aceptado |

**Rendimiento (escenario 2).**

| Condición | Sin TLS | Con TLS |
|---|---|---|
| 1 nodo, trozo 1 MiB | 148,7 MB/s (desviación 18,1) | 84,8 MB/s (desviación 3,6) |
| Cadena de 3, trozo 1 MiB | 101,4 MB/s (desviación 6,9) | 66,6 MB/s (desviación 0,6) |
| Cadena de 3, trozo 64 KiB | 61,9 MB/s (desviación 1,6) | 39,5 MB/s (desviación 8,9) |

**Cómo leer el rendimiento.** Aquí los tres nodos y el cliente comparten un solo computador, así que cada byte se procesa tres veces en el mismo procesador, y con TLS además se cifra y descifra en cada salto. Por eso la cadena rinde menos que un nodo solo y TLS reduce el rendimiento entre un 30 y un 45 %. En AWS cada nodo tiene su propia máquina y el límite pasa a ser la red, que es donde la cadena muestra su ventaja (el cliente envía cada bloque una sola vez). Esas mediciones son los experimentos E3 y E10. El trozo de 1 MiB rindió más que el de 64 KiB con y sin TLS, lo que respalda el valor por defecto de D5. Entre una sesión y otra los valores absolutos cambian según la carga del computador, por eso cada comparación se hace dentro de la misma sesión.

## Qué cambió en el diseño gracias al prototipo

- **Un nodo caído no arrastra al anterior.** En la primera versión, si el último nodo no respondía, el del medio se quedaba esperando a poner trozos en una cola que nadie leía, y el primero terminaba dándolo también por caído. Quedaba 1 copia en vez de 2. Ahora cada envío espera a la vez a la cola y a la conexión, y si la conexión falla se deja de reenviar al instante.
- **La respuesta de la cadena se espera según su longitud.** Cada nodo espera la respuesta del siguiente con un plazo proporcional a los nodos que faltan, porque el siguiente puede estar esperando a su vez a los de atrás.
- **La confirmación decide el destino del temporal.** Si el nodo anterior corta la conexión mientras un nodo está confirmando su copia, la confirmación termina sola. Renombra el bloque si la huella coincide y lo borra si no, sin chocar con un borrado simultáneo.
- **El id del bloque se valida** contra el formato `b-` y 16 caracteres hexadecimales antes de usarlo como nombre de archivo, porque llega por la red (D8).
- **Puertos internos para el tráfico entre servidores.** gRPC para Python no permite pedir el certificado del cliente de forma opcional en un mismo puerto, así que el TLS mutuo entre servidores (D8) va por puertos propios (50053 y 50054) y la cadena entrega a los nodos siguientes con su puerto interno (docs/tls.md).
- **Conexiones por IP en las pruebas locales.** En Windows cada conexión nueva a `localhost` tarda unos 260 ms porque gRPC prueba primero IPv6. Con 127.0.0.1 tarda 1 ms. Los certificados de prueba incluyen esa IP, así que TLS sigue verificando la identidad. En AWS se usan los nombres de Route 53, y conviene reutilizar los canales en lugar de abrir uno por bloque.
