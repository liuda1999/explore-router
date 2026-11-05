"""统一消息模板层。

跨模块传递的文本（后台 system、任务包、结果前缀、进度前缀、截断标记、
前台降级文案）集中在这里，业务代码不再内联拼接字符串。

约定：
- 模板只负责「文本形态」，不参与路由决策，也不读取配置；
- `final_reply` 永远是模型原始输出，任何前缀只用于会话历史或即时回复；
- 修改任一模板后递增 PROMPT_VERSION，并在 PR 说明影响面。
"""

PROMPT_VERSION = "1"

# 后台执行模型的 system prompt
BACKEND_SYSTEM = "你是本地执行模型，请直接完成任务并输出结果。"

# 后台结果写入会话历史时的前缀（仅历史使用，API 的 final_reply 保持原样）
BACKEND_RESULT_PREFIX = "[后台结果] "

# 进度应答前缀（只追加到即时回复，不写入历史）
PROGRESS_PREFIX = "\n\n[后台任务进度] "

# 超长输入的中间截断标记
TRUNCATION_MARK = "\n...[中间内容已截断]...\n"

# 前台不可用时的降级文案；`前台模型暂时不可用` 是审计脚本的断言关键词，勿改
FRONT_UNAVAILABLE_TEMPLATE = "收到，我记下了。（前台模型暂时不可用：{error_type}）"

# 后台任务包模板
TASK_PACKAGE_TEMPLATE = (
    "任务：{task_text}\n"
    "语义线索：关键词={keywords}；长度={char_len}字；代码相关={has_code}\n"
    "要求：直接给出完整结果，不要寒暄，不要复述任务。"
    "{truncated_note}"
)

# 输入被截断时追加的说明
TRUNCATED_NOTE = "\n注意：原始输入过长，中间部分已截断，请基于保留内容完成。"

# 历史因预算被截断时的系统提示；避免模型把最近轮次误认为更早的内容
HISTORY_TRUNCATED_NOTE = (
    "\n【重要】更早的对话已因上下文预算被省略，你只能看到最近几轮。"
    "如果用户询问的轮次或内容不在当前对话中，必须只回答“更早的记录已不可用”，"
    "禁止用当前可见的其他轮次内容代替、推测或编造。"
)


def build_task_package(task_text, clues, truncated=False):
    """把用户输入 + 语义线索渲染成后台任务包（固定字段顺序）。"""
    return TASK_PACKAGE_TEMPLATE.format(
        task_text=task_text,
        keywords=",".join(clues.get("keywords", [])[:6]),
        char_len=clues.get("char_len", len(task_text)),
        has_code="是" if clues.get("has_code_hint") else "否",
        truncated_note=TRUNCATED_NOTE if truncated else "",
    )


def backend_messages(task_text, clues, truncated=False):
    """后台调用的标准 messages：system + user，角色严格交替。"""
    return [
        {"role": "system", "content": BACKEND_SYSTEM},
        {"role": "user", "content": build_task_package(task_text, clues, truncated)},
    ]


def history_message(reply):
    """后台结果写入会话历史时的封装；API 返回仍使用原始 reply。"""
    return BACKEND_RESULT_PREFIX + reply


def progress_note(desc):
    """即时回复末尾的进度附注；不写入历史。"""
    return PROGRESS_PREFIX + desc


def front_failure(exc):
    """前台不可用时的兜底文案，保持一句有着落。"""
    return FRONT_UNAVAILABLE_TEMPLATE.format(error_type=type(exc).__name__)
