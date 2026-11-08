#!/usr/bin/env bash
# Crea la red y los servicios de apoyo de DFSha en AWS (D7, D8 y D10). Es idempotente: lo que ya
# existe (según deploy/aws/out/estado.env) no se vuelve a crear.
#   VPC con 3 subredes privadas en 3 zonas y 1 pública, puerta de enlace a internet solo para la
#   pública, endpoint de puerta de enlace de S3 y endpoint de interfaz de KMS (sin NAT, ajuste A13),
#   grupos de seguridad por rol,
#   zona privada de Route 53, clave KMS, bucket privado de artefactos y llave SSH.
source "$(dirname "$0")/comun.sh"

# ---------------------------------------------------------------- red
VPC=$(leer VPC)
if [ -z "$VPC" ]; then
  VPC=$($AWS ec2 create-vpc --cidr-block "$VPC_CIDR" --tag-specifications "$(etiquetas vpc vpc)" --query Vpc.VpcId --output text)
  $AWS ec2 modify-vpc-attribute --vpc-id "$VPC" --enable-dns-support
  $AWS ec2 modify-vpc-attribute --vpc-id "$VPC" --enable-dns-hostnames
  guardar VPC "$VPC"; log "VPC $VPC"
fi
for s in "${!SUBREDES[@]}"; do
  clave="SUBRED_${s//-/_}"
  [ -n "$(leer "$clave")" ] && continue
  read -r cidr zona <<< "${SUBREDES[$s]}"
  id=$($AWS ec2 create-subnet --vpc-id "$VPC" --cidr-block "$cidr" --availability-zone "$zona" \
       --tag-specifications "$(etiquetas subnet "$s")" --query Subnet.SubnetId --output text)
  guardar "$clave" "$id"; log "subred $s $id ($zona)"
done
IGW=$(leer IGW)
if [ -z "$IGW" ]; then
  IGW=$($AWS ec2 create-internet-gateway --tag-specifications "$(etiquetas internet-gateway igw)" --query InternetGateway.InternetGatewayId --output text)
  $AWS ec2 attach-internet-gateway --internet-gateway-id "$IGW" --vpc-id "$VPC"
  guardar IGW "$IGW"; log "puerta de enlace $IGW"
fi
RT_PUB=$(leer RT_PUB)
if [ -z "$RT_PUB" ]; then
  RT_PUB=$($AWS ec2 create-route-table --vpc-id "$VPC" --tag-specifications "$(etiquetas route-table publica)" --query RouteTable.RouteTableId --output text)
  $AWS ec2 create-route --route-table-id "$RT_PUB" --destination-cidr-block 0.0.0.0/0 --gateway-id "$IGW" > /dev/null
  $AWS ec2 associate-route-table --route-table-id "$RT_PUB" --subnet-id "$(leer SUBRED_publica)" > /dev/null
  $AWS ec2 modify-subnet-attribute --subnet-id "$(leer SUBRED_publica)" --map-public-ip-on-launch
  guardar RT_PUB "$RT_PUB"; log "rutas públicas $RT_PUB"
fi
RT_PRIV=$(leer RT_PRIV)
if [ -z "$RT_PRIV" ]; then
  RT_PRIV=$($AWS ec2 create-route-table --vpc-id "$VPC" --tag-specifications "$(etiquetas route-table privada)" --query RouteTable.RouteTableId --output text)
  for s in privada_a privada_b privada_c; do
    $AWS ec2 associate-route-table --route-table-id "$RT_PRIV" --subnet-id "$(leer "SUBRED_$s")" > /dev/null
  done
  guardar RT_PRIV "$RT_PRIV"; log "rutas privadas $RT_PRIV (sin salida a internet)"
fi
if [ -z "$(leer ENDPOINT_S3)" ]; then
  id=$($AWS ec2 create-vpc-endpoint --vpc-id "$VPC" --vpc-endpoint-type Gateway \
       --service-name "com.amazonaws.$AWS_DEFAULT_REGION.s3" --route-table-ids "$RT_PRIV" "$RT_PUB" \
       --tag-specifications "$(etiquetas vpc-endpoint s3)" --query VpcEndpoint.VpcEndpointId --output text)
  guardar ENDPOINT_S3 "$id"; log "endpoint de S3 $id"
fi

