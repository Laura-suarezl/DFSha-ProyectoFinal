"""Carga y validación de la configuración (dfsha/common/config.py)."""
import pytest

from dfsha.common.config import MIB, ErrorDeConfiguracion, cargar


def test_valores_por_defecto_son_los_de_las_decisiones():
    cfg = cargar(entorno={})
    assert cfg.cluster.nombre == "dfsha.internal"
    assert (cfg.cluster.puerto_metadatos, cfg.cluster.puerto_datos) == (50051, 50052)
    assert cfg.almacenamiento.tamano_bloque == 16 * MIB
    assert (cfg.almacenamiento.replicas, cfg.almacenamiento.confirmaciones) == (3, 2)
    assert cfg.almacenamiento.tamano_trozo == 1 * MIB
    assert (cfg.tiempos.sesion_ttl_s, cfg.tiempos.renovacion_s, cfg.tiempos.espera_lock_s) == (30, 10, 10)
    assert (cfg.tiempos.latido_s, cfg.tiempos.sospechoso_s, cfg.tiempos.muerto_s) == (3, 10, 30)


def test_el_entorno_gana_sobre_el_archivo(tmp_path):
    archivo = tmp_path / "dfsha.toml"
    archivo.write_text(
        '[almacenamiento]\ntamano_trozo = "64KiB"\nreplicas = 3\n'
        '[nodo]\nnombre = "dn1.dfsha.internal"\nzona = "us-east-1a"\n',
        encoding="utf-8",
    )
    cfg = cargar(archivo, entorno={"DFSHA_NODO_ZONA": "us-east-1b", "DFSHA_TLS_HABILITADO": "false"})
    assert cfg.almacenamiento.tamano_trozo == 64 * 1024
    assert cfg.nodo.nombre == "dn1.dfsha.internal"
    assert cfg.nodo.zona == "us-east-1b"
    assert cfg.tls.habilitado is False


def test_ruta_desde_dfsha_config(tmp_path):
    archivo = tmp_path / "otro.toml"
    archivo.write_text('[cluster]\nnombre = "prueba.local"\n', encoding="utf-8")
    assert cargar(entorno={"DFSHA_CONFIG": str(archivo)}).cluster.nombre == "prueba.local"


@pytest.mark.parametrize("entorno, mensaje", [
    ({"DFSHA_ALMACENAMIENTO_CONFIRMACIONES": "4"}, "confirmaciones"),
    ({"DFSHA_ALMACENAMIENTO_REPLICAS": "1"}, "replicas"),
    ({"DFSHA_ALMACENAMIENTO_TAMANO_TROZO": "8MiB"}, "4 MiB"),
    ({"DFSHA_TIEMPOS_RENOVACION_S": "20"}, "mitad"),
    ({"DFSHA_TIEMPOS_SOSPECHOSO_S": "40"}, "latido_s < sospechoso_s"),
    ({"DFSHA_CLUSTER_PUERTO_DATOS": "50051"}, "distintos"),
    ({"DFSHA_TIEMPOS_LATIDO_S": "tres"}, "valor inválido"),
])
def test_configuraciones_incoherentes_se_rechazan(entorno, mensaje):
    with pytest.raises(ErrorDeConfiguracion, match=mensaje):
        cargar(entorno=entorno)


def test_claves_desconocidas_se_rechazan(tmp_path):
    archivo = tmp_path / "malo.toml"
    archivo.write_text("[almacenamiento]\nreplica = 3\n", encoding="utf-8")
    with pytest.raises(ErrorDeConfiguracion, match="desconocidas"):
        cargar(archivo, entorno={})


def test_es_inmutable():
    cfg = cargar(entorno={})
    with pytest.raises(AttributeError):
        cfg.tiempos.sesion_ttl_s = 5
