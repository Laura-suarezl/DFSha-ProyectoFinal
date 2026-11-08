# Imagen de DFSha. La misma imagen sirve para todos los roles y el rol lo decide el comando
# (python -m dfsha.datanode o python -m dfsha.metadata, y el cliente se agrega después).
# No contiene llaves ni certificados, que se montan al arrancar en /etc/dfsha/certs (docs/tls.md).
#
# Construir desde la raíz del repositorio con
#   docker build -f deploy/docker/Dockerfile -t dfsha:dev .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY proto ./proto
COPY scripts ./scripts
COPY dfsha ./dfsha

# El código gRPC se genera dentro de la imagen y se instala junto con el paquete.
RUN pip install "grpcio-tools>=1.66" \
    && python scripts/generar_codigo.py \
    && pip install . \
    && rm -rf build dfsha.egg-info \
    && python -c "import dfsha._gen.dfsha_pb2, dfsha.datanode.servicio, dfsha.metadata.servicio"

# Usuario sin privilegios, dueño solo de su directorio de datos.
RUN useradd --system --uid 10001 --no-create-home dfsha \
    && mkdir -p /var/lib/dfsha \
    && chown dfsha /var/lib/dfsha
USER dfsha

# 50051 y 50052 para clientes (TLS), 50053 y 50054 entre servidores (TLS mutuo)
EXPOSE 50051 50052 50053 50054

CMD ["python", "-m", "dfsha.datanode"]
