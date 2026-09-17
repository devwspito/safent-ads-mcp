"""Ciclos de orquestacion (plan.md §7, T047): `IngestionCycle`,
`SignalCycle`, `NotificationCycle`. Cada uno depende solo de puertos
declarados en `ports.py`; las implementaciones reales de ingesta y
evaluacion de senales las cablea la integracion (contextos `metrics` y
`signals`, lanes en paralelo)."""
