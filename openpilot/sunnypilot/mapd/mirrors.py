"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

import requests

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog

# China-reachable mirrors for the OSM hosts mapd downloads from. jsDelivr mirrors files out of the
# pfeiferj/openpilot-mapd repo (the bounding-box JSONs); everything is small and re-fetched against
# the origin on failure, so a flaky mirror never produces wrong data.
_MIRROR_HOST_MAP = {
  "https://raw.githubusercontent.com/pfeiferj/openpilot-mapd/main/":
    "https://cdn.jsdelivr.net/gh/pfeiferj/openpilot-mapd@main/",
}


def mirror_url(url: str, params: Params | None = None) -> str:
  """Rewrites known OSM hosts to their China-reachable mirrors for download time only"""
  if url and not (params or Params()).get_bool("OsmMirrorDisabled"):
    for source, mirror in _MIRROR_HOST_MAP.items():
      if url.startswith(source):
        return mirror + url[len(source):]
  return url


def fetch_json_with_fallback(url: str, timeout: float = 10.0, params: Params | None = None) -> dict | list | None:
  """GETs a JSON resource, trying the mirror first and falling back to the origin.

  Returns None when both fail; callers treat that as "listing unavailable" instead of a crash.
  """
  urls = [mirror_url(url, params), url]
  # dedupe while keeping order: when mirroring is disabled (or no mirror is known) this collapses
  # to a single attempt against the origin
  seen: set[str] = set()
  for candidate in urls:
    if candidate in seen:
      continue
    seen.add(candidate)
    try:
      response = requests.get(candidate, timeout=timeout, proxies=requests_proxies(params))
      response.raise_for_status()
      return response.json()
    except Exception as e:
      cloudlog.warning(f"OSM fetch failed for {candidate}: {e}")
  return None


def get_download_proxy(params: Params | None = None) -> str:
  """Reads the OsmProxyUrl param (http:// or socks5:// URL).

  When set it is exported as HTTPS_PROXY/HTTP_PROXY into the mapd process (the Go binary honors
  them) and used for the python-side OSM downloads; empty means direct connection.
  """
  try:
    return str((params or Params()).get("OsmProxyUrl") or "").strip()
  except Exception:
    return ""


def requests_proxies(params: Params | None = None) -> dict | None:
  proxy = get_download_proxy(params)
  return {"http": proxy, "https": proxy} if proxy else None
