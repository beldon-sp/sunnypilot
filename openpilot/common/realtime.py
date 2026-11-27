"""Utilities for reading real time clocks and keeping soft real time constraints."""
import gc
import os
import sys
import time

from setproctitle import getproctitle

from openpilot.common.utils import MovingAverage
from openpilot.common.hardware import PC


# time step for each process
DT_CTRL = 0.01  # controlsd
DT_MDL = 0.05  # model
DT_HW = 0.5  # hardwared and manager
DT_DMON = 0.05  # driver monitoring


class Priority:
  # CORE 2
  # - modeld = 55
  # - camerad = 54
  CTRL_LOW = 51 # plannerd & radard

  # CORE 3
  # - pandad = 55
  CTRL_HIGH = 53


def drop_realtime() -> None:
  if sys.platform == 'linux' and not PC:
    os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))


def set_core_affinity(cores: list[int]) -> None:
  if sys.platform == 'linux' and not PC:
    os.sched_setaffinity(0, cores)


def config_realtime_process(cores: int | list[int], priority: int) -> None:
  gc.disable()
  if sys.platform == 'linux' and not PC:
    os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(priority))
  c = cores if isinstance(cores, list) else [cores, ]
  set_core_affinity(c)


class PeriodicGC:
  """Explicit cyclic-GC sweep for realtime processes.

  config_realtime_process disables the cyclic GC (upstream behaviour), but the
  capnp builders these daemons churn per frame form reference cycles their
  wrappers never release, so without an explicit sweep the process leaks arena
  memory (measured ~1.1 GB/h in radard: 39 -> 104 MB in 150 s with gc disabled,
  flat with gc enabled) until long drives exhaust the device and trigger
  lowMemory alerts plus the page-reclaim stalls behind locationdTemporaryError.
  A full collect every ~30 s bounds retention at ~20 MB; the pause is a few ms
  against typical frame budgets.
  """

  def __init__(self, every_n_frames: int = 600) -> None:
    self.every_n_frames = every_n_frames
    self._counter = 0

  def tick(self) -> None:
    self._counter += 1
    if self._counter >= self.every_n_frames:
      gc.collect()
      self._counter = 0


class Ratekeeper:
  def __init__(self, rate: float, print_delay_threshold: float | None = 0.0) -> None:
    """Rate in Hz for ratekeeping. print_delay_threshold must be nonnegative."""
    self._interval = 1. / rate
    self._print_delay_threshold = print_delay_threshold
    self._frame = 0
    self._remaining = 0.0
    self._process_name = getproctitle()
    self._last_monitor_time = -1.
    self._next_frame_time = -1.

    self.avg_dt = MovingAverage(100)
    self.avg_dt.add_value(self._interval)

  @property
  def frame(self) -> int:
    return self._frame

  @property
  def remaining(self) -> float:
    return self._remaining

  @property
  def lagging(self) -> bool:
    expected_dt = self._interval * (1 / 0.9)
    return self.avg_dt.get_average() > expected_dt

  # Maintain loop rate by calling this at the end of each loop
  def keep_time(self) -> bool:
    lagged = self.monitor_time()
    if self._remaining > 0:
      time.sleep(self._remaining)
    return lagged

  # Monitors the cumulative lag, but does not enforce a rate
  def monitor_time(self) -> bool:
    if self._last_monitor_time < 0:
      self._next_frame_time = time.monotonic() + self._interval
      self._last_monitor_time = time.monotonic()

    prev = self._last_monitor_time
    self._last_monitor_time = time.monotonic()
    self.avg_dt.add_value(self._last_monitor_time - prev)

    lagged = False
    now = time.monotonic()
    remaining = self._next_frame_time - now
    if remaining < -self._interval:
      # more than a frame behind (e.g. a stall): resync instead of running catch-up frames back-to-back
      self._next_frame_time = now + self._interval
    else:
      self._next_frame_time += self._interval
    if self._print_delay_threshold is not None and remaining < -self._print_delay_threshold:
      print(f"{self._process_name} lagging by {-remaining * 1000:.2f} ms")
      lagged = True
    self._frame += 1
    self._remaining = remaining
    return lagged
