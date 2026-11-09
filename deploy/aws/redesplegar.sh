#!/usr/bin/env bash
# Actualiza las máquinas con los artefactos del bucket (después de subir_artefactos.sh).
#   redesplegar.sh                 todas, primero metadatos, luego datos y clientes
#   redesplegar.sh dn1 dn2         solo esas
# Cada máquina se intenta hasta 3 veces. Si una falla, sigue con las demás y al final la informa.
source "$(dirname "$0")/comun.sh"
BUCKET=$(leer BUCKET)
fallidas=()
for n in ${*:-$ORDEN_NODOS}; do
  read -r rol _ _ <<< "${NODOS[$n]}"
  ok=""
  for intento in 1 2 3; do
    log "redesplegando $n (intento $intento)"
    if bash "$AQUI/ssh.sh" "$n" "sudo AWS_DEFAULT_REGION=$AWS_DEFAULT_REGION bash -c 'aws s3 cp --quiet s3://$BUCKET/nodo.sh /opt/nodo.sh && bash /opt/nodo.sh $n $rol $BUCKET'" | tail -1; then
      [ "${PIPESTATUS[0]}" = "0" ] && { ok=si; break; }
    fi
    sleep 10
  done
  [ -z "$ok" ] && fallidas+=("$n")
done
if [ ${#fallidas[@]} -gt 0 ]; then
  log "FALLARON: ${fallidas[*]}"
  exit 1
fi
log "todas las máquinas actualizadas"
