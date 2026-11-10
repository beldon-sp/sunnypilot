"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import datetime
import time

import pyray as rl

from opendbc.sunnypilot.car.interfaces import has_bsm
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.sunnypilot.selfdrive.controls.lib.auto_lane_change import AutoLaneChangeMode
from openpilot.system.ui.lib.application import FontWeight, TextAlignment
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.sunnypilot.lib.styles import style
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.button import Button, ButtonStyle
from openpilot.system.ui.widgets.label import gui_label

# Colors come from the shared sunnypilot palette; card font sizes are derived from
# the same base sizes, scaled down for the denser two-column home layout.
BG_COLOR = style.BUTTON_NEUTRAL_GRAY
MUTED_COLOR = style.ITEM_TEXT_VALUE_COLOR
TEXT_COLOR = style.ITEM_TEXT_COLOR

TITLE_FONT_SIZE = style.ITEM_TEXT_FONT_SIZE + 10  # 60
BUTTON_FONT_SIZE = style.ITEM_TEXT_FONT_SIZE + 2  # 52
ROW_FONT_SIZE = style.ITEM_TEXT_FONT_SIZE - 6  # 44
HINT_FONT_SIZE = style.ITEM_DESC_FONT_SIZE - 6  # 34

CARD_PADDING_X = 56
CARD_PADDING_Y = 48
TITLE_GAP = 92
MAX_ROW_HEIGHT = 72

TIMER_LABELS = {
  AutoLaneChangeMode.OFF: lambda: tr("Off"),
  AutoLaneChangeMode.NUDGE: lambda: tr("Nudge"),
  AutoLaneChangeMode.NUDGELESS: lambda: tr("Nudgeless"),
  AutoLaneChangeMode.HALF_SECOND: lambda: f"0.5 {tr('s')}",
  AutoLaneChangeMode.ONE_SECOND: lambda: f"1 {tr('s')}",
  AutoLaneChangeMode.TWO_SECONDS: lambda: f"2 {tr('s')}",
  AutoLaneChangeMode.THREE_SECONDS: lambda: f"3 {tr('s')}",
}

TIMER_VALUES = list(TIMER_LABELS.keys())


