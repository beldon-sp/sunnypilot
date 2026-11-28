"""UI 屏幕镜像抓帧旁路（v6.2）。

把车机 UI 渲染的帧导出为 PNG 写到 /dev/shm，由 mate server 转发给客户端
（carrotmate / 浏览器）显示"车机屏幕"。设计约束：

- UiMirrorEnabled 参数门控（默认关），5s 惰性刷新一次；
- 没有消费者（mate 侧 .active 哨兵文件不存在）时每 tick 只做一次 stat，
  抓帧/编码全部跳过——零 CPU 开销；
- GL 线程只做 GPU 读回（grab_frame，~2ms），PNG 编码 + 落盘在独立线程；
  编码线程跟不上就丢帧（queue maxsize=1），绝不阻塞 UI 渲染；
- 任何异常整体停用（_failed），UI 主循环不受影响——镜像功能永远
  不能威胁到驾驶界面的可用性。
"""
import os
import queue
import threading

import pyray as rl

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog

# 与 mate/server.py 的 UI_MIRROR_PATH / UI_MIRROR_ACTIVE_PATH 保持一致
MIRROR_PATH = "/dev/shm/mate_ui_mirror.png"
ACTIVE_PATH = "/dev/shm/mate_ui_mirror.png.active"

GRAB_INTERVAL_S = 0.1   # 10fps
PARAM_CHECK_S = 5.0
DOWNSCALE_W = 640       # 车机 UI 全尺寸 PNG 太大（onroad 带相机画面），降采样后 LAN 可接受


class UiMirror:
  def __init__(self, grab_fn):
    """grab_fn: application.GuiApplication.grab_frame，返回 (Image, flipped)。"""
    self._grab_fn = grab_fn
    self._params = Params()
    self._enabled = False
    self._param_ts = 0.0
    self._last_grab = 0.0
    self._failed = False
    self._q: queue.Queue = queue.Queue(maxsize=1)
    threading.Thread(target=self._writer, name="ui-mirror-writer", daemon=True).start()

  def tick(self, now: float) -> None:
    """UI 渲染循环每帧调用（yield 点，GL 线程）。永不抛异常。"""
    if self._failed:
      return
    try:
      if now - self._param_ts > PARAM_CHECK_S:
        self._param_ts = now
        self._enabled = self._params.get("UiMirrorEnabled") == b"true"
      if not self._enabled or (now - self._last_grab) < GRAB_INTERVAL_S:
        return
      self._last_grab = now
      try:
        os.stat(ACTIVE_PATH)  # 没消费者 = 零抓帧开销
        active = True
      except OSError:
        active = False
      if not active or not self._q.empty():  # 编码跟不上也丢帧
        return
      img, flipped = self._grab_fn()
      if img is not None:
        self._q.put_nowait((img, flipped))
    except Exception:
      self._failed = True
      cloudlog.exception("ui mirror disabled after error")

  def _writer(self) -> None:
    while True:
      img, flipped = self._q.get()
      try:
        if flipped:
          rl.image_flip_vertical(img)  # texture 读回上下颠倒（已验证原地生效）
        if img.width > DOWNSCALE_W:
          rl.image_resize(img, DOWNSCALE_W, max(1, round(img.height * DOWNSCALE_W / img.width)))
        size = rl.ffi.new("int *")
        ptr = rl.export_image_to_memory(img, b".png", size)  # 本 raylib 构建仅支持 png
        data = bytes(rl.ffi.buffer(ptr, size[0]))
        tmp = MIRROR_PATH + ".tmp"
        with open(tmp, "wb") as f:
          f.write(data)
        os.replace(tmp, MIRROR_PATH)  # 原子替换，消费侧永不读到半截 PNG
      except Exception:
        pass  # 单帧失败丢帧即可
      finally:
        try:
          rl.unload_image(img)
        except Exception:
          pass
