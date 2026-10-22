"""UDP 自动发现测试：应答器 + 探测器（同机回环，无需真实多机）。

运行：python test_discover.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))
                )
import discover  # noqa: E402


def main():
    port = 9119  # 用独立端口，避免与线上服务器的应答器冲突

    # 1) 起应答器，探测器应能拿到 tcp_port / ws_port
    discover.run_responder_thread(9999, name="TestSrv", ws_port=9106, port=port)
    time.sleep(0.3)
    res = discover.discover_sync(1.5, port=port)
    hit = [r for r in res if r["ip"] == "127.0.0.1"]
    assert hit, f"应发现本机应答器: {res}"
    assert hit[0]["tcp_port"] == 9999, hit[0]
    assert hit[0].get("ws_port") == 9106, hit[0]
    assert hit[0].get("name") == "TestSrv", hit[0]
    print("PASS 同机发现（IP/tcp_port/ws_port/name 正确）")

    # 2) 没有应答器的端口 → 返回空
    res2 = discover.discover_sync(0.8, port=port + 1)
    assert res2 == [], f"无服务器应返回空: {res2}"
    print("PASS 无服务器时返回空")

    # 3) 端口被占用时启动应答器不应崩溃（只是打印告警）
    discover.run_responder_thread(1, port=port)  # 已在监听，绑定失败→告警
    time.sleep(0.2)
    print("PASS 端口占用时应答器启动不崩溃")

    print("\nOK discover 全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("\nFAIL:", e)
        sys.exit(1)
