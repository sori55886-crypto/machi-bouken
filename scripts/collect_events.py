"""
まちの冒険手帳 - イベント自動収集スクリプト

やっていること:
1. Google カスタム検索で、千葉県・茨城県の子供向けイベント情報を検索
2. 見つかったページを取得し、Gemini APIで「イベントかどうか」を判定・情報を整形
3. 新しいイベントだけをFirestoreの `events` コレクションに保存
4. 一度チェックしたURLは `seen_urls` コレクションに記録し、次回以降スキップ

必要な環境変数(GitHub Actionsのsecretsから渡されます):
- GEMINI_API_KEY
- GOOGLE_SEARCH_API_KEY
- GOOGLE_SEARCH_ENGINE_ID
- FIREBASE_SERVICE_ACCOUNT_KEY (サービスアカウントJSONの中身をそのまま文字列で)
"""

import os
import re
import json
import time
import hashlib
from datetime import datetime, timezone
from html.parser import HTMLParser

import requests
import firebase_admin
from firebase_admin import credentials, firestore

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# 検索クエリ。無料枠(1日100回)に収まるよう、数を絞ってあります。
SEARCH_QUERIES = [
    "千葉県 子供 イベント 無料",
    "千葉県 親子 イベント 自然体験",
    "千葉県 子育て イベント カレンダー",
    "茨城県 子供 イベント 無料",
    "茨城県 親子 イベント 自然体験",
    "茨城県 子育て イベント カレンダー",
]

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


def google_search(query, api_key, cx, num=10):
    url = "https://www.googleapis.com/customsearch/v1"
    params = {"key": api_key, "cx": cx, "q": query, "num": num, "lr": "lang_ja"}
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return [item["link"] for item in data.get("items", []) if "link" in item]


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


def fetch_page_text(url, max_chars=6000):
    try:
        resp = requests.get(
            url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (compatible; MachiBoukenBot/1.0)"}
        )
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


def build_prompt(url, page_text):
    return f"""あなたは、日本の千葉県・茨城県における未就学児〜小学生向けの
おでかけイベント情報を抽出するアシスタントです。

以下はウェブページから抽出したテキストです。このページに、具体的な開催日
(西暦・月・日が特定できるもの)が明記された、子供向け・親子向けのイベントや
お出かけスポットの情報が含まれているか判断してください。

含まれている場合は、次のJSON形式で「1件だけ」出力してください。
説明文やコードブロックの記号(```)は付けず、JSONオブジェクトのみを出力してください。

{{
  "title": "イベント名(20文字程度)",
  "pref": "千葉県 または 茨城県 のどちらか",
  "place": "開催場所(市区町村名+施設名など)",
  "date": "YYYY-MM-DD形式の開催日(西暦に変換すること)",
  "cat": ["free","nature","indoor","outdoor","workshop"の中から当てはまるものを1〜3個],
  "age": ["preschool","elementary"の中から当てはまるものを1〜2個],
  "desc": "40文字程度のやさしい説明文"
}}

以下のいずれかに当てはまる場合は、JSONではなく文字列 null だけを出力してください:
- 具体的な開催日が書かれていない(期間限定情報や常設施設の紹介のみなど)
- 千葉県・茨城県以外の情報である
- 未就学児〜小学生向けとは言えない内容である
- ページの内容がエラーページや広告、無関係な内容である

ページURL: {url}

ページのテキスト:
---
{page_text}
---
"""


def extract_event_with_gemini(url, page_text, api_key):
    body = {
        "contents": [{"parts": [{"text": build_prompt(url, page_text)}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 500},
    }
    try:
        resp = requests.post(GEMINI_URL, params={"key": api_key}, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"    Gemini呼び出し失敗: {e}")
        return None

    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"```$", "", text).strip()

    if text.lower().startswith("null"):
        return None

    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        return None

    required = ["title", "pref", "place", "date", "cat", "age", "desc"]
    if not isinstance(event, dict) or not all(k in event for k in required):
        return None
    if event.get("pref") not in ("千葉県", "茨城県"):
        return None
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(event.get("date", ""))):
        return None

    event["cat"] = [c for c in event.get("cat", []) if c in VALID_CATS] or ["indoor"]
    event["age"] = [a for a in event.get("age", []) if a in VALID_AGES] or ["preschool", "elementary"]

    return event


def main():
    gemini_key = get_env("GEMINI_API_KEY")
    search_key = get_env("GOOGLE_SEARCH_API_KEY")
    search_cx = get_env("GOOGLE_SEARCH_ENGINE_ID")

    db = init_firestore()
    events_ref = db.collection("events")
    seen_ref = db.collection("seen_urls")

    all_urls = []
    for q in SEARCH_QUERIES:
        print(f"検索中: {q}")
        try:
            urls = google_search(q, search_key, search_cx)
            print(f"  {len(urls)}件のURLを取得")
            all_urls.extend(urls)
        except Exception as e:
            print(f"  検索失敗: {e}")
        time.sleep(1)

    unique_urls = list(dict.fromkeys(all_urls))
    print(f"\n重複除去後のURL数: {len(unique_urls)}")

    new_count = 0
    for url in unique_urls:
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:24]
        seen_doc = seen_ref.document(url_hash).get()
        if seen_doc.exists:
            continue

        print(f"\n処理中: {url}")
        page_text = fetch_page_text(url)

        seen_ref.document(url_hash).set(
            {"url": url, "checked_at": datetime.now(timezone.utc).isoformat()}
        )

        if not page_text or len(page_text) < 100:
            print("  本文が短すぎるためスキップ")
            continue

        event = extract_event_with_gemini(url, page_text, gemini_key)
        time.sleep(1)

        if not event:
            print("  イベント情報なしと判定")
            continue

        event["source_url"] = url
        event["collected_at"] = datetime.now(timezone.utc).isoformat()
        event_id = hashlib.sha256(f"{event['title']}_{event['date']}".encode()).hexdigest()[:24]
        events_ref.document(event_id).set(event, merge=True)
        new_count += 1
        print(f"  ✔ 追加/更新: {event['title']} ({event['date']} / {event['pref']})")

    print(f"\n完了。今回追加・更新したイベント数: {new_count}")


if __name__ == "__main__":
    main()
