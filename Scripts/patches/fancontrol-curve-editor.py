#!/usr/bin/env python3
"""让 luci-app-h5000m-fancontrol 的「曲线点编辑器」编辑当前选中的配置档案。

背景（2026-11-12 实测，固件 h5000m-fancontrol 3.2.0）：
  LuCI「Fan curve」页的曲线点编辑器把结果固定写进 curve_custom，而控制器只读
  CURVE_OPTION[curve] 指向的那一条。于是：

    profile = Balanced 时，在该页拖点/加点——数值确实存进了 curve_custom，
    页面也真的显示「已修改」，但守护进程永远不读它，风扇毫无变化，界面也不提示。

  同页的「Effective curve」图又按 CURVE_OPTION[curve] 画，所以「图上曲线没变、
  编辑器里的点变了」，更容易误判为「梯度没生效」。

  另外编辑器初始为空：editorFromSpec 只被「恢复默认」和「恢复全部默认」两处调用，
  首次进入该页没有任何行，必须先点按钮才会出现点。

本补丁（只改前端 JS，不碰 Rust 控制器与 UCI 结构）：
  1. 曲线点编辑器改为编辑「当前选中的配置档案」（Balanced → curve_balanced），
     面板标题追加当前档案名，所见即所改；
  2. 打开该页 / 切换档案时，用当前生效的曲线填充编辑器；
  3. 「恢复默认曲线」恢复当前档案的默认值；
  4. 空曲线不再被写回，避免误把某个档案清空后触发 failsafe 全速。

用法：
  fancontrol-curve-editor.py <包目录 | fancontrol.js 路径>

幂等：已打过补丁则直接跳过；任一锚点找不到（上游改了前端）则报错退出，
使构建在「导入自定义插件」阶段显式失败，而不是静默装回有缺陷的界面。
"""

import sys
import os

REL = "htdocs/luci-static/resources/view/h5000m/fancontrol.js"

