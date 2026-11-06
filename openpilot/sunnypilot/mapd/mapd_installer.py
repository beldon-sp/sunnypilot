#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import hashlib
import logging
import os
import stat
import time
import traceback
import requests
from pathlib import Path
from urllib.request import urlopen

from openpilot.cereal import messaging
from openpilot.common.params import Params
from openpilot.common.hardware.hw import Paths
from openpilot.common.spinner import Spinner
from openpilot.common.version import is_prebuilt
from openpilot.sunnypilot import get_file_hash
from openpilot.sunnypilot.mapd import MAPD_PATH, MAPD_BIN_DIR
from openpilot.sunnypilot.mapd.mirrors import requests_proxies

VERSION = "v1.12.0"
URL = f"https://github.com/pfeiferj/openpilot-mapd/releases/download/{VERSION}/mapd"

# The vendored binary is NOT the upstream release artifact: it is rebuilt from upstream source with
# third_party/mapd_pfeiferj/mapd_parallel.patch (parallel tile downloads, see that README). Every
# download is sha256-verified before it may replace the installed file, so a broken/mutated mirror
# can never win: the installer just moves on to the next candidate.
MAPD_SHA256 = "ec90f5f0da1a11d87d78bb6509acff30c5990f4f7c59431da84c2381420216e2"    # vendored (parallel-patched) build
UPSTREAM_SHA256 = "fdb3b49ee19956e6ce09fdc3373cbba557f1263b2180e9f344c1d4053852284b"  # stock v1.12.0 release binary

# Download candidates in preference order. The repo mirrors serve the patched build straight out of
# this repo's git host (CN-reachable); the upstream release is the last resort and installs the
# stock binary if everything else failed.
VENDORED_REPO_PATH = "openpilot/third_party/mapd_pfeiferj/mapd"
DOWNLOAD_CANDIDATES = [
  (f"https://gh-proxy.com/https://raw.githubusercontent.com/beldon-sp/sunnypilot/byd-tici/{VENDORED_REPO_PATH}", MAPD_SHA256),
  (f"https://raw.githubusercontent.com/beldon-sp/sunnypilot/byd-tici/{VENDORED_REPO_PATH}", MAPD_SHA256),
  (f"https://cdn.jsdelivr.net/gh/beldon-sp/sunnypilot@byd-tici/{VENDORED_REPO_PATH}", MAPD_SHA256),
  (URL, UPSTREAM_SHA256),
]


def update_installed_version(version: str, params: Params | None = None) -> None:
  if params is None:
    params = Params()

  params.put("MapdVersion", version, block=True)


def _sha256_bytes(content: bytes) -> str:
  return hashlib.sha256(content).hexdigest()


