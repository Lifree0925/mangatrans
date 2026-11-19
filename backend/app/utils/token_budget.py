"""Token 粗估算：中文按字符计，英文按 4 字符 1 token，估算值不代替模型实际限制。"""


def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(1, other // 4)


def budget_trim(parts: list[tuple[str, str]], limit: int) -> tuple[list[str], list[str]]:
    """parts: [(名称, 文本)]，总 token 超 limit 时从尾部裁剪。
    返回 (保留文本列表, 被裁剪名称列表)。"""
    kept: list[str] = []
    dropped: list[str] = []
    total = 0
    for name, text in parts:
        t = estimate_tokens(text)
        if total + t <= limit:
            kept.append(text)
            total += t
        else:
            dropped.append(name)
    return kept, dropped
