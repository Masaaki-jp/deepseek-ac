"""DeepSeek first request. Default: offline. Use --send only after checking costs."""
import argparse
import getpass
import json
import os
import urllib.error
import urllib.request

ENDPOINT = "https://api.deepseek.com/chat/completions"
PROMPT = "次の文章を箇条書き3項目で要約し、原文にない条件は追加しないでください。\n架空の青空図書室は土曜日の午前10時から午後3時まで開室します。貸出は一人2冊までです。"


def payload():
    return {"model": "deepseek-flash", "messages": [{"role": "user", "content": PROMPT}],
            "thinking": {"type": "disabled"}, "max_tokens": 256, "stream": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Send one billable request")
    args = parser.parse_args(argv)
    body = payload()
    if not args.send:
        print("確認モード：通信しません。APIキーも読み込みません。")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0
    key = os.environ.get("DEEPSEEK_API_KEY") or getpass.getpass("APIキー（画面には表示されません）: ")
    if not key.strip():
        print("APIキーが空のため送信しません。")
        return 1
    request = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.load(response)
        choice = data["choices"][0]
        print(choice["message"]["content"])
        if choice.get("finish_reason") == "length":
            print("出力上限で途中終了しました。完全な要約として扱わないでください。")
        print("使用量:", json.dumps(data.get("usage", {}), ensure_ascii=False))
        return 0
    except urllib.error.HTTPError as error:
        print(f"HTTP {error.code}。キー・残高・入力条件を確認してください。自動再試行はしません。")
    except (urllib.error.URLError, TimeoutError):
        print("通信を確認できませんでした。課金状況を確認してから再試行を判断してください。")
    except (KeyError, IndexError, TypeError, ValueError):
        print("応答形式を確認できませんでした。レスポンスやキーを共有せず公式資料を確認してください。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
