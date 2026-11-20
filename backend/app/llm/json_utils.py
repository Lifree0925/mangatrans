"""LLM 输出 JSON 容错解析：模型偶发格式瑕疵不应中断长剧本翻译。"""

import json
import re


def parse_llm_json(content: str) -> dict | list:
    """剥代码围栏、提取完整 JSON、修复常见瑕疵（尾逗号/前后缀文本）。

    全部失败时抛 ValueError，调用方据此决定重试或报错。
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("模型返回空内容")
    # 1) Markdown 代码围栏
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    # 2) 直接解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # 3) 提取第一个完整 JSON 值（对象或数组）：括号配对扫描
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start < 0:
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
                        try:
                            return json.loads(fixed)
                        except json.JSONDecodeError:
                            break
                    break
    raise ValueError("模型输出不是有效 JSON")
