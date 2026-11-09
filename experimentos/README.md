# Experimentos

Experimentos de D11 sobre el despliegue de AWS (deploy/aws/).

| Archivo | Qué hace |
|---|---|
| `carga.py` | Carga que corre en las máquinas cliente, un subcomando por experimento |
| `ejecutar.py` | Copia la carga a los clientes, crea usuarios de prueba, ejecuta los experimentos e inyecta las fallas por SSH (docker kill y stop, kill -9, tc netem y nftables) |
| `analizar.py` | Calcula las estadísticas y escribe `resultados/resumen.md` |
| `resultados/` | Resultados en bruto (`resultados.json`), registro de la corrida y resumen |

```
python experimentos/ejecutar.py            # todos, o solo algunos: python experimentos/ejecutar.py e1 e6
python experimentos/analizar.py
```

En Windows con Git Bash se exporta antes `AWS_BIN="/c/Program Files/Amazon/AWSCLIV2/aws.exe"`.
