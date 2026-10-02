import copy
from pathlib import Path
import pytest

from async_workflow.core import Settings, Manifest, eligibility, recorded_time, safe_source, file_hash, Attention, UnknownOutcome
from async_workflow.splitter import VoiceDumpSplitResult, validate_split, units
from async_workflow.markdown import parse_outline
from async_workflow.executor import execute


@pytest.fixture
def cfg(tmp_path):
    value = Settings(root=tmp_path / "state", inbox=tmp_path / "audio", artifacts=tmp_path / "out", prompt=tmp_path / "prompt", ffprobe=Path("ffprobe"), codex=Path("codex"))
    value.prepare()
    return value


@pytest.mark.parametrize("name,duration,expected", [("20261002_130000.m4a", 1200, "eligible"), ("20261002_130000.m4a",1200.01,"over_duration"), ("电话20261002_130000.m4a",4,"name_not_datetime"), ("微信录音.m4a",4,"name_not_datetime"), ("2026-10-02_13-00-00.wav",4,"eligible"), ("20260230_130000.wav",4,"name_not_datetime")])
def test_filter(name,duration,expected):
    assert eligibility(name,duration) == expected


@pytest.mark.parametrize("duration",[0,-1,float("nan"),float("inf")])
def test_unknown_duration_not_uploaded(duration):
    with pytest.raises(Attention):
        eligibility("20261002_130000.wav",duration)


def test_source_type_conflict():
    assert eligibility("20261002_130000.wav",5,"PHONE_CALL") != "eligible"


@pytest.mark.parametrize("name,expected", [
    ("12-31-2025 23.58.m4a", "2025-12-31T23:58:00+08:00"),
    ("11-09-2026 07.08.09.m4a", "2026-11-09T07:08:09+08:00"),
    ("02-30-2026 23.58.m4a", None),
    ("通话12-31-2025 23.58.m4a", None),
])
def test_xiaomi_month_first_names(name,expected):
    result=recorded_time(name)
    assert (result.isoformat() if result else None) == expected
    assert eligibility(name,10,"PHONE_CALL") == "source_not_recorder"


def test_source_boundary(cfg,tmp_path):
    outside=tmp_path / "outside.wav"
    outside.write_bytes(b"audio")
    with pytest.raises(Attention):
        safe_source(outside,cfg.inbox)


def test_content_identity_and_same_name(cfg,tmp_path):
    a=cfg.inbox / "20261002_130000.wav"
    b=cfg.inbox / "renamed.wav"
    a.write_bytes(b"same")
    b.write_bytes(b"same")
    assert file_hash(a)==file_hash(b)
    a.write_bytes(b"different")
    assert file_hash(a)!=file_hash(b)


def item(kind,ids,**kw):
    return {"kind":kind,"spans":[{"unit":n} for n in ids],"reason":"fixture",**kw}


def test_natural_command_no_keyword_gate():
    raw="帮我查一下月球的半径。一个灵感是先记录来源。"
    data={"items":[item("command",[0],route="codex",intent_evidence="帮我查一下月球的半径"),item("inspiration",[1])]}
    result=validate_split(VoiceDumpSplitResult.model_validate(data),raw)
    assert result[0]["raw_text"]==units(raw)[0]["text"]
    assert len(result)==2


def test_substrings_unique_and_exact():
    raw="先查询然后记笔记。"
    data={"items":[item("record",[]) ,item("record",[])]}
    data["items"][0]["spans"]=[{"unit":0,"quote":"先查询"}]
    data["items"][1]["spans"]=[{"unit":0,"quote":"然后记笔记。"}]
    assert len(validate_split(VoiceDumpSplitResult.model_validate(data),raw))==2
    data["items"][1]["spans"][0]["quote"]="改写的文字"
    with pytest.raises(ValueError): validate_split(VoiceDumpSplitResult.model_validate(data),raw)


@pytest.mark.parametrize("ids",[[],[0,0],[1]])
def test_missing_or_overlapping_sources_fail(ids):
    with pytest.raises(ValueError):
        validate_split(VoiceDumpSplitResult.model_validate({"items":[item("record",ids)]}),"资料一。资料二。")


def test_cancel_and_dependency():
    raw="请检查文档。取消刚才的检查。"
    data={"items":[item("command",[0],route="codex",intent_evidence="请检查文档"),item("record",[1],supersedes=[0])]}
    result=validate_split(VoiceDumpSplitResult.model_validate(data),raw)
    assert result[0]["cancelled"]
    data["items"][0]["depends_on"]=[1]
    with pytest.raises(ValueError): validate_split(VoiceDumpSplitResult.model_validate(data),raw)


def test_command_evidence_cannot_be_rewritten():
    with pytest.raises(ValueError):
        validate_split(VoiceDumpSplitResult.model_validate({"items":[item("command",[0],route="codex",intent_evidence="请执行")] }),"以后可能做这个。")


def test_knowledge_routing_not_executor():
    with pytest.raises(ValueError):
        validate_split(VoiceDumpSplitResult.model_validate({"items":[item("command",[0],route="remnote",intent_evidence="保存到文档")] }),"保存到文档。")


def test_markdown_title_only_and_nested():
    assert parse_outline("# 单一观点")["children"]==[]
    tree=parse_outline("# 想法\n- 原因\n  - 例子 $x^2$ → 结果\n- 限制 :: 条件")
    assert tree["children"][0]["children"][0]["text"]=="例子 $x^2$ → 结果"


@pytest.mark.parametrize("text",["plain", "# 标题\n段落", "# 标题\n- ", "# 标题\n```python\nx=1\n```"])
def test_bad_markdown_rejected(text):
    with pytest.raises(ValueError): parse_outline(text)


def test_unknown_command_never_relaunches(cfg,monkeypatch):
    store=Manifest(cfg,"a"*64)
    store.save_effect("one",{"status":"outcome_unknown"})
    def forbidden(*a,**kw): raise AssertionError("must not launch")
    monkeypatch.setattr("subprocess.Popen",forbidden)
    with pytest.raises(UnknownOutcome): execute(cfg,store,"one",{})


def test_manifest_keeps_other_effects(cfg):
    store=Manifest(cfg,"b"*64)
    store.save_effect("one",{"status":"verified"})
    store.save_effect("two",{"status":"outcome_unknown"})
    store.update(model="new")
    assert store.effect("one")["status"]=="verified"
