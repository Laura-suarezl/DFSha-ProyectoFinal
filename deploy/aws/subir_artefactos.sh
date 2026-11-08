#!/usr/bin/env bash
# Construye y sube al bucket privado todo lo que necesitan las máquinas (ajuste A13):
# las imágenes (DFSha y etcd), el binario de Docker Compose, los Compose por rol, nodo.sh y los
# certificados de los nodos con sus nombres de AWS. La llave de la CA nunca sale del equipo.
source "$(dirname "$0")/comun.sh"
BUCKET=$(leer BUCKET)
PYTHON="${PYTHON:-python}"
COMPOSE_VERSION="v2.29.7"
cd "$RAIZ"

"$PYTHON" deploy/certs/crear_certificados.py --salida deploy/certs/out/aws > /dev/null
log "certificados de AWS en deploy/certs/out/aws"

docker build -q -f deploy/docker/Dockerfile -t dfsha:dev . > /dev/null
docker image inspect quay.io/coreos/etcd:v3.5.35 > /dev/null 2>&1 || docker pull -q quay.io/coreos/etcd:v3.5.35
docker save dfsha:dev quay.io/coreos/etcd:v3.5.35 | gzip -1 > deploy/aws/out/imagenes.tar.gz
log "imágenes empaquetadas ($(du -h deploy/aws/out/imagenes.tar.gz | cut -f1))"
if [ ! -f deploy/aws/out/docker-compose ]; then
  curl -fsSL -o deploy/aws/out/docker-compose \
    "https://github.com/docker/compose/releases/download/$COMPOSE_VERSION/docker-compose-linux-x86_64"
fi

$AWS s3 cp --quiet deploy/aws/out/imagenes.tar.gz "s3://$BUCKET/imagenes.tar.gz"
$AWS s3 cp --quiet deploy/aws/out/docker-compose "s3://$BUCKET/docker-compose"
$AWS s3 cp --quiet deploy/aws/nodo.sh "s3://$BUCKET/nodo.sh"
for rol in metadatos datos cliente; do
  $AWS s3 cp --quiet "deploy/aws/compose/$rol.yml" "s3://$BUCKET/compose/$rol.yml"
done
$AWS s3 cp --quiet deploy/certs/out/aws/ca.crt "s3://$BUCKET/certs/ca.crt"
for n in mds1 mds2 mds3 dn1 dn2 dn3 dn4; do
  $AWS s3 cp --quiet "deploy/certs/out/aws/$n.crt" "s3://$BUCKET/certs/$n.crt"
  $AWS s3 cp --quiet "deploy/certs/out/aws/$n.key" "s3://$BUCKET/certs/$n.key"
done
log "artefactos subidos a s3://$BUCKET"
