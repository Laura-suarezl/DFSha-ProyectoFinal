# TLS, CA interna y puertos

Todos los canales de DFSha usan TLS 1.3, y entre servidores el TLS es mutuo (D8). Cada nodo tiene un certificado emitido por una CA interna para su nombre DNS (D7), así que un cliente o un nodo sabe que habla con quien dice ser, y una máquina sin certificado de la CA no puede hacerse pasar por un nodo.

## Puertos

gRPC para Python no permite que un mismo puerto pida el certificado del cliente de forma opcional, o lo exige a todos o no lo pide a nadie (se comprobó con grpcio 1.85). Los clientes no tienen certificado, así que el tráfico entre servidores va por puertos propios.

| Puerto | Servicio | Quién se conecta | Seguridad |
|---|---|---|---|
| 50051 | MetadataService y AdminService | Clientes y administrador | TLS, el cliente verifica al servidor |
| 50052 | DataService | Clientes | TLS, el cliente verifica al servidor |
| 50053 | NodeService | Nodos de datos | TLS mutuo, solo certificados de nodos de datos |
| 50054 | DataService (cadena y copias de reposición) | Otros nodos de datos | TLS mutuo |
| 2379 y 2380 | etcd (clientes y pares) | Nodos de metadatos | TLS mutuo |

Los grupos de seguridad de AWS (D10) cierran los puertos internos a los clientes, así que un cliente ni siquiera llega a ellos. La cadena que entrega AllocateBlocks trae el primer nodo con su puerto de clientes y los siguientes con el interno, por eso el reenvío del bloque entre nodos va siempre por TLS mutuo.

## Certificados

Los crea `deploy/certs/crear_certificados.py` con la biblioteca `cryptography`.

| Certificado | Nombres que cubre | Uso |
|---|---|---|
| `ca.crt` y `ca.key` | CA interna de dfsha.internal | Firma los certificados de los nodos. Vigencia de 2 años |
| `mdsN.crt` y `mdsN.key` | `mdsN.dfsha.internal` y `dfsha.internal` | Nodo de metadatos y su miembro de etcd. Lleva el nombre lógico porque los clientes se conectan a `dfsha.internal` |
| `dnN.crt` y `dnN.key` | `dnN.dfsha.internal` | Nodo de datos |

- Las llaves son de curva elíptica P-256 y los certificados se firman con SHA-256.
- Los certificados de los nodos sirven como servidor y como cliente, porque entre servidores ambos lados presentan certificado. Vigencia de 1 año.
- Con `--local` se agregan el nombre corto (`mds1`, `dn1`), `localhost` y `127.0.0.1`, para Docker Compose y las pruebas en un solo computador.
- Si la CA ya existe en la carpeta de salida se reutiliza, así se pueden emitir certificados de nodos nuevos sin invalidar los anteriores.
- Las llaves privadas se guardan con permisos solo para el dueño y la carpeta de salida (`deploy/certs/out/`) está en `.gitignore`. Nunca se suben al repositorio.

```
python deploy/certs/crear_certificados.py                       # 3 nodos de metadatos y 4 de datos
python deploy/certs/crear_certificados.py --local               # además para pruebas locales
python deploy/certs/crear_certificados.py --datos 5              # agrega dn5 con la misma CA
```

## Configuración en cada nodo

Cada servidor recibe la CA, su certificado y su llave en la sección `[tls]` de la configuración (docs/configuracion.md). El cliente solo necesita la CA.

```
DFSHA_TLS_CA=/etc/dfsha/certs/ca.crt
DFSHA_TLS_CERTIFICADO=/etc/dfsha/certs/dn2.crt
DFSHA_TLS_LLAVE=/etc/dfsha/certs/dn2.key
```

En el código, `dfsha/common/tls.py` arma las credenciales de gRPC.
- **`credenciales_servidor(cfg.tls)`.** Puerto de clientes, con TLS.
- **`credenciales_servidor(cfg.tls, exigir_certificado_cliente=True)`.** Puerto interno, con TLS mutuo.
- **`credenciales_canal(cfg.tls)`.** Canal de un cliente, que verifica al servidor con la CA.
- **`credenciales_canal(cfg.tls, con_certificado=True)`.** Canal entre servidores, que presenta el certificado del nodo.
- **`identidad_par(context)` y `es_nodo(...)`.** Dicen qué nodo está al otro lado de un puerto interno, por ejemplo para que NodeService solo acepte nodos de datos.

Para desarrollar sin certificados existe `DFSHA_TLS_HABILITADO=false`, que deja los dos puertos sin cifrar. No se usa en AWS.

## Qué se comprobó

Las pruebas de `tests/test_tls.py` y el escenario 6 del prototipo de `prototipos/cadena-datanodes` comprueban lo siguiente.
- El puerto de clientes acepta a un cliente sin certificado y rechaza una conexión sin TLS.
- El puerto interno rechaza a quien no presenta certificado y a quien lo trae de otra CA, y acepta a un nodo con certificado de la CA, identificándolo por su nombre.
- El cliente rechaza a un servidor cuyo certificado no es de la CA.
- Se negocia TLS 1.3.
- La cadena de 3 nodos funciona completa sobre TLS, con el reenvío por los puertos internos.
