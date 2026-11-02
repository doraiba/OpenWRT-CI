#!/bin/bash
# H5000M：让 luci-app-h5000m-fancontrol 独占风扇 PWM（设备树侧）
#
# ============================ 为什么要动设备树 ============================
# 节点本体在 target/linux/mediatek/dts/mt7987.dtsi（`fan: pwm-fan`，
# cooling-levels = <0 128 192 255>），板级 dts 只用 `&fan { status = "okay" }`
# 打开它，冷却映射同样继承自 dtsi：
#
#   cpu-passive      <&fan 1 1> → trip active-low   40 ℃  → level[1] = 128 (50%)
#   cpu-active-low   <&fan 2 2> → trip active-med   85 ℃  → level[2] = 192 (75%)
#   cpu-active-high  <&fan 3 3> → trip active-high 115 ℃  → level[3] = 255 (100%)
#
# 也就是说 state 1 恰好是 128/255 = 50%。
# 于是内核 governor 与用户态控制器抢同一路输出，控制器只能把目标抬到映射
# 要求的档位：CPU 一过 40 ℃，下限就是 50%，静音曲线里的 0~40% 永远落不了地。
#
# 真机实测（3.2.0，CPU 42 ℃，curve=silent=…40:40…）：
#   requested_pwm=102 (40%)  target_pwm=128 (50%)  reason=auto+kernel-floor
#   floor_pwm=128  owner=shared  basis="conservative fallback thresholds …"
#
# 另外控制器读不到 trip 节点（stock 命名是 active-*/active-med/hot/crit，
# 不是它假定的 trip-point*），phandle 匹配一律落空后退回 38/83/113 保守阈值。
# 退回来的结果同样落在 state 1 → 50%，说明真正的约束来自 cooling-map 本身，
# 而不是阈值或节点名。
#
# 删掉这三条映射后 owner 变成 exclusive（控制器独占，和上游 patch 一致）：
#   · 内核不再要求任何档位，85 ℃ 以下完全没有下限，静音曲线的 0~40% 真正生效；
#   · 高温兜底改由控制器自己扛：≥83 ℃ 顶到 cooling-levels[2] = 192（75%），
#     ≥113 ℃ 顶到 255（100%）；读不到 CPU 温度时直接 255。这两个常数比内核
#     trip（85/115 ℃）略保守，是控制器 3.x 自带的 EMERGENCY_STATE_2/3_C；
#   · hot(120 ℃) / critical(125 ℃) trip 与 cpu-active-hot（cpufreq 降频）原样
#     保留，CPU 降频与过热保护不受影响。
#
# 映射定义在 dtsi 里被所有 MT7987 板子共享，因此 /delete-node/ 只追加到
# 板级 h5000m dts，其它机型不受影响。
#
# 与上游 luci-app-h5000m-fancontrol 自带的
# openwrt-patches/h5000m-userspace-fan-control.patch 语义一致；之所以在这里
# 直接追加而不是 `patch -p1`，是因为后者依赖 dts 上下文行，上游一改 dts
# 就会 `Hunk #1 FAILED` 中断整包编译。追加式写法只依赖节点路径，
# 且带幂等标记，dts 增删上下文都不会失效。
# ==========================================================================

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
MARK="h5fan-userspace-ownership"

if [ ! -d "$DTS_DIR" ]; then
	echo "WARNING: $DTS_DIR 不存在，风扇内核下限补丁未应用（静音模式仍会被抬到 50%）" >&2
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

/* ${MARK}：风扇 PWM 只由 luci-app-h5000m-fancontrol 驱动。
 * 只摘掉指向 pwm-fan 的三条主动映射，hot/critical trip 与其它冷却设备
 * （cpu-active-hot）保持原样，内核 CPU 降频与过热保护不受影响。
 * 保留这些映射会让内核把输出下限顶到 cooling-levels[1]=128(50%)，
 * 静音曲线失效。详见仓库 Scripts/fan/dt-fan-ownership.sh 的说明。
 */
&{/thermal-zones/cpu-thermal/cooling-maps} {
	/delete-node/ cpu-active-high;
	/delete-node/ cpu-active-low;
	/delete-node/ cpu-passive;
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
	echo "WARNING: $DTS_DIR 下没有 *h5000m*.dts，风扇内核下限补丁未应用（静音模式仍会被抬到 50%）" >&2
fi

exit 0
