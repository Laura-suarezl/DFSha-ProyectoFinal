#!/usr/bin/env bash
# Borra todo lo que crearon los scripts de DFSha, según deploy/aws/out/estado.env. No toca los
# recursos propios del laboratorio (LabRole, LabInstanceProfile, vockey, la VPC por defecto).
source "$(dirname "$0")/comun.sh"
VPC=$(leer VPC)

ids=$(grep '^INST_' "$ESTADO" 2>/dev/null | cut -d= -f2 | tr '\n' ' ' || true)
if [ -n "${ids// }" ]; then
  $AWS ec2 terminate-instances --instance-ids $ids > /dev/null
  log "esperando a que terminen las instancias"
  $AWS ec2 wait instance-terminated --instance-ids $ids
fi
$AWS ec2 delete-key-pair --key-name "$LLAVE" 2>/dev/null || true
rm -f "$PEM"
BUCKET=$(leer BUCKET)
if [ -n "$BUCKET" ]; then
  $AWS s3 rm --quiet --recursive "s3://$BUCKET" || true
  $AWS s3api delete-bucket --bucket "$BUCKET" || true
fi
if [ -n "$(leer KMS)" ]; then
  $AWS kms delete-alias --alias-name "alias/$PROYECTO-kek" || true
  $AWS kms schedule-key-deletion --key-id "$(leer KMS)" --pending-window-in-days 7 > /dev/null || true
fi
ZONA=$(leer ZONA)
if [ -n "$ZONA" ]; then
  cambios=$($AWS route53 list-resource-record-sets --hosted-zone-id "$ZONA" \
    --query "ResourceRecordSets[?Type=='A'].{Action:'DELETE',ResourceRecordSet:@}" --output json)
  [ "$cambios" != "[]" ] && $AWS route53 change-resource-record-sets --hosted-zone-id "$ZONA" \
    --change-batch "{\"Changes\":$cambios}" > /dev/null
  $AWS route53 delete-hosted-zone --id "$ZONA" > /dev/null
fi
for ep in ENDPOINT_S3 ENDPOINT_KMS; do
  [ -n "$(leer $ep)" ] && $AWS ec2 delete-vpc-endpoints --vpc-endpoint-ids "$(leer $ep)" > /dev/null
done
if [ -n "$(leer ENDPOINT_KMS)" ]; then   # la interfaz de red del endpoint tarda en liberarse
  while [ -n "$($AWS ec2 describe-network-interfaces --filters Name=vpc-id,Values="$VPC" Name=interface-type,Values=vpc_endpoint --query 'NetworkInterfaces[].NetworkInterfaceId' --output text)" ]; do sleep 10; done
fi
for g in cliente metadatos datos endpoints; do   # primero se quitan las reglas que se referencian entre grupos
  sg=$(leer "SG_$g"); [ -z "$sg" ] && continue
  reglas=$($AWS ec2 describe-security-groups --group-ids "$sg" --query "SecurityGroups[0].IpPermissions" --output json)
  [ "$reglas" != "[]" ] && $AWS ec2 revoke-security-group-ingress --group-id "$sg" --ip-permissions "$reglas" > /dev/null
done
for g in cliente metadatos datos endpoints; do sg=$(leer "SG_$g"); [ -n "$sg" ] && $AWS ec2 delete-security-group --group-id "$sg"; done
for s in publica privada_a privada_b privada_c; do id=$(leer "SUBRED_$s"); [ -n "$id" ] && $AWS ec2 delete-subnet --subnet-id "$id"; done
for rt in RT_PUB RT_PRIV; do id=$(leer "$rt"); [ -n "$id" ] && $AWS ec2 delete-route-table --route-table-id "$id"; done
if [ -n "$(leer IGW)" ]; then
  $AWS ec2 detach-internet-gateway --internet-gateway-id "$(leer IGW)" --vpc-id "$VPC"
  $AWS ec2 delete-internet-gateway --internet-gateway-id "$(leer IGW)"
fi
[ -n "$VPC" ] && $AWS ec2 delete-vpc --vpc-id "$VPC"
mv "$ESTADO" "$ESTADO.borrado-$(date +%Y%m%d%H%M%S)"
log "todo borrado (la clave KMS queda programada para borrarse en 7 días, es lo mínimo que permite AWS)"
