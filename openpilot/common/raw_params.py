# raw param access helpers: read a key even before the compiled params
# registry (params_pyx.so on device) knows it - falls back to the raw file
# under /data/params, which is the manual provisioning path for brand-new keys.
# Ported from carrot (openpilot/common/raw_params.py) for the byd desire
# helper's LaneChangeAssistSpeed / BydRadarGapGate reads.
import logging

from openpilot.common.params import Params, UnknownKeyName

logger = logging.getLogger(__name__)


def get_int_param(params: Params, key: str, default: int) -> int:
  try:
    return int(params.get(key, return_default=True))
  except UnknownKeyName:
    pass
  except (TypeError, ValueError):
    return default
  try:
    with open(params.get_param_path(key)) as f:
      val = f.read().strip()
    return int(val) if val else default
  except (OSError, ValueError):
    return default


def get_float_param(params: Params, key: str, default: float) -> float:
  try:
    return float(params.get(key, return_default=True))
  except UnknownKeyName:
    pass
  except (TypeError, ValueError):
    return default
  try:
    with open(params.get_param_path(key)) as f:
      val = f.read().strip()
    return float(val) if val else default
  except (OSError, ValueError):
    return default
