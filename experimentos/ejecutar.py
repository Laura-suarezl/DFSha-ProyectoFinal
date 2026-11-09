"""Ejecuta los experimentos de D11 sobre el despliegue de AWS y guarda los resultados.

Corre en el equipo de quien despliega. La carga corre en las máquinas cliente (experimentos/carga.py)
y las fallas se inyectan por SSH con deploy/aws/ssh.sh: docker kill y stop, kill -9, tc netem para
retrasar una réplica y una regla de firewall (nftables) para desconectarla sin apagarla.

Uso:  python experimentos/ejecutar.py [experimento ...]      (sin argumentos, todos)
Deja experimentos/resultados/resultados.json y, con analizar.py, experimentos/resultados/resumen.md.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
SSH = ["bash", str(RAIZ / "deploy" / "aws" / "ssh.sh")]
SALIDA = RAIZ / "experimentos" / "resultados"
SALIDA.mkdir(parents=True, exist_ok=True)
ARCHIVO = SALIDA / "resultados.json"
DATOS = ["dn1", "dn2", "dn3", "dn4"]
REPETICIONES = 3
resultados = json.loads(ARCHIVO.read_text(encoding="utf-8")) if ARCHIVO.exists() else {}


def ssh(nodo: str, comando: str, entrada: bytes | None = None, timeout: int = 900) -> str:
    for intento in range(3):                       # 255: falló la conexión SSH, no el comando
        r = subprocess.run(SSH + [nodo, comando], input=entrada, capture_output=True, timeout=timeout)
        if r.returncode != 255:
            break
        time.sleep(5)
    return r.stdout.decode(errors="replace") + r.stderr.decode(errors="replace")


def log(texto: str) -> None:
    print(time.strftime("%H:%M:%S"), texto, flush=True)


class Usuarios:
    ana = f"ana{secrets.token_hex(2)}"
    pedro = f"pedro{secrets.token_hex(2)}"
    clave = secrets.token_urlsafe(16)


def carga(cliente: str, argumentos: str, entorno: str = "", usuario: str | None = None, timeout: int = 1800) -> dict:
    env = f"-e DFSHA_USUARIO={usuario or Usuarios.ana} -e DFSHA_CONTRASENA={Usuarios.clave} {entorno}"
    salida = ssh(cliente, f"cd /opt/dfsha && sudo docker compose exec -T {env} cliente python /trabajo/carga.py {argumentos}",
                 timeout=timeout)
    for linea in reversed(salida.splitlines()):
        if linea.startswith("RESULTADO "):
            return json.loads(linea[len("RESULTADO "):])
    raise RuntimeError(f"sin resultado en {cliente}: {salida[-600:]}")


def en_paralelo(*tareas):
    """Ejecuta funciones sin argumentos en hilos y devuelve sus resultados en orden."""
    res = [None] * len(tareas)

    def correr(i, f):
        try:
            res[i] = f()
        except Exception as e:
            res[i] = {"error": str(e)[-300:]}
    hilos = [threading.Thread(target=correr, args=(i, f)) for i, f in enumerate(tareas)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    return res


def restaurar() -> None:
    """Deja los 9 servicios arriba y sin fallas inyectadas (lo llama main antes de cada experimento)."""
    for n in DATOS:
        ssh(n, "sudo tc qdisc del dev $(ip -o -4 route show to default | awk '{print $5}') root 2>/dev/null; "
               "sudo nft delete table inet dfsha 2>/dev/null; sudo docker start dfsha-datos-1 > /dev/null")
    for n in ("mds1", "mds2", "mds3"):
        ssh(n, "cd /opt/dfsha && sudo sed -i '/^REPLICAS=/d' .env && sudo docker compose up -d > /dev/null 2>&1")


def salud() -> dict:
    """Reinicios, memoria y muertes por falta de memoria de cada nodo (evidencia de estabilidad)."""
    r = {}
    for n in ("mds1", "mds2", "mds3", *DATOS):
        c = "dfsha-metadatos-1" if n.startswith("mds") else "dfsha-datos-1"
        r[n] = ssh(n, f"echo reinicios=$(sudo docker inspect {c} --format '{{{{.RestartCount}}}}') "
                      f"memoria=$(sudo docker stats --no-stream --format '{{{{.MemUsage}}}}' {c} | cut -d/ -f1) "
                      f"oom_desde_arranque=$(sudo dmesg | grep -ci 'oom-kill')").strip()
    return r


def lider() -> str:
    return carga("cliente2", "estado --ruta exp")["lider"]


def con_copia(ruta: str) -> str:
    """Primer nodo de datos con una copia del bloque 0 (el que el cliente lee primero)."""
    return carga("cliente2", f"estado --ruta {ruta}")["replicas"][0][0]


# ---------------------------------------------------------------- preparación
def preparar():
    for c in ("cliente1", "cliente2"):
        ssh(c, "sudo tee /opt/dfsha/trabajo/carga.py > /dev/null", (RAIZ / "experimentos" / "carga.py").read_bytes())
    for u in (Usuarios.ana, Usuarios.pedro):
        salida = ssh("mds1", f"sudo docker exec -e DFSHA_CONTRASENA={Usuarios.clave} dfsha-metadatos-1 python -m "
                             f"dfsha.metadata crear-admin --usuario {u}")
        if "creado" not in salida:
            raise RuntimeError(f"no se pudo crear el usuario {u}: {salida[-300:]}")
    for n in DATOS:      # herramientas para inyectar fallas, desde los repositorios de Amazon Linux por el endpoint de S3
        ssh(n, "sudo dnf install -y -q iproute-tc tcpdump nftables > /dev/null 2>&1; echo listo")
    carga("cliente2", "transferencia --mib 16 --repeticiones 0 --ruta exp/inicio.bin")


# ---------------------------------------------------------------- experimentos
def e1():
    r = {f"{m}MiB": carga("cliente2", f"transferencia --mib {m} --repeticiones {REPETICIONES}") for m in (16, 128, 1024)}
    reparto = {}                                      # RNF4: bloques del archivo de 1 GiB por nodo
    for replicas in carga("cliente2", "estado --ruta exp/1024MiB.bin")["replicas"]:
        for n in replicas:
            reparto[n] = reparto.get(n, 0) + 1
    r["reparto_1GiB"] = dict(sorted(reparto.items()))
    return r


def e0():
    return carga("cliente2", "rf3")


def e2():
    return {f"paralelismo_{p}": carga("cliente2", f"transferencia --mib 128 --repeticiones {REPETICIONES}",
                                      f"-e DFSHA_ALMACENAMIENTO_PARALELISMO={p}") for p in (1, 4, 8)}


def trozo():
    return {t: carga("cliente2", f"transferencia --mib 128 --repeticiones {REPETICIONES}",
                     f"-e DFSHA_ALMACENAMIENTO_TAMANO_TROZO={t}") for t in ("64KiB", "1MiB")}


def e10():
    return carga("cliente2", "cifrado")


def e7():
    carga("cliente2", "transferencia --mib 256 --repeticiones 0 --ruta exp/concurrencia.bin")
    r = {}
    for modo in ("distintos", "mismo"):
        for n in (2, 4, 8):
            a, b = en_paralelo(lambda: carga("cliente1", f"concurrencia --escritores {n} --modo {modo} --base 0 --prefijo a"),
                               lambda: carga("cliente2", f"concurrencia --escritores {n} --modo {modo} --base {n} --prefijo b"))
            v = carga("cliente2", f"verificar_marcas --modo {modo} --escritores {n}")
            r[f"{modo}_{2 * n}"] = {"cliente1": a, "cliente2": b, "verificacion": v}
            log(f"  {modo} {2 * n} escritores: {v}")
    return r


def e11():
    return {f"{n}_clientes": carga("cliente2", f"metadatos --clientes {n} --segundos 10") for n in (1, 5, 10, 20, 50)}


def e9():
    for n in DATOS:
        ssh(n, "sudo rm -f /tmp/c.pcap; sudo nohup timeout 60 tcpdump -i any -U -w /tmp/c.pcap 'port 50052 or port 50054' > /dev/null 2>&1 &")
    time.sleep(3)
    r = carga("cliente2", f"seguridad --otro {Usuarios.pedro} --otro-clave {Usuarios.clave}")
    time.sleep(3)
    r["captura"] = {n: ssh(n, "sudo pkill tcpdump; sleep 1; echo $(sudo stat -c %s /tmp/c.pcap) $(sudo grep -c 'secreto de ana' /tmp/c.pcap)").strip()
                    for n in DATOS}
    r["disco"] = {n: ssh(n, "echo $(sudo ls /var/lib/dfsha/blocks | wc -l) $(sudo grep -rl 'secreto de ana' /var/lib/dfsha/blocks | wc -l)").strip()
                  for n in DATOS}
    r["nota"] = "captura: bytes capturados y apariciones del texto en claro. disco: bloques en el nodo y bloques con el texto en claro"
    pruebas = "mds1.dfsha.internal:50053 mds1.dfsha.internal:2379 dn1.dfsha.internal:50054 dn1.dfsha.internal:50052 mds1.dfsha.internal:50051"
    r["puertos_desde_cliente"] = ssh("cliente2", f"for d in {pruebas}; do h=${{d%:*}}; p=${{d#*:}}; "
                                     "timeout 3 bash -c \"</dev/tcp/$h/$p\" 2>/dev/null && echo \"$d abierto\" || echo \"$d cerrado\"; done").split("\n")[:5]
    return r


def e5():
    r = {}
    carga("cliente2", "transferencia --mib 128 --repeticiones 0 --ruta exp/e5.bin")
    victima = con_copia("exp/e5.bin")
    r["4_nodos_caido"] = victima
    ssh(victima, "sudo docker kill dfsha-datos-1")
    t = time.time()
    r["4_nodos_get_tras_caida"] = carga("cliente2", "transferencia --mib 128 --repeticiones 1 --ruta exp/e5b.bin")
    while time.time() - t < 180:
        e = carga("cliente2", "estado --ruta exp/e5.bin")
        if e.get("copias") and min(e["copias"]) >= 3:
            break
        time.sleep(5)
    r["4_nodos_re_replicacion_s"] = round(time.time() - t, 1)
    r["4_nodos_estado_final"] = e
    ssh(victima, "sudo docker start dfsha-datos-1")
    time.sleep(15)
    # con 3 nodos: uno detenido de antes, luego cae otro que tiene copias
    apagado = next(n for n in DATOS if n != victima)
    ssh(apagado, "sudo docker stop dfsha-datos-1")
    time.sleep(40)
    carga("cliente2", "transferencia --mib 128 --repeticiones 0 --ruta exp/e5c.bin")
    segundo = con_copia("exp/e5c.bin")
    r["3_nodos_apagado_y_caido"] = [apagado, segundo]
    ssh(segundo, "sudo docker kill dfsha-datos-1")
    r["3_nodos_get_y_put_tras_caida"] = carga("cliente2", "transferencia --mib 64 --repeticiones 1 --ruta exp/e5d.bin")
    time.sleep(45)
    r["3_nodos_estado"] = carga("cliente2", "estado --ruta exp/e5c.bin")
    for n in (apagado, segundo):
        ssh(n, "sudo docker start dfsha-datos-1")
    time.sleep(20)
    return r


def kill9_lectura():
    base = carga("cliente2", "transferencia --mib 1024 --repeticiones 0 --ruta exp/grande.bin")
    victima = con_copia("exp/grande.bin")

    def matar():
        time.sleep(4)
        return ssh(victima, "sudo docker kill -s KILL dfsha-datos-1")
    lectura, muerte = en_paralelo(lambda: carga("cliente2", "transferencia --mib 1024 --repeticiones 1 --ruta exp/grande.bin"),
                                  matar)
    ssh(victima, "sudo docker start dfsha-datos-1")
    return {"lectura_con_kill9": lectura, "base": base, "nodo_matado": victima,
            "nota": "kill -9 al nodo de la primera réplica, a los 4 s de empezar put y get de 1 GiB"}


def e6():
    victima = lider()

    def matar():
        time.sleep(10)
        return ssh(victima, "sudo docker kill dfsha-metadatos-1")
    disp, _ = en_paralelo(lambda: carga("cliente2", "disponibilidad --segundos 40"), matar)
    ssh(victima, "sudo docker start dfsha-metadatos-1")
    time.sleep(25)                                    # los nodos de datos deben volver a registrarse solos
    registrados = ssh(victima, "sudo docker logs --since 25s dfsha-metadatos-1 2>&1 | "
                               "grep -o 'dn[0-9].dfsha.internal registrado' | sort -u | wc -l").strip()
    return {"lider_caido": victima, **disp, "nodos_re_registrados_en_25s": registrados}


def e4():
    dev = "$(ip -o -4 route show to default | awk '{print $5}')"
    ssh("dn2", f"sudo tc qdisc add dev {dev} root netem delay 200ms")
    try:
        r = carga("cliente2", f"transferencia --mib 128 --repeticiones {REPETICIONES} --ruta exp/e4.bin")
    finally:
        ssh("dn2", f"sudo tc qdisc del dev {dev} root")
    return {"replica_dn2_mas_200ms": r}


def escenario2():
    regla = ("sudo nft add table inet dfsha; sudo nft add chain inet dfsha entrada '{ type filter hook input priority 0; }'; "
             "sudo nft add rule inet dfsha entrada tcp dport '{ 50052, 50054 }' drop")
    ssh("dn3", regla)
    try:
        r = {"escritura": carga("cliente2", "transferencia --mib 64 --repeticiones 1 --ruta exp/e2.bin")}
    finally:
        ssh("dn3", "sudo nft delete table inet dfsha")
    time.sleep(30)
    r["copias_tras_reconectar"] = carga("cliente2", "estado --ruta exp/e2.bin")
    return r


def e8():
    v1 = carga("cliente2", "transferencia --mib 64 --repeticiones 0 --ruta exp/e8.bin")

    def matar():
        time.sleep(6)
        return ssh("cliente2", "sudo pkill -9 -f 'carga.py transferencia --mib 1024'")   # desde el anfitrión
    en_paralelo(lambda: carga("cliente2", "transferencia --mib 1024 --repeticiones 0 --ruta exp/e8.bin"), matar)
    time.sleep(35)                                    # la sesión del escritor muerto vence (30 s)
    return {"antes": v1, "despues": carga("cliente2", "estado --ruta exp/e8.bin"),
            "nota": "escritor matado con kill -9 a mitad del put de 1 GiB sobre un archivo de 64 MiB"}


def e3():
    for n in ("mds1", "mds2", "mds3"):
        ssh(n, "cd /opt/dfsha && echo REPLICAS=2 | sudo tee -a .env > /dev/null && sudo docker compose up -d metadatos")
    time.sleep(25)
    try:
        r2 = carga("cliente2", f"transferencia --mib 128 --repeticiones {REPETICIONES} --ruta exp/e3.bin")
    finally:
        for n in ("mds1", "mds2", "mds3"):
            ssh(n, "cd /opt/dfsha && sudo sed -i '/^REPLICAS=/d' .env && sudo docker compose up -d metadatos")
        time.sleep(25)
    return {"R2": r2, "nota": "R = 3 es la configuración de E1. R = 1 no se admite (validación R >= 2, D5), se compara R = 2"}


ORDEN = ["e10", "e1", "e0", "e2", "trozo", "e7", "e11", "e9", "e4", "escenario2", "e8", "kill9_lectura", "e5", "e6", "e3"]


def main() -> int:
    pedidos = sys.argv[1:] or ORDEN
    log("preparando máquinas y usuarios")
    restaurar()
    preparar()
    resultados["salud_inicial"] = salud()
    for nombre in pedidos:
        log(f"experimento {nombre}")
        restaurar()
        time.sleep(5)
        inicio = time.time()
        try:
            resultados[nombre] = {"resultado": globals()[nombre](), "duracion_s": round(time.time() - inicio)}
        except Exception as e:
            resultados[nombre] = {"error": str(e)[-500:]}
            log(f"  falló: {str(e)[-300:]}")
        ARCHIVO.write_text(json.dumps(resultados, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
    restaurar()
    time.sleep(10)
    resultados["salud_final"] = salud()
    ARCHIVO.write_text(json.dumps(resultados, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
    log("terminado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
