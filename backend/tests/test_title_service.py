"""已有英文标题识别：标题原样复用，不二次翻译。"""

from types import SimpleNamespace

from app.services.title_service import detect_source_title, preserved_title_for_segment


def _seg(no: int, text: str, segment_type: str = "narrative", sid: str | None = None):
    return SimpleNamespace(
        id=sid or f"seg-{no}",
        segment_no=no,
        segment_type=segment_type,
        source_text=text,
    )


def test_detects_bilingual_script_title():
    segs = [
        _seg(1, "The Alpha's Rejected Mate\n被狼王抛弃的新娘"),
        _seg(2, "第一集 初遇", "heading"),
    ]
    info = detect_source_title(segs)
    assert info is not None
    assert info.english_title == "The Alpha's Rejected Mate"
    assert info.chinese_title == "被狼王抛弃的新娘"
    assert info.segment_ids == ("seg-1",)


def test_detects_inline_bilingual_title():
    info = detect_source_title([_seg(1, "The Alpha's Rejected Mate / 被狼王抛弃的新娘")])
    assert info is not None
    assert info.english_title == "The Alpha's Rejected Mate"
    assert info.chinese_title == "被狼王抛弃的新娘"


def test_chinese_only_title_is_not_treated_as_existing_english():
    assert detect_source_title([_seg(1, "被狼王抛弃的新娘")]) is None
    assert preserved_title_for_segment(_seg(1, "被狼王抛弃的新娘")) is None


def test_only_opening_short_segment_preserves_title():
    assert preserved_title_for_segment(_seg(1, "The Alpha's Rejected Mate")) == "The Alpha's Rejected Mate"
    assert preserved_title_for_segment(_seg(2, "The Alpha's Rejected Mate")) == "The Alpha's Rejected Mate"
    assert preserved_title_for_segment(_seg(4, "The Alpha's Rejected Mate")) is None
    assert preserved_title_for_segment(_seg(1, "Elara: Hello", "dialogue")) is None


def test_front_matter_block_still_exposes_title_and_skips_duplicate_body():
    from app.services.title_service import detect_source_title

    segs = [
        _seg(1, "My Little Hacker Picked Three Billionaire Dads\n"
                 "《天才萌宝选了三个亿万富豪爹地》\n"
                 "故事大纲\n"
                 "纽约布鲁克林，单亲妈妈Chloe在破旧洗车行谋生。\n"
                 "人物小传\n"
                 "Chloe Vance\n"
                 "隐姓埋名的传奇黑客Ghost。\n"),
    ]
    info = detect_source_title(segs)
    assert info is not None
    assert info.english_title == "My Little Hacker Picked Three Billionaire Dads"
    assert info.chinese_title == "天才萌宝选了三个亿万富豪爹地"
    assert info.segment_ids == ("seg-1",)
