"""El contrato con etcd compila y expone los servicios y operaciones que usa DFSha."""
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module", autouse=True)
def codigo_generado():
    resultado = subprocess.run(
        [sys.executable, str(RAIZ / "scripts" / "generar_codigo.py")],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    importlib.invalidate_caches()


@pytest.mark.parametrize("modulo, servicio, operaciones", [
    ("etcd.api.etcdserverpb.rpc", "KV", {"Range", "Put", "DeleteRange", "Txn"}),
    ("etcd.api.etcdserverpb.rpc", "Lease", {"LeaseGrant", "LeaseRevoke", "LeaseKeepAlive"}),
    ("etcd.api.etcdserverpb.rpc", "Watch", {"Watch"}),
    ("etcd.api.v3lockpb.v3lock", "Lock", {"Lock", "Unlock"}),
    ("etcd.api.v3electionpb.v3election", "Election", {"Campaign", "Leader", "Resign", "Observe"}),
])
def test_servicio_de_etcd(modulo, servicio, operaciones):
    pb2 = importlib.import_module(f"dfsha._gen.{modulo}_pb2")
    pb2_grpc = importlib.import_module(f"dfsha._gen.{modulo}_pb2_grpc")
    descriptor = pb2.DESCRIPTOR.services_by_name[servicio]
    assert operaciones <= {m.name for m in descriptor.methods}
    assert hasattr(pb2_grpc, f"{servicio}Stub")


def test_comparacion_por_revision_de_creacion():
    """El token de cerco se compara con Compare.CREATE, así que ese objetivo debe existir."""
    rpc = importlib.import_module("dfsha._gen.etcd.api.etcdserverpb.rpc_pb2")
    comparacion = rpc.Compare(key=b"/dfsha/locks/x/b0/1", target=rpc.Compare.CREATE,
                              result=rpc.Compare.EQUAL, create_revision=4)
    assert rpc.Compare.FromString(comparacion.SerializeToString()).create_revision == 4


def test_protos_sin_anotaciones():
    """Los .proto de etcd no deben importar ni usar las anotaciones quitadas (los comentarios no cuentan)."""
    for proto in (RAIZ / "proto" / "etcd").rglob("*.proto"):
        codigo = "\n".join(
            linea.split("//", 1)[0] for linea in proto.read_text(encoding="utf-8").splitlines()
        )
        assert "gogoproto" not in codigo and "google.api" not in codigo, proto.name