# (说明, 原文, 替换为)
PATCHES = [
    (
        "曲线点编辑器写入当前档案，且不再写空值",
        """\t\tif (!parsed.error || spec === '') {
\t\t\tthis.values.curve_custom = spec;
\t\t\tthis.syncWidget('curve_custom');
\t\t\tthis.validateOption('curve_custom', true);
\t\t\tthis.markDirty();
\t\t}""",
        """\t\tvar curveKey = CURVE_OPTION[this.values.curve] || 'curve_custom';

\t\t/* Write into the profile the controller actually reads, and never
\t\t * blank it out: an empty curve would make the controller fall back to
\t\t * the failsafe output. */
\t\tif (spec !== '' && !parsed.error && this.values[curveKey] !== spec) {
\t\t\tthis.values[curveKey] = spec;
\t\t\tthis.syncWidget(curveKey);
\t\t\tthis.validateOption(curveKey, true);
\t\t\tthis.markDirty();
\t\t}""",
    ),
    (
        "编辑器的兜底默认值跟随当前档案",
        """\t\t\tthis.editorPoints = OPTIONS.curve_custom.def.split(',').map(function(pair) {""",
        """\t\t\tthis.editorPoints = OPTIONS[this.activeCurveKey()].def.split(',').map(function(pair) {""",
    ),
    (
        "新增 activeCurveKey / syncEditorToProfile",
        """\teditorFromSpec: function(spec) {""",
        """\tactiveCurveKey: function() {
\t\treturn CURVE_OPTION[this.values.curve] || 'curve_custom';
\t},

\t/* Fill the point editor with the curve that is in effect for the selected
\t * profile, so the panel shows what the user is actually editing. */
\tsyncEditorToProfile: function() {
\t\tvar key = this.activeCurveKey(), spec;

\t\tif (!this.nodeList || !this.nodeError || !this.values)
\t\t\treturn;

\t\tspec = known(this.values[key]) && this.values[key] !== ''
\t\t\t? this.values[key]
\t\t\t: OPTIONS[key].def;

\t\tthis.editorFromSpec(spec);
\t},

\teditorFromSpec: function(spec) {""",
    ),
    (
        "切换档案后重建编辑器内容",
        """\t\tif (key === 'curve_custom' || key === 'curve')
\t\t\tthis.syncCurvePreview();""",
        """\t\tif (key === 'curve_custom' || key === 'curve')
\t\t\tthis.syncCurvePreview();

\t\tif (key === 'curve')
\t\t\tthis.syncEditorToProfile();""",
    ),
    (
        "「恢复默认曲线」恢复当前档案的默认值",
        """\tresetCustomCurve: function() {
\t\tthis.editorFromSpec(OPTIONS.curve_custom.def);
\t\tthis.notify(_('The default custom curve was loaded into the editor. Save to store it.'), 'info');
\t},""",
        """\tresetCustomCurve: function() {
\t\tthis.editorFromSpec(OPTIONS[this.activeCurveKey()].def);
\t\tthis.notify(_('Defaults loaded into the form. Save to store them.'), 'info');
\t},""",
    ),
    (
        "「恢复全部默认」后编辑器同步",
        """\t\t\t\tthis.rebuildWidgets();
\t\t\t\tthis.editorFromSpec(OPTIONS.curve_custom.def);""",
        """\t\t\t\tthis.rebuildWidgets();
\t\t\t\tthis.syncEditorToProfile();""",
    ),
    (
        "面板标题显示当前档案（复用已有译文，不引入新词条）",
        """\t\t\tE('h3', {}, [ _('Custom curve points') ]),""",
        """\t\t\tE('h3', {}, [ _('Fan curve'), ' \\u2014 ', this.profileName(this.values ? this.values.curve : 'custom') ]),""",
    ),
    (
        "首次进入曲线页即填充编辑器",
        """\t\t\tE('p', { 'class': 'h5fan-note' }, [
\t\t\t\t_('Temperatures must rise strictly and percentages stay within 0-100%. An invalid curve makes the controller run its failsafe output instead of stopping the fan.')
\t\t\t])
\t\t]));
\t},""",
        """\t\t\tE('p', { 'class': 'h5fan-note' }, [
\t\t\t\t_('Temperatures must rise strictly and percentages stay within 0-100%. An invalid curve makes the controller run its failsafe output instead of stopping the fan.')
\t\t\t])
\t\t]));

\t\tthis.syncEditorToProfile();
\t},""",
    ),
]

MARKERS = ("activeCurveKey", "syncEditorToProfile")


def die(msg, code=1):
    sys.stderr.write("fancontrol-curve-editor: %s\n" % msg)
    sys.exit(code)


def resolve(path):
    if os.path.isfile(path):
        return path
    target = os.path.join(path, REL)
    if os.path.isfile(target):
        return target
    die("找不到 %s（包目录=%s）。若上游调整了前端目录结构，请同步更新本脚本的 REL。" % (REL, path))


def main():
    if len(sys.argv) != 2:
        die("用法: %s <包目录 | fancontrol.js 路径>" % sys.argv[0])

    path = resolve(sys.argv[1])
    with open(path, encoding="utf-8") as fh:
        src = fh.read()

    if all(m in src for m in MARKERS):
        print("fancontrol-curve-editor: 已打过补丁，跳过 %s" % path)
        return 0

    for desc, old, new in PATCHES:
        if new in src:
            print("fancontrol-curve-editor: 跳过已完成项：%s" % desc)
            continue
        hits = src.count(old)
        if hits != 1:
            die(
                "锚点匹配 %d 次（应为 1）：%s\n"
                "上游前端可能已改动，请重新核对 fancontrol.js 后再更新本脚本。\n"
                "锚点片段：%r" % (hits, desc, old[:80])
            )
        src = src.replace(old, new, 1)

    missing = [m for m in MARKERS if m not in src]
    if missing:
        die("补丁后缺少标记 %s，拒绝写入" % ", ".join(missing))

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src)

    print("fancontrol-curve-editor: 已应用 %d 处修改 -> %s" % (len(PATCHES), path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
