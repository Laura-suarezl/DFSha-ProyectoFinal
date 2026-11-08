# Variables y funciones comunes de los scripts de AWS (D10, deploy/aws/README.md).
# Se cargan con: source "$(dirname "$0")/comun.sh"
set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAIZ="$(cd "$AQUI/../.." && pwd)"
OUT="$AQUI/out"
ESTADO="$OUT/estado.env"
mkdir -p "$OUT"

AWS_BIN="${AWS_BIN:-aws}"
export MSYS_NO_PATHCONV=1            # Git Bash en Windows: no reescribir argumentos que empiezan con /
aws_cmd() { "$AWS_BIN" "$@" | tr -d '\r'; }   # aws.exe en Windows termina las líneas con CR
AWS=aws_cmd
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export AWS_PAGER=""
PROYECTO="dfsha"
DOMINIO="dfsha.internal"
TIPO="${TIPO:-t3.small}"
DISCO_GB="${DISCO_GB:-20}"
LLAVE="dfsha-llave"
PEM="${PEM:-$HOME/.ssh/$LLAVE.pem}"

VPC_CIDR="10.20.0.0/16"
# subred -> "cidr zona"
declare -A SUBREDES=(
  [publica]="10.20.0.0/24 ${AWS_DEFAULT_REGION}a"
  [privada-a]="10.20.1.0/24 ${AWS_DEFAULT_REGION}a"
  [privada-b]="10.20.2.0/24 ${AWS_DEFAULT_REGION}b"
  [privada-c]="10.20.3.0/24 ${AWS_DEFAULT_REGION}c"
)
# nodo -> "rol subred ip"   (IPs fijas, así los registros de Route 53 existen antes que las máquinas)
declare -A NODOS=(
  [mds1]="metadatos privada-a 10.20.1.11"
  [mds2]="metadatos privada-b 10.20.2.11"
  [mds3]="metadatos privada-c 10.20.3.11"
  [dn1]="datos privada-a 10.20.1.21"
  [dn2]="datos privada-b 10.20.2.21"
  [dn3]="datos privada-c 10.20.3.21"
  [dn4]="datos privada-a 10.20.1.22"
  [cliente1]="cliente publica 10.20.0.31"
  [cliente2]="cliente privada-b 10.20.2.31"
)
ORDEN_NODOS="mds1 mds2 mds3 dn1 dn2 dn3 dn4 cliente1 cliente2"

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" >&2; }   # a stderr, así no se mezcla con valores capturados

guardar() {   # guardar CLAVE valor
  touch "$ESTADO"
  grep -v "^$1=" "$ESTADO" > "$ESTADO.tmp" || true
  echo "$1=$2" >> "$ESTADO.tmp"
  mv "$ESTADO.tmp" "$ESTADO"
}

leer() { [ -f "$ESTADO" ] && grep "^$1=" "$ESTADO" | tail -1 | cut -d= -f2- || true; }

etiquetas() { echo "ResourceType=$1,Tags=[{Key=Name,Value=$PROYECTO-$2},{Key=Proyecto,Value=$PROYECTO}]"; }
