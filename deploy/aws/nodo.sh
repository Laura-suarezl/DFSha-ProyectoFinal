#!/bin/bash
# Prepara o actualiza una máquina de DFSha. Corre en la EC2 como root, al arrancar (datos de usuario)
# y en cada redespliegue (redesplegar.sh).
#   nodo.sh <nodo> <rol> <bucket>      por ejemplo: nodo.sh dn2 datos dfsha-artefactos-123
# Todo sale del bucket privado por el endpoint de S3: no hace falta salida a internet (ajuste A13).
set -euo pipefail
NODO="$1"; ROL="$2"; BUCKET="$3"
export AWS_DEFAULT_REGION=us-east-1
S3="s3://$BUCKET"

token=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
ZONA=$(curl -s -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/placement/availability-zone)

if ! command -v docker > /dev/null; then
  dnf install -y docker                      # repositorios de Amazon Linux, alojados en S3
  systemctl enable --now docker
fi
mkdir -p /usr/local/lib/docker/cli-plugins /opt/dfsha/trabajo /etc/dfsha/certs /var/lib/dfsha /var/lib/etcd
aws s3 cp --quiet "$S3/docker-compose" /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

aws s3 cp --quiet "$S3/imagenes.tar.gz" /opt/dfsha/imagenes.tar.gz
gunzip -c /opt/dfsha/imagenes.tar.gz | docker load -q
rm -f /opt/dfsha/imagenes.tar.gz

aws s3 cp --quiet "$S3/certs/ca.crt" /etc/dfsha/certs/ca.crt
if [ "$ROL" != "cliente" ]; then
  aws s3 cp --quiet "$S3/certs/$NODO.crt" "/etc/dfsha/certs/$NODO.crt"
  aws s3 cp --quiet "$S3/certs/$NODO.key" "/etc/dfsha/certs/$NODO.key"
  chown 10001 "/etc/dfsha/certs/$NODO.key"   # el usuario sin privilegios de la imagen
  chmod 600 "/etc/dfsha/certs/$NODO.key"
fi
chown 10001 /var/lib/dfsha /opt/dfsha/trabajo

aws s3 cp --quiet "$S3/compose/$ROL.yml" /opt/dfsha/compose.yml
printf 'NODO=%s\nZONA=%s\n' "$NODO" "$ZONA" > /opt/dfsha/.env
cd /opt/dfsha
docker compose up -d --remove-orphans
echo "nodo $NODO ($ROL, $ZONA) listo"
