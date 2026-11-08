"""Descarga los .proto oficiales de etcd y los deja listos para generar código Python.

Los .proto de etcd importan gogoproto/gogo.proto y google/api/annotations.proto. Esas
anotaciones solo afectan el código Go y la puerta HTTP de etcd, no el formato de los mensajes
en la red, así que se quitan. El resultado queda en proto/etcd/ con la misma ruta de importación
que usa etcd ("etcd/api/..."), para que los import entre archivos sigan funcionando.

Uso:  python scripts/vendor_etcd_protos.py
Volver a ejecutarlo produce exactamente los mismos archivos (salvo que cambie ETCD_TAG).
"""
import re
import sys
import urllib.request
from pathlib import Path

ETCD_TAG = "v3.5.35"
BASE_URL = f"https://raw.githubusercontent.com/etcd-io/etcd/{ETCD_TAG}"
RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "proto"

# ruta en el repositorio de etcd -> ruta dentro de proto/
ARCHIVOS = {
    "api/etcdserverpb/rpc.proto": "etcd/api/etcdserverpb/rpc.proto",
    "api/mvccpb/kv.proto": "etcd/api/mvccpb/kv.proto",
    "api/authpb/auth.proto": "etcd/api/authpb/auth.proto",
    "server/etcdserver/api/v3lock/v3lockpb/v3lock.proto": "etcd/api/v3lockpb/v3lock.proto",
    "server/etcdserver/api/v3election/v3electionpb/v3election.proto": "etcd/api/v3electionpb/v3election.proto",
}

IMPORTS_QUITADOS = ("gogoproto/gogo.proto", "google/api/annotations.proto")

ENCABEZADO = (
    "// Copia de {origen} de etcd {tag} (licencia Apache 2.0, https://github.com/etcd-io/etcd).\n"
    "// Generada por scripts/vendor_etcd_protos.py, sin las anotaciones de gogoproto ni de google.api,\n"
    "// que no cambian el formato de los mensajes. No editar a mano.\n\n"
)


def quitar_opciones(texto: str) -> str:
    """Quita las sentencias `option (...) = ...;`, de una línea o con bloque entre llaves."""
    salida, i = [], 0
    patron = re.compile(r"^[ \t]*option[ \t]*\(", re.M)
    while True:
        m = patron.search(texto, i)
        if not m:
            salida.append(texto[i:])
            break
        salida.append(texto[i:m.start()])
        j, profundidad = m.end(), 0
        while j < len(texto):
            c = texto[j]
            if c == "{":
                profundidad += 1
            elif c == "}":
                profundidad -= 1
            elif c == ";" and profundidad == 0:
                break
            j += 1
        if j >= len(texto):
            raise ValueError("sentencia option sin cerrar")
        # se consume también el salto de línea que sigue a la sentencia
        fin = j + 1
        if texto[fin:fin + 1] == "\n":
            fin += 1
        i = fin
    return "".join(salida)


def limpiar(texto: str) -> str:
    lineas = []
    for linea in texto.splitlines(keepends=True):
        s = linea.strip()
        if s.startswith("import") and any(f'"{imp}"' in s for imp in IMPORTS_QUITADOS):
            continue
        if s == "// for grpc-gateway":
            continue
        lineas.append(linea)
    texto = quitar_opciones("".join(lineas))
    # Las operaciones quedan con un bloque vacío entre llaves y se normalizan a "{}"
    texto = re.sub(r"\{\s*\}", "{}", texto)
    # Se quitan las líneas en blanco que dejaron las sentencias borradas
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    for imp in IMPORTS_QUITADOS:
        if imp in texto:
            raise ValueError(f"quedó una referencia a {imp}")
    if "gogoproto" in texto or "google.api" in texto:
        raise ValueError("quedaron anotaciones sin quitar")
    return texto


def main() -> int:
    for origen, destino in ARCHIVOS.items():
        url = f"{BASE_URL}/{origen}"
        with urllib.request.urlopen(url, timeout=30) as resp:
            original = resp.read().decode("utf-8")
        limpio = ENCABEZADO.format(origen=origen, tag=ETCD_TAG) + limpiar(original)
        ruta = DESTINO / destino
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(limpio, encoding="utf-8", newline="\n")
        print(f"{destino}  ({len(limpio.splitlines())} líneas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
