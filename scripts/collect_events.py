"""
まちの冒険手帳 - イベント自動収集スクリプト (v2)

方針変更:
Google Custom Search JSON API は新規プロジェクトでは利用できない制限が
かかっていることが判明したため、検索は行わず、子供向けイベントを豊富に
掲載している「いこーよ」サイトの千葉県・茨城県の一覧ページを直接取得する
方式に変更しています。

やっていること:
1. 対象ページ(いこーよの千葉県・茨城県イベント一覧、複数ページ分)を取得
2. Gemini APIに「このページに載っている子供向けイベントを全部JSON配列で
   抜き出して」と依頼
3. 新しいイベントだけをFirestoreの `events` コレクションに保存
4. 一度取り込んだページは `seen_pages` コレクションに記録し、
   毎回同じページを全部読み直さないようにする(古い情報で上書きしないよう
   一定期間ごとに再取得もする)

必要な環境変数(GitHub Actionsのsecretsから渡されます):
- GEMINI_API_KEY
- FIREBASE_SERVICE_ACCOUNT_KEY (サービスアカウントJSONの中身をそのまま文字列で)

(GOOGLE_SEARCH_API_KEY / GOOGLE_SEARCH_ENGINE_ID は現在使用していませんが、
 将来また検索を使う場合に備えてワークフロー側のSecretsはそのまま残してOKです)
"""

import os
import re
import json
import time
import hashlib
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

import requests
import firebase_admin
from firebase_admin import credentials, firestore

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# 取得対象ページ。複数の実在サイトの「イベント一覧ページ」を直接読み込みます。
# 無料枠のレート制限に収まるよう、件数は絞ってあります。
TARGET_PAGES = []

# いこーよ(子供向けイベント専門サイト)千葉県(12)・茨城県(8)、それぞれ最初の3ページ
for page in range(1, 4):
    TARGET_PAGES.append(
        (f"https://iko-yo.net/events?prefecture_ids%5B%5D=12&page={page}", "千葉県")
    )
    TARGET_PAGES.append(
        (f"https://iko-yo.net/events?prefecture_ids%5B%5D=8&page={page}", "茨城県")
    )

# ウォーカープラス(地域イベント情報)
TARGET_PAGES.append(("https://www.walkerplus.com/event_list/ar0312/", "千葉県"))
TARGET_PAGES.append(("https://www.walkerplus.com/event_list/ar0308/", "茨城県"))

# 千葉県公式観光サイト「ちば観光ナビ」/ 茨城県公式観光サイト「観光いばらき」
TARGET_PAGES.append(("https://maruchiba.jp/event/index.html", "千葉県"))
TARGET_PAGES.append(("https://www.ibarakiguide.jp/event.php", "茨城県"))

# 同じページを再取得するまでの間隔(時間)。あまり短いと無駄打ちになるため。
RECHECK_INTERVAL_HOURS = 20

VALID_CATS = {"free", "nature", "indoor", "outdoor", "workshop"}
VALID_AGES = {"preschool", "elementary"}


def get_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"環境変数 {name} が設定されていません")
    return value


def init_firestore():
    key_json = get_env("FIREBASE_SERVICE_ACCOUNT_KEY")
    key_dict = json.loads(key_json)
    cred = credentials.Certificate(key_dict)
    firebase_admin.initialize_app(cred)
    return firestore.client()


class _TextExtractor(HTMLParser):
    """HTMLからタグを取り除いてテキストだけ抜き出す簡易パーサー"""

    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = False

    def handle_data(self, data):
        if not self.skip:
            stripped = data.strip()
            if stripped:
                self.text_parts.append(stripped)


def fetch_page_text(url, max_chars=12000):
    try:
        resp = requests.get(
            url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (compatible; MachiBoukenBot/1.0)"}
        )
        if not resp.ok:
            print(f"    HTTPステータス: {resp.status_code}")
            print(f"    レスポンス冒頭: {resp.text[:300]}")
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding
    except Exception as e:
        print(f"    ページ取得失敗: {e}")
        return None

    parser = _TextExtractor()
    try:
        parser.feed(resp.text)
    except Exception:
        return None
    text = "\n".join(parser.text_parts)
    return text[:max_chars] if text else None


