"""局域网多人聊天一键启动：同时拉起 服务器 + 网页端，并打印本机局域网 IP。

两种网页模式：
  默认（开发）：Vite 开发服务器（热更新，需 Node）
  --prod      ：serve_web.py 静态托管 web/dist（运行时无需 Node，需先 npm run build）

用法：
    python start_lan.py                 # 开发模式
    python start_lan.py --prod          # 生产模式（先 cd web && npm run build）
    MOYU_STORAGE=mysql python start_lan.py --prod   # 继续用 MySQL

- 服务器绑定 0.0.0.0（已含局域网 IP），桌面客户端登录框填打印出的 IP；
- 桌面客户端支持 UDP 自动发现，免手输 IP；
- 按 Ctrl+C 停止全部服务。
"""

import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
sys.path.insert(0, ROOT)
from common import lan_ip, PORT  # noqa: E402


def main():
    prod = "--prod" in sys.argv
    ip = lan_ip()
    procs = []

    def start(cmd, cwd, name, log):
        f = open(log, "w", encoding="utf-8")
        p = subprocess.Popen(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT)
        procs.append((name, p, f))
        return p

    print("=" * 58)
    print(" MoyuChat 局域网一键启动" + ("（生产模式）" if prod else "（开发模式）"))
    print("=" * 58)
    print(f" 本机局域网 IP：{ip}")
    print(f" 服务器：{ip}:{PORT}   （桌面客户端登录框填这个 IP）")

    start([sys.executable, "-u", "server.py"], ROOT, "server", "server.log")
    time.sleep(1.0)

    if prod:
        # 生产：serve_web 静态托管 web/dist + 内置 WS 桥接，无需 Node / 独立桥接进程
        if not os.path.isfile(os.path.join(WEB, "dist", "index.html")):
            print(" [!] 未找到 web/dist，请先构建：cd web && npm run build")
        start([sys.executable, "-u", "serve_web.py"], ROOT, "web", "serveweb.log")
        web_port = os.environ.get("MOYU_WEB_PORT", "8080")
        print(f" 网页端：http://{ip}:{web_port}   （静态托管，无需 Node）")
    else:
        start([sys.executable, "-u", "ws_bridge.py"], ROOT, "bridge", "bridge.log")
        time.sleep(1.0)
        node = shutil.which("node")
        if node:
            start([node, "node_modules/vite/bin/vite.js", "--host",
                   "--port", "5173"], WEB, "web", os.path.join(WEB, "vite.log"))
            print(f" 网页端：http://{ip}:5173   （Vite 开发服务器）")
        else:
            print(" 未检测到 node：可改用生产模式  python start_lan.py --prod")

    print("=" * 58)
    print(" 提示：放行防火墙端口；按 Ctrl+C 停止全部服务")
    try:
        while True:
            time.sleep(1)
            if all(p.poll() is not None for _n, p, _f in procs):
                print("所有服务已退出")
                break
    except KeyboardInterrupt:
        print("\n正在停止全部服务…")
    finally:
        for _n, p, _f in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for _n, p, f in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            try:
                f.close()
            except Exception:
                pass
        print("已全部停止")


if __name__ == "__main__":
    main()
