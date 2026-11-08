#!/usr/bin/env bash
# Actualiza las máquinas con los artefactos del bucket (después de subir_artefactos.sh).
#   redesplegar.sh                 todas, primero metadatos, luego datos y clientes
#   redesplegar.sh dn1 dn2         solo esas
source "$(dirname "$0")/comun.sh"
BUCKET=$(leer BUCKET)
for n in ${*:-$ORDEN_NODOS}; do
  read -r rol _ _ <<< "${NODOS[$n]}"
  log "redesplegando $n"
  bash "$AQUI/ssh.sh" "$n" "sudo AWS_DEFAULT_REGION=$AWS_DEFAULT_REGION bash -c 'aws s3 cp --quiet s3://$BUCKET/nodo.sh /opt/nodo.sh && bash /opt/nodo.sh $n $rol $BUCKET'"
done