def build_prompt(url, pref, page_text):
    return f"""あなたは、日本の{pref}における未就学児〜小学生向けの
おでかけイベント情報を抽出するアシスタントです。

以下は、子供向けイベントの一覧ページから抽出したテキストです。このページに
載っている個別のイベントを、できるだけ多く見つけて、次のJSON配列の形式で
出力してください。説明文やコードブロックの記号(```)は付けず、
JSON配列だけを出力してください。1件も見つからない場合は空配列 [] を
出力してください。

[
  {{
    "title": "イベント名(20文字程度)",
    "pref": "{pref}",
    "place": "開催場所(市区町村名+施設名など。ページから読み取れる範囲でよい)",
    "date": "YYYY-MM-DD形式の開催日(範囲がある場合は開始日。西暦に変換すること)",
    "cat": ["free","nature","indoor","outdoor","workshop"の中から当てはまるものを1〜3個],
    "age": ["preschool","elementary"の中から当てはまるものを1〜2個],
    "desc": "40文字程度のやさしい説明文"
  }},
  ...
]

以下のイベントは含めないでください:
- 開催日が具体的に読み取れないもの
- 明らかに大人向け・{pref}以外の情報

ページURL: {url}

ページのテキスト:
---
{page_text}
---
"""


def extract_events_with_gemini(url, pref, page_text, api_key, max_retries=1):
    body = {
        "contents": [{"parts": [{"text": build_prompt(url, pref, page_text)}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4000},
    }

    text = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(GEMINI_URL, params={"key": api_key}, json=body, timeout=60)
            if resp.status_code == 429:
                print(f"    レート制限(429)。しばらく待って再試行します。")
                print(f"    詳細: {resp.text[:400]}")
                time.sleep(30)
                continue
            if not resp.ok:
                print(f"    Geminiエラー本文: {resp.text[:500]}")
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            break
        except Exception as e:
            print(f"    Gemini呼び出し失敗: {e}")
            return []

    if text is None:
        print("    再試行しても失敗したため、このページはスキップします")
        return []

    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"```$", "", text).strip()

    try:
        events = json.loads(text)
    except json.JSONDecodeError:
        print(f"    JSON解析失敗。先頭200文字: {text[:200]}")
        return []

    if not isinstance(events, list):
        return []

    valid_events = []
    for event in events:
        if not isinstance(event, dict):
            continue
        required = ["title", "pref", "place", "date", "cat", "age", "desc"]
        if not all(k in event for k in required):
            continue
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(event.get("date", ""))):
            continue
        event["cat"] = [c for c in event.get("cat", []) if c in VALID_CATS] or ["indoor"]
        event["age"] = [a for a in event.get("age", []) if a in VALID_AGES] or [
            "preschool",
            "elementary",
        ]
        valid_events.append(event)

    return valid_events


def main():
    gemini_key = get_env("GEMINI_API_KEY")

    db = init_firestore()
    events_ref = db.collection("events")
    seen_ref = db.collection("seen_pages")

    now = datetime.now(timezone.utc)
    total_new = 0

    for url, pref in TARGET_PAGES:
        page_hash = hashlib.sha256(url.encode()).hexdigest()[:24]
        seen_doc = seen_ref.document(page_hash).get()
        if seen_doc.exists:
            checked_at_str = seen_doc.to_dict().get("checked_at")
            try:
                checked_at = datetime.fromisoformat(checked_at_str)
                if now - checked_at < timedelta(hours=RECHECK_INTERVAL_HOURS):
                    print(f"スキップ(前回取得から{RECHECK_INTERVAL_HOURS}時間未満): {url}")
                    continue
            except Exception:
                pass

        print(f"\n取得中: {url}")
        page_text = fetch_page_text(url)

        seen_ref.document(page_hash).set({"url": url, "checked_at": now.isoformat()})

        if not page_text or len(page_text) < 200:
            print("  本文が短すぎるためスキップ")
            continue

        events = extract_events_with_gemini(url, pref, page_text, gemini_key)
        time.sleep(13)  # 無料枠は1分間に5回までのため、間隔を空ける
        print(f"  {len(events)}件のイベントを抽出")

        for event in events:
            event["source_url"] = url
            event["collected_at"] = now.isoformat()
            event_id = hashlib.sha256(
                f"{event['title']}_{event['date']}".encode()
            ).hexdigest()[:24]
            events_ref.document(event_id).set(event, merge=True)
            total_new += 1
            print(f"  ✔ 追加/更新: {event['title']} ({event['date']} / {event['pref']})")

    print(f"\n完了。今回追加・更新したイベント数(のべ): {total_new}")


if __name__ == "__main__":
    main()
