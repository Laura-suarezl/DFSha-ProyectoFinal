#!/usr/bin/env bash
# Ejecuta un comando en una máquina de DFSha por SSH, pasando por el bastión (cliente1).
#   ssh.sh mds1 "sudo docker ps"
#   ssh.sh cliente1                   (sesión interactiva en el bastión)
source "$(dirname "$0")/comun.sh"
n="$1"; shift
BASTION=$($AWS ec2 describe-instances --instance-ids "$(leer INST_cliente1)" \
          --query "Reservations[0].Instances[0].PublicIpAddress" --output text)
read -r _ _ ip <<< "${NODOS[$n]}"
OPC=(-i "$PEM" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=15)
if [ "$n" = "cliente1" ]; then
  ssh "${OPC[@]}" "ec2-user@$BASTION" "$@"
else
  ssh "${OPC[@]}" -o "ProxyCommand=ssh ${OPC[*]} -W %h:%p ec2-user@$BASTION" "ec2-user@$ip" "$@"
fi
