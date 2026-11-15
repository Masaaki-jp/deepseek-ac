from pydantic import BaseModel, Field
from typing import Optional

class Diagram(BaseModel):
    """図解の定義。Phase 1 では未使用（None）だが、将来の拡張用に残す。"""
    type: str = Field(default="mermaid", description="図のフォーマット（mermaid, d2 等）")
    source: str = Field(description="図のソースコード")

class SlideIR(BaseModel):
    """1枚のスライドの意味的な構造。座標やデザインは含めない。"""
    layout: str = Field(default="title_and_content", description="title, title_and_content など")
    title: str = Field(description="スライドのタイトル")
    bullets: list[str] = Field(default_factory=list, description="箇条書きのリスト")
    narration: list[str] = Field(default_factory=list, description="音声合成用の原稿（文の配列）")
    notes: str = Field(default="", description="発表者ノート")
    diagram: Optional[Diagram] = Field(default=None, description="図解データ（Phase 1ではNone）")

class DeckIR(BaseModel):
    """記事から抽出されたスライドデッキ全体の構造。"""
    meta: dict = Field(default_factory=dict, description="生成日時、モデル名、実測値などのメタデータ")
    slides: list[SlideIR] = Field(description="スライドのリスト")
