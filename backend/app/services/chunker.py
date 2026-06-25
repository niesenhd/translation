"""段落分片器：按字符上限把段落聚合成片，保证不打破段落。

设计原则：
- 单分片字符数 <= TRANSLATION_CHUNK_MAX_CHARS（默认 6000）
- 末分片若 < TRANSLATION_CHUNK_MIN_CHARS（默认 1500）则与前一片合并，避免碎片
- 触发条件由调用方判断：总字符 > TRANSLATION_LARGE_FILE_THRESHOLD 才分片，
  否则直接段落级并发即可

注意：分片只是"调度组织"概念，并不会把多段合并成一次 API 请求；
每段仍然单独调用，因此段落对应关系永远不会丢失。
"""
from __future__ import annotations


def chunk_paragraphs(
    paragraphs: list[str],
    max_chars: int = 6000,
    min_tail_chars: int = 1500,
) -> list[list[int]]:
    """把段落按字符上限聚合成分片，返回每个分片包含的段落索引。

    返回值示例：[[0,1,2], [3,4,5,6], [7,8]]
    表示分 3 片，第 1 片含原段落索引 0/1/2 …

    单段超过 max_chars 时单独成片（避免无限放大），由翻译层自行处理超长段。
    """
    chunks: list[list[int]] = []
    current: list[int] = []
    current_size = 0

    for idx, p in enumerate(paragraphs):
        size = len(p)
        if size == 0:
            current.append(idx)  # 保留空段，不计入字符
            continue
        if size > max_chars and not current:
            # 单段就超过上限，独占一片
            chunks.append([idx])
            current = []
            current_size = 0
            continue
        if current_size + size > max_chars and current:
            chunks.append(current)
            current = [idx]
            current_size = size
        else:
            current.append(idx)
            current_size += size

    if current:
        chunks.append(current)

    # 末片合并：若末片字符数小于阈值且前面还有片，则并入前一片
    if len(chunks) >= 2:
        last = chunks[-1]
        last_size = sum(len(paragraphs[i]) for i in last)
        if last_size < min_tail_chars:
            chunks[-2].extend(last)
            chunks.pop()

    return chunks


def total_chars(paragraphs: list[str]) -> int:
    return sum(len(p) for p in paragraphs)
