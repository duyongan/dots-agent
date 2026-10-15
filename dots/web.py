"""只读活动视图。stdlib http.server,服务端渲染,不用任何前端框架。

只绑 127.0.0.1。这个页面能看到你 agent 的全部活动,
绑到 0.0.0.0 就等于挂公网上了。
ponytail: 没有鉴权,靠只听本机;真要对外先加 auth 再改 host。

它开自己的一条连接,和 tick 循环那条不共享 —— 理由同 worker.checkpointer。
"""

from __future__ import annotations

import html
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dots import db

CSS = """
*{box-sizing:border-box}
body{background:#111;color:#ddd;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;
     margin:0;padding:24px;max-width:1200px}
h1{font-size:15px;font-weight:600;margin:0 0 4px}
h2{font-size:12px;font-weight:600;color:#888;margin:28px 0 8px;
   text-transform:uppercase;letter-spacing:.08em}
.sub{color:#666;font-size:12px;margin-bottom:4px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:5px 10px 5px 0;border-bottom:1px solid #222;
      vertical-align:top}
th{color:#666;font-weight:500;font-size:11px;text-transform:uppercase}
td.k{color:#888;white-space:nowrap}
.done{color:#5c9} .failed{color:#e66} .interrupted{color:#ec5}
.ready{color:#6af} .running{color:#6af} .pending,.pending_review{color:#a8a}
.empty{color:#555}
.tag{background:#1c1c1c;border:1px solid #2a2a2a;border-radius:3px;
     padding:0 5px;color:#aaa;font-size:11px}
pre{background:#1a1a1a;border:1px solid #262626;border-radius:4px;
    padding:8px;margin:0;white-space:pre-wrap;max-height:200px;overflow:auto}
"""


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _clock(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")


def _ago(ts) -> str:
    d = max(0, int(datetime.now().timestamp() - ts))
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


def render(conn) -> str:
    goals = db.list_goals(conn, limit=60)
    pending = db.pending_approvals(conn)
    audit = db.recent_audit(conn, 60)

    parts = [f"<!doctype html><html><head><meta charset='utf-8'>",
             f"<title>dots</title>",
             f"<meta http-equiv='refresh' content='10'>",
             f"<style>{CSS}</style></head><body>"]
    parts.append(f"<h1>dots</h1>")
    parts.append(
        f"<div class='sub'>{len(goals)} 个目标 · "
        f"{len(pending)} 个待批 · "
        f"今天是 {_clock(datetime.now().timestamp())} · 每 10 秒自刷新</div>"
    )

    parts.append("<h2>待批准</h2>")
    if pending:
        parts.append("<table><tr><th>编号</th><th>目标</th><th>动作</th>"
                     "<th>参数</th><th>等了</th></tr>")
        for a in pending:
            parts.append(
                f"<tr><td class='k'><span class='tag'>{_esc(a['token'])}</span></td>"
                f"<td class='k'>{_esc(a['goal_id'])}</td>"
                f"<td>{_esc(a['tool'])}</td>"
                f"<td><pre>{_esc(a['args'][:300])}</pre></td>"
                f"<td class='k'>{_ago(a['created_at'])}</td></tr>"
            )
        parts.append("</table>")
        parts.append(
            "<div class='sub' style='margin-top:8px'>"
            "批准:<code>python -m dots approve &lt;编号&gt;</code> · "
            "拒绝:<code>python -m dots reject &lt;编号&gt;</code></div>"
        )
    else:
        parts.append("<div class='empty'>没有等着你的动作。</div>")

    parts.append("<h2>目标</h2>")
    if goals:
        parts.append("<table><tr><th>ID</th><th>状态</th><th>token</th>"
                     "<th>更新</th><th>目标 / 结论</th></tr>")
        for g in goals:
            summary = g["summary"] or ""
            detail = f"<div>{_esc(g['text'][:300])}</div>"
            if summary:
                detail += f"<div style='color:#999;margin-top:4px'>{_esc(summary[:400])}</div>"
            if g["evidence"]:
                detail += f"<div class='k' style='margin-top:4px'>证据 {_esc(g['evidence'][:200])}</div>"
            parts.append(
                f"<tr><td class='k'>{_esc(g['id'])}</td>"
                f"<td class='{_esc(g['status'])}'>{_esc(g['status'])}</td>"
                f"<td class='k'>{g['tokens']:,}</td>"
                f"<td class='k'>{_ago(g['updated_at'])}</td>"
                f"<td>{detail}</td></tr>"
            )
        parts.append("</table>")
    else:
        parts.append("<div class='empty'>还没有目标。</div>")

    parts.append("<h2>活动</h2>")
    if audit:
        parts.append("<table><tr><th>时间</th><th>目标</th><th>事件</th><th>细节</th></tr>")
        for r in reversed(audit):
            parts.append(
                f"<tr><td class='k'>{_clock(r['ts'])}</td>"
                f"<td class='k'>{_esc(r['goal_id'] or '-')}</td>"
                f"<td>{_esc(r['kind'])}</td>"
                f"<td class='k'>{_esc(r['detail'][:220])}</td></tr>"
            )
        parts.append("</table>")
    else:
        parts.append("<div class='empty'>还没有活动。</div>")

    parts.append("</body></html>")
    return "".join(parts)


def make_handler(db_path: str):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802  (http.server 的命名约定)
            if self.path.split("?")[0] not in ("/", "/index.html"):
                self.send_error(404)
                return
            conn = db.connect(db_path)   # 每次请求一条,读完就关,不做连接池
            try:
                body = render(conn).encode("utf-8")
            finally:
                conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass   # 别把 HTTP 访问日志混进终端

    return Handler


def start(db_path, port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """后台线程起服务。返回 server 对象,进程退出时自动结束。"""
    try:
        server = ThreadingHTTPServer((host, port), make_handler(db_path))
    except OSError as e:
        raise SystemExit(
            f"活动视图起不来({host}:{port}):{e}\n换个端口:python -m dots serve --web --web-port 8899"
        )
    thread = threading.Thread(target=server.serve_forever, name="dot-web", daemon=True)
    thread.start()
    # 用实际绑上的端口,不是传进来的 —— 传 0 时由系统分配
    print(f"[dot] 活动视图 http://{host}:{server.server_address[1]}/  (只读,只听本机)")
    return server
