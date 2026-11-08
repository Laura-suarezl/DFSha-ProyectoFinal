# Entorno local de desarrollo

`docker-compose.dev.yml` levanta DFSha en un solo computador para desarrollar y probar. No es el despliegue. En AWS cada servicio corre en su propia máquina con un Compose por rol (D10), porque una simulación en un solo equipo no basta como producto final.

## Qué levanta

| Servicio | Nombre en la red de Docker | Puerto en el equipo anfitrión |
|---|---|---|
| etcd1, etcd2 y etcd3 (clúster de 3 miembros, v3.5.35) | etcd1, etcd2, etcd3 | Ninguno |
| Nodo de metadatos 1 | mds1.dfsha.internal, dfsha.internal | 51041 (su puerto de clientes, 50051) |
| Nodo de metadatos 2 | mds2.dfsha.internal, dfsha.internal | 51042 |
| Nodo de metadatos 3 | mds3.dfsha.internal, dfsha.internal | 51043 |
| Nodo de datos 1 | dn1.dfsha.internal | 51052 (su puerto de clientes, 50052) |
| Nodo de datos 2 | dn2.dfsha.internal | 51053 |
| Nodo de datos 3 | dn3.dfsha.internal | 51054 |
| Nodo de datos 4 | dn4.dfsha.internal | 51055 |

- Todos los nodos usan la imagen de `deploy/docker/Dockerfile` y corren como un usuario sin privilegios. Cada nodo de datos guarda sus bloques en un volumen propio.
- Cada nodo de metadatos comparte la red de su miembro de etcd (`network_mode: service:etcdN`), igual que en AWS, donde los dos corren en la misma máquina. Por eso los nombres `mdsN.dfsha.internal` y `dfsha.internal` están en los servicios de etcd, y `dfsha.internal:2379` resuelve a los tres miembros.
- Dentro de la red cada nodo responde a su nombre de AWS, así los certificados se validan igual que en producción.
- Los nodos de datos usan TLS en el puerto de clientes y TLS mutuo en el interno (docs/tls.md). El puerto interno (50054) no se publica en el anfitrión, como lo cerrarán los grupos de seguridad en AWS.
- Los nodos de metadatos atienden con TLS. etcd todavía va sin TLS y pasa a TLS mutuo más adelante en la etapa 7. Su puerto (2379) no se publica en el anfitrión.
- La clave maestra de desarrollo (`kek-dev.bin`) reemplaza a KMS en local y envuelve las claves del clúster (docs/metadatos.md).

## Uso

Desde la raíz del repositorio. Los certificados y la clave maestra de desarrollo se crean una vez y quedan en `deploy/certs/out/`, que no se sube al repositorio.

```
python deploy/certs/crear_certificados.py --local
python -m dfsha.metadata crear-clave-local deploy/certs/out/kek-dev.bin
docker compose -f deploy/compose/docker-compose.dev.yml up -d --build
python deploy/compose/prueba_humo.py
docker compose -f deploy/compose/docker-compose.dev.yml down -v
```

`--local` agrega `127.0.0.1` a los certificados, para que la prueba de humo pueda conectarse desde el anfitrión con TLS. `down -v` borra también los volúmenes con los bloques.

## Prueba de humo

`prueba_humo.py` comprueba que el entorno funciona de punta a punta.
1. Crea un administrador de prueba con contraseña aleatoria (`crear-admin` dentro de mds1), busca al líder de metadatos siguiendo las respuestas NOT_LEADER e inicia sesión.
2. Crea un usuario, comprueba que ese usuario no puede administrar y renueva la sesión.
3. Comprueba que los dos seguidores remiten al líder.
4. Escribe un bloque de 16 MiB en dn1 con la cadena dn1, dn2 y dn3. Dentro de la red, dn1 lo reenvía a dn2 y dn3 por sus puertos internos con TLS mutuo.
5. Lee el bloque desde los tres nodos y compara su huella.
6. Comprueba que dn4, que no estaba en la cadena, no tiene el bloque.
7. Comprueba que los puertos internos y el de etcd no se alcanzan desde el anfitrión.

Resultado esperado.

```
Líder de metadatos: mds3, sesión 37ada1bc1d96d70d iniciada por humo1dba05
Usuario ana5dda1c creado e inicia sesión: True
Un usuario sin rol administrador intenta crear usuarios: FORBIDDEN
Renew de la sesión: le quedan 30 s
mds1 responde: NOT_LEADER mds3.dfsha.internal
mds2 responde: NOT_LEADER mds3.dfsha.internal
Escritura de 16 MiB por la cadena: copias en ['dn1.dfsha.internal', 'dn2.dfsha.internal', 'dn3.dfsha.internal']
Lectura desde dn1: idéntica
Lectura desde dn2: idéntica
Lectura desde dn3: idéntica
dn4, que no estaba en la cadena, tiene el bloque: False
Puerto interno 50054 alcanzable desde el anfitrión: False
Puerto interno 50053 alcanzable desde el anfitrión: False
Puerto interno 2379 alcanzable desde el anfitrión: False

RESULTADO: correcto
```

El líder y los nombres generados cambian en cada corrida.

## Simular fallas

- **Parada ordenada del líder.** `docker compose -f deploy/compose/docker-compose.dev.yml stop mdsN`. El nodo renuncia y otro asume en milisegundos.
- **Caída abrupta del líder.** `docker compose -f deploy/compose/docker-compose.dev.yml kill mdsN`. Otro asume cuando vence el lease del líder, en unos 3 s.
- **Caída de una máquina de metadatos.** Se detienen juntos el nodo y su miembro de etcd (`stop mdsN etcdN`) y se arrancan en orden (`start etcdN mdsN`).

**Nota:** como mdsN comparte la red de etcdN, si se detiene y arranca solo etcdN, mdsN queda con la red anterior y sin conexión. `restart etcdN` sí reinicia también a mdsN. Para recuperar un mdsN en ese estado basta `docker compose -f deploy/compose/docker-compose.dev.yml up -d --force-recreate mdsN`.
