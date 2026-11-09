# Arquitectura de DFSha

DFSha sigue la opción 1 del enunciado, cliente/servidor con un servicio distribuido entre servidores (S2S) dentro de una red privada. Hay dos planos.

- **Plano de control.** Tres nodos de metadatos, cada uno junto a un miembro de etcd. etcd replica el estado con Raft y el nodo que gana la elección es el líder, que atiende a los clientes (D1).
- **Plano de datos.** Cuatro nodos de datos guardan los bloques cifrados de 16 MiB, con tres copias por bloque escritas en cadena (D5). Los datos nunca pasan por los nodos de metadatos.

Los diagramas están escritos en Mermaid, que GitHub dibuja directamente. El bloque de código de cada diagrama es su fuente (D12). Las secuencias, los casos de uso y los escenarios están en [docs/diagramas/](diagramas/).

## Contexto (C4 nivel 1)

```mermaid
flowchart LR
    usuario(["Usuario<br/>[Persona]<br/>CLI dfsha"])
    app(["Aplicación<br/>[Sistema externo]<br/>biblioteca del cliente"])
    admin(["Administrador<br/>[Persona]<br/>usuarios, cuotas, nodos"])
    dfsha["DFSha<br/>[Sistema]<br/>Sistema de archivos distribuido<br/>con alta disponibilidad, rendimiento y seguridad"]
    kms["AWS KMS<br/>[Sistema externo]<br/>clave maestra"]
    r53["Route 53<br/>[Sistema externo]<br/>zona privada dfsha.internal"]
    usuario -->|"RF1 y RF2 por gRPC con TLS"| dfsha
    app -->|"RF3 por gRPC con TLS"| dfsha
    admin -->|"AdminService por gRPC con TLS"| dfsha
    dfsha -->|"envuelve claves del clúster y de archivo"| kms
    dfsha -->|"resuelve nombres de nodos"| r53
```

## Componentes (C4 nivel 2)

Las conexiones A a F son las del diseño de la Fase I, con los puertos de D9 y el ajuste A7.

```mermaid
flowchart TB
    subgraph cliente["Máquina cliente"]
        cli["CLI y biblioteca<br/>[dfsha.client]<br/>cifrado AES-GCM, huellas, paralelismo"]
    end
    subgraph control["Plano de control (3 máquinas, una por zona)"]
        mds["Nodo de metadatos<br/>[dfsha.metadata]<br/>MetadataService, AdminService, NodeService<br/>líder, locks, commits, recuperación"]
        etcd[("etcd v3.5<br/>[Raft]<br/>árbol, manifiestos, locks, sesiones")]
    end
    subgraph datos["Plano de datos (4 máquinas)"]
        dn1["Nodo de datos<br/>[dfsha.datanode]<br/>DataService, almacén de bloques"]
        dn2["Nodo de datos<br/>(siguiente de la cadena)"]
    end
    cli -->|"A. 50051 TLS<br/>sesión, Open, AllocateBlocks, Lock, Commit"| mds
    cli -->|"B. 50052 TLS<br/>WriteBlock y ReadBlock con token HMAC"| dn1
    dn1 -->|"C. 50054 TLS mutuo<br/>cadena y copias"| dn2
    dn1 -->|"D. 50053 TLS mutuo<br/>Register, latidos, BlockReceived, inventario"| mds
    mds -->|"E. 2379 TLS mutuo<br/>transacciones, leases, Lock, Election"| etcd
    etcd <-->|"F. 2380 TLS mutuo<br/>replicación Raft entre miembros"| etcd
```

| Componente | Responsabilidad | Código |
|---|---|---|
| Cliente | CLI (RF1 y RF2) y biblioteca (RF3). Descubre al líder, cifra cada bloque, lo envía en paralelo y verifica las huellas | `dfsha/client/` |
| Nodo de metadatos | Autenticación, espacio de nombres, apertura, locks, commit, ubicación de bloques, recuperación y administración. Solo el líder atiende | `dfsha/metadata/` |
| etcd | Estado persistente del plano de control con consenso Raft | imagen oficial v3.5.35 |
| Nodo de datos | Guarda bloques cifrados, verifica su huella, reenvía en cadena, avisa a los nodos de metadatos y ejecuta sus órdenes | `dfsha/datanode/` |

## Despliegue en AWS (D10)

```mermaid
flowchart TB
    internet(("Equipo de quien despliega"))
    subgraph vpc["VPC 10.20.0.0/16 (us-east-1)"]
        subgraph pub["Subred pública 10.20.0.0/24 (zona a)"]
            c1["cliente1<br/>bastión SSH y cliente"]
        end
        subgraph za["Subred privada a 10.20.1.0/24"]
            m1["mds1<br/>metadatos + etcd"]
            d1["dn1"]
            d4["dn4"]
            kmsep["endpoint de KMS"]
        end
        subgraph zb["Subred privada b 10.20.2.0/24"]
            m2["mds2<br/>metadatos + etcd"]
            d2["dn2"]
            c2["cliente2"]
        end
        subgraph zc["Subred privada c 10.20.3.0/24"]
            m3["mds3<br/>metadatos + etcd"]
            d3["dn3"]
        end
        s3ep["endpoint de S3<br/>(imágenes y certificados)"]
    end
    internet -->|"SSH solo desde la IP de quien despliega"| c1
    c1 -.->|"SSH"| za
    za -.-> s3ep
    zb -.-> s3ep
    zc -.-> s3ep
```

- **Máquinas.** 9 EC2 t3.small con Amazon Linux 2023, disco gp3 cifrado y LabInstanceProfile. Cada una corre un Docker Compose de su rol (`deploy/aws/compose/`).
- **Red.** Las subredes privadas no tienen salida a internet. Docker se instala desde los repositorios de Amazon Linux y las imágenes llegan de un bucket privado, ambos por el endpoint de S3. KMS se alcanza por su endpoint de interfaz (ajuste A13).
- **Nombres.** Zona privada de Route 53 `dfsha.internal`. El nombre lógico `dfsha.internal` resuelve a los 3 nodos de metadatos y cada nodo tiene su nombre (`mds1.dfsha.internal`, `dn1.dfsha.internal`). El cliente no conoce ninguna IP (RNF7, D7).
- **Grupos de seguridad.** Los clientes alcanzan solo 50051 (metadatos) y 50052 (datos). 50053, 50054, 2379 y 2380 solo se abren entre servidores, con TLS mutuo además.
- **Scripts.** `deploy/aws/` crea, despliega, actualiza y borra todo con AWS CLI ([deploy/aws/README.md](../deploy/aws/README.md)).

## Flujo de una escritura

1. El cliente abre el archivo en el líder (Open), que le entrega la clave del archivo y, en modo escritura, el lock del archivo entero.
2. Pide bloques nuevos (AllocateBlocks). El líder asigna ids y una cadena de 3 nodos de datos en zonas distintas, con un token de escritura por bloque.
3. El cliente cifra cada bloque con AES-256-GCM y lo envía en trozos de 1 MiB al primer nodo, que lo reenvía al segundo y este al tercero mientras lo reciben.
4. Cada nodo verifica la huella de su copia, la guarda y avisa a los 3 nodos de metadatos con BlockReceived. El bloque cuenta como escrito con 2 copias.
5. El cliente confirma con Commit. El líder comprueba los avisos, los locks y la versión base, y publica el manifiesto nuevo en una sola transacción de etcd. Ese es el único punto de commit.

La semántica y las garantías están en [docs/semantica.md](semantica.md), y cada decisión con su justificación en [docs/decisiones.md](decisiones.md).
