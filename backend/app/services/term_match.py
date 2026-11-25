"""术语匹配与一致性策略。

核心原则：
- 只有专有名词/头衔/具名实体进入“精确强制”层；
- 普通名词、职业、物种、通用物件只作为情境参考，不做逐字强制；
- 同一 source 词形命中多个实体时视为歧义，禁止任意覆盖；
- 多个词形重叠时最长匹配优先，避免「荒原边缘酒吧」里的「酒吧」误命中其他地点。
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

EXACT = "exact"
LEXICAL = "lexical"
CONTEXTUAL = "contextual"
_POLICIES = {EXACT, LEXICAL, CONTEXTUAL}

# 泛词/普通名词：不能独立作为“必须逐字使用某译名”的实体。
# 这些词仍可在上下文中作为参考术语出现，但不会触发 QA 强制替换。
GENERIC_SOURCE_TERMS = {
    # 场所/建筑
    "庄园", "大厅", "宴会厅", "主厅", "祭坛", "高台", "高台祭坛", "前院", "后院",
    "大门", "二楼", "地牢", "墓园", "墓地", "修道院", "树林", "森林", "酒吧",
    "房间", "密室", "地下室", "地下密室", "山洞", "洞穴", "走廊", "楼梯", "广场",
    "荒原", "城堡", "古堡", "阳台", "后巷", "庭院", "医院", "办公室", "教堂",
    "祈祷室", "实验室", "仓库", "车厢", "池塘", "花园", "卧室", "书房", "厨房",
    "训练场", "会议室", "会客厅", "餐厅", "酒窖", "地下俱乐部", "大堂", "门口",
    "石阶", "祭台", "乱石堆", "乱石坡", "山路", "公路", "屋顶", "窗边", "床边",
    # 概念/物件/群体
    "血脉", "银器", "银剑", "长剑", "剑", "手铐", "匕首", "匕首", "徽章", "通讯器",
    "吊坠", "契约", "标记", "满月", "月亮", "狼群", "狼人", "吸血鬼", "贵族",
    "卫兵", "护卫", "守卫", "仆人", "侍女", "医生", "刺客", "信使", "战士",
    "长老", "头目", "老大", "管家", "众人", "村民", "村民们", "酒客", "酒客众",
    "围观者", "黑衣人", "黑衣人众", "哨兵", "侍卫", "侍卫们", "守卫们",
    "人类", "女人", "男人", "女孩", "男孩", "老者", "老头", "孩子", "孩童",
    # 泛称组合/场景修饰（模型常把普通描述拼成“专名”）
    "吸血鬼贵族", "狼人贵族", "贵族们", "地下酒吧", "二楼走廊", "二楼阳台",
    "前庭广场", "家族庄园", "密林", "地牢方向", "古堡祭坛", "吸血鬼古堡祭坛",
    "后山树林", "山间密林", "废弃地牢", "庄园大门", "家族墓园外小路",
    "客房外走廊", "二楼露天阳台", "庄园走廊", "城堡走廊", "地下走廊", "石头走廊",
    "主楼", "庄园主楼", "楼内", "屋内", "门外", "身后", "身旁",
    # 通用经营场所/视觉描述：模型常把普通场景词升级成具名地点或道具。
    "洗车行", "老旧洗车行", "破洗车行", "蓝色药丸", "药丸", "药瓶",
}

# 反复出现、需要固定英文表达的头衔/世界设定词。
# 没有显式 term_kind 的旧知识条目用此表兜底。
EXACT_TITLE_TERMS = {
    "狼王", "狼后", "女狼王", "阿尔法", "月亮女神", "命定伴侣", "永恒标记",
    "猎魔人", "血族", "猎魔人核心血脉", "核心血脉", "狼王之血", "永恒标记之吻",
    "Van Helsing", "芬里尔", "Fenrir", "Lucian", "Elara", "Talia",
}

PLACE_SUFFIXES = (
    "庄园", "城堡", "古堡", "别墅", "酒吧", "修道院", "教堂", "墓园", "墓地",
    "森林", "荒林", "树林", "山洞", "洞穴", "密室", "地下室", "地牢", "大厅",
    "宴会厅", "主厅", "祭坛", "祭台", "前院", "后院", "大门", "阳台", "后巷",
    "医院", "办公室", "房间", "走廊", "楼梯", "广场", "营地", "领地", "俱乐部",
    "家族", "家族领地", "部落", "部族", "族", "组织", "教团", "议会", "帮派",
    "阁", "殿", "楼", "堂", "院", "府", "宫", "塔", "寺", "庙",
)

_IRREGULAR_PLURALS = {
    "wolf": "wolves", "werewolf": "werewolves", "man": "men", "woman": "women",
    "child": "children", "person": "people", "mouse": "mice", "tooth": "teeth",
}


def normalize(s: str) -> str:
    """NFKC 归一化 + casefold，用于匹配前的文本规整。"""
    return unicodedata.normalize("NFKC", s or "").casefold()


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


_GENERIC_NORMALIZED = {normalize(x) for x in GENERIC_SOURCE_TERMS}


_ADDRESS_SUFFIXES = ("先生", "夫人", "小姐", "女士", "太太", "少爷", "老板")


def is_address_form(term: str) -> bool:
    """人物称谓（Richard先生/ Helen夫人）只能按语境翻译，不能作为精确术语。"""
    t = (term or "").strip()
    if not t or len(t) > 24:
        return False
    return any(t.endswith(suffix) and len(t) > len(suffix) for suffix in _ADDRESS_SUFFIXES)


def is_generic_source(term: str) -> bool:
    """是否为不能独立强制译名的泛词。"""
    t = (term or "").strip()
    if not t:
        return True
    if len(t) <= 1:
        return True
    return normalize(t) in _GENERIC_NORMALIZED


def _is_place_like(canonical: str, kind: str) -> bool:
    if kind in ("place", "location"):
        return True
    return any(canonical.endswith(s) for s in PLACE_SUFFIXES)


def classify_term(canonical: str, data: dict | None = None) -> str:
    """推断术语类型；优先使用模型/人工写入的 term_kind。"""
    data = data or {}
    kind = str(data.get("term_kind") or "").strip().lower()
    if kind:
        return kind
    if canonical in EXACT_TITLE_TERMS:
        return "title"
    if is_generic_source(canonical):
        return "common_noun"
    if _is_place_like(canonical, ""):
        return "place"
    if re.search(r"[A-Za-z]", canonical):
        return "proper_name"
    return "term"


def enforcement_for(canonical: str, data: dict | None = None) -> str:
    """返回术语执行策略：exact 精确强制 / lexical 词形参考 / contextual 情境参考。"""
    data = data or {}
    # 泛词优先级最高：即使模型误标成 place/title，也不能获得逐字强制权。
    if is_generic_source(canonical) and canonical not in EXACT_TITLE_TERMS:
        return CONTEXTUAL
    declared = str(data.get("enforcement") or "").strip().lower()
    if declared in _POLICIES:
        return declared
    kind = classify_term(canonical, data)
    if kind in ("proper_name", "proper_noun", "title", "place", "location", "item"):
        return EXACT
    if kind in ("common_noun", "species", "group", "concept"):
        return CONTEXTUAL
    if is_generic_source(canonical):
        return CONTEXTUAL
    # 旧数据没有 term_kind：只有明显的专名/地点才逐字强制，其余降为情境参考。
    # 新抽取会显式写 term_kind/enforcement，不会走这个保守兜底。
    return CONTEXTUAL


def _auto_variants(standard_en: str, declared: list[str] | None = None) -> list[str]:
    """补足常见合法词形，避免 werewolves/plural 被误判未命中。"""
    out = [v.strip() for v in (declared or []) if isinstance(v, str) and v.strip()]
    en = (standard_en or "").strip()
    if not en or _has_cjk(en):
        return list(dict.fromkeys(out))

    words = en.split()
    last = words[-1]
    lower = last.lower()
    plural_last = _IRREGULAR_PLURALS.get(lower)
    if not plural_last:
        if lower.endswith(("s", "x", "z", "ch", "sh")):
            plural_last = last + "es"
        elif len(lower) > 1 and lower.endswith("y") and lower[-2] not in "aeiou":
            plural_last = last[:-1] + "ies"
        elif lower.endswith("f"):
            plural_last = last[:-1] + "ves"
        elif lower.endswith("fe"):
            plural_last = last[:-2] + "ves"
        else:
            plural_last = last + "s"
    plural_phrase = " ".join([*words[:-1], plural_last])
    out.extend([plural_phrase, en + "'s"])
    # 家族/部族名称允许省略类别词（Van Helsing Family ↔ Van Helsing），
    # 但不能把 Blood Clan 这种由普通词组成的名称缩成 Blood。
    for suffix in (" Family", " Tribe", " Clan"):
        if en.endswith(suffix):
            core = en[: -len(suffix)].strip()
            if core and core.casefold() not in {"blood", "pure", "royal"}:
                out.append(core)
    return list(dict.fromkeys(x for x in out if x and x.casefold() != en.casefold()))


_PATTERN_CACHE: dict[tuple[str, bool], re.Pattern] = {}


def _word_pattern(word: str, ignore_case: bool = True) -> re.Pattern:
    """英文词边界匹配：前后不能是字母/数字。"""
    key = (word, ignore_case)
    pat = _PATTERN_CACHE.get(key)
    if pat is None:
        prefix = r"(?<![A-Za-z0-9])"
        suffix = r"(?![A-Za-z0-9])"
        flags = re.IGNORECASE if ignore_case else 0
        pat = re.compile(prefix + re.escape(word) + suffix, flags)
        _PATTERN_CACHE[key] = pat
    return pat


def target_hits_term(
    target_text: str, standard_en: str, variants: list[str] | None = None
) -> str:
    """译文是否命中术语（标准译名或任一合法变体）。命中返回声明形态，未命中返回空串。"""
    for candidate in [standard_en, *_auto_variants(standard_en, variants)]:
        candidate = (candidate or "").strip()
        if not candidate:
            continue
        if _has_cjk(candidate):
            if normalize(candidate) in normalize(target_text):
                return candidate
        elif _word_pattern(candidate).search(target_text):
            return candidate
    return ""


def case_drifted(target_text: str, standard_en: str, variants: list[str] | None = None) -> bool:
    """casefold 命中但精确大小写不一致（如 wolf king vs Wolf King）→ True。"""
    if not target_hits_term(target_text, standard_en, variants):
        return False
    return not bool(_word_pattern(standard_en, ignore_case=False).search(target_text))


@dataclass
class TermRecord:
    id: str
    canonical: str
    aliases: list[str]
    en: str
    variants: list[str]
    kind: str
    policy: str
    source_forms: list[str] = field(default_factory=list)
    forced_forms: list[str] = field(default_factory=list)
    merged_into: str | None = None


def _place_alias_compatible(canonical: str, alias: str) -> bool:
    """地点别名必须指向同类地点，避免“家族”被当作“庄园”的别名。"""
    c_suffix = next((s for s in PLACE_SUFFIXES if canonical.endswith(s)), "")
    a_suffix = next((s for s in PLACE_SUFFIXES if alias.endswith(s)), "")
    if not c_suffix and not a_suffix:
        return True
    if not c_suffix or not a_suffix:
        return False
    return a_suffix == c_suffix or canonical.endswith(alias) or alias.endswith(canonical)


def _clean_aliases(canonical: str, aliases: list[str] | None) -> list[str]:
    result: list[str] = []
    for raw in aliases or []:
        alias = str(raw or "").strip()
        if not alias or normalize(alias) == normalize(canonical):
            continue
        if is_generic_source(alias) or is_address_form(alias):
            continue
        if not _place_alias_compatible(canonical, alias):
            continue
        if alias not in result:
            result.append(alias)
    return result


def _same_named_entity(short: TermRecord, long: TermRecord) -> bool:
    """保守合并“正式全名 + 同后缀简称”的同一地点。

    例：边缘酒吧 / 荒原边缘酒吧 -> 后者为规范名；Edge Bar / Wasteland Edge Bar
    也满足英文后缀关系。绝不由裸泛词（酒吧、庄园）反向合并具体地点。
    """
    if short.policy != EXACT or long.policy != EXACT:
        return False
    if not (_is_place_like(short.canonical, short.kind) and _is_place_like(long.canonical, long.kind)):
        return False
    if is_generic_source(short.canonical) or len(short.canonical) < 3:
        return False
    if not long.canonical.endswith(short.canonical):
        return False
    short_en = normalize(short.en)
    long_en = normalize(long.en)
    return bool(short_en and long_en and (short_en == long_en or long_en.endswith(short_en)))


def build_term_records(entries: list[dict]) -> list[TermRecord]:
    """把 DB/上下文知识条目规整为可匹配的术语记录。

    entries: [{id, item_type, canonical_key, aliases, data}]
    """
    records: list[TermRecord] = []
    for item in entries:
        if item.get("item_type") != "term":
            continue
        canonical = str(item.get("canonical_key") or "").strip()
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        en = str(data.get("translation") or "").strip()
        if not canonical or is_address_form(canonical):
            continue
        kind = classify_term(canonical, data)
        policy = enforcement_for(canonical, data)
        aliases = _clean_aliases(canonical, item.get("aliases") or [])
        if not en:
            policy = CONTEXTUAL
        source_forms = [canonical, *aliases]
        # 地点/物件的别名是同一实体的名字，必须统一；
        # 人物头衔/称谓的别名按剧情语境翻译，不能一律换成正式名。
        forced_forms = (
            source_forms if kind in ("place", "location", "item")
            else [canonical]
        )
        records.append(
            TermRecord(
                id=str(item.get("id") or canonical),
                canonical=canonical,
                aliases=aliases,
                en=en,
                variants=_auto_variants(en, data.get("variants") or []),
                kind=kind,
                policy=policy,
                source_forms=source_forms,
                forced_forms=forced_forms,
            )
        )

    # 子地点沿用地点的正式英文前缀，修复 legacy 数据里的拼写漂移
    # （如 Valentine Castle / Valentin Castle Dungeon）。
    exact_records = [r for r in records if r.policy == EXACT and r.en]
    for child in exact_records:
        for parent in exact_records:
            if child is parent or not child.canonical.startswith(parent.canonical):
                continue
            if len(child.canonical) <= len(parent.canonical):
                continue
            parent_words = parent.en.split()
            child_words = child.en.split()
            if len(parent_words) > len(child_words):
                continue
            prefix = child_words[:len(parent_words)]
            similar = all(
                difflib.SequenceMatcher(None, a.casefold(), b.casefold()).ratio() >= 0.86
                for a, b in zip(prefix, parent_words)
            )
            if similar:
                child.en = " ".join([*parent_words, *child_words[len(parent_words):]])
                child.variants = _auto_variants(child.en)

    # 同一地点的规范全名与简称合并：运行时统一用规范名，但数据库条目保留可追溯。
    exact = [r for r in records if r.policy == EXACT]
    for short in exact:
        for long in exact:
            if short is long or short.merged_into:
                continue
            if _same_named_entity(short, long):
                short.merged_into = long.id
                for form in [short.canonical, *short.aliases]:
                    if form not in long.source_forms:
                        long.source_forms.append(form)
                    if form not in long.forced_forms:
                        long.forced_forms.append(form)
                break
    return records


def _occurrences(text: str, form: str) -> list[tuple[int, int]]:
    normalized_text = normalize(text)
    normalized_form = normalize(form)
    if not normalized_form:
        return []
    if _has_cjk(form):
        out: list[tuple[int, int]] = []
        start = 0
        while True:
            idx = normalized_text.find(normalized_form, start)
            if idx < 0:
                break
            out.append((idx, idx + len(normalized_form)))
            start = idx + max(1, len(normalized_form))
        return out
    return [(m.start(), m.end()) for m in _word_pattern(normalized_form).finditer(normalized_text)]


def select_source_matches(records: list[TermRecord], text: str) -> list[dict]:
    """从文本中选出应强制/参考的术语命中。

    - 最长词形优先，短词不会覆盖长专名；
    - 同一词形映射多个实体时按歧义丢弃；
    - 规范化全名与简称合并后的简称仍可命中，但统一使用规范名。
    """
    active = {r.id: r for r in records if not r.merged_into}
    candidates: list[dict] = []
    for record in active.values():
        for form in record.source_forms:
            for start, end in _occurrences(text, form):
                candidates.append({
                    "start": start, "end": end, "form": form,
                    "norm_form": normalize(form), "record": record,
                })
    if not candidates:
        return []

    # 同一位置的词形冲突：规范 canonical 优先，其次同译名可合并，否则歧义丢弃。
    by_span: dict[tuple[int, int], list[dict]] = {}
    for cand in candidates:
        by_span.setdefault((cand["start"], cand["end"]), []).append(cand)
    span_choice: list[dict] = []
    for group in by_span.values():
        records_here = {c["record"].id: c["record"] for c in group}
        if len(records_here) == 1:
            span_choice.append(group[0])
            continue
        canonical_hits = [c for c in group if normalize(c["record"].canonical) == c["norm_form"]]
        if len(canonical_hits) == 1:
            span_choice.append(canonical_hits[0])
            continue
        ens = {normalize(c["record"].en) for c in group if c["record"].en}
        if len(ens) == 1 and ens:
            span_choice.append(group[0])
        # 其余视为歧义：不进入强制表

    # 最长词形优先；同长度按出现位置稳定排序，确保长专名不被内部短词覆盖。
    span_choice.sort(key=lambda c: (-(c["end"] - c["start"]), c["start"]))
    chosen: list[dict] = []
    occupied: list[tuple[int, int]] = []
    seen_forms: set[str] = set()
    for cand in span_choice:
        s, e = cand["start"], cand["end"]
        if any(not (e <= a or s >= b) for a, b in occupied):
            continue
        occupied.append((s, e))
        key = cand["norm_form"]
        if key in seen_forms:
            continue
        seen_forms.add(key)
        rec: TermRecord = cand["record"]
        forced = any(normalize(cand["form"]) == normalize(f) for f in rec.forced_forms)
        chosen.append({
            "source_form": cand["form"],
            "canonical": rec.canonical,
            "en": rec.en,
            "variants": rec.variants,
            "policy": rec.policy,
            "kind": rec.kind,
            "force": forced,
            "record_id": rec.id,
        })
    chosen.sort(key=lambda x: normalize(x["source_form"]))
    return chosen


def select_forced_specs_for_text(
    specs: dict[str, dict], text: str,
) -> dict[str, dict]:
    """在指定段落内按最长词形选择强制术语，避免短词命中长专名内部。"""
    candidates: list[tuple[int, int, str]] = []
    for form in specs:
        for start, end in _occurrences(text, form):
            candidates.append((start, end, form))
    candidates.sort(key=lambda x: (-(x[1] - x[0]), x[0]))
    selected: list[tuple[int, int]] = []
    forms: set[str] = set()
    for start, end, form in candidates:
        if any(not (end <= a or start >= b) for a, b in selected):
            continue
        selected.append((start, end))
        forms.add(form)
    return {form: specs[form] for form in forms}


def forced_term_specs(records: list[TermRecord], text: str) -> dict[str, dict]:
    """返回需要精确执行的术语：{source_form: spec}。普通名词不会进入此表。"""
    matched = select_source_matches(records, text)
    return {
        m["source_form"]: m
        for m in matched
        if m["policy"] == EXACT and m.get("en") and m.get("force")
    }


def canonicalize_exact_terms(text: str, specs: dict[str, dict]) -> str:
    """把专名译文的合法大小写写法统一为知识库规范写法。

    只处理 exact 专名，不改普通名词、不改时态/单复数变体、不改变事实内容。
    """
    out = text or ""
    seen: set[str] = set()
    for spec in specs.values():
        en = str(spec.get("en") or "").strip()
        if not en or en in seen or _has_cjk(en):
            continue
        seen.add(en)
        out = _word_pattern(en).sub(en, out)
    return out


def build_speaker_map(entries: list[dict]) -> dict[str, str]:
    """角色中文名/别名 -> 英文正式名；冲突别名不参与锚点校验。"""
    canonical_map: dict[str, str] = {}
    alias_map: dict[str, set[str]] = {}
    for item in entries:
        if item.get("item_type") != "character":
            continue
        canonical = str(item.get("canonical_key") or "").strip()
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        en = str(data.get("translation") or canonical).strip()
        if not canonical or not en:
            continue
        canonical_map.setdefault(normalize(canonical), en)
        for alias in item.get("aliases") or []:
            alias = str(alias or "").strip()
            if not alias or len(alias) <= 1:
                continue
            if normalize(alias) in {"他", "她", "它", "我", "你", "你们", "我们", "他们", "她们"}:
                continue
            alias_map.setdefault(normalize(alias), set()).add(en)
    mapping = dict(canonical_map)
    for form, names in alias_map.items():
        if form in mapping:
            continue
        if len(names) == 1:
            mapping[form] = next(iter(names))
        # 多角色共享的别名（如“大人”“小姐”）不参与身份校验
    return mapping


def expected_speakers(source_speaker: str, speaker_map: dict[str, str]) -> set[str]:
    """把原文说话人（支持 A/B、A、B 等组合）解析为预期英文名集合。"""
    raw = (source_speaker or "").strip()
    if not raw:
        return set()
    parts = [p.strip() for p in re.split(r"[/、,&]|和", raw) if p.strip()]
    expected = {speaker_map[normalize(p)] for p in parts if normalize(p) in speaker_map}
    if not expected and normalize(raw) in speaker_map:
        expected.add(speaker_map[normalize(raw)])
    return expected
