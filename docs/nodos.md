# Nodos de datos y NodeService

Relación entre los nodos de datos y los 3 nodos de metadatos (D5, D6 y D8). El código está en `dfsha/datanode/latidos.py`, `dfsha/metadata/nodos.py` y `dfsha/common/tokens.py`.

## Ciclo de vida de un nodo de datos

1. Al arrancar se registra en los 3 nodos de metadatos por el puerto interno 50053 con TLS mutuo y envía su inventario. La respuesta trae la clave HMAC de los tokens de acceso.
2. Cada 3 s envía un latido a los tres. Solo la respuesta del líder trae órdenes de copiar o borrar bloques.
3. Al guardar o borrar un bloque avisa a los tres con BlockReceived o BlockDeleted.
4. Cada hora reenvía el inventario completo y cada día revisa la huella de todos sus bloques. Una copia dañada se borra y se avisa, y el líder ordena otra copia.
5. Si un nodo de metadatos reinició y no lo conoce, el nodo se registra de nuevo en él con su inventario.

## Estado en los nodos de metadatos

Cada nodo de metadatos guarda en memoria el estado de cada nodo de datos (vivo, sospechoso a los 10 s sin latido, muerto a los 30 s), qué bloques tiene cada uno y las órdenes pendientes. La cadena de un bloque nuevo se elige entre los nodos vivos, primero de zonas distintas y con más espacio libre. AdminService.ListNodes muestra este estado.

## Tokens de acceso por bloque

Cada token sirve para un bloque, una operación (leer o escribir) y 10 minutos, y va firmado con HMAC-SHA256. El nodo de datos lo verifica en el puerto de clientes sin consultar a nadie. En el puerto interno, otro nodo de datos se identifica con su certificado y no necesita token. Si el nodo atiende ya el máximo de peticiones simultáneas responde OVERLOADED.
