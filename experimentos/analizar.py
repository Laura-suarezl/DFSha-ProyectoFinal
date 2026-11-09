"""Analiza experimentos/resultados/resultados.json y escribe experimentos/resultados/resumen.md.

Uso:  python experimentos/analizar.py
Cada punto de tiempo tiene una repetición de calentamiento y 3 medidas (ajuste A14). Se reportan
media, desviación estándar, mínimo y máximo. Las latencias de metadatos (E11) tienen cientos de
muestras por punto, así que ahí se reportan p50 y p95.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

AQUI = Path(__file__).resolve().parent / "resultados"
R = json.loads((AQUI / "resultados.json").read_text(encoding="utf-8"))
lineas: list[str] = []


def res(nombre: str) -> dict | None:
    x = R.get(nombre)
    if not x:
        return None
    if "error" in x:
        lineas.append(f"**Nota:** el experimento {nombre} falló: {x['error'][:200]}\n")
        return None
    return x["resultado"]


def est(v: list[float]) -> str:
    if not v:
        return "sin datos"
    d = statistics.stdev(v) if len(v) > 1 else 0.0
    return f"{statistics.mean(v):.2f} ± {d:.2f} (mín {min(v):.2f}, máx {max(v):.2f})"


def mbs(mib: int, v: list[float]) -> str:
    return f"{mib / statistics.mean(v):.1f}" if v else "sin datos"


def transferencia(t: dict, mib: int | None = None) -> str:
    mib = mib or t["mib"]
    return f"{est(t['put_s'])} | {est(t['get_s'])} | {mbs(mib, t['put_s'])} | {mbs(mib, t['get_s'])} | {'sí' if t['integro'] else 'NO'}"


def titulo(t: str) -> None:
    lineas.append(f"\n## {t}\n")


lineas.append("# Resultados de los experimentos en AWS\n")
lineas.append("Despliegue de 9 EC2 t3.small en us-east-1 (3 zonas), R = 3 y W = 2, bloques de 16 MiB. La carga corre "
              "en las máquinas cliente y las fallas se inyectan con experimentos/ejecutar.py. Los tiempos están en "
              "segundos y el rendimiento en MiB/s. Cada punto tiene una repetición de calentamiento y 3 medidas.\n")

titulo("Correspondencia con las pruebas exigidas (5.2)")
lineas.append("| Prueba exigida | Experimentos |\n|---|---|")
for fila in [("Carga y recuperación", "E1"), ("Acceso RF3", "E0"), ("Concurrencia", "E7"),
             ("Replicación y fallos", "E5, E6 y kill -9 durante una lectura"),
             ("Consistencia", "E4, E8 y réplica desconectada (escenario 2)"), ("Seguridad", "E9"),
             ("Rendimiento", "E2, E3, E10, E11 y tamaño de trozo")]:
    lineas.append(f"| {fila[0]} | {fila[1]} |")

if e1 := res("e1"):
    titulo("E1. Carga y recuperación")
    lineas.append("| Tamaño | put (s) | get (s) | put MiB/s | get MiB/s | Hash idéntico | Bloques | Copias por bloque |\n|---|---|---|---|---|---|---|---|")
    for k, t in e1.items():
        if k == "reparto_1GiB":
            continue
        lineas.append(f"| {k} | {transferencia(t)} | {t['bloques']} | mín {min(t['copias_por_bloque'])}, máx {max(t['copias_por_bloque'])} |")

    if "reparto_1GiB" in e1:
        lineas.append("\nReparto de las copias de los 64 bloques del archivo de 1 GiB (RNF4): "
                      + ", ".join(f"{n} {c}" for n, c in e1["reparto_1GiB"].items()) + ".")

if e0 := res("e0"):
    titulo("E0. Acceso RF3")
    lineas.append("| Comprobación | Resultado |\n|---|---|")
    nombres = {"read_rango": "read de un rango que cruza los bloques 6 y 7", "lock": "lock del bloque 7 con token de cerco",
               "invisible_antes_de_close": "otro cliente no ve el write antes de close",
               "visible_tras_close": "otro cliente ve el write después de close", "bloque_6_intacto": "el bloque vecino no cambia",
               "append": "append de 30 bytes", "version_final": "versión final del archivo", "read_s": "tiempo del read (s)",
               "close_s": "tiempo del close con commit (s)"}
    for k, v in e0.items():
        valor = ("sí" if v else "NO") if isinstance(v, bool) else (f"{v:.3f}" if isinstance(v, float) else v)
        lineas.append(f"| {nombres.get(k, k)} | {valor} |")

if e7 := res("e7"):
    titulo("E7. Concurrencia (escritores repartidos entre los dos clientes)")
    lineas.append("En el mismo bloque, el lock serializa a los escritores. El que abrió antes del commit de otro recibe "
                  "VERSION_CONFLICT, vuelve a abrir y reintenta, así que ningún cambio se pierde. En bloques distintos no hay "
                  "conflictos y los escritores avanzan en paralelo.\n")
    lineas.append("| Caso | Escritores | Tiempo total (s) | Latencia por escritor (s) | Reintentos por conflicto | Cambios presentes al final |\n|---|---|---|---|---|---|")
    for k, x in e7.items():
        a, b = x["cliente1"], x["cliente2"]
        if "error" in a or "error" in b:
            lineas.append(f"| {k} | falló | | | | |")
            continue
        modo, n = k.split("_")
        lat = a["lat_s"] + b["lat_s"]
        v = x["verificacion"]
        lineas.append(f"| {'mismo bloque' if modo == 'mismo' else 'bloques distintos'} | {n} | {max(a['total_s'], b['total_s']):.2f} | "
                      f"{est(lat)} | {a['reintentos'] + b['reintentos']} | {v['presentes']} de {v['marcas']} |")

if e11 := res("e11"):
    titulo("E11. Metadatos con varios clientes (stat y ls)")
    lineas.append("| Clientes | Operaciones/s | p50 (ms) | p95 (ms) | Muestras |\n|---|---|---|---|---|")
    for x in e11.values():
        lineas.append(f"| {x['clientes']} | {x['ops_s']:.0f} | {x['p50_ms']:.1f} | {x['p95_ms']:.1f} | {x['muestras']} |")

base128 = (res("e1") or {}).get("128MiB")
for nombre, tit, filas in [("e2", "E2. Lectura y escritura en paralelo (128 MiB)", lambda r: r.items()),
                           ("trozo", "Tamaño de trozo del stream (128 MiB)", lambda r: r.items())]:
    if r := res(nombre):
        titulo(tit)
        lineas.append("| Caso | put (s) | get (s) | put MiB/s | get MiB/s | Hash idéntico |\n|---|---|---|---|---|---|")
        for k, t in filas(r):
            lineas.append(f"| {k} | {transferencia(t)} |")

if (e3 := res("e3")) and base128:
    titulo("E3. Factor de replicación (128 MiB)")
    lineas.append("| R | put (s) | get (s) | put MiB/s | get MiB/s | Hash idéntico |\n|---|---|---|---|---|---|")
    lineas.append(f"| 3 (E1) | {transferencia(base128)} |")
    lineas.append(f"| 2 | {transferencia(e3['R2'])} |")
    lineas.append("\nLa tercera copia casi no cuesta rendimiento: la cadena reenvía mientras recibe y el límite es la subida "
                  "del cliente al primer nodo. R = 3 da tolerancia a una falla más por casi el mismo tiempo.")
    lineas.append(f"\n{e3['nota']}.")

if (e4 := res("e4")) and base128:
    titulo("E4. Réplica lenta (dn2 con 200 ms más de latencia, 128 MiB)")
    lineas.append("| Caso | put (s) | get (s) | put MiB/s | get MiB/s | Hash idéntico |\n|---|---|---|---|---|---|")
    lineas.append(f"| Sin retraso (E1) | {transferencia(base128)} |")
    lineas.append(f"| dn2 +200 ms | {transferencia(e4['replica_dn2_mas_200ms'])} |")
    lineas.append("\n**Interpretación.** Con W = 2, un bloque cuenta como escrito cuando lo tienen los dos primeros nodos de "
                  "su cadena. La réplica lenta solo deja de afectar cuando queda tercera (A15). En unos dos tercios de las "
                  "cadenas dn2 queda entre las dos primeras, así que la escritura en cadena la tiene en el camino crítico. "
                  "Las lecturas también empeoran porque cliente2 está en la subred de dn2 y la cercanía se decide por subred, "
                  "no por la latencia medida. La diferencia con lo previsto en la Fase I (\"con W = 2 apenas cambia\") viene "
                  "de la escritura en cadena (D5). Las mejoras posibles son ordenar las réplicas por latencia medida y "
                  "dejar a los nodos lentos al final de la cadena.")

if e10 := res("e10"):
    titulo("E10. Costo del cifrado en el cliente (t3.small)")
    lineas.append(f"AES-256-GCM cifra a {e10['aes_gcm_mib_s']:.0f} MiB/s y SHA-256 calcula a {e10['sha256_mib_s']:.0f} MiB/s, "
                  f"así que un bloque de 16 MiB se cifra en {16000 / e10['aes_gcm_mib_s']:.0f} ms y sus dos huellas en "
                  f"{2 * 16000 / e10['sha256_mib_s']:.0f} ms.")
    if base128:
        lineas.append(f" Con put de 128 MiB a {mbs(128, base128['put_s'])} MiB/s, el cifrado y las huellas son una fracción "
                      f"pequeña del tiempo, que domina la red y la escritura en cadena.")

if e9 := res("e9"):
    titulo("E9. Seguridad")
    lineas.append("| Caso | Resultado |\n|---|---|")
    for k in ("token_valido", "sin_token", "token_alterado", "otro_usuario", "sin_jwt"):
        lineas.append(f"| {k.replace('_', ' ')} | {e9.get(k)} |")
    lineas.append("\n| Nodo | Captura de tráfico (bytes, apariciones del texto en claro) | Disco (bloques, bloques con el texto en claro) |\n|---|---|---|")
    for n in sorted(e9.get("captura", {})):
        lineas.append(f"| {n} | {e9['captura'][n]} | {e9['disco'].get(n)} |")
    if e9.get("puertos_desde_cliente"):
        lineas.append("\nPuertos vistos desde la máquina cliente (los internos los cierran los grupos de seguridad): "
                      + ", ".join(x for x in e9["puertos_desde_cliente"] if x) + ".")

if e5 := res("e5"):
    titulo("E5. Caída de un nodo de datos")
    t = e5["4_nodos_get_tras_caida"]
    lineas.append(f"- **Con 4 nodos.** Cae {e5['4_nodos_caido']}. put y get de 128 MiB después de la caída en {est(t['put_s'])} s y "
                  f"{est(t['get_s'])} s, hash idéntico: {'sí' if t['integro'] else 'NO'}. Las copias vuelven a 3 en "
                  f"{e5['4_nodos_re_replicacion_s']} s (copias finales {e5['4_nodos_estado_final'].get('copias')}).")
    t = e5["3_nodos_get_y_put_tras_caida"]
    lineas.append(f"- **Con 3 nodos.** {e5['3_nodos_apagado_y_caido'][0]} apagado y cae {e5['3_nodos_apagado_y_caido'][1]}. put y get de "
                  f"64 MiB con 2 nodos vivos: hash idéntico {'sí' if t['integro'] else 'NO'}. Copias del archivo anterior: "
                  f"{e5['3_nodos_estado'].get('copias')} (sin un tercer nodo vivo no se puede volver a 3).")

if k9 := res("kill9_lectura"):
    titulo("kill -9 durante una lectura")
    t = k9["lectura_con_kill9"]
    if "error" in t:
        lineas.append(f"Falló: {t['error'][:200]}")
    else:
        lineas.append(f"Con kill -9 a {k9['nodo_matado']} a los 4 s, put y get de 1 GiB en {est(t['put_s'])} s y {est(t['get_s'])} s, "
                      f"hash idéntico: {'sí' if t['integro'] else 'NO'}.")

if e6 := res("e6"):
    titulo("E6. Caída del líder de metadatos")
    lineas.append(f"Con docker kill al líder ({e6['lider_caido']}), el mayor hueco sin respuesta de un cliente que hace stat cada "
                  f"50 ms fue de {e6['hueco_max_s']:.2f} s ({e6['fallos']} llamadas fallidas). Líderes vistos por el cliente: "
                  f"{', '.join(x.split('.')[0] for x in e6['lideres'])}. Al volver, el nodo caído recibió el registro de "
                  f"{e6.get('nodos_re_registrados_en_25s', '?')} de 4 nodos de datos en 25 s.")

if e2c := res("escenario2"):
    titulo("Réplica desconectada durante una escritura (escenario 2)")
    t = e2c["escritura"]
    lineas.append(f"Con un firewall que bloquea los puertos de datos de dn3, el put de 64 MiB terminó en {est(t['put_s'])} s con "
                  f"copias por bloque {t['copias_por_bloque']} (W = 2), hash idéntico: {'sí' if t['integro'] else 'NO'}. "
                  f"30 s después de quitar el bloqueo, copias {e2c['copias_tras_reconectar'].get('copias')}.")
    lineas.append("\n**Interpretación.** El firewall descarta los paquetes sin responder, así que el nodo anterior de la cadena "
                  "solo detecta la desconexión cuando vence su plazo de reenvío (10 s). Después el cliente pide otra cadena "
                  "sin dn3. La escritura es correcta pero lenta. Un plazo de reenvío más corto la aceleraría, a costa de más "
                  "falsas alarmas con la red cargada.")

if e8 := res("e8"):
    titulo("E8. Escritor muerto a mitad de un put")
    d = e8["despues"]
    intacto = d.get("tamano") == 64 * 1024 * 1024
    lineas.append(f"Antes: archivo de 64 MiB. Un escritor que reemplazaba el archivo por uno de 1 GiB murió con kill -9. "
                  f"Después: versión {d.get('version')}, tamaño {d.get('tamano')} bytes, copias {d.get('copias')}. "
                  + ("El archivo sigue en la versión anterior y la versión incompleta nunca se publicó."
                     if intacto else "**El archivo cambió: revisar si el escritor alcanzó a terminar antes de morir.**"))

if R.get("salud_final"):
    titulo("Estabilidad de los nodos durante la serie")
    lineas.append("| Nodo | Al empezar | Al terminar |\n|---|---|---|")
    for n, v in R["salud_final"].items():
        lineas.append(f"| {n} | {R.get('salud_inicial', {}).get(n, '')} | {v} |")

(AQUI / "resumen.md").write_text("\n".join(lineas) + "\n", encoding="utf-8", newline="\n")
print("resumen escrito en", AQUI / "resumen.md")
