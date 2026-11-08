"""任务状态与会话上下文：SQLite 持久化，重启不丢，无需 Redis/Celery。"""
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

DB = config.DATA / "state.db"
_lock = threading.RLock()


@contextmanager
def _db():
    """一次操作一个连接，退出时提交并显式关闭，避免连接/文件句柄堆积。"""
    c = sqlite3.connect(str(DB), check_same_thread=False, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    with _lock, _db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            user_input TEXT NOT NULL,
            status TEXT NOT NULL,
            progress INTEGER DEFAULT 0,
            tier TEXT,
            model_name TEXT,
            instant_reply TEXT,
            final_reply TEXT,
            release_info TEXT,
            error_msg TEXT,
            selection_reason TEXT,
            prefill_ms REAL,
            num_ctx INTEGER,
            finish_reason TEXT,
            created_at REAL,
            updated_at REAL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_task_session ON tasks(session_id, status)")
        # 轻量迁移：旧库补 finish_reason 列（只 ADD COLUMN，不改旧列、不动数据）。
        # 目的：让 max_tokens 截断（length）对调用方可观测。
        task_cols = {row[1] for row in c.execute("PRAGMA table_info(tasks)")}
        if "finish_reason" not in task_cols:
            c.execute("ALTER TABLE tasks ADD COLUMN finish_reason TEXT")
        # Responses API：previous_response_id 续接与 GET 检索。
        # 独立新表，只 CREATE IF NOT EXISTS，不对旧表做 ALTER。
        c.execute("""CREATE TABLE IF NOT EXISTS responses (
            response_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            task_id TEXT,
            model TEXT,
            input_text TEXT,
            output_text TEXT,
            status TEXT,
            created_at REAL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_resp_session "
                  "ON responses(session_id, created_at)")
        # 上下文压缩的滚动摘要：按会话保存，增量合并，避免每次对全量重算
        c.execute("""CREATE TABLE IF NOT EXISTS session_summaries (
            session_id TEXT PRIMARY KEY,
            summary TEXT NOT NULL,
            covered_messages INTEGER DEFAULT 0,
            updated_at REAL
        )""")


def reset_inflight():
    """启动时恢复中断状态：processing 标记失败（避免永久卡住），pending 返回重新入队。"""
    now = time.time()
    with _lock, _db() as c:
        c.execute(
            "UPDATE tasks SET status='failed', progress=0, error_msg=?, updated_at=? "
            "WHERE status='processing'",
            ("服务重启，任务中断；请重新提交", now))
        rows = c.execute(
            "SELECT task_id FROM tasks WHERE status='pending' ORDER BY created_at"
        ).fetchall()
    return [r["task_id"] for r in rows]


def create_task(task_id, session_id, user_input, tier, instant_reply, reason):
    now = time.time()
    with _lock, _db() as c:
        c.execute("""INSERT INTO tasks
            (task_id, session_id, user_input, status, progress, tier, instant_reply,
             selection_reason, num_ctx, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                  (task_id, session_id, user_input, "pending", 0, tier,
                   instant_reply, reason, config.NUM_CTX, now, now))


def update_task(task_id, **kw):
    kw["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in kw)
    with _lock, _db() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE task_id=?", (*kw.values(), task_id))


def get_task(task_id):
    with _lock, _db() as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def active_tasks(session_id):
    with _lock, _db() as c:
        rows = c.execute(
            "SELECT task_id, status, progress, tier, user_input, created_at "
            "FROM tasks WHERE session_id=? AND status IN ('pending','processing') "
            "ORDER BY created_at DESC LIMIT 5", (session_id,)).fetchall()
    return [dict(r) for r in rows]


def recent_tasks(session_id, limit=3, within_s=None):
    """进度应答用：含已完成/失败，可限定时间窗口，避免把很久前的任务当进度。"""
    sql = ("SELECT task_id, status, progress, tier, user_input, created_at "
           "FROM tasks WHERE session_id=?")
    args = [session_id]
    if within_s is not None:
        sql += " AND created_at >= ?"
        args.append(time.time() - float(within_s))
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with _lock, _db() as c:
        rows = c.execute(sql, tuple(args)).fetchall()
    return [dict(r) for r in rows]


def list_tasks(limit=20):
    with _lock, _db() as c:
        rows = c.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                         (limit,)).fetchall()
    return [dict(r) for r in rows]


def task_count():
    with _lock, _db() as c:
        return int(c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])


def save_response(response_id, session_id, task_id, model,
                  input_text, output_text, status):
    """记录一次 Responses API 的输入输出，供 previous_response_id 与 GET 检索。

    同一 response_id 重复写入时覆盖（重试场景），不产生重复行。
    """
    with _lock, _db() as c:
        c.execute("""INSERT OR REPLACE INTO responses
            (response_id, session_id, task_id, model, input_text, output_text,
             status, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                  (response_id, session_id, task_id, model, input_text,
                   output_text, status, time.time()))


def get_response(response_id):
    with _lock, _db() as c:
        row = c.execute("SELECT * FROM responses WHERE response_id=?",
                        (response_id,)).fetchone()
    return dict(row) if row else None


def get_session_summary(session_id):
    """取该会话的滚动摘要：{summary, covered_messages} 或 None。"""
    with _lock, _db() as c:
        row = c.execute(
            "SELECT summary, covered_messages FROM session_summaries "
            "WHERE session_id=?", (session_id,)).fetchone()
    return dict(row) if row else None


def save_session_summary(session_id, summary, covered_messages):
    with _lock, _db() as c:
        c.execute("INSERT OR REPLACE INTO session_summaries "
                  "(session_id, summary, covered_messages, updated_at) "
                  "VALUES (?,?,?,?)",
                  (session_id, summary, int(covered_messages), time.time()))


def append_message(session_id, role, content):
    """写入会话消息；连续同角色合并，保证 chat template 要求的 user/assistant 交替。

    后台结果是在即时回复之后追加的 assistant 消息，若直接 INSERT 会出现
    assistant -> assistant，Qwen/Ministral 等模板会抛 Jinja 异常并返回 500。
    """
    content = (content or "").strip()
    if not content:
        return
    with _lock, _db() as c:
        last = c.execute(
            "SELECT id, role, content FROM messages WHERE session_id=? "
            "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        if last and last["role"] == role:
            merged = (last["content"] + "\n\n" + content).strip()
            c.execute("UPDATE messages SET content=?, created_at=? WHERE id=?",
                      (merged, time.time(), last["id"]))
        else:
            c.execute(
                "INSERT INTO messages (session_id, role, content, created_at) "
                "VALUES (?,?,?,?)",
                (session_id, role, content, time.time()))


def _normalize(msgs):
    """合并连续同角色、丢弃开头非 user 与结尾非 assistant。返回 (out, truncated)。"""
    out = []
    truncated = False
    for m in msgs:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content") or ""
        if out and out[-1]["role"] == role:
            out[-1]["content"] = out[-1]["content"] + "\n\n" + content
        else:
            out.append({"role": role, "content": content})
    while out and out[0]["role"] != "user":
        out.pop(0)
        truncated = True
    # 结尾若是孤立的 user（例如上一轮只写入了提问、回复未落库），丢弃，
    # 避免与当前输入拼成连续两条 user 触发 chat template 异常。
    while out and out[-1]["role"] != "assistant":
        out.pop()
        truncated = True
    return out, truncated


def split_history(msgs, max_chars=None):
    """按预算切分历史，返回 `(kept, dropped, truncated)`。

    与 trim_history 的区别：把「被预算裁掉的部分」也返回出来，供上下文压缩
    使用——先摘要把丢弃内容保留成一段摘要，而不是直接扔掉。
    """
    out, truncated = _normalize(msgs)
    if max_chars is None:
        return out, [], truncated

    kept = []
    total_chars = 0
    cut = len(out)                      # 保留区间的起点
    i = len(out) - 1
    while i >= 0:
        if (out[i]["role"] == "assistant" and i - 1 >= 0
                and out[i - 1]["role"] == "user"):
            start = i - 1
        else:
            start = i
        pair = out[start:i + 1]
        pair_chars = sum(len(m["content"]) for m in pair)
        if total_chars + pair_chars > max_chars:
            break
        kept = pair + kept
        total_chars += pair_chars
        cut = start
        i = start - 1
    dropped = out[:cut]
    return kept, dropped, truncated or bool(dropped)


def trim_history(msgs, max_chars=None):
    """对任意消息列表做角色规范化与预算裁剪（纯函数，不读库）。

    规则与 get_history 完全一致：合并连续同角色、丢弃开头的非 user 与
    结尾的非 assistant、按字符预算从最近往前保留完整轮次。
    OpenAI 兼容层自带 messages，用它复用同一套预算逻辑，避免两套实现漂移。

    返回 (messages, truncated)。
    """
    kept, _dropped, truncated = split_history(msgs, max_chars)
    return kept, truncated


def get_history(session_id, max_chars=None, max_messages=40, with_meta=False):
    """取会话历史，按字符预算从最近往前保留完整轮次。

    max_chars=None 时返回窗口内全部（最多 max_messages 条）。
    预算不足时宁可少带历史，也不越过调用方分配的上下文预算。

    with_meta=True 时返回 (messages, truncated)。truncated 表示更早的对话
    因条数或字符预算被丢弃；调用方应提示模型不要猜测被省略的内容。
    """
    with _lock, _db() as c:
        total_rows = int(c.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)
        ).fetchone()[0])
        rows = c.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, max_messages)).fetchall()
    truncated = total_rows > len(rows)
    msgs = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    out, cut = trim_history(msgs, max_chars=max_chars)
    truncated = truncated or cut
    return (out, truncated) if with_meta else out


def get_history_split(session_id, max_chars=None, max_messages=40):
    """取历史并返回 `(kept, dropped, truncated)`。

    `dropped` 是按预算会被丢弃的早期轮次，交给上下文压缩做摘要，
    避免「预算不够就直接丢失早期信息」。
    """
    with _lock, _db() as c:
        total_rows = int(c.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)
        ).fetchone()[0])
        rows = c.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, max_messages)).fetchall()
    truncated = total_rows > len(rows)
    msgs = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    kept, dropped, cut = split_history(msgs, max_chars=max_chars)
    return kept, dropped, truncated or cut
