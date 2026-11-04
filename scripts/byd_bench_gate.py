#!/usr/bin/env python3
# BYD 台架回归门禁：包装 byd_replay_bench，先验语料哈希、清参数，再对 golden 全精度比对。
# 退出码语义（docs/byd-replay-bench.md「门禁」节）：
#   0 = PASS / --accept 固化成功(INTENDED-CHANGE)
#   1 = REGRESSION / FLAKY / 语料哈希不符 / --accept 缺归因被拒
#   2 = 环境错误（runner 子进程起不来/导入失败，非回归）
#   3 = SKIP（钉定语料不在本地——绝不等于过）
# 设计事实：bench 在同一 (代码,语料,参数初值) 下逐位确定（10-08 设备 / 10-09 M4 已证），
# 故比对不设容差带；浮点仅在 6 位有效内判等，防打印/序列化截尾假阳性。

# spawn 孙进程（process_replay 的 PythonProcess）会重新 import __mp_main__（本文件），
# 所以：1) 重组件必须在顶层 import（保证子解释器也拿到 byd_replay_bench 的 Mac Params
# 垫片/绑核补丁），2) 一切执行体必须包在 __main__ 守卫里。
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
  sys.path.insert(0, SCRIPT_DIR)

JSON_MARKER = '===BYD_GATE_JSON==='

try:
  import byd_replay_bench as _bench
except Exception as e:  # 环境错误单列，不与 REGRESSION 混谈
  if __name__ == '__main__':
    sys.exit(f'环境错误：无法导入 byd_replay_bench（检查 .venv/capnpc/PYTHONPATH）：{e!r}')
  raise


def sha256_file(path):
  h = hashlib.sha256()
  with open(path, 'rb') as f:
    for chunk in iter(lambda: f.read(1 << 20), b''):
      h.update(chunk)
  return h.hexdigest()


def clear_mac_params():
  # Mac 的 /tmp/params 是 /dev/shm/params 回退目录，跨运行持久（docs 已记的坑）。
  # 落在 gate 实现里，杜绝「忘记清参数导致的假漂移」。
  if sys.platform == 'darwin':
    shutil.rmtree('/tmp/params', ignore_errors=True)


def run_bench(seg_path, chain):
  """跑一次 bench，返回 {metric: {"n": int, "mean": float, "max": float}}（全精度）。"""
  clear_mac_params()
  cmd = [sys.executable, os.path.abspath(__file__), '--_runner', seg_path, chain]
  r = subprocess.run(cmd, capture_output=True, text=True)
  if r.returncode != 0:
    tail = (r.stdout + r.stderr)[-2000:]
    sys.exit(f'环境错误：bench runner 失败(rc={r.returncode})。\n--- tail ---\n{tail}')
  try:
    payload = r.stdout.split(JSON_MARKER, 1)[1]
    return json.loads(payload)
  except (IndexError, json.JSONDecodeError) as e:
    sys.exit(f'环境错误：runner 输出无法解析：{e!r}\n--- stdout tail ---\n{r.stdout[-2000:]}')


def floats_agree(a, b):
  if a == b:
    return True
  scale = max(abs(a), abs(b), 1.0)
  return abs(a - b) <= 1e-6 * scale  # 6 位有效内判等


def metric_agree(got, want):
  if want is None or got is None:
    return got is None and want is None
  return got['n'] == want['n'] and floats_agree(got['mean'], want['mean']) and floats_agree(got['max'], want['max'])


def golden_metrics(golden, chain, seg):
  out = {}
  for row in golden['runs']:
    if row['chain'] == chain and row['seg'] == seg:
      out[row['metric']] = {'n': row['n'], 'mean': row['mean'], 'max': row['max']}
  return out


def head_commit():
  r = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=REPO_ROOT, capture_output=True, text=True)
  return r.stdout.strip() if r.returncode == 0 else 'unknown'


def find_drift_note(golden, chain, metric, old, new):
  for note in golden.get('drift_notes', []):
    if note['chain'] != chain or note['metric'] != metric:
      continue
    old_row = {'n': note['old']['n'], 'mean': note['old']['mean'], 'max': note['old']['max']}
    new_row = {'n': note['new']['n'], 'mean': note['new']['mean'], 'max': note['new']['max']}
    if metric_agree(old, old_row) and metric_agree(new, new_row):
      return note
  return None


def cmd_runner(argv):
  # 子进程模式：与手动跑 byd_replay_bench.py 完全同一取数路径（bench_pair/diff_metrics），
  # 只把指标以全精度 JSON 打到标记行后供父进程比对。
  seg, chain = argv
  logged, gen = _bench.bench_pair(seg, chain.split(','))
  metrics = _bench.diff_metrics(logged, gen)
  print(JSON_MARKER)
  print(json.dumps(metrics))


