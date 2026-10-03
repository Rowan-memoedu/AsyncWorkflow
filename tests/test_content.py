import pytest
from async_workflow.splitter import VoiceDumpSplitResult, validate_split


@pytest.mark.parametrize('kind,route', [('record','remnote'),('command','codex')])
def test_container_control_and_task_body_share_one_unit_without_loss(kind,route):
    source='新建一个条目，比较这两种协议，保留成本限制。'
    control='新建一个条目，'
    item={'kind':kind,'route':route,'reason':'fixture','intent_evidence':'比较这两种协议',
          'spans':[{'unit':0,'quote':control,'role':'control'},
                   {'unit':0,'quote':source[len(control):],'role':'content'}]}
    result=validate_split(VoiceDumpSplitResult.model_validate({'items':[item]}),source)[0]
    assert result['raw_text']==source
    assert result['content_text']=='比较这两种协议，保留成本限制。'
    assert result['control_spans']==[[0,len(control)]]
    item['spans'].pop()
    with pytest.raises(ValueError,match='coverage|source evidence'):
        validate_split(VoiceDumpSplitResult.model_validate({'items':[item]}),source)


def test_content_repair_reuses_only_unchanged_body(tmp_path,monkeypatch):
    from pathlib import Path
    from async_workflow import repairs,splitter
    from async_workflow.core import Settings,Manifest,atomic
    cfg=Settings(root=tmp_path/'state',artifacts=tmp_path/'out',ffprobe=Path('ffprobe'),codex=Path('codex'))
    cfg.prepare();store=Manifest(cfg,'a'*64)
    part={'index':0,'kind':'record','raw_text':'Make a note. Actual words.','markdown':'# Make a note\n- Actual words',
          'key':'old','source_spans':[[0,26]],'destination':''}
    store.update(segments=[part])
    atomic(store.folder/'asr.json',{'text':part['raw_text']})
    monkeypatch.setattr(splitter,'extract_content',lambda c,p:{**p,'content_text':'Actual words.'})
    calls=[]
    monkeypatch.setattr(splitter,'refine',lambda c,t:calls.append(t) or '# Actual words')
    _,_,plan=repairs.prepare(cfg,store,'content','content-revision')
    assert calls==['Actual words.']
    assert 'reuses_effect' not in plan['segments'][0]
    assert plan['reused_asr']
