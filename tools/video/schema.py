import re
from typing import Optional

from pydantic import BaseModel, Field, model_validator

# 対応している図の種類と、Mermaidソース先頭のキーワードの対応
DIAGRAM_KINDS = {
    "flowchart": ("flowchart", "graph"),
    "sequence": ("sequenceDiagram",),
    "state": ("stateDiagram-v2", "stateDiagram"),
    "timeline": ("timeline",),
}


def detect_diagram_kind(source: str) -> Optional[str]:
    """Mermaidソースの先頭行から図の種類を判定する。対応外なら None。"""
    for line in source.splitlines():
        line = line.strip()
        if not line or line.startswith("%%"):
            continue
        head = re.split(r"\s+", line, maxsplit=1)[0]
        for kind, keywords in DIAGRAM_KINDS.items():
            if head in keywords:
                return kind
        return None
    return None


class Diagram(BaseModel):
    """図解の定義。"""
    type: str = Field(default="mermaid", description="図のフォーマット（mermaid, d2 等）")
    kind: Optional[str] = Field(default=None, description="図の種類（flowchart / sequence / state / timeline）")
    reason: str = Field(default="", description="その種類を選んだ理由（確認用）")
    source: str = Field(description="図のソースコード")

    @model_validator(mode="after")
    def _sync_kind(self):
        # LLMが申告した kind より、実際のソースの先頭行を信用する
        detected = detect_diagram_kind(self.source)
        if detected:
            self.kind = detected
        return self


class SlideIR(BaseModel):
    """1枚のスライドの意味的な構造。座標やデザインは含めない。"""
    layout: str = Field(default="title_and_content", description="title, title_and_content など")
    title: str = Field(description="スライドのタイトル")
    bullets: list[str] = Field(default_factory=list, description="箇条書きのリスト")
    narration: list[str] = Field(default_factory=list, description="音声合成用の原稿（文の配列）")
    notes: str = Field(default="", description="発表者ノート")
    diagram: Optional[Diagram] = Field(default=None, description="図解データ（図がないスライドは None）")


class DeckIR(BaseModel):
    """記事から抽出されたスライドデッキ全体の構造。"""
    meta: dict = Field(default_factory=dict, description="生成日時、モデル名、実測値などのメタデータ")
    slides: list[SlideIR] = Field(description="スライドのリスト")
