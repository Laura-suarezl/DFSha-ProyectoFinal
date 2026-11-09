# Despliegue en AWS

Scripts de AWS CLI que crean y despliegan DFSha en AWS Academy (D10 y ajuste A13). Se ejecutan con Bash desde la raíz del repositorio, con las credenciales del laboratorio en `~/.aws/credentials`. En Windows con Git Bash se indica la ruta de la CLI con `AWS_BIN="/c/Program Files/Amazon/AWSCLIV2/aws.exe"`.

| Script | Qué hace |
|---|---|
| `crear_infraestructura.sh` | VPC con 3 subredes privadas en 3 zonas y 1 pública, endpoints de S3 y KMS (sin NAT), grupos de seguridad por rol, zona privada de Route 53 `dfsha.internal`, clave KMS `alias/dfsha-kek`, bucket privado de artefactos y llave SSH (en `~/.ssh`, fuera del repositorio) |
| `subir_artefactos.sh` | Construye la imagen, empaqueta la de etcd, crea los certificados con los nombres de AWS y sube todo al bucket. La llave de la CA no sale del equipo |
| `crear_instancias.sh` | Crea las 9 EC2 (Amazon Linux 2023, t3.small, gp3 cifrado, LabInstanceProfile, IP privada fija). Al arrancar cada una ejecuta `nodo.sh` |
| `nodo.sh` | Corre en cada EC2. Instala Docker, descarga los artefactos del bucket y levanta el Compose de su rol (`compose/`) |
| `redesplegar.sh` | Vuelve a ejecutar `nodo.sh` en las máquinas indicadas, después de subir artefactos nuevos |
| `ssh.sh` | Ejecuta un comando en una máquina pasando por el bastión (cliente1) |
| `destruir.sh` | Borra todo lo creado. No toca los recursos propios del laboratorio |

Los ids de los recursos quedan en `deploy/aws/out/estado.env`, que no se sube al repositorio.

```
bash deploy/aws/crear_infraestructura.sh
bash deploy/aws/subir_artefactos.sh
bash deploy/aws/crear_instancias.sh
bash deploy/aws/ssh.sh mds1 "sudo docker exec -e DFSHA_CONTRASENA='<contraseña>' dfsha-metadatos-1 python -m dfsha.metadata crear-admin --usuario admin"
bash deploy/aws/ssh.sh cliente2 "cd /opt/dfsha && sudo docker compose exec -T -e DFSHA_CONTRASENA='<contraseña>' cliente dfsha login admin"
```

**Nota:** el laboratorio apaga las instancias al cerrar la sesión. Al volver, los contenedores arrancan solos. La IP pública del bastión cambia, y `ssh.sh` la consulta cada vez. Si cambia la IP pública de quien despliega, se borra la línea `REGLAS` de `estado.env` y se vuelve a ejecutar `crear_infraestructura.sh` para abrir el SSH a la IP nueva.
