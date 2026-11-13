"""XMP 侧车写入：只改我们自己负责的字段，其他内容原样保留。

DxO PhotoLab / Lightroom 都会读同名 xmp，因此这里必须做“合并写”：
- 星级只改 xmp:Rating
- 关键词只增删带 AI/ 前缀的条目
- 其余节点（修图参数、IPTC、已有层级关键词）不动
"""

from __future__ import annotations

import os
import re
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

XMP_NS = "http://ns.adobe.com/xap/1.0/"
DC_NS = "http://purl.org/dc/elements/1.1/"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
X_NS = "adobe:ns:meta/"

ET.register_namespace("x", X_NS)
ET.register_namespace("rdf", RDF_NS)
ET.register_namespace("xmp", XMP_NS)
ET.register_namespace("dc", DC_NS)

AI_PREFIX = "AI/"


def sidecar_path(image_path: str | Path) -> Path:
    return Path(image_path).with_suffix(".xmp")


def _new_tree(rating: int, tags: list[str], label: str | None) -> ET.ElementTree:
    root = ET.Element(f"{{{X_NS}}}xmpmeta")
    rdf = ET.SubElement(root, f"{{{RDF_NS}}}RDF")
    desc = ET.SubElement(rdf, f"{{{RDF_NS}}}Description")
    desc.set(f"{{{RDF_NS}}}about", "")
    desc.set(f"{{{XMP_NS}}}Rating", str(rating))
    desc.set(f"{{{XMP_NS}}}MetadataDate", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if label:
        desc.set(f"{{{XMP_NS}}}Label", label)
    if tags:
        subject = ET.SubElement(desc, f"{{{DC_NS}}}subject")
        bag = ET.SubElement(subject, f"{{{RDF_NS}}}Bag")
        for tag in tags:
            ET.SubElement(bag, f"{{{RDF_NS}}}li").text = tag
    return ET.ElementTree(root)


def _find_description(root: ET.Element) -> ET.Element | None:
    for desc in root.iter(f"{{{RDF_NS}}}Description"):
        return desc
    return None


def _set_ai_tags(desc: ET.Element, tags: list[str]) -> None:
    subject = desc.find(f"{{{DC_NS}}}subject")
    if subject is None:
        subject = ET.SubElement(desc, f"{{{DC_NS}}}subject")
    bag = subject.find(f"{{{RDF_NS}}}Bag")
    if bag is None:
        subject.clear()
        bag = ET.SubElement(subject, f"{{{RDF_NS}}}Bag")

    kept = []
    for item in list(bag):
        text = (item.text or "").strip()
        if text.startswith(AI_PREFIX):
            bag.remove(item)
        else:
            kept.append(text)
    merged = kept + [tag for tag in tags if tag.startswith(AI_PREFIX)]
    for item in list(bag):
        bag.remove(item)
    for text in merged:
        ET.SubElement(bag, f"{{{RDF_NS}}}li").text = text
    if not merged:
        subject.remove(bag)


def write_sidecar(
    image_path: str | Path,
    rating: int,
    ai_tags: list[str] | None = None,
    label: str | None = None,
    backup: bool = True,
) -> dict:
    """写入 / 合并 xmp。返回 {status, path, rating, tags}。"""
    source = Path(image_path)
    if not source.is_file():
        return {"status": "missing", "path": str(source), "rating": rating, "tags": []}

    target = sidecar_path(source)
    rating = max(0, min(5, int(rating or 0)))
    tags = [tag.strip() for tag in (ai_tags or []) if str(tag).strip()]
    tags = [tag if tag.startswith(AI_PREFIX) else AI_PREFIX + tag for tag in tags]

    if target.exists():
        try:
            text = target.read_text("utf-8", errors="replace")
            # 真正的 xmp 文件末尾常带 <?xpacket end="w"?>，ElementTree 会当作
            # 根节点之后的垃圾内容，先剥掉。
            cleaned = re.sub(r"<\?xpacket\b.*?\?>", "", text, flags=re.DOTALL)
            tree = ET.ElementTree(ET.fromstring(cleaned.strip()))
        except (OSError, ET.ParseError) as error:
            return {"status": "failed", "path": str(target), "rating": rating, "error": str(error)}
        root = tree.getroot()
        desc = _find_description(root)
        if desc is None:
            return {"status": "failed", "path": str(target), "rating": rating, "error": "no rdf:Description"}
        desc.set(f"{{{XMP_NS}}}Rating", str(rating))
        if label is not None:
            desc.set(f"{{{XMP_NS}}}Label", label)
        if tags:
            _set_ai_tags(desc, tags)
        status = "updated"
    else:
        tree = _new_tree(rating, tags, label)
        status = "created"

    if backup and target.exists():
        try:
            shutil.copy2(target, target.with_suffix(".xmp.bak"))
        except OSError:
            pass

    tmp = target.with_suffix(".xmp.tmp")
    try:
        tree.write(tmp, encoding="utf-8", xml_declaration=True)
        os.replace(tmp, target)
    except OSError as error:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return {"status": "failed", "path": str(target), "rating": rating, "error": str(error)}

    return {
        "status": status,
        "path": str(target),
        "rating": rating,
        "label": label or "",
        "tags": tags,
    }
