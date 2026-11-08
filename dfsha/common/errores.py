"""Errores de DFSha sobre gRPC (ver la sección Errores de docs/api.md).

Cada error es un código de estado estándar de gRPC más un ErrorDetail serializado en los
metadatos de cierre, con la clave "dfsha-error-bin". El servidor lo envía con abortar() y el
cliente lo recupera con detalle_de().
"""
from __future__ import annotations

import grpc

from dfsha._gen import dfsha_pb2 as pb

CLAVE_METADATOS = "dfsha-error-bin"

# Código de gRPC que corresponde a cada motivo (tabla de docs/api.md)
CODIGO_DE_MOTIVO = {
    pb.REASON_TOKEN_INVALID: grpc.StatusCode.UNAUTHENTICATED,
    pb.REASON_FORBIDDEN: grpc.StatusCode.PERMISSION_DENIED,
    pb.REASON_NOT_FOUND: grpc.StatusCode.NOT_FOUND,
    pb.REASON_EXISTS: grpc.StatusCode.ALREADY_EXISTS,
    pb.REASON_DIR_NOT_EMPTY: grpc.StatusCode.FAILED_PRECONDITION,
    pb.REASON_NOT_LEADER: grpc.StatusCode.FAILED_PRECONDITION,
    pb.REASON_VERSION_CONFLICT: grpc.StatusCode.ABORTED,
    pb.REASON_LEASE_EXPIRED: grpc.StatusCode.ABORTED,
    pb.REASON_LOCKED: grpc.StatusCode.ABORTED,
    pb.REASON_NO_QUORUM: grpc.StatusCode.UNAVAILABLE,
    pb.REASON_INSUFFICIENT_REPLICAS: grpc.StatusCode.UNAVAILABLE,
    pb.REASON_QUOTA_EXCEEDED: grpc.StatusCode.RESOURCE_EXHAUSTED,
    pb.REASON_NO_SPACE: grpc.StatusCode.RESOURCE_EXHAUSTED,
    pb.REASON_OVERLOADED: grpc.StatusCode.RESOURCE_EXHAUSTED,
    pb.REASON_CHECKSUM_MISMATCH: grpc.StatusCode.DATA_LOSS,
    pb.REASON_BAD_OFFSET: grpc.StatusCode.INVALID_ARGUMENT,
}


async def abortar(context: grpc.aio.ServicerContext, motivo: int, mensaje: str, **datos) -> None:
    """Termina la llamada con el código de gRPC del motivo y el ErrorDetail en los metadatos.

    Los datos son campos de ErrorDetail, por ejemplo abortar(ctx, pb.REASON_NOT_LEADER, "...",
    leader="mds2.dfsha.internal").
    """
    detalle = pb.ErrorDetail(reason=motivo, message=mensaje, **datos)
    context.set_trailing_metadata(((CLAVE_METADATOS, detalle.SerializeToString()),))
    await context.abort(CODIGO_DE_MOTIVO[motivo], mensaje)


def detalle_de(error: grpc.aio.AioRpcError | grpc.RpcError) -> pb.ErrorDetail | None:
    """ErrorDetail de un error recibido, o None si el servidor no envió uno."""
    metadatos = error.trailing_metadata() or ()
    for clave, valor in metadatos:
        if clave == CLAVE_METADATOS:
            return pb.ErrorDetail.FromString(valor)
    return None


def motivo_de(error: grpc.aio.AioRpcError | grpc.RpcError) -> str:
    """Nombre corto del motivo (por ejemplo "LOCKED"), o el código de gRPC si no hay detalle."""
    detalle = detalle_de(error)
    if detalle is None:
        return error.code().name
    return pb.Reason.Name(detalle.reason).removeprefix("REASON_")
