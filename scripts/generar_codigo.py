"""Genera el código Python de todos los .proto de proto/ dentro de dfsha/_gen/.

El código generado no se versiona (está en .gitignore): cada integrante lo regenera con este
script después de clonar o de cambiar un .proto.

protoc escribe los import como si cada .proto estuviera en la raíz (por ejemplo
"from etcd.api.mvccpb import kv_pb2" o "import dfsha_pb2"). Este script los reescribe para
que apunten al paquete dfsha._gen y el código se pueda importar desde cualquier lugar.

Uso:  python scripts/generar_codigo.py
Requiere las dependencias de desarrollo: pip install -e ".[dev]"
"""
import re
import shutil
import sys
from pathlib import Path

from grpc_tools import protoc

RAIZ = Path(__file__).resolve().parent.parent
PROTO = RAIZ / "proto"
SALIDA = RAIZ / "dfsha" / "_gen"
PAQUETE = "dfsha._gen"


def reescribir_imports(archivo: Path, carpetas_raiz: set[str]) -> None:
    texto = archivo.read_text(encoding="utf-8")
    # from etcd.api.mvccpb import kv_pb2  ->  from dfsha._gen.etcd.api.mvccpb import kv_pb2
    for carpeta in carpetas_raiz:
        texto = re.sub(rf"^(\s*)from {carpeta}(\.| )", rf"\1from {PAQUETE}.{carpeta}\2", texto, flags=re.M)
    # import dfsha_pb2 as dfsha__pb2  ->  from dfsha._gen import dfsha_pb2 as dfsha__pb2
    texto = re.sub(r"^(\s*)import (\w+_pb2(?:_grpc)?)\b", rf"\1from {PAQUETE} import \2", texto, flags=re.M)
    archivo.write_text(texto, encoding="utf-8", newline="\n")


def main() -> int:
    protos = sorted(p.relative_to(PROTO).as_posix() for p in PROTO.rglob("*.proto"))
    if not protos:
        print("No hay archivos .proto en proto/")
        return 1
    if SALIDA.exists():
        shutil.rmtree(SALIDA)
    SALIDA.mkdir(parents=True)

    incluir_estandar = Path(protoc.__file__).parent / "_proto"   # google/protobuf/*.proto
    argumentos = [
        "grpc_tools.protoc",
        f"-I{PROTO}",
        f"-I{incluir_estandar}",
        f"--python_out={SALIDA}",
        f"--pyi_out={SALIDA}",
        f"--grpc_python_out={SALIDA}",
        *protos,
    ]
    codigo = protoc.main(argumentos)
    if codigo != 0:
        print("protoc falló")
        return codigo

    carpetas_raiz = {p.name for p in PROTO.iterdir() if p.is_dir()}
    for generado in list(SALIDA.rglob("*.py")) + list(SALIDA.rglob("*.pyi")):
        reescribir_imports(generado, carpetas_raiz)
    for carpeta in [SALIDA, *(d for d in SALIDA.rglob("*") if d.is_dir())]:
        (carpeta / "__init__.py").touch()

    print(f"Generados {len(protos)} .proto en {SALIDA.relative_to(RAIZ)}:")
    for p in protos:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