class VehicleStatusWidget(Widget):
  """Home left column: vehicle, version and car-specific status. Replaces the comma prime ad.

  Deliberately omits network / temperature / panda / sunnylink connectivity, which the
  sidebar already shows. Everything here is either car-specific or absent from the sidebar.
  """

  def __init__(self):
    super().__init__()
    self._rows: list[tuple[str, str]] = []
    self._last_refresh = 0.0

  def _refresh_rows(self):
    params = ui_state.params
    cp = ui_state.CP
    sm = ui_state.sm

    version = params.get("Version") or tr("Unknown")
    branch = params.get("GitBranch") or tr("Unknown")
    commit = (params.get("GitCommit") or "")[:7] or tr("Unknown")

    try:
      date_raw = params.get("GitCommitDate")
      unix_ts = int(date_raw.strip("'").split()[0])
      build_date = datetime.datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d")
    except (ValueError, IndexError, TypeError, AttributeError):
      build_date = tr("Unknown")

    vehicle = cp.carFingerprint if cp is not None else tr("Unknown")
    bsm_supported = cp is not None and has_bsm(cp, ui_state.CP_SP)

    ds = sm["deviceState"]
    # carState alive/valid is the UI-level proxy for the CAN feed that carries BSD_RADAR.
    bsd_online = sm.alive["carState"] and sm.valid["carState"]
    bsd_status = (f"{tr('Supported')} · {tr('Online') if bsd_online else tr('No data')}"
                  if bsm_supported else tr("Not available"))

    storage = f"{ds.freeSpacePercent:.0f} %" if ds.freeSpacePercent else "—"

    self._rows = [
      (tr("Car model"), vehicle),
      (tr("Version"), f"{version}  ·  {build_date}"),
      (tr("Branch / commit"), f"{branch}  ·  {commit}"),
      (tr("BSD radar"), bsd_status),
      (tr("Blind spot"), self._blindspot_text()),
      (tr("Model"), self._model_text()),
      (tr("Sunnylink"), self._sunnylink_text()),
      (tr("Storage"), storage),
    ]
    self._last_refresh = time.monotonic()

  @staticmethod
  def _blindspot_text() -> str:
    sm = ui_state.sm
    if not sm.alive["carState"] or not sm.valid["carState"]:
      return "—"
    cs = sm["carState"]
    sides = [side for side, detected in (("L", cs.leftBlindspot), ("R", cs.rightBlindspot)) if detected]
    return f"{'+'.join(sides)}  {tr('Detected')}" if sides else tr("Clear")

  @staticmethod
  def _model_text() -> str:
    bundle = getattr(ui_state, "active_bundle", None)
    if isinstance(bundle, dict):
      name = bundle.get("displayName") or bundle.get("internalName")
      if name:
        return str(name)
    return tr("Default")

  @staticmethod
  def _sunnylink_text() -> str:
    # Show pairing state, not connectivity: the sidebar already shows ONLINE/OFFLINE.
    if not ui_state.sunnylink_enabled:
      return tr("Disabled")
    return tr("Paired") if ui_state.sunnylink_state.is_paired() else tr("Unpaired")

  def _update_state(self):
    if time.monotonic() - self._last_refresh > 2.0:
      self._refresh_rows()

  def _render(self, rect: rl.Rectangle):
    rl.draw_rectangle_rounded(rect, 0.025, 10, BG_COLOR)

    x = rect.x + CARD_PADDING_X
    w = rect.width - 2 * CARD_PADDING_X
    y = rect.y + CARD_PADDING_Y

    gui_label(rl.Rectangle(x, y, w, TITLE_FONT_SIZE + 20), tr("Vehicle & version"),
              TITLE_FONT_SIZE, TEXT_COLOR, font_weight=FontWeight.BOLD)
    y += TITLE_GAP

    # The home layout can shrink this card when the alert banner is visible.
    # Scale row height and font to the remaining space so nothing overflows.
    available = max(rect.y + rect.height - CARD_PADDING_Y - y, 0)
    row_height = min(MAX_ROW_HEIGHT, available / len(self._rows)) if self._rows else MAX_ROW_HEIGHT
    row_font_size = min(ROW_FONT_SIZE, max(24, int(row_height * 0.62)))

    for label, value in self._rows:
      gui_label(rl.Rectangle(x, y, w * 0.34, row_height), label, row_font_size, MUTED_COLOR)
      gui_label(rl.Rectangle(x + w * 0.34, y, w * 0.66, row_height), value, row_font_size, TEXT_COLOR,
                font_weight=FontWeight.MEDIUM, alignment=TextAlignment.RIGHT)
      y += row_height


