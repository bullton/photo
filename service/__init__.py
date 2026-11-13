"""PhotoPlus 分析服务（本地、离线）。

模块分工：
- preview   从 RAW 容器提取内嵌 JPEG
- metrics   清晰度 / 曝光 / 噪声 / 运动模糊
- scoring   分项分、综合分、判定、星级建议
- clip_tagger  CLIP 零样本标签（可选）
- xmp       XMP 侧车合并写
- library   SQLite 结果库
"""

from .pipeline import VERSION, analyze, status

__all__ = ["VERSION", "analyze", "status"]