# ---------------------------------------------------------------- grupos de seguridad
grupo() {   # grupo nombre descripción
  local clave="SG_$1" id
  id=$(leer "$clave")
  if [ -z "$id" ]; then
    id=$($AWS ec2 create-security-group --vpc-id "$VPC" --group-name "$PROYECTO-$1" --description "$2" \
         --tag-specifications "$(etiquetas security-group "$1")" --query GroupId --output text)
    guardar "$clave" "$id"; log "grupo $1 $id"
  fi
  echo "$id"
}
SG_CLI=$(grupo cliente "DFSha clientes y bastion")
SG_MDS=$(grupo metadatos "DFSha nodos de metadatos y etcd")
SG_DN=$(grupo datos "DFSha nodos de datos")
SG_EP=$(grupo endpoints "DFSha endpoints de servicios de AWS")
abrir() {   # abrir grupo puerto origen(grupo o cidr)
  local origen
  if [[ "$3" == sg-* ]]; then origen="UserIdGroupPairs=[{GroupId=$3}]"; else origen="IpRanges=[{CidrIp=$3}]"; fi
  $AWS ec2 authorize-security-group-ingress --group-id "$1" \
    --ip-permissions "IpProtocol=tcp,FromPort=$2,ToPort=$2,$origen" > /dev/null 2>&1 || true
}
if [ -z "$(leer REGLAS)" ]; then
  MI_IP=$(curl -s https://checkip.amazonaws.com | tr -d '\r\n')
  abrir "$SG_CLI" 22 "$MI_IP/32"         # SSH al bastión solo desde el equipo de quien despliega
  abrir "$SG_CLI" 22 "$SG_CLI"
  for g in "$SG_MDS" "$SG_DN"; do abrir "$g" 22 "$SG_CLI"; done
  abrir "$SG_MDS" 50051 "$SG_CLI"        # clientes -> metadatos (TLS)
  abrir "$SG_MDS" 50053 "$SG_DN"         # nodos de datos -> NodeService (TLS mutuo)
  abrir "$SG_MDS" 2379 "$SG_MDS"         # etcd entre nodos de metadatos (TLS mutuo)
  abrir "$SG_MDS" 2380 "$SG_MDS"
  abrir "$SG_DN" 50052 "$SG_CLI"         # clientes -> nodos de datos (TLS)
  abrir "$SG_DN" 50054 "$SG_DN"          # cadena y copias entre nodos de datos (TLS mutuo)
  abrir "$SG_EP" 443 "$VPC_CIDR"         # KMS desde la VPC
  guardar REGLAS "$MI_IP"; log "reglas de entrada creadas (SSH desde $MI_IP)"
fi

# Los nodos de metadatos usan KMS al arrancar (D8). Con un endpoint de interfaz y DNS privado,
# kms.us-east-1.amazonaws.com resuelve a una IP de la VPC y no hace falta salida a internet.
if [ -z "$(leer ENDPOINT_KMS)" ]; then
  id=$($AWS ec2 create-vpc-endpoint --vpc-id "$VPC" --vpc-endpoint-type Interface        --service-name "com.amazonaws.$AWS_DEFAULT_REGION.kms" --subnet-ids "$(leer SUBRED_privada_a)"        --security-group-ids "$SG_EP" --private-dns-enabled        --tag-specifications "$(etiquetas vpc-endpoint kms)" --query VpcEndpoint.VpcEndpointId --output text)
  guardar ENDPOINT_KMS "$id"; log "endpoint de KMS $id"
fi

# ---------------------------------------------------------------- Route 53 (D7)
ZONA=$(leer ZONA)
if [ -z "$ZONA" ]; then
  ZONA=$($AWS route53 create-hosted-zone --name "$DOMINIO" --caller-reference "$PROYECTO-$(date +%s)" \
         --vpc "VPCRegion=$AWS_DEFAULT_REGION,VPCId=$VPC" --hosted-zone-config PrivateZone=true \
         --query HostedZone.Id --output text | sed 's|/hostedzone/||')
  guardar ZONA "$ZONA"; log "zona privada $DOMINIO $ZONA"
fi
if [ -z "$(leer REGISTROS)" ]; then
  cambios=""
  for n in $ORDEN_NODOS; do
    read -r _ _ ip <<< "${NODOS[$n]}"
    cambios+="{\"Action\":\"UPSERT\",\"ResourceRecordSet\":{\"Name\":\"$n.$DOMINIO\",\"Type\":\"A\",\"TTL\":60,\"ResourceRecords\":[{\"Value\":\"$ip\"}]}},"
  done
  # el nombre lógico resuelve a los 3 nodos de metadatos
  cambios+="{\"Action\":\"UPSERT\",\"ResourceRecordSet\":{\"Name\":\"$DOMINIO\",\"Type\":\"A\",\"TTL\":10,\"ResourceRecords\":[{\"Value\":\"10.20.1.11\"},{\"Value\":\"10.20.2.11\"},{\"Value\":\"10.20.3.11\"}]}}"
  $AWS route53 change-resource-record-sets --hosted-zone-id "$ZONA" --change-batch "{\"Changes\":[${cambios}]}" > /dev/null
  guardar REGISTROS si; log "registros de Route 53 creados"
fi

# ---------------------------------------------------------------- KMS, bucket y llave
if [ -z "$(leer KMS)" ]; then
  id=$($AWS kms create-key --description "DFSha clave maestra (KEK)" --tags TagKey=Proyecto,TagValue=$PROYECTO --query KeyMetadata.KeyId --output text)
  $AWS kms create-alias --alias-name "alias/$PROYECTO-kek" --target-key-id "$id"
  guardar KMS "$id"; log "clave KMS $id (alias/$PROYECTO-kek)"
fi
BUCKET=$(leer BUCKET)
if [ -z "$BUCKET" ]; then
  BUCKET="$PROYECTO-artefactos-$($AWS sts get-caller-identity --query Account --output text)"
  $AWS s3api create-bucket --bucket "$BUCKET" > /dev/null
  $AWS s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  $AWS s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  guardar BUCKET "$BUCKET"; log "bucket privado $BUCKET"
fi
if [ ! -f "$PEM" ]; then
  mkdir -p "$(dirname "$PEM")"
  $AWS ec2 create-key-pair --key-name "$LLAVE" --query KeyMaterial --output text > "$PEM"
  chmod 600 "$PEM"; log "llave SSH $LLAVE en $PEM"
fi
log "infraestructura lista"
