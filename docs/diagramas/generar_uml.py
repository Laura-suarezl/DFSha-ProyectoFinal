"""Genera los diagramas UML de DFSha: casos de uso, secuencias S0 a S6 y escenarios 1 y 3 (D12).

Uso:  python docs/diagramas/generar_uml.py
Salida: archivos PNG en docs/diagramas/. Son los diagramas de la Fase I actualizados con lo
implementado (etcd, locks de etcd, commit, cadena con W = 2, recuperación y API real).
"""
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse, FancyArrowPatch, FancyBboxPatch, Rectangle

OUT = Path(__file__).resolve().parent
C_TXT = "#1A1A1A"
C_BORDE = "#37474F"
C_CU = "#E8F1FB"
C_NOTA = "#FFF8C4"
C_FRAG = "#5D4037"
C_SYNC = "#111111"      # petición, línea continua
C_REPLY = "#0D47A1"     # respuesta, línea punteada
C_SELF = "#6A1B9A"      # paso interno del participante
ANCHO = 120


def guardar(fig, nombre):
    fig.savefig(OUT / f"{nombre}.png", dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def lienzo(alto, w_in=17):
    fig, ax = plt.subplots(figsize=(w_in, w_in * alto / ANCHO))
    ax.set_xlim(0, ANCHO)
    ax.set_ylim(0, alto)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.patch.set_facecolor("white")
    return fig, ax


# ============================================================ casos de uso
def actor(ax, x, y, nombre):
    ax.add_patch(plt.Circle((x, y + 5.2), 1.3, fc="white", ec=C_TXT, lw=1.4, zorder=3))
    ax.plot([x, x], [y + 3.9, y + 0.9], color=C_TXT, lw=1.4)
    ax.plot([x - 2.1, x + 2.1], [y + 3.0, y + 3.0], color=C_TXT, lw=1.4)
    ax.plot([x, x - 1.7], [y + 0.9, y - 1.6], color=C_TXT, lw=1.4)
    ax.plot([x, x + 1.7], [y + 0.9, y - 1.6], color=C_TXT, lw=1.4)
    ax.text(x, y - 2.6, nombre, ha="center", va="top", fontsize=8.6, fontweight="bold")


def caso(ax, x, y, codigo, texto, w=21, h=6.2):
    ax.add_patch(Ellipse((x, y), w, h, fc=C_CU, ec=C_BORDE, lw=1.2, zorder=2))
    ax.text(x, y + 0.9, codigo, ha="center", va="center", fontsize=7.6, fontweight="bold", zorder=3)
    ax.text(x, y - 1.0, texto, ha="center", va="center", fontsize=7.4, zorder=3)


def dep(ax, a, b, texto, pos_txt, rad=0.0):
    """Relación «include» o «extend»: flecha punteada con punta abierta."""
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle="->", mutation_scale=12, color=C_TXT,
                                 lw=1.1, linestyle=(0, (4, 3)),
                                 connectionstyle=f"arc3,rad={rad}", zorder=4))
    ax.text(pos_txt[0], pos_txt[1], texto, ha="center", va="center", fontsize=6.8,
            style="italic", zorder=5, bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none"))


def diagrama_casos_de_uso():
    alto = 92
    fig, ax = lienzo(alto)
    ax.text(60, 91.5, "Diagrama de casos de uso de DFSha", ha="center", va="top",
            fontsize=14, fontweight="bold")
    ax.add_patch(Rectangle((20, 3), 81, 84, fc="#FCFCFC", ec=C_BORDE, lw=1.6, zorder=0))
    ax.text(22, 86, "Sistema DFSha", ha="left", va="top", fontsize=10.5, fontweight="bold")

    xL, xM, xR = 38, 66, 87
    usuario = [("CU02", "Navegar (ls, cd, stat)", 79), ("CU03", "Gestionar directorios", 72),
               ("CU04", "Subir archivo (put)", 65), ("CU05", "Descargar archivo (get)", 58),
               ("CU10", "Borrar archivo (rm)", 51), ("CU11", "Renombrar o mover (mv)", 44)]
    app = [("CU06", "Leer una parte", 30), ("CU07", "Modificar una parte", 23),
           ("CU08", "Anexar (append)", 16), ("CU09", "Bloquear (lock)", 9)]
    for c, t, y in usuario + app:
        caso(ax, xL, y, c, t)
    caso(ax, xL, 37, "CU01", "Iniciar sesión")
    caso(ax, xM, 44, "CU16", "Leer de otra réplica")
    admin = [("CU13", "Usuarios y cuotas", 79), ("CU12", "Ver y retirar nodos", 70)]
    tiempo = [("CU14", "Detectar falla y re-replicar", 30), ("CU15", "Recolectar basura", 22)]
    for c, t, y in admin + tiempo:
        caso(ax, xR, y, c, t, w=22)

    actor(ax, 8, 60, "Usuario")
    actor(ax, 8, 21, "Aplicación\ncliente")
    actor(ax, 112, 72, "Administrador")
    actor(ax, 112, 24, "Temporizador\n«cada 10 s»")

    borde_izq = xL - 10.5
    for _, _, y in usuario + [(0, 0, 37)]:
        ax.plot([10.5, borde_izq], [62.5, y], color=C_TXT, lw=1.0, zorder=1)
    for _, _, y in app + [(0, 0, 37)]:
        ax.plot([10.5, borde_izq], [23.5, y], color=C_TXT, lw=1.0, zorder=1)
    for _, _, y in admin:
        ax.plot([109.5, xR + 11], [74.5, y], color=C_TXT, lw=1.0, zorder=1)
    for _, _, y in tiempo:
        ax.plot([109.5, xR + 11], [26.5, y], color=C_TXT, lw=1.0, zorder=1)

    dep(ax, (xL + 10.5, 23), (xL + 10.5, 9.6), "«include»", (55.2, 16.5), rad=-0.55)
    dep(ax, (xL + 9.8, 15.0), (xL + 9.8, 10.2), "", (0, 0), rad=-0.6)
    ax.text(55.2, 14.6, "(CU07 y CU08)", ha="center", va="center", fontsize=6.4, style="italic")
    dep(ax, (xM - 10.5, 44.8), (xL + 10.3, 57.2), "«extend»", (52.2, 52.6))
    dep(ax, (xM - 10.5, 43.2), (xL + 10.3, 30.8), "«extend»", (52.2, 35.6))
    dep(ax, (xR, 66.9), (xR, 33.1), "«include»", (xR, 50))

    ax.add_patch(FancyBboxPatch((53, 69), 22, 13.5, boxstyle="round,pad=0,rounding_size=0.6",
                                fc=C_NOTA, ec="#9E9D24", lw=1.0, zorder=2))
    nota = ("Todos los casos de uso del Usuario y de la Aplicación «incluyen» CU01 (sesión "
            "válida). Esas flechas se omiten para no saturar. CU04 toma el lock del archivo "
            "entero, y CU10 responde LOCKED si alguien tiene locks sobre el archivo (D4).")
    ax.text(64, 75.75, textwrap.fill(nota, 34), ha="center", va="center", fontsize=6.9,
            zorder=3)
    ax.add_patch(FancyBboxPatch((56.5, 20), 18, 14, boxstyle="round,pad=0,rounding_size=0.6",
                                fc=C_NOTA, ec="#9E9D24", lw=1.0, zorder=2))
    nota2 = ("Retirar un nodo (CU12, Decommission) copia sus bloques a otros nodos antes de "
             "vaciarlo (CU14). Los nodos de metadatos y de datos son parte del sistema, no actores.")
    ax.text(65.5, 27, textwrap.fill(nota2, 27), ha="center", va="center", fontsize=6.7, zorder=3)
    guardar(fig, "casos_de_uso")


# ============================================================ secuencias
FS = 7.4          # tamaño de letra de los mensajes
LH = 1.5          # alto de una línea de texto


def _lineas(t):
    return t.count("\n") + 1


def _alto(paso):
    k = paso[0]
    if k == "m":
        return 3.1 + LH * (_lineas(paso[3]) - 1)
    if k == "s":
        return max(4.1, LH * _lineas(paso[2]) + 2.3)
    if k == "n":
        return LH * _lineas(paso[3]) + 2.4
    if k == "f":
        return 3.0
    if k == "e":
        return 2.6
    if k == "/":
        return 1.6
    raise ValueError(k)


def _numero(num):
    """Número del paso en negrita, seguido del texto normal."""
    return rf"$\mathbf{{{num}.}}$ "


def _leyenda(ax, y):
    x = 14
    ax.add_patch(FancyArrowPatch((x, y), (x + 6, y), arrowstyle="-|>", mutation_scale=10,
                                 color=C_SYNC, lw=1.3))
    ax.text(x + 7, y, "petición", va="center", fontsize=7.4)
    x = 34
    ax.add_patch(FancyArrowPatch((x, y), (x + 6, y), arrowstyle="->", mutation_scale=10,
                                 color=C_REPLY, lw=1.3, linestyle=(0, (5, 3))))
    ax.text(x + 7, y, "respuesta", va="center", fontsize=7.4, color=C_REPLY)
    x = 55
    ax.plot([x, x + 2.4, x + 2.4], [y + 0.8, y + 0.8, y - 0.8], color=C_SELF, lw=1.3)
    ax.add_patch(FancyArrowPatch((x + 2.4, y - 0.8), (x, y - 0.8), arrowstyle="-|>",
                                 mutation_scale=9, color=C_SELF, lw=1.3))
    ax.text(x + 3.6, y, "paso interno", va="center", fontsize=7.4, color=C_SELF)
    ax.add_patch(Rectangle((76, y - 0.9), 3.4, 1.8, fc="#EFEBE9", ec=C_FRAG, lw=1.0))
    ax.text(80.2, y, "loop (se repite), alt (alternativa), opt (opcional)", va="center",
            fontsize=7.4, color=C_FRAG)


def secuencia(nombre, titulo, participantes, pasos, subtitulo=None):
    n = len(participantes)
    wcab = min((ANCHO - 18) / (n - 1) * 0.86, 22)
    margen = max(9.0, wcab / 2 + 1.0)
    xs = [margen + i * (ANCHO - 2 * margen) / (n - 1) for i in range(n)]
    CH = 0.48           # ancho aproximado de un carácter, en unidades del lienzo

    def ajustar(texto, ancho, reservado=0):
        maxc = max(int(ancho / CH) - reservado, 12)
        lineas = texto.split("\n")
        if all(len(l) <= maxc for l in lineas):
            return texto
        return textwrap.fill(" ".join(l.strip() for l in lineas), maxc)

    pasos2 = []
    for p in pasos:
        if p[0] == "n":
            ancho = min(xs[p[2]] + 7, ANCHO - 1) - max(xs[p[1]] - 7, 1) - 6
            pasos2.append(("n", p[1], p[2], ajustar(p[3], ancho)))
        elif p[0] == "m":
            ancho = abs(xs[p[2]] - xs[p[1]]) - 2
            pasos2.append(("m", p[1], p[2], ajustar(p[3], ancho, reservado=3), p[4]))
        elif p[0] == "s":
            i = p[1]
            if i == n - 1:
                ancho = (xs[i] - 3.6) - xs[i - 1] - 1.2
            else:
                ancho = xs[i + 1] - (xs[i] + 3.6) - 1.2
            pasos2.append(("s", i, ajustar(p[2], ancho, reservado=3)))
        else:
            pasos2.append(p)
    pasos = pasos2

    cabecera = 20.5 if subtitulo else 18.0
    alto = sum(_alto(p) for p in pasos) + cabecera + 2
    fig, ax = lienzo(alto)
    ax.text(60, alto - 0.6, titulo, ha="center", va="top", fontsize=12.5, fontweight="bold")
    y_ley = alto - 4.4
    if subtitulo:
        ax.text(60, alto - 3.3, subtitulo, ha="center", va="top", fontsize=8.0, style="italic",
                color="#37474F")
        y_ley = alto - 6.9
    _leyenda(ax, y_ley)

    top_cab = y_ley - 2.2
    cab_h = 5.0
    for x, p in zip(xs, participantes):
        ax.add_patch(FancyBboxPatch((x - wcab / 2, top_cab - cab_h), wcab, cab_h,
                                    boxstyle="round,pad=0,rounding_size=0.5",
                                    fc="#ECEFF1", ec=C_BORDE, lw=1.2, zorder=3))
        ax.text(x, top_cab - cab_h / 2, p, ha="center", va="center", fontsize=7.9,
                fontweight="bold", zorder=4)
        ax.plot([x, x], [top_cab - cab_h, 1.0], color="#B0BEC5", lw=1.0, ls=(0, (4, 3)), zorder=0)

    y = top_cab - cab_h - 1.2
    num = 0
    pila = []
    caja_txt = dict(boxstyle="round,pad=0.12", fc="white", ec="none")
    for p in pasos:
        h = _alto(p)
        k = p[0]
        if k == "m":
            _, i, j, texto, tipo = p
            num += 1
            yl = y - h + 1.0
            reply = tipo == "r"
            col = C_REPLY if reply else C_SYNC
            ax.add_patch(FancyArrowPatch((xs[i], yl), (xs[j], yl),
                                         arrowstyle="->" if reply else "-|>",
                                         mutation_scale=11, color=col,
                                         lw=1.3 if reply else 1.2,
                                         linestyle=(0, (5, 3)) if reply else "-", zorder=5))
            ax.text((xs[i] + xs[j]) / 2, yl + 0.4, _numero(num) + texto, ha="center",
                    va="bottom", fontsize=FS, color=col, zorder=6, bbox=caja_txt,
                    linespacing=1.2)
        elif k == "s":
            _, i, texto = p
            num += 1
            yt = y - 1.0
            izq = i == n - 1
            d = -2.8 if izq else 2.8
            x0 = xs[i]
            ax.plot([x0, x0 + d, x0 + d], [yt, yt, yt - 1.8], color=C_SELF, lw=1.3, zorder=5)
            ax.add_patch(FancyArrowPatch((x0 + d, yt - 1.8), (x0, yt - 1.8), arrowstyle="-|>",
                                         mutation_scale=10, color=C_SELF, lw=1.3, zorder=5))
            ax.text(x0 + d * 1.25, yt - 0.9 - LH * (_lineas(texto) - 1) / 2,
                    _numero(num) + texto, ha="right" if izq else "left", va="center",
                    fontsize=FS, color=C_SELF, zorder=6, bbox=caja_txt, linespacing=1.2)
        elif k == "n":
            _, i, j, texto = p
            x0, x1 = max(xs[i] - 7, 1.0), min(xs[j] + 7, ANCHO - 1.0)
            ax.add_patch(FancyBboxPatch((x0, y - h + 0.5), x1 - x0, h - 0.9,
                                        boxstyle="round,pad=0,rounding_size=0.4",
                                        fc=C_NOTA, ec="#9E9D24", lw=1.0, zorder=5))
            ax.text((x0 + x1) / 2, y - h / 2 + 0.05, texto, ha="center", va="center",
                    fontsize=FS, zorder=6, linespacing=1.2)
        elif k == "f":
            _, tipo, cond, i, j = p
            pila.append(dict(top=y - 0.4, tipo=tipo, cond=cond, i=i, j=j, elses=[],
                             nivel=len(pila)))
        elif k == "e":
            pila[-1]["elses"].append((y - 0.6, p[1]))
        elif k == "/":
            f = pila.pop()
            m = 1.2 * f["nivel"]
            x0 = max(xs[f["i"]] - 7.5 + m, 0.8)
            x1 = min(xs[f["j"]] + 7.5 - m, ANCHO - 0.8)
            bot = y - 0.6
            ax.add_patch(Rectangle((x0, bot), x1 - x0, f["top"] - bot, fc="none", ec=C_FRAG,
                                   lw=1.1, zorder=2))
            ax.add_patch(Rectangle((x0, f["top"] - 2.1), 5.0, 2.1, fc="#EFEBE9", ec=C_FRAG,
                                   lw=1.0, zorder=3))
            ax.text(x0 + 2.5, f["top"] - 1.05, f["tipo"], ha="center", va="center",
                    fontsize=7.4, fontweight="bold", color=C_FRAG, zorder=4)
            ax.text(x0 + 5.8, f["top"] - 1.05, f["cond"], ha="left", va="center", fontsize=7.2,
                    color=C_FRAG, style="italic", zorder=4, bbox=caja_txt)
            for ye, cond in f["elses"]:
                ax.plot([x0, x1], [ye, ye], color=C_FRAG, lw=0.9, ls=(0, (3, 2)), zorder=2)
                ax.text(x0 + 0.8, ye - 1.0, cond, ha="left", va="center", fontsize=7.2,
                        color=C_FRAG, style="italic", zorder=4, bbox=caja_txt)
        y -= h
    guardar(fig, nombre)


def M(i, j, t):
    return ("m", i, j, t, "s")


def R(i, j, t):
    return ("m", i, j, t, "r")


def S(i, t):
    return ("s", i, t)


def N(i, j, t):
    return ("n", i, j, t)


def F(tipo, cond, i, j):
    return ("f", tipo, cond, i, j)


def E(cond):
    return ("e", cond)


FIN = ("/",)

# Historia única: S2 crea la versión 1 de /ana/videos/viaje.mp4, S3 la lee, S4 crea la 2 (bloque 7
# pasa de b-5e10 a b-9c02), el escenario 1 crea la 3 y la 4, S6 repone el bloque 1 y S5 lo borra.


def todas_las_secuencias():
    secuencia(
        "secuencia_s0_sesion", "S0. Iniciar sesión y encontrar el sistema (CU01)",
        ["Cliente de Ana\n(CLI o biblioteca)", "Route 53\n(zona privada)", "mds1\n(seguidor)",
         "mds2\n(líder)"],
        [
            M(0, 1, "¿qué direcciones tiene dfsha.internal?"),
            R(1, 0, "10.20.1.11, 10.20.2.11 y 10.20.3.11 (los 3 nodos de metadatos)"),
            M(0, 2, "Login(ana, contraseña)   [TLS, puerto 50051]"),
            R(2, 0, "NOT_LEADER, leader = mds2.dfsha.internal"),
            M(0, 3, "Login(ana, contraseña)"),
            S(3, "verifica la contraseña contra su\nhash Argon2id guardado en etcd"),
            S(3, "crea la sesión: un lease de etcd\nde 30 s con la clave de la sesión"),
            R(3, 0, "JWT firmado con Ed25519 (1 h), token de\nrenovación (1 día) e id de sesión"),
            S(0, "guarda en caché el líder, mds2"),
            F("loop", "[cada 10 s mientras la sesión esté abierta]", 0, 3),
            M(0, 3, "Renew(sesión) con el JWT"),
            R(3, 0, "ttl 30 s: locks y arriendos de la sesión renovados"),
            FIN,
            F("alt", "[contraseña incorrecta o usuario inexistente]", 0, 3),
            R(3, 0, "TOKEN_INVALID, el mismo mensaje en los dos casos"),
            E("[el JWT está por vencer]"),
            M(0, 3, "Refresh(token de renovación): JWT, sesión y token nuevos"),
            E("[mds2 deja de responder]"),
            S(0, "vuelve al paso 1: resuelve el nombre\ny busca al nuevo líder"),
            FIN,
            N(0, 3, "El JWT viaja en cada petición y cualquier nodo de metadatos lo verifica con la "
                    "clave pública. La contraseña nunca se guarda, solo su hash Argon2id. Renew renueva "
                    "de una vez todos los locks y arriendos de la sesión (D4 y D8). El cliente solo "
                    "conoce el nombre dfsha.internal, nunca una IP (RNF7). Es la primera mitad del "
                    "escenario 4."),
        ])

    secuencia(
        "secuencia_s1_directorios", "S1. Navegar y crear directorios (CU02, CU03)",
        ["Cliente de Ana", "Nodo de metadatos\nlíder", "etcd\n(3 miembros, Raft)"],
        [
            M(0, 1, "Mkdir(/ana/videos) con el JWT"),
            S(1, "verifica el JWT, normaliza la ruta,\ncomprueba que esté dentro de /ana\ny que exista /ana"),
            M(1, 2, "transacción: crea la entrada si no existe\ny si este nodo sigue siendo el líder"),
            R(2, 1, "confirmada por 2 de los 3 miembros"),
            R(1, 0, "OK"),
            M(0, 1, "List(/ana)"),
            R(1, 0, "entradas: videos (directorio)"),
            M(0, 1, "Stat(/ana/videos), lo pide cd videos"),
            R(1, 0, "tipo directorio"),
            S(0, "cd guarda /ana/videos como directorio\nactual, solo en el cliente"),
            F("alt", "[si algo falla]", 0, 2),
            R(1, 0, "EXISTS, FORBIDDEN (por ejemplo, Pedro en /ana),\n"
                    "DIR_NOT_EMPTY (rmdir con contenido) o NO_QUORUM (sin mayoría)"),
            FIN,
            N(0, 2, "Solo metadatos, ningún nodo de datos interviene. Un cambio queda confirmado cuando "
                    "lo tienen 2 de los 3 miembros de etcd. La comparación de liderazgo impide que un "
                    "líder que perdió el puesto sin saberlo escriba (D1, A3)."),
        ])

    secuencia(
        "secuencia_s2_carga", "S2. Carga: put viaje.mp4 /ana/videos/viaje.mp4 (CU04)",
        ["Cliente de Ana", "Nodo de metadatos\nlíder", "etcd\n(Raft)", "DN_a", "DN_b", "DN_c"],
        [
            M(0, 1, 'Open("/ana/videos/viaje.mp4", "w", sesión)'),
            S(1, "autoriza, crea la entrada como\nversión 0 vacía, genera la clave\ndel archivo y la envuelve con KMS"),
            M(1, 2, "transacción: entrada, archivo,\ndescriptor y lock del archivo entero"),
            R(2, 1, "confirmada (mayoría)"),
            R(1, 0, "fd, id del archivo, clave del archivo,\nbloques de 16 MiB y lock (token de cerco)"),
            S(0, "parte en 64 bloques. Por cada i:\nhᵢ huella en claro, cifra con AES-GCM,\nHᵢ huella cifrada"),
            F("loop", "[por cada tanda de 8 bloques, 4 viajan a la vez]", 0, 5),
            M(0, 1, "AllocateBlocks: 8 índices"),
            R(1, 0, "id de cada bloque, cadena en 3 zonas\n(DN_a :50052, DN_b y DN_c :50054)\ny token HMAC"),
            M(0, 3, "WriteBlock(bloque i cifrado, trozos de 1 MiB)"),
            M(3, 4, "reenvía mientras recibe"),
            M(4, 5, "reenvía mientras recibe"),
            S(5, "temporal, comprueba Hᵢ,\nlo graba y lo renombra"),
            R(5, 4, "guardado"),
            R(4, 3, "guardado (DN_b y DN_c)"),
            R(3, 0, "WriteResult: 3 copias (bastan 2)"),
            M(3, 1, "BlockReceived(id, Hᵢ): cada DN avisa a los 3 nodos de metadatos"),
            FIN,
            M(0, 1, "Commit: versión nueva con los 64 bloques\n(huellas, token de cerco y op_id)"),
            S(1, "comprueba que cada bloque tenga\n2 o más avisos con su Hᵢ, que el lock\n"
                 "siga vigente y calcula la huella del archivo"),
            M(1, 2, "transacción única: manifiesto, versión 1,\ncuota y op_id"),
            R(2, 1, "confirmada (mayoría)"),
            R(1, 0, "OK: versión 1 confirmada"),
            M(0, 1, "Close(fd)"),
            R(1, 0, "OK: lock del archivo liberado"),
            N(0, 5, "Hasta el Commit (paso 17) los bloques ya están guardados, pero ningún lector ve la "
                    "versión nueva. El líder no confía en el cliente: comprueba con los avisos de los "
                    "propios nodos de datos que cada bloque llegó entero (18). La transacción de etcd "
                    "(19) es el único punto de commit (G1) y desde ahí cualquiera que abra el archivo ve "
                    "la versión 1 (G2)."),
            N(0, 5, "fd es el descriptor del archivo abierto, guardado en etcd y atado a la sesión. hᵢ y "
                    "Hᵢ son las huellas SHA-256 del bloque i en claro y cifrado. La cadena cambia de un "
                    "bloque a otro (D5). op_id es un número único de la operación: si el Commit se "
                    "reintenta, el líder no lo aplica dos veces (D3). Si un DN cae a mitad de la cadena: "
                    "escenario 3."),
        ],
        subtitulo="Ana sube un video de 1 GB en 64 bloques de 16 MiB, que quedan como versión 1 del archivo")

    secuencia(
        "secuencia_s3_lectura", "S3. Lectura: get /ana/videos/viaje.mp4 (CU05) y read parcial (CU06)",
        ["Cliente de Ana", "Nodo de metadatos\nlíder", "DN más cercano\n(1.ª réplica)", "DN siguiente\n(2.ª réplica)"],
        [
            M(0, 1, 'Open("/ana/videos/viaje.mp4", "r", sesión)'),
            S(1, "autoriza y crea un arriendo de\nlectura de la versión 1, atado a la sesión"),
            R(1, 0, "versión 1, clave del archivo y, por bloque: id, Hᵢ, hᵢ,\nréplicas ordenadas por cercanía y token HMAC de lectura"),
            F("loop", "[para los 64 bloques, 4 a la vez]", 0, 3),
            M(0, 2, "ReadBlock(id, token)  [:50052, TLS]"),
            S(2, "verifica el token HMAC\nsin consultar a nadie"),
            R(2, 0, "bloque cifrado, en trozos de 1 MiB"),
            S(0, "comprueba Hᵢ, descifra (AES-GCM),\ncomprueba hᵢ y lo escribe en su\nposición (i × 16 MiB)"),
            F("alt", "[la réplica no responde en 3 s o su huella no coincide]", 0, 3),
            M(0, 3, "ReadBlock(id, token)"),
            R(3, 0, "bloque cifrado"),
            FIN,
            FIN,
            F("loop", "[cada 10 s durante la descarga]", 0, 1),
            M(0, 1, "Renew(sesión): mantiene el arriendo"),
            FIN,
            S(0, "comprobación final: la huella de las 64 hᵢ\ncoincide con la huella del archivo,\n"
                 "así que está completo y en orden"),
            M(0, 1, "ReportBadBlock por cada réplica que falló"),
            M(0, 1, "Close(fd)"),
            R(1, 0, "OK: arriendo liberado"),
            N(0, 3, "El arriendo de lectura impide que el recolector borre los bloques de la versión "
                    "que se está leyendo, aunque otro publique una nueva mientras tanto (G3). Una réplica "
                    "con la huella equivocada se informa con ReportBadBlock: el líder la deja de contar, "
                    "ordena borrarla y el siguiente ciclo de recuperación repone la copia."),
            N(0, 3, "Leer solo una parte, read(offset, n) (CU06), sigue el mismo flujo con los bloques "
                    "que cubren ese rango. Por ejemplo, 100 bytes desde la posición 117 445 000 caen en "
                    "el bloque 7, porque 117 445 000 ÷ 16 777 216 = 7,0003."),
        ],
        subtitulo="Ana descarga la versión 1 que subió en S2")

    secuencia(
        "secuencia_s4_escritura", "S4. Escritura parcial: cambiar 100 bytes del bloque 7 (CU07)",
        ["App de Ana", "Nodo de metadatos\nlíder", "etcd\n(Raft)", "DN con el\nbloque 7 actual",
         "Cadena de DN\n(bloque 7 nuevo)"],
        [
            M(0, 1, 'Open("/ana/videos/viaje.mp4", "rw", sesión)'),
            R(1, 0, "fd, versión 1 y clave del archivo"),
            S(0, "write(117 445 000, 100 B):\n117 445 000 ÷ 16 MiB, bloque 7"),
            M(0, 1, "Lock(bloque 7), implícito al escribir"),
            M(1, 2, "Lock de etcd con el lease de la sesión\n(cola en orden de llegada, hasta 10 s)"),
            R(2, 1, "concedido: revisión 41"),
            R(1, 0, "token de cerco 41, bloque 7 vigente b-5e10"),
            M(0, 3, "ReadBlock(b-5e10, token)"),
            R(3, 0, "bloque 7 cifrado"),
            S(0, "descifra y cambia los 100 bytes\nen el búfer local"),
            S(0, "close() o fsync(): huella h₇′, cifra\ncon un nonce nuevo, huella H₇′"),
            M(0, 1, "AllocateBlocks(bloque 7)"),
            R(1, 0, "id nuevo b-9c02, cadena y token"),
            M(0, 4, "WriteBlock(b-9c02) por la cadena, como en S2"),
            M(4, 1, "BlockReceived(b-9c02) de cada copia"),
            R(4, 0, "WriteResult: 3 copias"),
            M(0, 1, "Commit: bloque 7 = b-9c02\n(base b-5e10, token 41, op_id)"),
            S(1, "token 41 vigente, el bloque 7 sigue\nsiendo b-5e10 y tiene 2 o más copias"),
            M(1, 2, "transacción única: versión 2"),
            R(2, 1, "confirmada (mayoría)"),
            R(1, 0, "OK: versión 2"),
            M(0, 1, "Close(fd)"),
            R(1, 0, "OK: lock del bloque 7 liberado"),
            F("alt", "[en lugar del paso 7, otro proceso tiene el lock del bloque 7]", 0, 2),
            R(1, 0, "tras 10 s en la cola: LOCKED\n(quién lo tiene y cuándo vence)"),
            FIN,
            N(0, 4, "b-5e10 y b-9c02 son el bloque 7 actual y el nuevo (copy-on-write: el viejo no se "
                    "toca y el recolector lo borra cuando nadie lee la versión 1). El token de cerco es la "
                    "revisión de etcd, que solo crece: el Commit lo presenta para demostrar que el lock "
                    "sigue vigente. Si la base ya no es la vigente, el Commit responde VERSION_CONFLICT. "
                    "Dos escritores sobre el mismo bloque: escenario 1."),
        ],
        subtitulo="Una aplicación de Ana cambia 100 bytes del video, de la versión 1 a la versión 2")

    secuencia(
        "secuencia_s5_borrado", "S5. Borrado: rm /ana/videos/viaje.mp4 (CU10)",
        ["Cliente de Ana", "Nodo de metadatos\nlíder", "etcd\n(Raft)", "Otra app de Ana\n(video abierto)",
         "DN con\nlos bloques"],
        [
            M(0, 1, 'Remove("/ana/videos/viaje.mp4")'),
            S(1, "autoriza y comprueba que nadie\ntenga locks sobre el archivo"),
            M(1, 2, "transacción: borra la entrada y el archivo\ny descuenta su tamaño de la cuota"),
            R(2, 1, "confirmada (mayoría)"),
            R(1, 0, "OK: ls ya no lo muestra\ny open() responde NOT_FOUND"),
            M(3, 4, "ReadBlock(id, token): sigue leyendo\nsu versión (arriendo vigente, G3)"),
            R(4, 3, "bloque"),
            M(3, 1, "Close(fd): libera el arriendo de lectura"),
            S(1, "ciclo de recuperación (cada 10 s):\nya nadie lee esa versión, borra\nsu manifiesto"),
            F("loop", "[en las respuestas a los latidos siguientes]", 1, 4),
            M(4, 1, "Heartbeat"),
            R(1, 4, "orden: borra b-7f31, b-2d44 y los\ndemás bloques del video"),
            FIN,
            S(4, "borra los bloques del disco"),
            M(4, 1, "BlockDeleted(ids): avisa a los 3 nodos de metadatos"),
            F("alt", "[en lugar del paso 2, alguien tiene un lock sobre el archivo]", 0, 2),
            R(1, 0, "LOCKED: se reintenta cuando termine quien escribe"),
            FIN,
            N(0, 4, "La otra aplicación había abierto el video antes del borrado y puede terminar de "
                    "leerlo, como un archivo borrado pero abierto en Unix. Si un DN estaba caído durante "
                    "el borrado, al volver su inventario (BlockReport) muestra bloques que ya no usa "
                    "ningún archivo y el líder ordena borrarlos."),
        ],
        subtitulo="Ana borra el video y otra aplicación suya lo estaba leyendo desde antes")

    secuencia(
        "secuencia_s6_caida_nodo", "S6. Caída de un nodo de datos y re-replicación (CU14)",
        ["Cliente de Ana", "Nodo de metadatos\nlíder", "DN3\n(se cae)", "DN2\n(tiene copia)", "DN1\n(destino nuevo)"],
        [
            M(2, 1, "Heartbeat: sigue vivo (cada 3 s)"),
            S(2, "se apaga y deja de enviar latidos"),
            M(0, 2, "ReadBlock(b-2d44): DN3 era la primera\nréplica de la lista"),
            S(0, "sin respuesta en 3 s"),
            M(0, 3, "ReadBlock(b-2d44) a la réplica siguiente"),
            R(3, 0, "bloque, el usuario no nota nada"),
            S(1, "10 s sin latido: SOSPECHOSO, pasa al\nfinal de las listas de lectura"),
            S(1, "30 s: MUERTO, sus copias dejan de\ncontar y el ciclo de recuperación\nbusca los bloques con menos de 3"),
            F("loop", "[por cada bloque afectado, hasta 8 órdenes por nodo y ciclo]", 1, 4),
            M(3, 1, "Heartbeat"),
            R(1, 3, "orden: copia b-2d44 a DN1\n(DN1 no tenía copia de ese bloque)"),
            M(3, 4, "WriteBlock(b-2d44)  [:50054, TLS mutuo]"),
            M(4, 1, "BlockReceived(b-2d44): 3 copias otra vez"),
            FIN,
            F("opt", "[DN3 vuelve]", 1, 4),
            M(2, 1, "Register e inventario (BlockReport)"),
            R(1, 2, "orden: borra los bloques que ya no\nusa ningún archivo"),
            FIN,
            N(0, 4, "Cada DN hace como máximo 2 copias a la vez y lee el bloque del disco por trozos, "
                    "así la recuperación no agota su memoria. Decommission (CU12) es un retiro planificado: "
                    "el nodo deja de contar como copia, sus bloques se copian a otros y después se le "
                    "ordena borrarlos. Con 4 DN se restauran las 3 copias (unos 20 s después de detectar "
                    "la caída en AWS). Con 3 DN el sistema sigue con 2 copias hasta que el nodo vuelva. "
                    "Si DN3 vuelve, sus copias de bloques vigentes quedan como copias de más."),
        ],
        subtitulo="b-2d44 es el bloque 1 del video, con copias en DN2, DN3 y DN4")

    secuencia(
        "escenario1_escritura_concurrente",
        "Escenario obligatorio 1. Dos procesos escriben el mismo bloque a la vez",
        ["P1\n(app de Ana)", "Nodo de metadatos\nlíder", "etcd\n(Raft)", "Cadena de DN", "P2\n(otra app de Ana)"],
        [
            N(0, 4, "P1 y P2 abrieron el archivo en modo rw cuando estaba en la versión 2 (bloque 7 = b-9c02)."),
            M(0, 1, "Lock(bloque 7)"),
            M(1, 2, "Lock de etcd para P1"),
            R(2, 1, "concedido: revisión 42"),
            R(1, 0, "token 42, vigente b-9c02"),
            M(4, 1, "Lock(bloque 7, espera hasta 10 s)"),
            S(1, "bloque 7 ocupado: P2 espera\nen la cola de etcd, detrás de P1"),
            S(0, "aplica su cambio sobre b-9c02"),
            M(0, 3, "WriteBlock(b-c771) por la cadena"),
            M(3, 1, "BlockReceived(b-c771) de cada copia"),
            M(0, 1, "Commit: bloque 7 = b-c771\n(base b-9c02, token 42)"),
            M(1, 2, "transacción: versión 3"),
            R(1, 0, "OK: versión 3"),
            M(0, 1, "Close: libera el lock"),
            R(1, 4, "lock concedido: token 43, vigente b-c771 (distinto de su base)"),
            S(4, "aplica su cambio sobre su copia\nde la versión 2 (base b-9c02)"),
            M(4, 3, "WriteBlock(b-d4e8) por la cadena"),
            M(4, 1, "Commit: bloque 7 = b-d4e8 (base b-9c02, token 43)"),
            R(1, 4, "VERSION_CONFLICT: el bloque 7 vigente es b-c771"),
            S(4, "vuelve a abrir el archivo:\nahora ve la versión 3"),
            M(4, 1, "Lock(bloque 7) y aplica su cambio sobre b-c771"),
            M(4, 3, "WriteBlock(b-e0f5) por la cadena"),
            M(4, 1, "Commit: bloque 7 = b-e0f5 (base b-c771, token 44)"),
            R(1, 4, "OK: versión 4, con los cambios de P1 y P2"),
            F("alt", "[variante: P1 se congela más de 30 s antes de su Commit]", 0, 4),
            S(1, "la sesión de P1 vence en etcd, su lock\ndesaparece y P2 recibe el lock"),
            M(0, 1, "Commit(…, token 42) al despertar"),
            R(1, 0, "LEASE_EXPIRED: el lock ya no existe"),
            FIN,
            N(0, 4, "Resultado: los dos cambios se aplican uno después del otro, ninguno se pierde y "
                    "ningún lector ve una mezcla. Si P1 y P2 cambiaran bloques distintos, trabajarían en "
                    "paralelo, porque la base se compara por bloque. El token de cerco solo crece, así un "
                    "lock vencido nunca puede publicar (D4). En AWS, 16 escritores sobre el mismo bloque "
                    "terminaron con sus 16 cambios presentes (E7)."),
        ],
        subtitulo="El video está en la versión 2 y su bloque 7 vigente es b-9c02 (S4)")

    secuencia(
        "escenario3_nodo_cae_en_bloque",
        "Escenario obligatorio 3. Un nodo falla después de recibir parte de un bloque",
        ["Cliente de Ana", "Nodo de metadatos\nlíder", "DN_a", "DN_b", "DN_c", "DN_d\n(otra zona)"],
        [
            M(0, 1, "AllocateBlocks(bloque 12)"),
            R(1, 0, "b-3c90, cadena\n[DN_a, DN_b, DN_c]"),
            M(0, 2, "WriteBlock(b-3c90)"),
            M(2, 3, "reenvía mientras recibe"),
            S(3, "se apaga con el temporal a medias:\nnunca se renombra, así que no cuenta"),
            S(2, "la conexión con DN_b se corta"),
            R(2, 0, "WriteResult: 1 copia (DN_a),\nmenos que W = 2"),
            M(2, 1, "BlockReceived(b-3c90) de DN_a"),
            M(0, 1, "AllocateBlocks(bloque 12) otra vez"),
            S(1, "DN_b es el primer nodo de la cadena\nanterior sin copia: lo excluye"),
            R(1, 0, "id nuevo b-4a17, cadena\n[DN_a, DN_c, DN_d]"),
            M(0, 2, "WriteBlock(b-4a17) completo"),
            M(2, 4, "reenvía mientras recibe"),
            M(4, 5, "reenvía"),
            R(5, 4, "guardado"),
            R(4, 2, "guardado"),
            R(2, 0, "WriteResult: 3 copias"),
            M(2, 1, "BlockReceived(b-4a17) de DN_a, DN_c y DN_d"),
            N(0, 5, "Mientras no haya Commit, la versión nueva no existe para nadie (G4). Si no se "
                    "lograran 2 copias, Ana recibe un error y la versión anterior sigue intacta. El "
                    "temporal de DN_b se borra cuando el nodo vuelve a arrancar, y la copia suelta de "
                    "b-3c90 en DN_a la borra el recolector porque ningún manifiesto la usa. En AWS, un "
                    "kill -9 a un nodo durante un put de 1 GiB terminó con el archivo completo e íntegro."),
        ],
        subtitulo="Durante la carga de S2, el bloque 12 del video va por la cadena DN_a, DN_b y DN_c, "
                  "y DN_b se apaga a mitad")


if __name__ == "__main__":
    diagrama_casos_de_uso()
    todas_las_secuencias()
    print("Diagramas UML generados en", OUT)
