"""carrot turnType 语义的单一事实来源。

从 carrot_serv.py 抽出：navi_lane_change_gate（跑在 modeld）等轻量消费方
只取映射表与方向归类，不再拖入 carrot_serv 的完整依赖。编码本身与
carrot_serv.nav_type_mapping 保持一致（Group 编码沿 CarrotPilot 约定）。

turn_direction() 把 turnType 归类为 'left' / 'right' / ''（未知、直行、
掉头、到达、通知一律返回 ''，消费方据此 fail-open，不得臆测方向）。
掉头（uturn）不归入 left：掉头车道位置因路而异，等真车校准后再议。
"""
LEFT_MODIFIERS = frozenset({"left", "slight left", "sharp left"})
RIGHT_MODIFIERS = frozenset({"right", "slight right", "sharp right"})

nav_type_mapping = {
  12: ("turn", "left", 1),
  16: ("turn", "sharp left", 1),
  1000: ("turn", "slight left", 1),
  1001: ("turn", "slight right", 2),
  1002: ("fork", "slight left", 3),
  1003: ("fork", "slight right", 4),
  1006: ("off ramp", "left", 3),
  1007: ("off ramp", "right", 4),
  13: ("turn", "right", 2),
  19: ("turn", "sharp right", 2),
  102: ("off ramp", "slight left", 3),
  105: ("off ramp", "slight left", 3),
  112: ("off ramp", "slight left", 3),
  115: ("off ramp", "slight left", 3),
  101: ("off ramp", "slight right", 4),
  104: ("off ramp", "slight right", 4),
  111: ("off ramp", "slight right", 4),
  114: ("off ramp", "slight right", 4),
  7: ("fork", "left", 3),
  44: ("fork", "left", 3),
  17: ("fork", "left", 3),
  75: ("fork", "left", 3),
  76: ("fork", "left", 3),
  118: ("fork", "left", 3),
  6: ("fork", "right", 4),
  43: ("fork", "right", 4),
  73: ("fork", "right", 4),
  74: ("fork", "right", 4),
  123: ("fork", "right", 4),
  124: ("fork", "right", 4),
  117: ("fork", "right", 4),
  131: ("rotary", "slight right", 5),
  132: ("rotary", "slight right", 5),
  140: ("rotary", "slight left", 5),
  141: ("rotary", "slight left", 5),
  133: ("rotary", "right", 5),
  134: ("rotary", "sharp right", 5),
  135: ("rotary", "sharp right", 5),
  136: ("rotary", "sharp left", 5),
  137: ("rotary", "sharp left", 5),
  138: ("rotary", "sharp left", 5),
  139: ("rotary", "left", 5),
  142: ("rotary", "straight", 5),
  14: ("turn", "uturn", 5),
  201: ("arrive", "straight", 5),
  51: ("notification", "straight", None),
  52: ("notification", "straight", None),
  53: ("notification", "straight", None),
  54: ("notification", "straight", None),
  55: ("notification", "straight", None),
  153: ("", "", 6),  #TG
  154: ("", "", 6),  #TG
  249: ("", "", 6)   #TG
}


def turn_direction(turn_type: int) -> str:
  """turnType → 'left' / 'right' / ''；未知或非左右转编码返回 ''。"""
  entry = nav_type_mapping.get(turn_type)
  if entry is None:
    return ""
  _, modifier, _ = entry
  if modifier in LEFT_MODIFIERS:
    return "left"
  if modifier in RIGHT_MODIFIERS:
    return "right"
  return ""
