from pathlib import Path

from openpilot.common.test import OpenpilotTestCase
from openpilot.common.params import Params
from openpilot.system.athena.registration import register, UNREGISTERED_DONGLE_ID
from openpilot.common.hardware.hw import Paths


class TestRegistration(OpenpilotTestCase):

  def setup_method(self):
    # clear params and setup key paths
    self.params = Params()

    persist_dir = Path(Paths.persist_root()) / "comma"
    persist_dir.mkdir(parents=True, exist_ok=True)

    self.dongle_id = persist_dir / "dongle_id"

  def test_valid_cache(self, mocker):
    # if all params are written, return the cached dongle id.
    # should work with a dongle ID on either /persist/ or normal params
    dongle = "DONGLE_ID_123"
    m = mocker.patch("openpilot.system.athena.registration.api_get", autospec=True)
    for persist, params in [(True, True), (True, False), (False, True)]:
      self.params.put("DongleId", dongle if params else "", block=True)
      with open(self.dongle_id, "w") as f:
        f.write(dongle if persist else "")
      assert register() == dongle
      assert not m.called

  def test_skip_registration(self, mocker):
    # no cached dongle id: registration with the comma.ai backend is
    # skipped entirely and the device stays unregistered
    m = mocker.patch("openpilot.system.athena.registration.api_get", autospec=True)
    dongle = register()
    assert m.call_count == 0
    assert dongle == UNREGISTERED_DONGLE_ID
    assert self.params.get("DongleId") == dongle
