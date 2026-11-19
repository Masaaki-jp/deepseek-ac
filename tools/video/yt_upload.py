#!/usr/bin/env python3
"""MP4をYouTubeにアップロードする（OAuth2）。"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CLIENT_SECRET = PROJECT_ROOT / "client_secret.json"
DEFAULT_TOKEN = PROJECT_ROOT / "token.json"


def get_authenticated_service(client_secret: Path, token_path: Path):
    """保存済みのトークンを使う。期限切れならリフレッシュトークンで更新し、
    それもできないとき（取り消し・期限切れ）だけブラウザで認証する。"""
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            print("[info] 保存済みの認証を更新しました（ブラウザ認証は不要）")
        except RefreshError as e:
            print(f"[warn] 保存済みの認証を更新できませんでした。ブラウザで認証し直します: {e}", file=sys.stderr)
            creds = None

    if not creds or not creds.valid:
        if not client_secret.exists():
            print(f"[error] client_secret.json が見つかりません: {client_secret}", file=sys.stderr)
            print("  Google Cloud Console で OAuth2 クライアント（デスクトップアプリ）を作成し、", file=sys.stderr)
            print("  JSON をダウンロードして配置してください。", file=sys.stderr)
            sys.exit(1)
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
        creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        print(f"[info] 認証情報を保存: {token_path}")
    return build("youtube", "v3", credentials=creds)


# ------------------------------------------
# 記事への反映（動画リンクの挿入と、draft の解除）
# ------------------------------------------
_FRONT_MATTER = re.compile(r"\A(---[ \t]*\n.*?\n---[ \t]*\n)", re.DOTALL)  # 後ろの空行は含めない
_VIDEO_LINK = re.compile(
    r"\[!\[[^\]]*\]\(https://img\.youtube\.com/vi/([\w-]+)/0\.jpg\)\]\(https://www\.youtube\.com/watch\?v=[\w-]+\)")


def video_link(video_id: str) -> str:
    return (f"[![動画を見る](https://img.youtube.com/vi/{video_id}/0.jpg)]"
            f"(https://www.youtube.com/watch?v={video_id})")


def add_video_link(markdown: str, video_id: str) -> tuple[str, str]:
    """フロントマターの直後に動画リンクを入れる。戻り値は (新しい本文, 何をしたか)。"""
    found = _VIDEO_LINK.search(markdown)
    if found:
        if found.group(1) == video_id:
            return markdown, "同じ動画のリンクがあるので変更なし"
        return markdown.replace(found.group(0), video_link(video_id), 1), f"動画リンクを差し替え（{found.group(1)} → {video_id}）"
    m = _FRONT_MATTER.match(markdown)
    if not m:
        return markdown, "フロントマターが見つからないためリンクを入れませんでした"
    head, body = m.group(1), markdown[m.end():]
    return f"{head}\n{video_link(video_id)}\n\n{body.lstrip(chr(10))}", "動画リンクを挿入"


def publish(markdown: str) -> tuple[str, str]:
    """フロントマターの draft: true を false にする（本文は触らない）。"""
    m = _FRONT_MATTER.match(markdown)
    if not m:
        return markdown, "フロントマターが見つからないため draft を変更しませんでした"
    head = m.group(1)
    if re.search(r"^draft:\s*false\s*$", head, flags=re.MULTILINE):
        return markdown, "すでに draft: false"
    if re.search(r"^draft:\s*true\s*$", head, flags=re.MULTILINE):
        new_head = re.sub(r"^draft:\s*true\s*$", "draft: false", head, count=1, flags=re.MULTILINE)
    else:  # draft の行がない（既定値は true）ので足す
        new_head = re.sub(r"\n---[ \t]*\n\Z", "\ndraft: false\n---\n", head)
    return new_head + markdown[m.end():], "draft: false にしました"


def upload_video(youtube, mp4_path: Path, meta: dict) -> str:
    body = {
        "snippet": {
            "title": meta["title"][:100],
            "description": meta["description"][:5000],
            "tags": meta.get("tags", []),
            "categoryId": meta.get("category_id", "28"),
        },
        "status": {
            "privacyStatus": meta.get("privacy_status", "unlisted"),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(mp4_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    last_pct = -10
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            if pct - last_pct >= 10:
                print(f"  [upload] {pct}%")
                last_pct = pct
    return response["id"]


def main():
    parser = argparse.ArgumentParser(description="MP4をYouTubeにアップロードする")
    parser.add_argument("mp4", help="アップロードするMP4のパス")
    parser.add_argument("-m", "--meta", required=True, help="yt_meta.json のパス")
    parser.add_argument("--client-secret", default=str(DEFAULT_CLIENT_SECRET), help="OAuth2 client_secret.json")
    parser.add_argument("--token", default=str(DEFAULT_TOKEN), help="保存済みトークン（デフォルト: token.json）")
    parser.add_argument("--privacy", choices=["public", "unlisted", "private"], help="公開範囲を上書き")
    parser.add_argument("--dry-run", action="store_true", help="アップロードせず内容だけ表示")
    parser.add_argument("--publish", action="store_true", help="アップロード後、記事の draft を false にする")
    parser.add_argument("--no-link", action="store_true", help="記事に動画リンクを入れない")
    args = parser.parse_args()

    mp4_path = Path(args.mp4).resolve()
    if not mp4_path.exists():
        print(f"[error] MP4が見つかりません: {mp4_path}", file=sys.stderr)
        sys.exit(1)

    meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    if args.privacy:
        meta["privacy_status"] = args.privacy

    print(f"[info] mp4:   {mp4_path} ({mp4_path.stat().st_size // 1024 // 1024} MB)")
    print(f"[info] meta:  {args.meta}")
    print(f"[info] title: {meta['title']}")
    print(f"[info] privacy: {meta.get('privacy_status')}")

    if args.dry_run:
        print("[dry-run] アップロードをスキップしました")
        return

    youtube = get_authenticated_service(Path(args.client_secret), Path(args.token))

    try:
        video_id = upload_video(youtube, mp4_path, meta)
    except HttpError as e:
        print(f"[error] YouTube API エラー: {e}", file=sys.stderr)
        sys.exit(1)

    url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"[done] アップロード完了: {url}")

    # video.json を記事と同じ場所（content/articles/）に記録
    slug = mp4_path.parent.name
    article_file = PROJECT_ROOT / "content" / "articles" / f"{slug}.md"

    if article_file.exists():
        ir_path = mp4_path.parent / "deck_ir.json"
        ir_hash = hashlib.sha256(ir_path.read_bytes()).hexdigest() if ir_path.exists() else ""
        record = {
            "youtube_video_id": video_id,
            "youtube_url": url,
            "visibility": meta.get("privacy_status"),
            "deck_ir_sha256": ir_hash,
            "title": meta["title"],
        }
        out = PROJECT_ROOT / "content" / "articles" / f"{slug}.video.json"
        out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done] 記録: {out}")

        # 記事への反映
        text = article_file.read_text(encoding="utf-8")
        changes = []
        if not args.no_link:
            text, note = add_video_link(text, video_id)
            changes.append(note)
        if args.publish:
            text, note = publish(text)
            changes.append(note)
        article_file.write_text(text, encoding="utf-8")
        for note in changes:
            print(f"[done] 記事: {note}")
        rel = article_file.relative_to(PROJECT_ROOT)
        print("\n次の手順（確認してからコミット）:")
        print(f"  git diff {rel}")
        print(f"  git add {rel} {out.relative_to(PROJECT_ROOT)}")
        print(f'  git commit -m "docs: add video to {slug}" && git pull --rebase && git push')
    else:
        print(f"[warn] 記事ファイルが見つからないため video.json は作成しません: {article_file}")


if __name__ == "__main__":
    main()
