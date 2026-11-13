#!/usr/bin/env python3
"""只把出厂「均衡」曲线 40℃ 那一档的占空比从 50 压到 35，其余一律不动。

背景（2026-11-13）：
  包自带的 root/etc/config/h5000m_fancontrol 模板里，默认档案是
      option curve 'balanced'
      option curve_balanced '20:0,35:0,40:50,55:60,70:75,85:90,95:100'
  40℃ 给 50%（pwm 128），而 H5000M 日常待机就在 40~43℃，等于开机就半速吹，
  噪音明显。kernel DT 的 cooling-levels 第一档是 90（约 35%），再低的请求会被
  硬件抬到 90 —— 所以 40℃ 这一档给 35% 正好落在硬件下限上，是「能听到的最低档」。

  为什么只改这一处：
    - 模式（curve）保持 balanced，不改默认行为，与他人预期一致；
    - 静音档案（curve_silent）与自定义档案（curve_custom）保持包出厂值；
    - 高温段（55℃ 以上）完全不动，散热曲线与官方一致，不影响重载。
  只降低温起点，不改变整条策略。

  注意 sysupgrade 保留配置时 /etc/config 里的旧值优先，本补丁不会覆盖用户已
  调好的机器 —— 这正是期望行为。

用法：
  fancontrol-default-profile.py <包目录>

幂等：已是 35 则直接跳过；找不到 curve_balanced 或没有 40℃ 点则非零退出，
让构建在导入插件阶段显式失败，而不是静默发一个「还是 50%」的固件。
"""

import os
import re
import sys

REL = "root/etc/config/h5000m_fancontrol"

# 唯一的目标：均衡曲线里 40℃ 这一档 → 35%
KEY = "curve_balanced"
TEMP = 40
DUTY = 35


def die(msg):
    sys.stderr.write("fancontrol-default-profile: %s\n" % msg)
    raise SystemExit(1)


def resolve(path):
    if os.path.isfile(path):
        if not path.endswith("h5000m_fancontrol"):
            die("传入的是文件但不是配置模板: %s" % path)
        return path
    target = os.path.join(path, REL)
    if os.path.isfile(target):
        return target
    die("找不到 %s（包目录=%s）。若上游调整了 root/etc/config 布局，请同步更新本脚本的 REL。" % (REL, path))


def rewrite(spec):
    """把 spec 里 TEMP 那一档改成 DUTY；返回 (新串, 是否命中该温度点)。"""
    out = []
    hit = False
    for pair in spec.split(","):
        pair = pair.strip()
        if not pair:
            continue
        try:
            t_str, p_str = pair.split(":", 1)
            t, p = int(t_str), int(p_str)
        except ValueError:
            die("曲线格式无法解析: %r" % pair)
        if t == TEMP:
            p = DUTY
            hit = True
        out.append("%d:%d" % (t, p))
    return ",".join(out), hit


def main():
    if len(sys.argv) != 2:
        die("用法: %s <包目录 | h5000m_fancontrol 路径>" % sys.argv[0])

    path = resolve(sys.argv[1])
    with open(path, encoding="utf-8") as fh:
        src = fh.read()

    pat = re.compile(r"^(\s*option\s+%s\s+)'([^']*)'" % re.escape(KEY), re.M)
    found = pat.findall(src)
    if len(found) != 1:
        die(
            "锚点 %r 匹配 %d 次（应为 1）。上游可能改了配置模板，请核对后再更新本脚本。"
            % ("option " + KEY, len(found))
        )

    new_spec, hit = rewrite(found[0][1])
    if not hit:
        die(
            "%s 里没有 %d℃ 这个点：%s。上游可能调整了曲线分段，请核对后再更新本脚本的 TEMP。"
            % (KEY, TEMP, found[0][1])
        )

    if new_spec == found[0][1]:
        print("fancontrol-default-profile: %s 的 %d℃ 已是 %d%%，跳过 %s" % (KEY, TEMP, DUTY, path))
        return 0

    src = pat.sub(lambda m: "%s'%s'" % (m.group(1), new_spec), src, count=1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src)

    print("fancontrol-default-profile: %s %s -> %s" % (KEY, found[0][1], new_spec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
