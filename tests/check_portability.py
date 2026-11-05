"""CI 静态层：可移植性检查。

防止把当前设备的模型名、显卡型号、绝对路径重新写回代码。
配置源 settings.yaml 允许出现具体模型/路径（那是它的职责）。
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGETS = (
    sorted((ROOT / "app").glob("*.py"))
    + [ROOT / "run.sh"]
    + sorted((ROOT / "tests").glob("*.py"))
    + sorted((ROOT / "tests").glob("*.sh"))
)
SELF = pathlib.Path(__file__).resolve()

RULES = [
    (re.compile(r"/home/liuda"), "设备绝对路径"),
    (re.compile(r"\b(front3b|mid9b|moe35b)\b"), "具体模型文件名"),
    (re.compile(r"\b(RTX\s*3080|3080\s*Ti|A100|H100)\b", re.I), "显卡型号"),
    (re.compile(r'["\']0\s*,\s*1["\']'), "硬编码多卡索引"),
]

violations = []
for path in TARGETS:
    if path.resolve() == SELF:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        violations.append((str(path), 0, f"读取失败: {exc}"))
        continue
    for i, line in enumerate(text.splitlines(), 1):
        for rx, label in RULES:
            if rx.search(line):
                violations.append((str(path.relative_to(ROOT)), i, f"{label}: {line.strip()[:160]}"))

# requirements 必须固定版本，保证 CI 可复现
req = ROOT / "requirements.txt"
if req.exists():
    for i, line in enumerate(req.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if s and not s.startswith("#") and "==" not in s:
            violations.append(("requirements.txt", i, f"未固定版本: {s}"))

if violations:
    print(f"[FAIL] 可移植性检查发现 {len(violations)} 处问题：")
    for f, n, msg in violations:
        print(f"  {f}:{n}  {msg}")
    sys.exit(1)

print(f"[PASS] 可移植性检查：{len(TARGETS)} 个文件，无设备/模型硬编码；依赖版本已固定")
