"""
LINE への通知（LINE Messaging API の push メッセージ）。

必要な設定（GitHub の Settings → Secrets and variables → Actions → Secrets）
  LINE_CHANNEL_ACCESS_TOKEN  LINE Developers の Messaging API チャネルの「チャネルアクセストークン（長期）」
  LINE_USER_ID               同じチャネルの「あなたのユーザーID」（Basic settings の一番下、U から始まる33文字）
どちらかが無ければ送らずに画面に出すだけ（エラーにはしない）。

  python -m collector.notify_line "テスト"     # 試しに1通送る
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def enabled() -> bool:
    return bool(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("LINE_USER_ID"))


def send(text: str) -> bool:
    if not enabled():
        print("[LINE未設定のため送信せず]\n" + text, flush=True)
        return False
    body = {"to": os.environ["LINE_USER_ID"], "messages": [{"type": "text", "text": text[:4900]}]}
    req = urllib.request.Request("https://api.line.me/v2/bot/message/push", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + os.environ["LINE_CHANNEL_ACCESS_TOKEN"]})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = r.status == 200
    except Exception as e:  # noqa: BLE001  通知の失敗でオッズ記録を止めない
        print("LINE送信に失敗:", e, getattr(e, "read", lambda: b"")()[:300], flush=True)
        return False
    print("LINE送信:", text.splitlines()[0], flush=True)
    return ok


if __name__ == "__main__":
    print("送信" + ("成功" if send(" ".join(sys.argv[1:]) or "オートレースAIオフィスからのテスト通知です") else "せず"))
