"""`run_telegram_channel`: arranca/para el long-polling de aiogram dentro
de `ads-worker` (contracts/telegram.md dice "el proceso `ads-api`";
decision documentada aqui porque `ads-api` puede replicarse horizontalmente
y dos replicas compitiendo por el mismo `getUpdates` producirian
`Conflict: terminated by other long poll` -- `ads-worker` es el unico
proceso singleton de este despliegue, ver `composition/worker.py`). Una
sola llamada desde `composition/worker.py`: si Telegram no esta
configurado, no hace nada; si lo esta, bloquea hasta que `stop_event` se
active y entonces para el `Dispatcher` de forma ordenada."""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.notifications.application.ports import MessengerPort
from safent_ads.notifications.application.telegram_pairing import DeliverTelegramTestMessages
from safent_ads.notifications.infrastructure.telegram_callback_resolver import (
    ExecutionUseCasesFactory,
    TelegramCallbackResolver,
)
from safent_ads.notifications.infrastructure.telegram_command_resolvers import (
    TelegramBrakeCallbackResolver,
    TelegramBrakeCommandResolver,
    TelegramPendingCommandResolver,
    TelegramStatusCommandResolver,
)
from safent_ads.notifications.infrastructure.telegram_pairing_command_resolver import (
    TelegramPairingCommandResolver,
)
from safent_ads.notifications.infrastructure.telegram_pairing_sql import SqlTestMessageOutbox
from safent_ads.shared.clock import Clock

logger = structlog.get_logger(__name__)

# FR-25: `POST /telegram/pairing/test-message` encola en `ads-api`, este
# proceso drena -- 5s de latencia maxima es aceptable para un "mensaje de
# prueba" que el propietario pide con un clic y espera ver casi al
# instante, muy por debajo del ciclo de 15/60 min de `NotificationCycle`.
_TEST_MESSAGE_DRAIN_INTERVAL_SECONDS = 5.0
_TEST_MESSAGE_DRAIN_BATCH_SIZE = 10


async def run_telegram_channel(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    execution_use_cases_factory: ExecutionUseCasesFactory,
    bot_token: str | None,
    owner_chat_ids: tuple[int, ...],
    clock: Clock,
    stop_event: asyncio.Event,
) -> None:
    if not bot_token or not owner_chat_ids:
        logger.info("telegram_channel_not_configured")
        return
    # Import local: solo `ads-worker` carga `aiogram`/el bot real, y solo
    # cuando hay credenciales -- mismo criterio que
    # `orchestration/infrastructure/runtime.py::_build_messenger`.
    from safent_ads.notifications.infrastructure.aiogram_messenger import (  # noqa: PLC0415
        AiogramMessenger,
    )

    resolver = TelegramCallbackResolver(
        session_factory=session_factory,
        execution_use_cases_factory=execution_use_cases_factory,
        clock=clock,
    )
    pairing_resolver = TelegramPairingCommandResolver(session_factory=session_factory, clock=clock)
    status_resolver = TelegramStatusCommandResolver(session_factory=session_factory, clock=clock)
    brake_command_resolver = TelegramBrakeCommandResolver(
        session_factory=session_factory,
        execution_use_cases_factory=execution_use_cases_factory,
        clock=clock,
    )
    brake_callback_resolver = TelegramBrakeCallbackResolver(
        session_factory=session_factory,
        execution_use_cases_factory=execution_use_cases_factory,
        clock=clock,
    )
    messenger = AiogramMessenger(
        bot_token=bot_token,
        owner_chat_ids=owner_chat_ids,
        callback_resolver=resolver,
        pairing_command_resolver=pairing_resolver,
        status_command_resolver=status_resolver,
        brake_command_resolver=brake_command_resolver,
        brake_callback_resolver=brake_callback_resolver,
    )
    # `/pendientes` (US3, esta rama) necesita enviar por este mismo `Bot`
    # (contracts/telegram.md §1: uno solo por proceso) -- se cablea DESPUES
    # de construir `messenger` para romper el ciclo (ver docstring de
    # `AiogramMessenger.__init__`).
    messenger.set_pending_command_resolver(
        TelegramPendingCommandResolver(
            session_factory=session_factory,
            execution_use_cases_factory=execution_use_cases_factory,
            messenger=messenger,
            clock=clock,
        )
    )
    logger.info("telegram_channel_starting")
    polling_task = asyncio.create_task(messenger.start_polling())
    drain_task = asyncio.create_task(
        _drain_test_messages_loop(
            session_factory=session_factory, messenger=messenger, stop_event=stop_event
        )
    )
    try:
        await stop_event.wait()
    finally:
        await messenger.stop_polling()
        await polling_task
        await drain_task
        await messenger.aclose()
        logger.info("telegram_channel_stopped")


async def _drain_test_messages_loop(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    messenger: MessengerPort,
    stop_event: asyncio.Event,
) -> None:
    """`POST /telegram/pairing/test-message` (FR-25) encola en `ads-api`;
    este bucle es quien de verdad habla con Telegram, con el mismo `Bot`
    que ya sostiene el long-polling. Un fallo de una ronda no debe tumbar
    el long-polling: se registra y se reintenta en la siguiente vuelta."""
    while not stop_event.is_set():
        try:
            async with session_factory() as session:
                deliver = DeliverTelegramTestMessages(
                    outbox=SqlTestMessageOutbox(session), messenger=messenger
                )
                delivered = await deliver.execute(limit=_TEST_MESSAGE_DRAIN_BATCH_SIZE)
                await session.commit()
                if delivered:
                    logger.info("telegram_test_messages_delivered", count=delivered)
        except Exception:  # noqa: BLE001 - una ronda fallida no debe tumbar el long-polling
            logger.exception("telegram_test_message_drain_failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                stop_event.wait(), timeout=_TEST_MESSAGE_DRAIN_INTERVAL_SECONDS
            )