class LaneChangeQuickSettingsWidget(Widget):
  """Home right column: one-tap lane change params. Replaces the comma connect pairing prompt."""

  def __init__(self):
    super().__init__()
    self._open_settings_callback = None
    self._last_refresh = 0.0

    self._timer_value = AutoLaneChangeMode.NUDGE
    self._bsm_delay = False
    self._lane_turn = False
    self._bsm_supported = False

    self._timer_text = ""
    self._bsm_text = ""
    self._lane_turn_text = ""

    self._timer_btn = Button(lambda: self._timer_text, self._cycle_timer, font_size=BUTTON_FONT_SIZE)
    self._bsm_btn = Button(lambda: self._bsm_text, self._toggle_bsm_delay, font_size=ROW_FONT_SIZE + 4)
    self._lane_turn_btn = Button(lambda: self._lane_turn_text, self._toggle_lane_turn, font_size=ROW_FONT_SIZE + 4)
    self._more_btn = Button(lambda: tr("More lane change settings"), self._open_lane_change_settings,
                            font_size=ROW_FONT_SIZE, button_style=ButtonStyle.PRIMARY)

    self._refresh_values()

  # Keep the upstream MainLayout callback contract (layouts/main.py sets this).
  def set_open_settings_callback(self, callback):
    self._open_settings_callback = callback

  def _refresh_values(self):
    params = ui_state.params
    self._timer_value = int(params.get("AutoLaneChangeTimer", return_default=True) or AutoLaneChangeMode.NUDGE)
    self._bsm_delay = params.get_bool("AutoLaneChangeBsmDelay")
    self._lane_turn = params.get_bool("LaneTurnDesire")
    self._bsm_supported = ui_state.CP is not None and has_bsm(ui_state.CP, ui_state.CP_SP)

    timer_label = TIMER_LABELS.get(self._timer_value, TIMER_LABELS[AutoLaneChangeMode.NUDGE])()
    on, off = tr("On"), tr("Off")

    self._timer_text = f"{tr('Auto lane change')}    {timer_label}"
    self._bsm_text = (f"{tr('BSM delay')}    {on if self._bsm_delay else off}" if self._bsm_supported
                      else f"{tr('BSM delay')}    {tr('Not available')}")
    self._lane_turn_text = f"{tr('Low-speed lane change')}    {on if self._lane_turn else off}"
    self._bsm_btn.set_enabled(self._bsm_supported)
    self._last_refresh = time.monotonic()

  def _update_state(self):
    if time.monotonic() - self._last_refresh > 1.0:
      self._refresh_values()

  def _cycle_timer(self):
    try:
      idx = TIMER_VALUES.index(self._timer_value)
    except ValueError:
      idx = TIMER_VALUES.index(AutoLaneChangeMode.NUDGE)
    ui_state.params.put("AutoLaneChangeTimer", TIMER_VALUES[(idx + 1) % len(TIMER_VALUES)])
    self._refresh_values()

  def _toggle_bsm_delay(self):
    if not self._bsm_supported:
      return
    ui_state.params.put_bool("AutoLaneChangeBsmDelay", not self._bsm_delay)
    self._refresh_values()

  def _toggle_lane_turn(self):
    ui_state.params.put_bool("LaneTurnDesire", not self._lane_turn)
    self._refresh_values()

  def _open_lane_change_settings(self):
    if self._open_settings_callback is None:
      return
    # Imported lazily: sunnypilot extends OP.PanelType (adds STEERING) when this module loads.
    from openpilot.selfdrive.ui.sunnypilot.layouts.settings import settings as sp_settings
    panel_type = getattr(sp_settings.OP.PanelType, "STEERING", sp_settings.OP.PanelType.DEVICE)
    self._open_settings_callback(panel_type)

  def _render(self, rect: rl.Rectangle):
    rl.draw_rectangle_rounded(rect, 0.03, 20, BG_COLOR)

    x = rect.x + CARD_PADDING_X
    w = rect.width - 2 * CARD_PADDING_X
    y = rect.y + CARD_PADDING_Y

    gui_label(rl.Rectangle(x, y, w, TITLE_FONT_SIZE + 16), tr("Lane change quick settings"),
              TITLE_FONT_SIZE, TEXT_COLOR, font_weight=FontWeight.BOLD)
    y += 82

    gui_label(rl.Rectangle(x, y, w, 52), tr("Tap to change, takes effect in ~1s"), HINT_FONT_SIZE, MUTED_COLOR)
    y += 70

    button_height = 132
    for button in (self._timer_btn, self._bsm_btn, self._lane_turn_btn):
      button.render(rl.Rectangle(x, y, w, button_height))
      y += button_height + 26

    y += 12
    self._more_btn.render(rl.Rectangle(x, y, w, 100))
    y += 124

    if self._timer_value <= AutoLaneChangeMode.NUDGE:
      gui_label(rl.Rectangle(x, y, w, 48), tr("BSM delay needs a timed lane change mode"),
                HINT_FONT_SIZE, MUTED_COLOR)
