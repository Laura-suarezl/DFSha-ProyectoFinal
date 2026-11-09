"""Genera los diagramas de arquitectura de DFSha: contexto (C4 nivel 1), componentes (C4 nivel 2)
y despliegue en AWS (D10 y D12).

Uso:  python docs/diagramas/generar_arquitectura.py
Salida: contexto.png, componentes.png y despliegue.png en docs/diagramas/. Los de componentes y
despliegue son los de la Fase I actualizados con lo implementado.
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).resolve().parent

C_CLIENTE = "#E8F1FB"
C_CONTROL = "#FFF4E0"
C_DATOS = "#E9F7EF"
C_INFRA = "#F2F2F2"
C_SISTEMA = "#1F5F99"
C_BORDE = "#37474F"
C_CTRL = "#C77700"      # A, control
C_DAT = "#1B7F3B"       # B, datos
C_S2S = "#6A1B9A"       # C, D y E, entre servidores
C_GRIS = "#546E7A"      # DNS, certificados y endpoints (punteado)
C_ADMIN = "#0D47A1"     # F, administración
C_TXT = "#1A1A1A"


def caja(ax, x, y, w, h, titulo=None, lineas=(), fc="white", ec=C_BORDE, lw=1.4,
         tam_titulo=10, tam=8.2, radio=0.8, alinear="center", paso=1.85, z=1, color=C_TXT):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={radio}",
                                fc=fc, ec=ec, lw=lw, zorder=z))
    cy = y + h - 1.5
    if titulo:
        ax.text(x + w / 2, cy, titulo, ha="center", va="top", fontsize=tam_titulo,
                fontweight="bold", color=color, zorder=z + 1)
        cy -= 2.3
    for ln in lineas:
        tx = x + w / 2 if alinear == "center" else x + 1.0
        ax.text(tx, cy, ln, ha=alinear, va="top", fontsize=tam, color=color, zorder=z + 1)
        cy -= paso


def flecha(ax, a, b, color, lw=1.8, ls="-", rad=0.0, ambos=False, z=5):
    st = "<|-|>" if ambos else "-|>"
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle=st, mutation_scale=13, color=color, lw=lw,
                                 linestyle=ls, connectionstyle=f"arc3,rad={rad}", zorder=z))


def etiqueta(ax, x, y, texto, color, tam=7.6, rot=0):
    ax.text(x, y, texto, ha="center", va="center", fontsize=tam, color=color,
            fontweight="bold", rotation=rot, zorder=7,
            bbox=dict(boxstyle="round,pad=0.22", fc="white", ec=color, lw=0.8))


def lienzo(ancho, y0, y1, w_in):
    h_in = w_in * (y1 - y0) / ancho
    fig, ax = plt.subplots(figsize=(w_in, h_in))
    ax.set_xlim(0, ancho)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.patch.set_facecolor("white")
    return fig, ax


def guardar(fig, nombre):
    fig.savefig(OUT / f"{nombre}.png", dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def persona(ax, x, y, nombre, descripcion):
    """Persona en notación C4: cabeza y cuerpo con el nombre y su rol."""
    ax.add_patch(plt.Circle((x, y + 12.2), 2.2, fc="#08427B", ec="#073B6F", lw=1.2, zorder=3))
    caja(ax, x - 9, y, 18, 9.6, None, [], fc="#08427B", ec="#073B6F", radio=1.6, z=2)
    ax.text(x, y + 7.6, nombre, ha="center", va="top", fontsize=9.2, fontweight="bold",
            color="white", zorder=4)
    ax.text(x, y + 5.3, "[Persona]", ha="center", va="top", fontsize=6.9, color="white", zorder=4)
    ax.text(x, y + 3.4, descripcion, ha="center", va="top", fontsize=6.9, color="white", zorder=4,
            linespacing=1.2)


# ---------------------------------------------------------------- contexto (C4 nivel 1)
def diagrama_contexto():
    fig, ax = lienzo(120, 0, 62, 17)
    ax.text(60, 61.5, "Diagrama de contexto de DFSha (C4 nivel 1)", ha="center", va="top",
            fontsize=14, fontweight="bold")
    persona(ax, 13, 40, "Usuario", "RF1 y RF2 con\nla CLI dfsha")
    persona(ax, 13, 18, "Administrador", "usuarios, cuotas\ny nodos")
    caja(ax, 1, 1.5, 24, 11, None, [], fc="#999999", ec="#8A8A8A", radio=0.8)
    ax.text(13, 10.6, "Aplicación", ha="center", va="top", fontsize=9.2, fontweight="bold", color="white")
    ax.text(13, 8.3, "[Sistema externo]", ha="center", va="top", fontsize=6.9, color="white")
    ax.text(13, 6.4, "RF3 con la biblioteca\n(open, read, write, lock)", ha="center", va="top",
            fontsize=6.9, color="white", linespacing=1.2)

    caja(ax, 42, 18, 36, 26, None, [], fc=C_SISTEMA, ec="#174A78", radio=1.2)
    ax.text(60, 41.5, "DFSha", ha="center", va="top", fontsize=13, fontweight="bold", color="white")
    ax.text(60, 38.4, "[Sistema]", ha="center", va="top", fontsize=7.6, color="white")
    ax.text(60, 35.0, "Sistema de archivos distribuido con alta\ndisponibilidad, rendimiento y seguridad.\n"
            "3 nodos de metadatos con etcd (Raft) y\n4 nodos de datos con 3 copias por bloque,\n"
            "en 9 máquinas de AWS en 3 zonas", ha="center", va="top", fontsize=7.6, color="white",
            linespacing=1.35)

    for y, nombre, desc in [(40, "AWS KMS", "clave maestra que envuelve\nlas claves del clúster\ny de cada archivo"),
                            (22, "Amazon Route 53", "zona privada dfsha.internal\ncon los nombres de\ntodos los nodos"),
                            (4, "Amazon S3", "bucket privado con las\nimágenes y certificados\ndel despliegue")]:
        caja(ax, 95, y, 24, 13, None, [], fc="#999999", ec="#8A8A8A", radio=0.8)
        ax.text(107, y + 11.3, nombre, ha="center", va="top", fontsize=9.2, fontweight="bold", color="white")
        ax.text(107, y + 9.0, "[Sistema externo]", ha="center", va="top", fontsize=6.9, color="white")
        ax.text(107, y + 7.0, desc, ha="center", va="top", fontsize=6.9, color="white", linespacing=1.2)

    flecha(ax, (22, 46), (42, 38), C_TXT, lw=1.3)
    ax.text(31.5, 44.6, "gestiona archivos\n[gRPC con TLS]", ha="center", va="center", fontsize=6.9)
    flecha(ax, (22, 23), (42, 26), C_TXT, lw=1.3)
    ax.text(31.5, 22.2, "administra\n[gRPC con TLS]", ha="center", va="center", fontsize=6.9)
    flecha(ax, (25, 9), (42, 20), C_TXT, lw=1.3)
    ax.text(35, 11.2, "lee y escribe por partes\n[gRPC con TLS]", ha="center", va="center", fontsize=6.9)
    flecha(ax, (78, 38), (95, 46), C_TXT, lw=1.3)
    ax.text(86.5, 45.0, "envuelve claves\n[endpoint privado]", ha="center", va="center", fontsize=6.9)
    flecha(ax, (78, 31), (95, 29), C_TXT, lw=1.3)
    ax.text(86.5, 32.7, "resuelve nombres", ha="center", va="center", fontsize=6.9)
    flecha(ax, (78, 22), (95, 12), C_TXT, lw=1.3)
    ax.text(85.0, 14.0, "descarga artefactos\n[endpoint privado]", ha="center", va="center", fontsize=6.9)
    guardar(fig, "contexto")


# ---------------------------------------------------------------- componentes (C4 nivel 2)
def diagrama_componentes():
    fig, ax = lienzo(120, -4, 90, 17)
    ax.text(60, 89.5, "Diagrama de componentes de DFSha (C4 nivel 2, opción 1 cliente/servidor S2S)",
            ha="center", va="top", fontsize=14, fontweight="bold", color=C_TXT)

    ax.plot([17, 17, 103.5], [79, 83, 83], color=C_GRIS, ls=":", lw=1.4)
    flecha(ax, (103.5, 83), (103.5, 79), C_GRIS, lw=1.4, ls=":")
    etiqueta(ax, 60, 83, "resuelve el nombre lógico dfsha.internal y los nombres de los nodos (sin IPs, RNF7)",
             C_GRIS, tam=7.6)

    # ---- Cliente
    caja(ax, 2, 22, 30, 57, "Máquina cliente", fc=C_CLIENTE, tam_titulo=11.5)
    caja(ax, 4, 68.2, 26, 6.8, "CLI dfsha  [Python, typer]",
         ["login, ls, cd, mkdir, rmdir, rm, mv, put, get, admin"], tam=7.4, tam_titulo=9.4)
    caja(ax, 4, 24, 26, 42.5, "Biblioteca del cliente  [Python]", fc="white", tam_titulo=9.4)
    mods_cli = [
        ("API de archivos", "open, read, write, append, lock, fsync, close"),
        ("Sesión", "JWT Ed25519, Renew cada 10 s, Refresh"),
        ("Descubrimiento", "dfsha.internal, NOT_LEADER, caché del líder"),
        ("Particionado y ensamblado", "bloques de 16 MiB, 4 en paralelo"),
        ("Cifrado e integridad", "AES-256-GCM, SHA-256 cifrada y en claro"),
        ("Búfer de escritura", "copy-on-write por bloque, un commit"),
    ]
    yy = 57.2
    for t, d in mods_cli:
        caja(ax, 5.5, yy, 23, 5.2, t, [d], fc=C_CLIENTE, tam_titulo=8.6, tam=7.1, lw=0.9, radio=0.5)
        yy -= 5.55
    ax.text(17, 20.0, "Usado por el usuario (CLI) y la aplicación (biblioteca)", ha="center",
            fontsize=8.2, style="italic", color=C_TXT)

    # ---- Plano de control
    caja(ax, 42, 40, 40, 39, "Plano de control, 3 nodos de metadatos", fc=C_CONTROL, tam_titulo=11)
    ax.text(62, 74.9, "cada máquina corre un nodo de metadatos y un miembro de etcd, solo el líder atiende",
            ha="center", fontsize=6.9, style="italic", color=C_TXT)
    for x, nom, rol in [(44, "mds1", "líder"), (57.25, "mds2", "seguidor"), (70.5, "mds3", "seguidor")]:
        caja(ax, x, 65.6, 9.5, 7.4, nom, [rol, "con etcd"], fc="white", tam_titulo=8.8, tam=6.8, lw=1.0,
             radio=0.5, paso=1.45)
    flecha(ax, (53.5, 69.5), (57.25, 69.5), C_S2S, lw=1.5, ls="--", ambos=True)
    flecha(ax, (66.75, 69.5), (70.5, 69.5), C_S2S, lw=1.5, ls="--", ambos=True)
    flecha(ax, (48.75, 65.6), (75.25, 65.6), C_S2S, lw=1.5, ls="--", ambos=True, rad=0.18)
    etiqueta(ax, 62, 63.0, "E. etcd con Raft: 2379 y 2380, TLS mutuo, elección del líder", C_S2S, tam=6.8)
    caja(ax, 43.5, 55.4, 37, 5.6, "APIs gRPC de cada nodo  [Python, grpcio]",
         ["50051 MetadataService y AdminService (TLS), 50053 NodeService (TLS mutuo)"],
         fc="white", tam_titulo=8.4, tam=6.9, lw=0.9, radio=0.5)
    mods_ctl = [
        ("Autenticación", "Login, JWT Ed25519, Argon2id"),
        ("Autorización", "solo el dueño, tokens HMAC"),
        ("Espacio de nombres", "árbol, archivos, versiones"),
        ("Locks y arriendos", "Lock de etcd, cola, cerco"),
        ("Política de ubicación", "vivos, 3 nodos, zonas distintas"),
        ("Monitor de nodos", "vivo, sospechoso, muerto"),
        ("Re-replicación y recolector", "órdenes en la respuesta al latido"),
        ("Almacén en etcd", "estado persistente, claves con KMS"),
    ]
    for i, (t, d) in enumerate(mods_ctl):
        cx = 43.5 if i % 2 == 0 else 62.5
        cy = 51.2 - (i // 2) * 3.4
        caja(ax, cx, cy, 18, 3.1, None, [], fc="white", lw=0.8, radio=0.4)
        ax.text(cx + 9, cy + 2.3, t, ha="center", va="center", fontsize=7.6, fontweight="bold")
        ax.text(cx + 9, cy + 0.9, d, ha="center", va="center", fontsize=6.8)

    # ---- Plano de datos
    caja(ax, 42, 2, 40, 31, "Plano de datos, 4 nodos de datos", fc=C_DATOS, tam_titulo=11)
    caja(ax, 43.5, 9.5, 17, 19, "Cada nodo de datos",
         ["DataService [Python, grpcio]", "50052 clientes (TLS)", "50054 cadena (TLS mutuo)",
          "verifica tokens HMAC", "temporal y renombrado", "reenvío en cadena",
          "revisión diaria de huellas", "latidos a los 3 nodos"],
         fc="white", tam_titulo=8.6, tam=6.9, lw=0.9, radio=0.5, paso=1.75)
    zonas_dn = ["a", "b", "c", "a"]
    pos = {1: (63, 21), 2: (73, 21), 4: (63, 11.5), 3: (73, 11.5)}
    for i, (x, y) in pos.items():
        caja(ax, x, y, 8, 6, f"dn{i}", [f"zona {zonas_dn[i - 1]}"], fc="white",
             tam_titulo=9, tam=7, lw=1.0, radio=0.5)
    flecha(ax, (71, 24), (73, 24), C_S2S, lw=1.6)              # cadena dn1 -> dn2
    flecha(ax, (77, 21), (77, 17.5), C_S2S, lw=1.6)            # cadena dn2 -> dn3
    flecha(ax, (67, 17.5), (67, 21), C_S2S, lw=1.3, ls="--")   # re-replicación dn4 -> dn1
    ax.text(72, 8.2, "C. cadena dn1, dn2, dn3 (ejemplo)\nC. re-replicación dn4 a dn1 (ejemplo)",
            ha="center", va="center", fontsize=6.8, color=C_S2S, fontweight="bold")
    ax.text(62, 3.6, "Disco EBS cifrado por nodo, cada bloque es un objeto inmutable cifrado con su huella",
            ha="center", fontsize=6.9, style="italic")

    # ---- Infraestructura y administración (columna derecha)
    caja(ax, 89, 67.5, 29, 11.5, "DNS privado  [Route 53]",
         ["dfsha.internal: mds1, mds2 y mds3", "mdsN y dnN.dfsha.internal",
          "zona privada de la VPC", "la consultan el cliente y los nodos"],
         fc=C_INFRA, tam=7.2, paso=1.75)
    caja(ax, 89, 50.5, 29, 12.5, "Administrador",
         ["AdminService: CreateUser, SetQuota,", "ListNodes y Decommission",
          "no ve contenidos, que están", "cifrados por el cliente"],
         fc=C_INFRA, tam=7.2, paso=1.75)
    flecha(ax, (89, 56.5), (82, 56.5), C_ADMIN, lw=1.6)
    etiqueta(ax, 85.5, 58.6, "F", C_ADMIN, tam=7.2)
    caja(ax, 89, 34.5, 29, 12, "AWS KMS  [endpoint privado]",
         ["clave maestra alias/dfsha-kek", "envuelve las claves de firma", "y la clave de cada archivo"],
         fc=C_INFRA, tam=7.2, paso=1.75)
    flecha(ax, (89, 40), (82, 45), C_GRIS, lw=1.2, ls=":")
    caja(ax, 89, 18, 29, 12.5, "CA interna (certificados)",
         ["un certificado por nodo, por nombre", "TLS mutuo entre servidores",
          "el cliente confía en la CA (TLS)", "llaves fuera del repositorio"],
         fc=C_INFRA, tam=7.2, paso=1.75)
    flecha(ax, (89, 27), (82, 41), C_GRIS, lw=1.2, ls=":")
    flecha(ax, (89, 22), (82, 22), C_GRIS, lw=1.2, ls=":")

    # ---- Conexiones del cliente
    flecha(ax, (32, 62), (42, 62), C_CTRL, lw=2.2)
    etiqueta(ax, 37, 63.9, "A. control", C_CTRL, tam=7.6)
    ax.text(37, 60.2, "gRPC 50051, TLS\nLogin, Open\nAllocateBlocks\nLock, Commit\nClose",
            ha="center", va="top", fontsize=6.8, color=C_CTRL)
    flecha(ax, (32, 30), (42, 22), C_DAT, lw=3.2, ambos=True)
    etiqueta(ax, 37.5, 29.6, "B. datos", C_DAT, tam=7.6)
    ax.text(37.2, 20.5, "gRPC 50052, TLS\nWriteBlock\nReadBlock\ntrozos de 1 MiB\ntoken HMAC",
            ha="center", va="top", fontsize=6.8, color=C_DAT)

    # ---- nodos de datos <-> nodos de metadatos
    flecha(ax, (72, 33), (72, 40), C_S2S, lw=2.0, ls="--", ambos=True)
    ax.text(57, 36.5, "D. NodeService 50053, TLS mutuo\nRegister, latido cada 3 s, BlockReceived, BlockReport\n"
            "a los 3 nodos, la respuesta del líder trae órdenes",
            ha="center", va="center", fontsize=6.5, color=C_S2S, fontweight="bold")

    # ---- Leyenda
    caja(ax, 2, -2.5, 31, 16.8, "Conexiones (la letra es el tipo, no el orden)", fc="white",
         tam_titulo=8.4, lw=0.9)
    filas = [
        (C_CTRL, "-", 2.2, "A  control: cliente a metadatos (50051, TLS)"),
        (C_DAT, "-", 3.2, "B  datos: cliente y nodo de datos (50052, TLS)"),
        (C_S2S, "-", 1.6, "C  copias entre nodos de datos (50054, TLS mutuo)"),
        (C_S2S, "--", 2.0, "D  latidos y avisos a metadatos (50053, TLS mutuo)"),
        (C_S2S, "--", 1.5, "E  etcd con Raft (2379 y 2380, TLS mutuo)"),
        (C_ADMIN, "-", 1.6, "F  administración (50051, TLS)"),
        (C_GRIS, ":", 1.3, "DNS, certificados y KMS"),
    ]
    for i, (col, ls, lw, txt) in enumerate(filas):
        yl = 10.6 - i * 1.75
        ax.plot([3.5, 7.5], [yl, yl], color=col, ls=ls, lw=lw)
        ax.text(8.3, yl, txt, va="center", fontsize=6.8)
    ax.text(76, -2.5, "Los datos nunca pasan por los nodos de metadatos. El cliente pregunta dónde están los "
            "bloques al plano de control\ny los transfiere directamente con los nodos de datos.",
            ha="center", va="center", fontsize=7.8, style="italic", color=C_TXT)
    guardar(fig, "componentes")


# ---------------------------------------------------------------- despliegue
def vm(ax, x, y, nombre, rol, lineas, fc, w=22, h=12.4):
    caja(ax, x, y, w, h, None, [], fc=fc, lw=1.1, radio=0.6)
    ax.text(x + w / 2, y + h - 1.0, f"EC2  {nombre}", ha="center", va="top", fontsize=9.2,
            fontweight="bold")
    ax.text(x + w / 2, y + h - 2.9, rol, ha="center", va="top", fontsize=7.3, style="italic")
    cy = y + h - 4.8
    for ln in lineas:
        ax.text(x + 0.9, cy, "• " + ln, ha="left", va="top", fontsize=6.8)
        cy -= 1.7


def diagrama_despliegue():
    fig, ax = lienzo(120, 0, 105, 17)
    ax.text(60, 104.5, "Diagrama de despliegue de DFSha en AWS Academy (red privada)",
            ha="center", va="top", fontsize=14, fontweight="bold")

    caja(ax, 1, 15, 118, 85.5, None, [], fc="#FAFAFA", lw=1.6)
    ax.text(3, 99.6, "AWS Academy, región us-east-1", fontsize=11, fontweight="bold", va="top")
    caja(ax, 3, 17, 114, 79.5, None, [], fc="white", ec="#455A64", lw=1.3)
    ax.text(5, 95.6, "VPC 10.20.0.0/16 con DNS habilitado. Las subredes privadas no tienen salida a internet",
            fontsize=9.6, fontweight="bold", va="top")

    # Fila superior
    caja(ax, 5, 74, 34, 19, None, [], fc="#FFF8E1", ec="#8D6E63", lw=1.1)
    ax.text(22, 92.3, "Subred pública 10.20.0.0/24 (zona a)", fontsize=8.2, fontweight="bold",
            va="top", ha="center")
    vm(ax, 7, 75.5, "cliente1", "bastión SSH y cliente, 10.20.0.31",
       ["contenedor cliente (CLI dfsha)", "certificado de la CA interna",
        "SSH solo desde la IP de quien despliega", "única máquina con IP pública"],
       fc="white", w=28, h=14.2)
    caja(ax, 42, 74, 21, 19, "Endpoints de la VPC", [
        "S3 (puerta de enlace)", "Docker desde Amazon Linux,", "imágenes y certificados",
        "", "KMS (interfaz, zona a)", "clave maestra"],
        fc=C_INFRA, tam_titulo=9, tam=7.1, paso=1.7)
    caja(ax, 66, 74, 23, 19, "DNS privado (Route 53)", [
        "dfsha.internal:", "10.20.1.11, 10.20.2.11,", "10.20.3.11",
        "mdsN, dnN y clienteN", ".dfsha.internal", "con IPs privadas fijas"],
        fc=C_INFRA, tam_titulo=9, tam=7.1, paso=1.7)
    caja(ax, 92, 74, 23, 19, "Grupos de seguridad", [
        "metadatos: 50051 desde cliente,", "50053 desde datos,",
        "2379 y 2380 entre metadatos", "datos: 50052 desde cliente,",
        "50054 entre datos", "cliente: 22 desde la IP", "de quien despliega",
        "endpoints: 443 desde la VPC"],
        fc=C_INFRA, tam_titulo=9, tam=6.7, alinear="left", paso=1.6)

    zonas = [("a", "10.20.1.0/24", 5), ("b", "10.20.2.0/24", 42.5), ("c", "10.20.3.0/24", 80)]
    for z, red, x in zonas:
        caja(ax, x, 21.5, 35, 50, None, [], fc="#F5F9FF", ec="#1565C0", lw=1.2)
        ax.text(x + 17.5, 70.6, f"us-east-1{z}, subred privada {red}", ha="center",
                va="top", fontsize=8.2, fontweight="bold", color="#0D47A1")

    meta = ["contenedores etcd y metadatos", "50051 TLS, 50053 y 2379/2380 TLS mutuo",
            "EBS gp3 20 GB cifrado", "t3.small, Amazon Linux 2023"]
    datos = ["contenedor del nodo de datos", "50052 TLS, 50054 TLS mutuo",
             "EBS gp3 20 GB cifrado", "latidos a los 3 nodos cada 3 s"]
    vx = lambda zx: zx + 2
    vm(ax, vx(5), 54.5, "mds1", "metadatos, 10.20.1.11", meta, C_CONTROL, w=28)
    vm(ax, vx(5), 39.5, "dn1", "datos, 10.20.1.21", datos, C_DATOS, w=28)
    vm(ax, vx(5), 24.5, "dn4", "datos, 10.20.1.22", datos, C_DATOS, w=28)
    vm(ax, vx(42.5), 54.5, "mds2", "metadatos, 10.20.2.11", meta, C_CONTROL, w=28)
    vm(ax, vx(42.5), 39.5, "dn2", "datos, 10.20.2.21", datos, C_DATOS, w=28)
    vm(ax, vx(42.5), 24.5, "cliente2", "cliente, 10.20.2.31",
       ["contenedor cliente (CLI dfsha)", "carga de los experimentos", "sin IP pública", "t3.small"],
       C_CLIENTE, w=28)
    vm(ax, vx(80), 54.5, "mds3", "metadatos, 10.20.3.11", meta, C_CONTROL, w=28)
    vm(ax, vx(80), 39.5, "dn3", "datos, 10.20.3.21", datos, C_DATOS, w=28)

    flecha(ax, (35, 62.5), (44.5, 62.5), C_S2S, lw=1.6, ls="--", ambos=True)
    etiqueta(ax, 41.6, 64.4, "Raft", C_S2S, tam=6.8)
    flecha(ax, (72.5, 62.5), (82, 62.5), C_S2S, lw=1.6, ls="--", ambos=True)
    etiqueta(ax, 77.2, 64.4, "Raft", C_S2S, tam=6.8)
    flecha(ax, (35, 47.5), (44.5, 47.5), C_S2S, lw=1.6)
    etiqueta(ax, 41.6, 49.4, "cadena", C_S2S, tam=6.8)
    flecha(ax, (72.5, 47.5), (82, 47.5), C_S2S, lw=1.6)
    etiqueta(ax, 77.2, 49.4, "cadena", C_S2S, tam=6.8)
    flecha(ax, (96, 51.9), (96, 54.5), C_S2S, lw=1.6, ls="--")
    ax.text(96, 31, "latidos y avisos de cada nodo\nde datos a los 3 nodos de\nmetadatos (50053, TLS mutuo)",
            ha="center", va="center", fontsize=6.8, color=C_S2S, fontweight="bold")
    # Cliente a metadatos (control) y a nodos de datos (datos), por el carril libre de la zona a
    ax.plot([35, 36.3, 36.3], [83, 83, 58.5], color=C_CTRL, lw=1.8)
    flecha(ax, (36.3, 58.5), (35, 58.5), C_CTRL, lw=1.8)
    ax.text(36.9, 67, "control 50051", rotation=90, ha="left", va="center", fontsize=6.6,
            color=C_CTRL, fontweight="bold")
    ax.plot([35, 38.8, 38.8], [79, 79, 43.5], color=C_DAT, lw=2.4)
    flecha(ax, (38.8, 43.5), (35, 43.5), C_DAT, lw=2.4)
    ax.text(37.0, 51, "datos 50052", rotation=90, ha="left", va="center", fontsize=6.6,
            color=C_DAT, fontweight="bold")

    ax.text(60, 19.2, "Un nodo de metadatos por zona: si cae una zona entera quedan 2 de 3 (mayoría). Las 3 copias "
            "de cada bloque van en zonas distintas cuando se puede.", ha="center", va="center",
            fontsize=7.6, style="italic")
    ax.text(60, 16.1, "Se dibuja un ejemplo de cada conexión. El cliente habla con los 3 nodos de metadatos y los "
            "4 de datos, y todos los nodos de datos envían latidos a los 3 nodos de metadatos.",
            ha="center", va="center", fontsize=7.0, color="#455A64")

    caja(ax, 1, 0.5, 118, 12.5, "Despliegue reproducible con AWS CLI (deploy/aws/)", [
        "crear_infraestructura.sh crea la VPC, las subredes, los endpoints, los grupos, Route 53, la clave KMS, el bucket y la llave SSH",
        "subir_artefactos.sh sube la imagen, los certificados y un Compose por rol. crear_instancias.sh crea las 9 EC2, que se configuran solas",
        "redesplegar.sh actualiza las máquinas por el bastión y destruir.sh borra todo sin tocar los recursos propios del laboratorio"],
        fc="#FFFDE7", tam_titulo=9, tam=7.4)
    guardar(fig, "despliegue")


if __name__ == "__main__":
    diagrama_contexto()
    diagrama_componentes()
    diagrama_despliegue()
    print("Diagramas generados en", OUT)
