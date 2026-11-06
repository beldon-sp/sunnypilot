"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest
import requests

from openpilot.common.params import Params
from openpilot.common.test import OpenpilotTestCase
from openpilot.sunnypilot.mapd import mirrors as mirrors_module
from openpilot.sunnypilot.mapd.mapd_installer import MapdInstallManager, _sha256_bytes, DOWNLOAD_CANDIDATES
from openpilot.sunnypilot.mapd.mirrors import fetch_json_with_fallback, get_download_proxy, mirror_url, requests_proxies

import openpilot.sunnypilot.mapd.mapd_installer as installer

RAW_URL = "https://raw.githubusercontent.com/pfeiferj/openpilot-mapd/main/nation_bounding_boxes.json"
JSDELIVR_URL = "https://cdn.jsdelivr.net/gh/pfeiferj/openpilot-mapd@main/nation_bounding_boxes.json"
VENDORED_REPO_PATH = "openpilot/third_party/mapd_pfeiferj/mapd"
UPSTREAM_RELEASE_PREFIX = "https://github.com/pfeiferj/openpilot-mapd/releases/download/"

TEST_CONTENT = b"patched-mapd-binary"
TEST_SHA = hashlib.sha256(TEST_CONTENT).hexdigest()


def _temp_params() -> Params:
  return Params(tempfile.mkdtemp())


def _ok_response(content: bytes, json_value=None) -> mock.Mock:
  response = mock.Mock(status_code=200)
  response.raise_for_status.return_value = None
  response.content = content
  try:
    response.json.return_value = json.loads(content) if json_value is None else json_value
  except (UnicodeDecodeError, json.JSONDecodeError):
    pass  # binary payload; callers on this path never read .json()
  return response


class TestOsmMirrors(OpenpilotTestCase):
  def test_mirror_url_rewrites_known_hosts(self):
    assert mirror_url(RAW_URL, _temp_params()) == JSDELIVR_URL

  def test_mirror_url_passes_through_unknown_hosts(self):
    other = "https://example.com/some.json"
    assert mirror_url(other, _temp_params()) == other

  def test_mirror_url_disabled_param_bypasses_mirror(self):
    params = _temp_params()
    params.put_bool("OsmMirrorDisabled", True, block=True)
    assert mirror_url(RAW_URL, Params(params.d)) == RAW_URL

  def test_proxy_param_round_trip(self):
    params = _temp_params()
    assert get_download_proxy(params) == ""
    assert requests_proxies(params) is None

    params.put("OsmProxyUrl", "socks5://10.0.0.2:1080", block=True)
    reread = Params(params.d)
    assert get_download_proxy(reread) == "socks5://10.0.0.2:1080"
    assert requests_proxies(reread) == {"http": "socks5://10.0.0.2:1080", "https": "socks5://10.0.0.2:1080"}

  def test_fetch_json_prefers_mirror(self):
    with mock.patch.object(mirrors_module.requests, "get", return_value=_ok_response(b'{"CN": 1}')) as get:
      data = fetch_json_with_fallback(RAW_URL, params=_temp_params())
    assert data == {"CN": 1}
    assert get.call_count == 1
    assert get.call_args.args[0] == JSDELIVR_URL

  def test_fetch_json_falls_back_to_origin(self):
    with mock.patch.object(mirrors_module.requests, "get",
                           side_effect=[requests.exceptions.ConnectTimeout("mirrors down"), _ok_response(b'{}')]) as get:
      assert fetch_json_with_fallback(RAW_URL, params=_temp_params()) == {}
    assert [c.args[0] for c in get.call_args_list] == [JSDELIVR_URL, RAW_URL]

  def test_fetch_json_returns_none_when_all_fail(self):
    with mock.patch.object(mirrors_module.requests, "get", side_effect=requests.exceptions.ConnectTimeout("no network")):
      assert fetch_json_with_fallback(RAW_URL, params=_temp_params()) is None


