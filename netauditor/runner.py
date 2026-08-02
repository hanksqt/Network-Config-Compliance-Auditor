"""Run collection across many devices concurrently.

SSH to a network device is almost entirely wait time, so a thread pool gives a
near-linear speedup and keeps a single unreachable device from stalling the
whole run behind its connect timeout.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Sequence

from . import connect
from .connect import Connector
from .models import Device, DeviceResult

log = logging.getLogger(__name__)

DEFAULT_WORKERS = 8

#: Called with each result as it lands, for live progress output.
ProgressCallback = Callable[[DeviceResult], None]


def run(
    devices: Sequence[Device],
    commands: Sequence[str] | None = None,
    *,
    workers: int = DEFAULT_WORKERS,
    retries: int = 1,
    retry_delay: float = 2.0,
    connector: Connector = connect.ConnectHandler,
    on_result: ProgressCallback | None = None,
) -> list[DeviceResult]:
    """Collect from every device, returning results in inventory order.

    Args:
        commands: commands to run on every device. If ``None``, each device
            runs its own ``backup_command`` (which is platform-specific).
        workers: max concurrent SSH sessions.
        on_result: invoked as each device finishes, for progress display.
    """
    if not devices:
        return []

    pool_size = max(1, min(workers, len(devices)))
    results: dict[str, DeviceResult] = {}

    log.debug("running %d device(s) with %d worker(s)", len(devices), pool_size)

    with ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="netaudit") as pool:
        futures = {
            pool.submit(
                connect.collect,
                device,
                commands if commands is not None else [device.backup_command],
                retries=retries,
                retry_delay=retry_delay,
                connector=connector,
            ): device
            for device in devices
        }
        for future in as_completed(futures):
            device = futures[future]
            result = future.result()  # collect() never raises
            results[device.name] = result
            if on_result is not None:
                on_result(result)

    # Preserve inventory order so console output is stable between runs.
    return [results[device.name] for device in devices]
