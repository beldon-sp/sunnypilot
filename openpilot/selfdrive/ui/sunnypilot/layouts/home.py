"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import time

import pyray as rl
from openpilot.selfdrive.ui.layouts.home import HomeLayout, HomeLayoutState, HEAD_BUTTON_FONT_SIZE, SPACING, REFRESH_INTERVAL
from openpilot.selfdrive.ui.sunnypilot.layouts.home_cards import LaneChangeStatusWidget, VehicleStatusWidget
from openpilot.selfdrive.ui.widgets.offroad_alerts import AlertColors
from openpilot.system.ui.lib.application import gui_app, FontWeight, MousePos, TextAlignment
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.lib.multilang import tr, trn
from openpilot.system.ui.sunnypilot.lib.styles import style
from openpilot.system.ui.widgets.label import gui_label

BRAND_FONT_SIZE = 48
BRAND_DESC_SPACING = 12
BANNER_HEIGHT = 110


class HomeLayoutSP(HomeLayout):
  """Home layout where updates/alerts never take over the whole content area.

  Upstream swaps the entire home content for the update / alert panel whenever one
  appears, which hides both custom cards. Here those panels only ever occupy the
  left column, and the right column (lane change quick settings) stays put.
  """

  def __init__(self):
    super().__init__()
    # Replace the upstream comma prime ad and connect pairing prompt with car-specific panels
    self._prime_widget = VehicleStatusWidget()
    self._setup_widget = LaneChangeStatusWidget()
    self._banner_rect = rl.Rectangle(0, 0, 0, 0)

  def _render(self, rect: rl.Rectangle):
    current_time = time.monotonic()
    if current_time - self.last_refresh >= REFRESH_INTERVAL:
      self._refresh()
      self.last_refresh = current_time

    self._render_header()
    self._render_left_column()
    self._render_right_column()

  def _refresh(self):
    self._version_text = self._get_version_text()
    self.update_available = self.update_alert.refresh()
    self.alert_count = self.offroad_alert.refresh()

    # Never auto-open a detail panel: the header badges and inline banner are enough
    # to surface new alerts without hiding the quick settings. Only close a panel
    # when whatever opened it has gone away.
    if self.current_state == HomeLayoutState.UPDATE and not self.update_available:
      self._set_state(HomeLayoutState.HOME)
    elif self.current_state == HomeLayoutState.ALERTS and self.alert_count == 0:
      self._set_state(HomeLayoutState.HOME)

    self._prev_update_available = self.update_available
    self._prev_alerts_present = self.alert_count > 0

  def _render_left_column(self):
    rect = self.left_column_rect

    if self.current_state in self._layout_widgets:
      self._banner_rect = rl.Rectangle(0, 0, 0, 0)
      self._layout_widgets[self.current_state].render(rect)
      return

    has_banner = self.update_available or self.alert_count > 0
    reserved = BANNER_HEIGHT + SPACING if has_banner else 0
    card_rect = rl.Rectangle(rect.x, rect.y, rect.width, rect.height - reserved)
    self._prime_widget.render(card_rect)

    if has_banner:
      self._banner_rect = rl.Rectangle(rect.x, rect.y + card_rect.height + SPACING, rect.width, BANNER_HEIGHT)
      self._render_banner(self._banner_rect)
    else:
      self._banner_rect = rl.Rectangle(0, 0, 0, 0)

  def _render_banner(self, rect: rl.Rectangle):
    alert_present = self.alert_count > 0
    color = AlertColors.HIGH_SEVERITY if alert_present else style.BUTTON_PRIMARY_COLOR
    rl.draw_rectangle_rounded(rect, 0.14, 10, color)

    parts = []
    if alert_present:
      parts.append(trn("{} ALERT", "{} ALERTS", self.alert_count).format(self.alert_count))
    if self.update_available:
      parts.append(tr("Update available"))
    parts.append(tr("Tap to view"))

    gui_label(rl.Rectangle(rect.x + 50, rect.y, rect.width - 100, rect.height),
              "  ·  ".join(parts), style.ITEM_DESC_FONT_SIZE, style.ITEM_TEXT_COLOR,
              font_weight=FontWeight.MEDIUM)

  def _handle_mouse_release(self, mouse_pos: MousePos):
    # Intentionally not calling super(): upstream would force a full-content state
    # switch. Badges toggle the left-column detail instead; the banner opens the
    # highest-priority one (alerts over updates).
    if self.update_available and rl.check_collision_point_rec(mouse_pos, self.update_notif_rect):
      self._toggle_detail(HomeLayoutState.UPDATE)
    elif self.alert_count > 0 and rl.check_collision_point_rec(mouse_pos, self.alert_notif_rect):
      self._toggle_detail(HomeLayoutState.ALERTS)
    elif self._banner_rect.height > 0 and rl.check_collision_point_rec(mouse_pos, self._banner_rect):
      state = HomeLayoutState.ALERTS if self.alert_count > 0 else HomeLayoutState.UPDATE
      self._set_state(state)

  def _toggle_detail(self, state: HomeLayoutState):
    self._set_state(HomeLayoutState.HOME if self.current_state == state else state)

  def _render_header(self):
    font = gui_app.font(FontWeight.MEDIUM)

    version_text_width = self.header_rect.width

    if self.update_available:
      version_text_width -= self.update_notif_rect.width

      highlight_color = rl.Color(75, 95, 255, 255) if self.current_state == HomeLayoutState.UPDATE else rl.Color(54, 77, 239, 255)
      rl.draw_rectangle_rounded(self.update_notif_rect, 0.3, 10, highlight_color)

      text = tr("UPDATE")
      text_size = measure_text_cached(font, text, HEAD_BUTTON_FONT_SIZE)
      text_x = self.update_notif_rect.x + (self.update_notif_rect.width - text_size.x) // 2
      text_y = self.update_notif_rect.y + (self.update_notif_rect.height - text_size.y) // 2
      rl.draw_text_ex(font, text, rl.Vector2(int(text_x), int(text_y)), HEAD_BUTTON_FONT_SIZE, 0, rl.WHITE)

    if self.alert_count > 0:
      version_text_width -= self.alert_notif_rect.width

      highlight_color = rl.Color(255, 70, 70, 255) if self.current_state == HomeLayoutState.ALERTS else rl.Color(226, 44, 44, 255)
      rl.draw_rectangle_rounded(self.alert_notif_rect, 0.3, 10, highlight_color)

      alert_text = trn("{} ALERT", "{} ALERTS", self.alert_count).format(self.alert_count)
      text_size = measure_text_cached(font, alert_text, HEAD_BUTTON_FONT_SIZE)
      text_x = self.alert_notif_rect.x + (self.alert_notif_rect.width - text_size.x) // 2
      text_y = self.alert_notif_rect.y + (self.alert_notif_rect.height - text_size.y) // 2
      rl.draw_text_ex(font, alert_text, rl.Vector2(int(text_x), int(text_y)), HEAD_BUTTON_FONT_SIZE, 0, rl.WHITE)

    if self.update_available or self.alert_count > 0:
      version_text_width -= SPACING * 1.5

    version_right = self.header_rect.x + self.header_rect.width
    version_left = version_right - version_text_width

    brand = "sunnypilot"
    description = self.params.get("UpdaterCurrentDescription") or ""

    desc_width = 0
    if description:
      desc_size = measure_text_cached(gui_app.font(FontWeight.NORMAL), description, BRAND_FONT_SIZE)
      desc_width = desc_size.x
      desc_rect = rl.Rectangle(version_right - desc_width, self.header_rect.y, desc_width, self.header_rect.height)
      gui_label(desc_rect, description, BRAND_FONT_SIZE, rl.WHITE, alignment=TextAlignment.RIGHT)

    brand_size = measure_text_cached(gui_app.font(FontWeight.AUDIOWIDE), brand, BRAND_FONT_SIZE)
    spacing = BRAND_DESC_SPACING if description else 0
    brand_x = version_right - desc_width - spacing - brand_size.x
    brand_rect = rl.Rectangle(max(version_left, brand_x), self.header_rect.y, brand_size.x, self.header_rect.height)
    gui_label(brand_rect, brand, BRAND_FONT_SIZE, rl.WHITE, font_weight=FontWeight.AUDIOWIDE)
