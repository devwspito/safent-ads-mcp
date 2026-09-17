# Estudio creativo — infraestructura local (DORMIDA, no es el camino soportado)

> ## ⛔ AVISO: la generación local NO se usa
>
> El 9-sep-2026 esta infraestructura **tumbó la DGX Spark cuatro veces** por temperatura.
> Patrón medido: GPU de 0% a ~96%, de ~70 °C a **94-95 °C en uno o dos minutos**, corte en
> seco con el journal truncado. Una caída dejó la máquina apagada **54 minutos**. No era
> memoria (MemAvailable 96-102 GiB, cero OOM). Al parar el contenedor: 83 °C/96% → 64 °C/0%
> en 20 segundos. La Spark **no expone límite de potencia** (`power.limit` = `N/A`), así que
> la única palanca sería capar el reloj, y eso penaliza a toda la máquina.
>
> **El camino soportado es que el agente llame a herramientas y el proveedor haga el trabajo
> pesado.** Ver `research/creative-via-codex.md`.
>
> Lo que hay aquí (modelos descargados, flujos de ComfyUI verificados, recetas de TTS y
> música) se conserva **dormido** por si algún día interesa. Reactivarlo exige, sin excepción:
> autorización explícita del dueño para esa tanda · máquina sin otra carga · reloj capado
> (`nvidia-smi -lgc 300,2100`) · guardia térmica que no encole por encima de 80 °C y mate el
> trabajo a 85 °C. Los `compose.yaml` están con `restart: "no"` a propósito.

