# Entorno local de desarrollo

`docker-compose.dev.yml` levanta DFSha en un solo computador para desarrollar y probar. No es el despliegue. En AWS cada servicio corre en su propia máquina con un Compose por rol (D10), porque una simulación en un solo equipo no basta como producto final.

## Qué levanta

| Servicio | Nombre en la red de Docker | Puerto en el equipo anfitrión |
|---|---|---|
| etcd1, etcd2 y etcd3 (clúster de 3 miembros, v3.5.35) | etcd1, etcd2, etcd3 | Ninguno |
| Nodo de datos 1 | dn1.dfsha.internal | 51052 (su puerto de clientes, 50052) |
| Nodo de datos 2 | dn2.dfsha.internal | 51053 |
| Nodo de datos 3 | dn3.dfsha.internal | 51054 |
| Nodo de datos 4 | dn4.dfsha.internal | 51055 |

- Cada nodo de datos usa la imagen de `deploy/docker/Dockerfile`, corre como un usuario sin privilegios y guarda sus bloques en un volumen propio.
- Dentro de la red cada nodo responde a su nombre de AWS, así los certificados se validan igual que en producción.
- Los nodos de datos usan TLS en el puerto de clientes y TLS mutuo en el interno (docs/tls.md). El puerto interno (50054) no se publica en el anfitrión, como lo cerrarán los grupos de seguridad en AWS.
- Los nodos de metadatos se agregan en la etapa 7. Entonces etcd pasa a usar TLS mutuo y los nodos de metadatos se suman a la red con sus nombres `mdsN.dfsha.internal`.

## Uso

Desde la raíz del repositorio. Los certificados se crean una vez y quedan en `deploy/certs/out/`, que no se sube al repositorio.

```
python deploy/certs/crear_certificados.py --local
docker compose -f deploy/compose/docker-compose.dev.yml up -d --build
python deploy/compose/prueba_humo.py
docker compose -f deploy/compose/docker-compose.dev.yml down -v
```

`--local` agrega `127.0.0.1` a los certificados, para que la prueba de humo pueda conectarse desde el anfitrión con TLS. `down -v` borra también los volúmenes con los bloques.

## Prueba de humo

`prueba_humo.py` comprueba que el entorno funciona de punta a punta.
1. Escribe un bloque de 16 MiB en dn1 con la cadena dn1, dn2 y dn3. Dentro de la red, dn1 lo reenvía a dn2 y dn3 por sus puertos internos con TLS mutuo.
2. Lee el bloque desde los tres nodos y compara su huella.
3. Comprueba que dn4, que no estaba en la cadena, no tiene el bloque.
4. Comprueba que los puertos internos no se alcanzan desde el anfitrión.

Resultado esperado.

```
Escritura de 16 MiB por la cadena: copias en ['dn1.dfsha.internal', 'dn2.dfsha.internal', 'dn3.dfsha.internal']
Lectura desde dn1: idéntica
Lectura desde dn2: idéntica
Lectura desde dn3: idéntica
dn4, que no estaba en la cadena, tiene el bloque: False
Puerto interno 50054 alcanzable desde el anfitrión: False
Puerto interno 50053 alcanzable desde el anfitrión: False

RESULTADO: correcto
```
