#!/usr/bin/env bash
# Crea las EC2 de DFSha (D10): Amazon Linux 2023, t3.small, disco gp3 cifrado, LabInstanceProfile,
# IP privada fija y grupo de seguridad según el rol. Al arrancar, cada máquina ejecuta nodo.sh.
#   crear_instancias.sh              las 9 máquinas
#   crear_instancias.sh mds1 dn1     solo esas
source "$(dirname "$0")/comun.sh"
BUCKET=$(leer BUCKET)
cd "$RAIZ"
AMI=$($AWS ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
      --query Parameter.Value --output text)

for n in ${*:-$ORDEN_NODOS}; do
  [ -n "$(leer "INST_$n")" ] && { log "$n ya existe"; continue; }
  read -r rol subred ip <<< "${NODOS[$n]}"
  case "$rol" in metadatos) sg=$(leer SG_metadatos) ;; datos) sg=$(leer SG_datos) ;; *) sg=$(leer SG_cliente) ;; esac
  datos_usuario="deploy/aws/out/arranque-$n.sh"
  cat > "$datos_usuario" <<EOF
#!/bin/bash
export AWS_DEFAULT_REGION=$AWS_DEFAULT_REGION
hostnamectl set-hostname $n.$DOMINIO
aws s3 cp s3://$BUCKET/nodo.sh /opt/nodo.sh && bash /opt/nodo.sh $n $rol $BUCKET > /var/log/dfsha-nodo.log 2>&1
EOF
  id=$($AWS ec2 run-instances --image-id "$AMI" --instance-type "$TIPO" --key-name "$LLAVE" \
       --subnet-id "$(leer "SUBRED_${subred//-/_}")" --private-ip-address "$ip" --security-group-ids "$sg" \
       --iam-instance-profile Name=LabInstanceProfile --user-data "file://$datos_usuario" \
       --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2 \
       --block-device-mappings "[{\"DeviceName\":\"/dev/xvda\",\"Ebs\":{\"VolumeSize\":$DISCO_GB,\"VolumeType\":\"gp3\",\"Encrypted\":true}}]" \
       --tag-specifications "$(etiquetas instance "$n")" "$(etiquetas volume "$n")" \
       --query "Instances[0].InstanceId" --output text)
  guardar "INST_$n" "$id"; log "$n $id ($rol, $ip)"
done