def main():
  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument('--golden', default=os.path.join(SCRIPT_DIR, 'byd_bench_golden.json'))
  ap.add_argument('--corpus-dir', default=None, help='覆盖语料 realdata 目录（默认为 golden.corpus_base）')
  ap.add_argument('--accept', action='store_true', help='将漂移写回 golden（要求 drift_notes 已有该指标的归因条目）')
  ap.add_argument('--check-determinism', action='store_true', help='每个 (chain,seg) 连跑两次比对，不一致判 FLAKY')
  ap.add_argument('--_runner', nargs=2, metavar=('SEG', 'CHAIN'), help=argparse.SUPPRESS)
  args = ap.parse_args()
  if args._runner:
    cmd_runner(args._runner)
    return

  with open(args.golden) as f:
    golden = json.load(f)

  corpus_dir = args.corpus_dir or os.path.expanduser(golden['corpus_base'])

  # 1) 先验货：缺料 SKIP(3)，哈希不符 FAIL(1)——数据被换则结论无效，不进 replay。
  seg_paths = {}
  missing = []
  for entry in golden['corpus']:
    p = os.path.join(corpus_dir, entry['seg'], 'rlog.zst')
    if not os.path.exists(p):
      missing.append((entry['seg'], p))
    else:
      actual = sha256_file(p)
      if actual != entry['sha256']:
        print(f'REGRESSION(语料哈希不符) seg={entry["seg"]}\n  期望 {entry["sha256"]}\n  实际 {actual}\n  路径 {p}')
        sys.exit(1)
      seg_paths[entry['seg']] = p
  if missing:
    print('SKIP：钉定语料不在本地（SKIP≠过；OTA 检查单须在本机跑绿）：')
    for seg, p in missing:
      print(f'  {seg}  期望路径 {p}')
    sys.exit(3)

  # 2) 按 golden.runs 的 (chain, seg) 组合跑 bench。
  combos = sorted({(row['chain'], row['seg']) for row in golden['runs']})
  results = {}
  for chain, seg in combos:
    got = run_bench(seg_paths[seg], chain)
    if args.check_determinism:
      again = run_bench(seg_paths[seg], chain)
      unstable = sorted(k for k in set(got) | set(again) if not metric_agree(got.get(k), again.get(k)))
      if unstable:
        print(f'FLAKY：同一 commit 两次运行不自洽 (chain={chain} seg={seg}): {unstable}')
        print('问题归属 bench 层（非阈值问题，禁止放宽容差自洽）。')
        sys.exit(1)
    results[(chain, seg)] = got

  # 3) 全精度对 golden 比对。漂移默认只报不写。
  drifts = []
  for chain, seg in combos:
    want = golden_metrics(golden, chain, seg)
    got = results[(chain, seg)]
    for metric in sorted(set(want) | set(got)):
      g, w = got.get(metric), want.get(metric)
      if not metric_agree(g, w):
        drifts.append((chain, seg, metric, w, g))
  if not drifts:
    print(f'PASS：{len(combos)} 个链×段、全部指标与 golden 一致（baseline 记录于 {golden.get("baseline_commit")}）。')
    sys.exit(0)

  print(f'检出 {len(drifts)} 项基线漂移（golden={args.golden}）：')
  for chain, seg, metric, w, g in drifts:
    golden_s = 'None' if w is None else f'n={w["n"]} mean={w["mean"]} max={w["max"]}'
    now_s = 'None' if g is None else f'n={g["n"]} mean={g["mean"]} max={g["max"]}'
    print(f'  chain={chain} seg={seg} {metric}: golden {golden_s} -> 本次 {now_s}')

  if not args.accept:
    hint = ('REGRESSION：漂移未固化。若属预期变更，先在 golden 的 drift_notes[] 补 '
            + '(chain, metric, old, new, commit, mechanism) 归因条目，再以 --accept 复核；无归因的漂移一律按回归拦下。')
    print(hint)
    sys.exit(1)

  # --accept：脚本级强制「基线漂移永远带着出处进库」。
  unattributed = []
  for chain, _seg, metric, w, g in drifts:
    if find_drift_note(golden, chain, metric, w, g) is None:
      unattributed.append(f'chain={chain} metric={metric}')
  if unattributed:
    print('--accept 拒绝：以下漂移在 drift_notes 中无 old/new 吻合的归因条目：')
    for u in unattributed:
      print(f'  {u}')
    sys.exit(1)

  for row in golden['runs']:
    got = results[(row['chain'], row['seg'])].get(row['metric'])
    if got is not None:
      row['n'], row['mean'], row['max'] = got['n'], got['mean'], got['max']
  golden['baseline_commit'] = head_commit()
  with open(args.golden, 'w') as f:
    f.write(json.dumps(golden, indent=2, ensure_ascii=False) + '\n')
  print(f'INTENDED-CHANGE：{len(drifts)} 项漂移已带归因固化，golden 更新至 {golden["baseline_commit"]}。')
  sys.exit(0)


if __name__ == '__main__':
  main()
