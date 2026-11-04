"""BYD Song Plus DM-i 22 live-fingerprint robustness (openspec change
byd-fingerprint-yaw-hardening; docs/byd-carrot2-reference-2026-10-21.md s7.7).

The community-shared dict (205 addresses, identical to carrot2's Song_22 one)
was missing 10 frames our car actually sends on bus0. Live matching passed
only as a race won by the bus1 radar track converging first: the counterfactual
replay with the radar bus silent eliminated BYD at frame 28 (0x52A) of route
00000003--8801cc6fda--3. These tests pin the fix - the car's own always-on
bus0 stream alone must survive the legacy elimination logic AND converge to
BYD_SONG_PLUS_DMI_22 as the sole survivor.

BUS0_ALWAYSON: (address, DLC) pairs seen in >=400/415 segments of the 2026-10-05
real-vehicle CAN census (local_docs/byd/fingerprint_census_2026-10-21/full_census.json).
"""
from types import SimpleNamespace

from opendbc.car.byd.fingerprints import FINGERPRINTS
from opendbc.car.byd.values import CAR
from opendbc.car.fingerprints import all_legacy_fingerprint_cars, eliminate_incompatible_cars

BYD = str(CAR.BYD_SONG_PLUS_DMI_22)
BYD_DICT = FINGERPRINTS[CAR.BYD_SONG_PLUS_DMI_22][0]

# addresses the community sample car sent but our car never does (415-segment
# census, docs/byd-carrot2-reference-2026-10-21.md s7.4 ruling) - kept in the
# dict, must stay harmless (superset tolerance)
COMMUNITY_ONLY = [(309, 8), (418, 8), (635, 8), (733, 8)]

# this change's augmentation, decimal address == hex in parens:
# 0x41A 0x4BB 0x4D9 0x527 0x52A always-on slow broadcasts + 0x180 0x299 0x2BC
# 0x40C 0x4A8 conditionals
AUGMENTED = [1050, 1211, 1241, 1319, 1322, 384, 665, 700, 1036, 1192]

BUS0_ALWAYSON = [
  (85, 8), (140, 8), (213, 8), (269, 8), (270, 8), (287, 5), (289, 8), (290, 8), (291, 8),
  (300, 8), (301, 8), (307, 8), (337, 8), (356, 8), (371, 8), (450, 8), (496, 8), (522, 8),
  (525, 8), (527, 8), (536, 8), (537, 8), (544, 8), (546, 8), (547, 8), (575, 8), (576, 8),
  (577, 8), (578, 8), (588, 8), (593, 8), (629, 8), (638, 8), (660, 8), (681, 8), (694, 8),
  (703, 8), (724, 8), (748, 8), (781, 8), (786, 8), (792, 8), (797, 8), (798, 8), (800, 8),
  (801, 8), (802, 8), (803, 8), (812, 8), (833, 8), (834, 8), (835, 8), (836, 8), (847, 8),
  (854, 8), (860, 8), (863, 8), (878, 8), (879, 8), (906, 8), (940, 8), (941, 8), (944, 8),
  (951, 8), (965, 8), (973, 8), (985, 8), (1004, 8), (1020, 8), (1023, 8), (1028, 8),
  (1031, 8), (1037, 8), (1038, 8), (1040, 8), (1048, 8), (1050, 8), (1052, 8), (1058, 8),
  (1076, 8), (1097, 8), (1098, 8), (1141, 8), (1163, 8), (1178, 8), (1189, 8), (1193, 8),
  (1211, 8), (1215, 8), (1241, 8), (1246, 8), (1273, 8), (1274, 8), (1278, 8), (1297, 8),
  (1298, 8), (1319, 8), (1322, 8), (1337, 8), (1338, 8),
]


def msg(addr: int, dlc: int) -> SimpleNamespace:
  return SimpleNamespace(address=addr, dat=bytes(dlc))


class TestBydDictCoversOurCar:
  def test_alwayson_frames_all_known(self):
    for addr, dlc in BUS0_ALWAYSON:
      assert BYD_DICT.get(addr) == dlc, f"bus0 always-on frame {hex(addr)} (dlc {dlc}) not in dict"

  def test_augmented_entries_present(self):
    for addr in AUGMENTED:
      assert BYD_DICT.get(addr) == 8


class TestBydBus0OnlyConvergence:
  """the counterfactual that used to fail: radar bus silent, bus0 alone."""

  def test_byd_never_eliminated_by_our_own_bus0(self):
    cands = all_legacy_fingerprint_cars()
    for addr, dlc in BUS0_ALWAYSON:
      cands = eliminate_incompatible_cars(msg(addr, dlc), cands)
      assert BYD in [str(c) for c in cands], f"BYD eliminated by bus0 frame {hex(addr)}"

  def test_bus0_stream_converges_to_byd_unique(self):
    cands = all_legacy_fingerprint_cars()
    for addr, dlc in BUS0_ALWAYSON:
      cands = eliminate_incompatible_cars(msg(addr, dlc), cands)
    assert [str(c) for c in cands] == [BYD]


class TestBydSupersetTolerance:
  """elimination tolerates dict entries the car never sends - the community-car
  exclusive addresses stay harmless for us, and their frames must never
  eliminate BYD for a community car."""

  def test_community_exclusive_frames_survive(self):
    cands = all_legacy_fingerprint_cars()
    stream = [(85, 8), (287, 5)] + COMMUNITY_ONLY
    for addr, dlc in stream:
      cands = eliminate_incompatible_cars(msg(addr, dlc), cands)
      assert BYD in [str(c) for c in cands]
