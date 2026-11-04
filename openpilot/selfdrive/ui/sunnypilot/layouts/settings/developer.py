"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import datetime
import os
from pathlib import Path

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.layouts.settings.developer import DeveloperLayout
from openpilot.common.hardware import PC
from openpilot.common.hardware.hw import Paths
from openpilot.common.params import Params
from openpilot.common.raw_params import get_float_param, get_int_param
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.lib.multilang import tr, tr_noop
from openpilot.system.ui.widgets import DialogResult
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog
from openpilot.system.ui.widgets.list_view import button_item

from openpilot.system.ui.sunnypilot.widgets.html_render import HtmlModalSP
from openpilot.system.ui.sunnypilot.widgets.list_view import multiple_button_item_sp, toggle_item_sp

PREBUILT_PATH = os.path.join(Paths.comma_home(), "prebuilt") if PC else "/data/openpilot/prebuilt"

# BYD 0x109 b7 distance scale u [m/count] (docs/byd-radar-010x.md ②; pinned 0.25
# 2026-10-19 by owner decision, kept tunable for the road test). The gap monitor
# is u-free (r=b7/vEgo counts space) and the b7 counts path reads the factor-1
# MSG_109_B7 placeholder, so changing this can only affect future meters-side
# consumers (track synthesis / distance display), never control behavior.
BYD_RADAR_SCALE_PARAM = "BydRadarDistScale"
BYD_RADAR_SCALE_DEFAULT = 0.25
BYD_RADAR_SCALE_CHOICES = [0.20, 0.25, 0.3048, 0.30]

# BYD T8 forward-gap gate + slot-table layer, int params (card injects the slot
# level into the RadarInterface; desire_helper_byd clamps the gap gate).
BYD_GAP_GATE_PARAM = "BydRadarGapGate"
BYD_GAP_GATE_DEFAULT = 0
BYD_GAP_GATE_CHOICES = [0, 1, 2, 3]
BYD_SLOT_TABLE_PARAM = "BydRadarSlotTable"
BYD_SLOT_TABLE_DEFAULT = 1
BYD_SLOT_TABLE_CHOICES = [0, 1, 2]

BYD_DESCRIPTIONS = {
  'blinker_stalk_swap': tr_noop(
    "Swap the left/right turn-signal stalk detents (1 = exchange). Only for a " +
    "stalk that reads the opposite way; the Song Plus DM-i is confirmed correct " +
    "with the default. Takes effect at the next ignition."),
  'byd_radar_scale': tr_noop(
    "Distance scale u (meters per radar count) for the BYD 0x109 b7 field. " +
    "Pinned at 0.25 m/count; adjust only if a measured stop-to-lead gap disagrees. " +
    "Lane-change gap gating is scale-free and unaffected. " +
    "Applies at the next card restart."),
  'byd_gap_gate': tr_noop(
    "BYD forward-gap lane-change gate. 0 = off (factory behavior). " +
    "1 = a closing forward gap blocks a lane-change start. " +
    "2 = also abort a started change when a NEW target closes in. " +
    "3 = lane-aware: adjacent-lane closings are telemetry only (own-lane " +
    "closings keep the level-2 behavior; without lateral data everything " +
    "stays at level 2). Applies on next card restart."),
  'byd_slot_table': tr_noop(
    "BYD 67ms multi-target slot table. 0 = off (b7-only). " +
    "1 = display: multi-target points feed the UI/telemetry only. " +
    "2 = control: quality-gated slot points may become lead candidates. " +
    "Applies at the next card restart."),
}


