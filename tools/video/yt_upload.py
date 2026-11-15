#!/usr/bin/env python3
"""MP4をYouTubeにアップロードする（OAuth2）。"""
import argparse
import json
import sys
from pathlib import Path

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
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
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
    import hashlib
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
    else:
        print(f"[warn] 記事ファイルが見つからないため video.json は作成しません: {article_file}")


if __name__ == "__main__":
    main()
