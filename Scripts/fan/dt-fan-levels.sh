#!/bin/bash
# H5000M：把内核强制的风扇下限从 128（50%）降到 96（38%）（设备树侧）
#
# ============================= 背景 =============================
# pwm-fan 节点与 cooling-levels 都在 target/linux/mediatek/dts/mt7987.dtsi
# （所有 MT7987 板子共享），板级 mt7987a-hiveton-h5000m.dts 只做
#
#   &fan { pwms = <&pwm 1 50000 0>; status = "okay"; };
#
# 使能。cpu-thermal/cooling-maps 里三条映射直接指向这路风扇：
#
#   cpu-passive      <&fan 1 1> → trip active-low   40 ℃ → level[1] = 128 (50%)
#   cpu-active-low   <&fan 2 2> → trip active-med   85 ℃ → level[2] = 192 (75%)
#   cpu-active-high  <&fan 3 3> → trip active-high 115 ℃ → level[3] = 255 (100%)
#
# （另有 cpu-active-hot，挂的是 cpu0~cpu3 四个 cpufreq 冷却设备，只做降频，
#   与风扇无关；2026-11-09 复核 owrt 分支时它才出现，不影响本补丁。）
#
# 2026-11-09 复核（刷入上游固件 r0-8e4a3ef 实测）：上游并未改动这里 ——
# /proc/device-tree/pwm-fan/cooling-levels 仍是 0/128/192/255，
# cooling_device0(pwm-fan) max=3 cur=1，守护进程仍打印
# reason=auto+kernel-floor target=128。故本补丁继续保留。
# 注意下限只在 cur_state>=1（约 40 ℃ 以上）时生效，低温段 PWM 可低于 128，
# 别把「低温不夹」误判成「上游已解除下限」。
#
# 于是 CPU 一过 40 ℃ 内核就要求至少 128/255 = 50%，控制器算出来的 40% 会被
# apply_floor 抬回去。真机实测（3.2.0，CPU 42 ℃，curve_silent=…40:40…）：
#
#   requested_pwm=102 (40%)  target_pwm=128 (50%)  reason=auto+kernel-floor
#   floor_pwm=128  owner=shared  basis="conservative fallback thresholds …"
#
# UCI 里没有「下限」这个选项，改曲线、切 manual 都要过 apply_floor，所以只能
# 从设备树改。
#
# ============================= 改法 =============================
# 只把第一档 128 改成 96（96/255 ≈ 37.6%），其余三档与全部 trip 都不动：
#
#   · 40~85 ℃ 的下限降成 38%：静音曲线写 40% 时算出来 102 > 96，不再被夹；
#   · 85 ℃ 的 192（75%）、115 ℃ 的 255（100%）、hot(120)/critical(125) 兜底
#     原样保留，CPU 降频与过热保护链路完全不受影响；
#   · 内核 governor 仍然持有这路风扇（owner=shared），只是要求变低，比
#     「删掉 cooling-map 让控制器独占」更接近原厂，只覆盖一个属性。
#
# 覆盖写在板级 dts 里，dtsi 的默认值不动，其它 MT7987 机型不受影响。
# 追加式写入带幂等标记：重复构建不会重复追加，也不依赖 dts 上下文行
# （`patch -p1` 一旦上游改 dts 就会 Hunk FAILED 中断整包编译）。
# =================================================================

set -u

WRT_ROOT="${1:-}"
if [ -z "$WRT_ROOT" ]; then
	if [ -n "${GITHUB_WORKSPACE:-}" ] && [ -d "$GITHUB_WORKSPACE/wrt/target" ]; then
		WRT_ROOT="$GITHUB_WORKSPACE/wrt"
	else
		WRT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)/wrt"
	fi
fi

DTS_DIR="$WRT_ROOT/target/linux/mediatek/dts"
MARK="h5fan-level-96"

if [ ! -d "$DTS_DIR" ]; then
	echo "WARNING: $DTS_DIR 不存在，风扇下限补丁未应用（静音模式仍会被抬到 50%）" >&2
	exit 0
fi

applied=0
for DTS in "$DTS_DIR"/*h5000m*.dts; do
	[ -f "$DTS" ] || continue

	if grep -q "$MARK" "$DTS"; then
		echo "fan dt: $(basename "$DTS") 已含 ${MARK}，跳过"
		applied=$((applied + 1))
		continue
	fi

	cat >> "$DTS" <<EOF

/* ${MARK}：内核风扇下限 128(50%) → 96(38%)，其余档位与 trip 不动。
 * 只覆盖本板，dtsi 里的默认值不变。详见 Scripts/fan/dt-fan-levels.sh。
 */
&fan {
	cooling-levels = <0 96 192 255>;
};
EOF

	if grep -q "$MARK" "$DTS"; then
		echo "fan dt: 已追加 $MARK 到 $(basename "$DTS")"
		applied=$((applied + 1))
	else
		echo "ERROR: 追加 $MARK 到 $(basename "$DTS") 失败" >&2
		exit 1
	fi
done

if [ "$applied" -eq 0 ]; then
	echo "WARNING: $DTS_DIR 下没有 *h5000m*.dts，风扇下限补丁未应用（静音模式仍会被抬到 50%）" >&2
fi

exit 0