class DeveloperLayoutSP(DeveloperLayout):
  def __init__(self):
    super().__init__()
    self.error_log_path = os.path.join(Paths.crash_log_root(), "error.log")
    self._is_release_branch: bool = self._is_release or ui_state.params.get_bool("IsReleaseSpBranch")
    self._is_development_branch: bool = ui_state.params.get_bool("IsTestedBranch") or ui_state.params.get_bool("IsDevelopmentBranch")
    self._initialize_items()

    for item in self.items:
      self._scroller.add_widget(item)

  def _initialize_items(self):
    self.show_advanced_controls = toggle_item_sp(tr("Show Advanced Controls"),
                                                 tr("Toggle visibility of advanced sunnypilot controls.<br>This only changes the visibility of the toggles; " +
                                                    "it does not change the actual enabled/disabled state."), param="ShowAdvancedControls")

    self.enable_github_runner_toggle = toggle_item_sp(tr("GitHub Runner Service"), tr("Enables or disables the GitHub runner service."),
                                                      param="EnableGithubRunner")

    self.enable_copyparty_toggle = toggle_item_sp(tr("copyparty Service"),
                                                  tr("copyparty is a very capable file server, you can use it to download your routes, view your logs " +
                                                     "and even make some edits on some files from your browser. " +
                                                     "Requires you to connect to your comma locally via its IP address."), param="EnableCopyparty")

    self.prebuilt_toggle = toggle_item_sp(tr("Quickboot Mode"), "", param="QuickBootToggle", callback=self._on_prebuilt_toggled)

    # BYD (Song Plus DM-i) items - visible only when the selected car is a byd
    self._params = Params()
    self.byd_blinker_stalk_swap_toggle = toggle_item_sp(
      tr("BYD Blinker Stalk Swap"),
      BYD_DESCRIPTIONS['blinker_stalk_swap'],
      param="BydBlinkerStalkSwap",
    )

    self.byd_radar_scale_item = multiple_button_item_sp(
      tr("BYD Radar Distance Scale"),
      BYD_DESCRIPTIONS['byd_radar_scale'],
      buttons=["0.20", "0.25", "0.3048", "0.30"],
      selected_index=self._byd_radar_scale_index(),
      callback=self._on_byd_radar_scale,
    )

    self.byd_gap_gate_item = multiple_button_item_sp(
      tr("BYD Forward Gap Gate"),
      BYD_DESCRIPTIONS['byd_gap_gate'],
      buttons=["Off", "Level 1", "Level 2", "Level 3 (lane)"],
      selected_index=self._byd_int_param_index(BYD_GAP_GATE_PARAM, BYD_GAP_GATE_DEFAULT, BYD_GAP_GATE_CHOICES),
      param=BYD_GAP_GATE_PARAM,
    )

    self.byd_slot_table_item = multiple_button_item_sp(
      tr("BYD Radar Slot Table"),
      BYD_DESCRIPTIONS['byd_slot_table'],
      buttons=["Off", "Display", "Control"],
      selected_index=self._byd_int_param_index(BYD_SLOT_TABLE_PARAM, BYD_SLOT_TABLE_DEFAULT, BYD_SLOT_TABLE_CHOICES),
      param=BYD_SLOT_TABLE_PARAM,
    )

    self.error_log_btn = button_item(tr("Error Log"), tr("VIEW"), tr("View the error log for sunnypilot crashes."), callback=self._on_error_log_clicked)

    self.items: list = [self.show_advanced_controls, self.enable_github_runner_toggle, self.enable_copyparty_toggle, self.prebuilt_toggle,
                        self.byd_blinker_stalk_swap_toggle, self.byd_radar_scale_item, self.byd_gap_gate_item, self.byd_slot_table_item,
                        self.error_log_btn,]

  def _byd_radar_scale_index(self) -> int:
    u = get_float_param(self._params, BYD_RADAR_SCALE_PARAM, BYD_RADAR_SCALE_DEFAULT)
    return min(range(len(BYD_RADAR_SCALE_CHOICES)), key=lambda i: abs(BYD_RADAR_SCALE_CHOICES[i] - u))

  def _on_byd_radar_scale(self, button_index: int):
    self._params.put(BYD_RADAR_SCALE_PARAM, BYD_RADAR_SCALE_CHOICES[button_index])

  def _byd_int_param_index(self, key: str, default: int, choices: list[int]) -> int:
    v = get_int_param(self._params, key, default)
    return min(range(len(choices)), key=lambda i: abs(choices[i] - v))

  @staticmethod
  def _on_prebuilt_toggled(state):
    if state:
      Path(PREBUILT_PATH).touch(exist_ok=True)
    else:
      os.remove(PREBUILT_PATH)
    ui_state.params.put_bool("QuickBootToggle", state)

  def _on_delete_confirm(self, result):
    if result == DialogResult.CONFIRM:
      if os.path.exists(self.error_log_path):
        os.remove(self.error_log_path)

  def _on_error_log_closed(self, result, log_exists):
    if result == DialogResult.CONFIRM and log_exists:
      dialog2 = ConfirmDialog(tr("Would you like to delete this log?"), tr("Yes"), tr("No"), rich=False, callback=self._on_delete_confirm)
      gui_app.push_widget(dialog2)

  def _on_error_log_clicked(self):
    text = ""
    if os.path.exists(self.error_log_path):
      text = f"<b>{datetime.datetime.fromtimestamp(os.path.getmtime(self.error_log_path)).strftime('%d-%b-%Y %H:%M:%S').upper()}</b><br><br>"
      try:
        with open(self.error_log_path) as file:
          text += file.read()
      except Exception:
        pass
    dialog = HtmlModalSP(text=text, callback=lambda result: self._on_error_log_closed(result, os.path.exists(self.error_log_path)))
    gui_app.push_widget(dialog)

  def _update_state(self):
    disable_updates = ui_state.params.get_bool("DisableUpdates")
    show_advanced = ui_state.params.get_bool("ShowAdvancedControls")
    byd = ui_state.CP is not None and ui_state.CP.brand == "byd"

    self.byd_blinker_stalk_swap_toggle.set_visible(byd)
    self.byd_radar_scale_item.set_visible(byd)
    self.byd_gap_gate_item.set_visible(byd)
    self.byd_slot_table_item.set_visible(byd)

    if (prebuilt_file := os.path.exists(PREBUILT_PATH)) != ui_state.params.get_bool("QuickBootToggle"):
      ui_state.params.put_bool("QuickBootToggle", prebuilt_file)
      self.prebuilt_toggle.action_item.set_state(prebuilt_file)

    self.prebuilt_toggle.set_visible(show_advanced and not (self._is_release_branch or self._is_development_branch))
    self.prebuilt_toggle.action_item.set_enabled(disable_updates)

    if disable_updates:
      self.prebuilt_toggle.set_description(tr("When toggled on, this creates a prebuilt file to allow accelerated boot times. When toggled off, it " +
                                              "removes the prebuilt file so compilation of locally edited cpp files can be made."))
    else:
      self.prebuilt_toggle.set_description(tr("Quickboot mode requires updates to be disabled.<br>Enable 'Disable Updates' in the Software panel first."))

    self.enable_copyparty_toggle.set_visible(show_advanced)
    self.enable_github_runner_toggle.set_visible(show_advanced and not self._is_release_branch)
    self.error_log_btn.set_visible(not self._is_release_branch)
