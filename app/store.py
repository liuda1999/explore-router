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


def get_history(session_id, max_chars=None, max_messages=40):
    """取会话历史，按字符预算从最近往前保留完整轮次。

    max_chars=None 时返回窗口内全部（最多 max_messages 条）。
    预算不足时宁可少带历史，也不越过调用方分配的上下文预算。
    """
    with _lock, _db() as c:
        rows = c.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, max_messages)).fetchall()
    msgs = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # 兼容旧数据：合并连续同角色，并丢弃窗口截断后开头的 assistant
    out = []
    for m in msgs:
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] = out[-1]["content"] + "\n\n" + m["content"]
        else:
            out.append(dict(m))
    while out and out[0]["role"] != "user":
        out.pop(0)
    # 结尾若是孤立的 user（例如上一轮只写入了提问、回复未落库），丢弃，
    # 避免与当前输入拼成连续两条 user 触发 chat template 异常。
    while out and out[-1]["role"] != "assistant":
        out.pop()

    if max_chars is None:
        return out

    selected = []
    total = 0
    i = len(out) - 1
    while i >= 0:
        if (out[i]["role"] == "assistant" and i - 1 >= 0
                and out[i - 1]["role"] == "user"):
            pair = [out[i - 1], out[i]]
            i -= 2
        else:
            pair = [out[i]]
            i -= 1
        pair_chars = sum(len(m["content"]) for m in pair)
        if total + pair_chars > max_chars:
            # 最近一对都放不下时返回空历史，优先保证不溢出
            break
        selected = pair + selected
        total += pair_chars
    return selected
