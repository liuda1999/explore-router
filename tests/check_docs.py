"""CI 静态层：文档与实现的一致性。

- 文档里提到的本项目 API 端点必须在 app/main.py 中真实注册；
- 文档不得残留旧项目名；
- 文档中出现的 settings.yaml 顶层配置段必须真实存在；
- 中英双语介绍、技术栈与部署文档同样纳入检查。
"""
import ast
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = [ROOT / name for name in
        ("README.md", "OPERATIONS.md", "PROJECT_INTRO.md",
         "PROJECT_INTRO.en.md", "TECH_STACK.md", "DEPLOYMENT.md",
         "TEST_REPORT.md", "AUDIT_REPORT.md", "ACCEPTANCE_REPORT.md")]
SETTINGS = ROOT / "settings.yaml"

routes = set()
tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
for node in ast.walk(tree):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    for dec in node.decorator_list:
        if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in ("get", "post", "put", "delete", "patch")
                and dec.args and isinstance(dec.args[0], ast.Constant)):
            routes.add(str(dec.args[0].value))

problems = []
endpoint_rx = re.compile(r"(?<![\w/])/(chat|task_result|health|config|gpu|tasks)(?![\w/])")
old_name_rx = re.compile(r"open\s*claw", re.I)

with open(SETTINGS, "r", encoding="utf-8") as f:
    settings = yaml.safe_load(f) or {}
top_keys = set(settings.keys())

for doc in DOCS:
    if not doc.exists():
        problems.append((doc.name, 0, "文档不存在"))
        continue
    text = doc.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        for ep in endpoint_rx.findall(line):
            if "/" + ep not in routes:
                problems.append((doc.name, i, f"文档提到未注册端点 /{ep}"))
        if old_name_rx.search(line):
            problems.append((doc.name, i, f"残留旧项目名: {line.strip()[:120]}"))

    # 文档里以 `settings.yaml` 段落名出现的顶层键，必须真实存在
    for m in re.finditer(r"`([a-z_]+)`\s*(?:段|配置段|:|：)", text):
        key = m.group(1)
        if key in ("runtime", "context", "embedding", "slots", "routing", "frontend") \
                and key not in top_keys:
            problems.append((doc.name, 0, f"文档提到的配置段不存在: {key}"))

if problems:
    print(f"[FAIL] 文档一致性检查发现 {len(problems)} 处问题：")
    for f, n, msg in problems:
        print(f"  {f}:{n}  {msg}")
    sys.exit(1)

print(f"[PASS] 文档一致性：{len(DOCS)} 份文档，API 端点均已注册，无旧项目名残留")