class MapdInstallManager:
  def __init__(self, spinner_ref: Spinner):
    self._spinner = spinner_ref
    self._params = Params()

  def download(self) -> None:
    self.ensure_directories_exist()
    self._download_file()
    update_installed_version(VERSION, self._params)

  def check_and_download(self) -> None:
    if self.download_needed():
      self.download()
    else:
      update_installed_version(VERSION, self._params)

  def download_needed(self) -> bool:
    # The vendored tree ships the binary, so the common case is "already here and good" - only a
    # missing or unrecognized file triggers a download. A stock upstream binary (last-resort
    # candidate) is accepted too: re-attempting the upgrade on every boot would burn ~10MB of
    # download whenever the repo mirrors are down. The patched build comes back with the next
    # release tree instead (OTA replaces the whole file).
    if not os.path.exists(MAPD_PATH):
      return True
    installed_hash = get_file_hash(MAPD_PATH)
    return installed_hash not in (MAPD_SHA256, UPSTREAM_SHA256)

  @staticmethod
  def ensure_directories_exist() -> None:
    if not os.path.exists(Paths.mapd_root()):
      os.makedirs(Paths.mapd_root())
    if not os.path.exists(MAPD_BIN_DIR):
      os.makedirs(MAPD_BIN_DIR)

  @staticmethod
  def _safe_write_and_set_executable(file_path: Path, content: bytes) -> None:
    with open(file_path, 'wb') as output:
      output.write(content)
      output.flush()
      os.fsync(output.fileno())
    current_permissions = stat.S_IMODE(os.lstat(file_path).st_mode)
    os.chmod(file_path, current_permissions | stat.S_IEXEC)

  def _download_candidate(self, url: str, expected_sha256: str, num_retries: int, download_timeout: int) -> bool:
    temp_file = Path(MAPD_PATH + ".tmp")
    for cnt in range(num_retries):
      try:
        response = requests.get(url, stream=True, timeout=download_timeout, proxies=requests_proxies(self._params))
        response.raise_for_status()
        content = response.content
        file_hash = _sha256_bytes(content)
        if file_hash != expected_sha256:
          self._spinner.update(f"Checksum mismatch for {url}: got {file_hash[:12]}..., expected {expected_sha256[:12]}...")
          return False
        self._safe_write_and_set_executable(temp_file, content)
        # No exceptions encountered. Safe to replace original file.
        temp_file.replace(MAPD_PATH)
        return True
      except requests.exceptions.ReadTimeout:
        self._spinner.update(f"ReadTimeout caught. Timeout is [{download_timeout}]. Retrying download... [{cnt}]")
        time.sleep(0.5)
      except requests.exceptions.RequestException as e:
        self._spinner.update(f"RequestException caught: {e}. Retrying download... [{cnt}]")
        time.sleep(0.5)
    return False

  def _download_file(self, num_retries=3) -> None:
    download_timeout = 60
    for url, expected_sha256 in DOWNLOAD_CANDIDATES:
      if self._download_candidate(url, expected_sha256, num_retries, download_timeout):
        return
      time.sleep(0.5)

    # Delete temp file if the process was not successful.
    temp_file = Path(MAPD_PATH + ".tmp")
    if temp_file.exists():
      temp_file.unlink()
    logging.error("Failed to download file from any mirror")

  def get_installed_version(self) -> str:
    return str(self._params.get("MapdVersion") or "")

  def wait_for_internet_connection(self, return_on_failure: bool = False) -> bool:
    max_retries = 10
    for retries in range(max_retries + 1):
      self._spinner.update(f"Waiting for internet connection... [{retries}/{max_retries}]")
      time.sleep(2)
      try:
        _ = urlopen('https://sentry.io', timeout=10)
        return True
      except Exception as e:
        print(f'Wait for internet failed: {e}')
        if return_on_failure and retries == max_retries:
          return False

    return False

  def non_prebuilt_install(self) -> None:
    sm = messaging.SubMaster(['deviceState'])
    metered = sm['deviceState'].networkMetered

    if metered:
      self._spinner.update("Can't proceed with mapd install since network is metered!")
      time.sleep(5)
      return

    try:
      self.ensure_directories_exist()
      if not self.download_needed():
        self._spinner.update("Mapd is good!")
        time.sleep(0.1)
        return

      if self.wait_for_internet_connection(return_on_failure=True):
        self._spinner.update(f"Downloading pfeiferj's mapd [{self.get_installed_version()}] => [{VERSION}].")
        time.sleep(0.1)
        self.check_and_download()
      self._spinner.close()

    except Exception:
      for i in range(6):
        self._spinner.update("Failed to download OSM maps won't work until properly downloaded!" +
                             "Try again manually rebooting. " +
                             f"Boot will continue in {5 - i}s...")
        time.sleep(1)

      traceback.print_exc()


if __name__ == "__main__":
  spinner = Spinner()
  install_manager = MapdInstallManager(spinner)
  install_manager.ensure_directories_exist()
  if is_prebuilt():
    debug_msg = f"[DEBUG] This is prebuilt, no mapd install required. VERSION: [{VERSION}], Param [{install_manager.get_installed_version()}]"
    spinner.update(debug_msg)
    update_installed_version(VERSION)
  else:
    spinner.update(f"Checking if mapd is installed and valid. Prebuilt [{is_prebuilt()}]")
    install_manager.non_prebuilt_install()