class TestMapdInstaller(OpenpilotTestCase):
  def setUp(self):
    super().setUp()
    self.tmp = Path(tempfile.mkdtemp())
    self.mapd_path = self.tmp / "mapd"
    self.path_patch = mock.patch.object(installer, "MAPD_PATH", str(self.mapd_path))
    self.path_patch.start()
    self.addCleanup(self.path_patch.stop)

  def _manager(self) -> MapdInstallManager:
    manager = MapdInstallManager(mock.Mock())
    manager._params = _temp_params()
    return manager

  def test_download_candidates_cover_repo_mirrors_then_upstream(self):
    assert all(url.endswith(VENDORED_REPO_PATH) for url, _ in DOWNLOAD_CANDIDATES[:-1]), \
      "repo mirrors must serve the vendored patched binary"
    assert DOWNLOAD_CANDIDATES[-1][0].startswith(UPSTREAM_RELEASE_PREFIX), "upstream release is the last resort"
    assert all(sha for _, sha in DOWNLOAD_CANDIDATES), "every candidate needs a pinned sha256"

  def test_download_needed_for_missing_or_unknown_file(self):
    manager = self._manager()
    assert manager.download_needed() is True  # missing file

    self.mapd_path.write_bytes(TEST_CONTENT)
    with mock.patch.object(installer, "MAPD_SHA256", TEST_SHA):
      assert manager.download_needed() is False  # matches vendored hash

      self.mapd_path.write_bytes(TEST_CONTENT + b"corrupted")
      assert manager.download_needed() is True  # unknown hash

  def test_download_file_uses_first_working_candidate(self):
    candidates = [(f"https://mirror-a/{VENDORED_REPO_PATH}", TEST_SHA), (f"https://mirror-b/{VENDORED_REPO_PATH}", TEST_SHA)]
    manager = self._manager()
    # first attempt of candidate 0 gets a 500, the retry gets a working response -> candidate 0 wins
    with mock.patch.object(installer, "DOWNLOAD_CANDIDATES", candidates), \
         mock.patch.object(installer.requests, "get", side_effect=[self._fail_response(), _ok_response(TEST_CONTENT)]) as get:
      manager._download_file()

    assert self.mapd_path.read_bytes() == TEST_CONTENT
    assert os.access(self.mapd_path, os.X_OK)
    assert not Path(str(self.mapd_path) + ".tmp").exists()
    assert [c.args[0] for c in get.call_args_list] == [candidates[0][0], candidates[0][0]]

  def test_download_file_falls_through_checksum_mismatch(self):
    candidates = [(f"https://mirror-a/{VENDORED_REPO_PATH}", TEST_SHA), (f"https://mirror-b/{VENDORED_REPO_PATH}", TEST_SHA)]
    manager = self._manager()
    with mock.patch.object(installer, "DOWNLOAD_CANDIDATES", candidates), \
         mock.patch.object(installer.requests, "get", side_effect=[_ok_response(b"tampered"), _ok_response(TEST_CONTENT)]) as get:
      manager._download_file()

    assert self.mapd_path.read_bytes() == TEST_CONTENT
    assert [c.args[0] for c in get.call_args_list] == [candidates[0][0], candidates[1][0]]

  def test_download_file_retries_transport_error_within_candidate(self):
    candidates = [(f"https://mirror-a/{VENDORED_REPO_PATH}", TEST_SHA)]
    manager = self._manager()
    with mock.patch.object(installer, "DOWNLOAD_CANDIDATES", candidates), \
         mock.patch.object(installer.requests, "get",
                           side_effect=[requests.exceptions.ConnectTimeout("down"), _ok_response(TEST_CONTENT)]) as get:
      manager._download_file()

    assert self.mapd_path.read_bytes() == TEST_CONTENT
    assert [c.args[0] for c in get.call_args_list] == [candidates[0][0], candidates[0][0]]

  def test_download_file_never_writes_checksum_mismatch(self):
    manager = self._manager()
    tampered = _ok_response(b"tampered-binary")  # matches no pinned sha
    with mock.patch.object(installer, "DOWNLOAD_CANDIDATES", [(f"https://mirror-a/{VENDORED_REPO_PATH}", TEST_SHA)]), \
         mock.patch.object(installer.requests, "get", return_value=tampered):
      manager._download_file()

    assert not self.mapd_path.exists()
    assert not Path(str(self.mapd_path) + ".tmp").exists()

  def test_download_file_all_candidates_fail(self):
    manager = self._manager()
    with mock.patch.object(installer, "DOWNLOAD_CANDIDATES", [(f"https://mirror-a/{VENDORED_REPO_PATH}", TEST_SHA)]), \
         mock.patch.object(installer.requests, "get", side_effect=requests.exceptions.ConnectTimeout("down")):
      manager._download_file()

    assert not self.mapd_path.exists()

  @staticmethod
  def _fail_response() -> mock.Mock:
    response = mock.Mock(status_code=500)
    response.raise_for_status.side_effect = requests.exceptions.HTTPError("500 Server Error")
    return response


def test_sha256_bytes_matches_file_hash_helper():
  assert _sha256_bytes(b"abc") == hashlib.sha256(b"abc").hexdigest()


if __name__ == "__main__":
  pytest.main([__file__])
