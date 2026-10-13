from pathlib import Path
from types import SimpleNamespace
import asyncio
import pytest
from async_workflow import meetings, meeting_audio, recording_types, asr
from async_workflow.core import Settings, Manifest, atomic, file_hash, read_json


@pytest.fixture
def cfg(tmp_path):
    cfg = Settings(root=tmp_path/'state', inbox=tmp_path/'audio', artifacts=tmp_path/'out',
                   ffprobe=Path('ffprobe'), codex=Path('codex'), stability_seconds=0,
                   conversation_profiles={'Kim': {'mode':'diarized','self_name':'Lee','other_name':'Kim',
                                                  'document_title':'Lee&Kim','document_id':'person-doc'}},
                   trash_rem_id='trash', enabled=True, enabled_at='2026-01-01T00:00:00+08:00')
    cfg.prepare()
    return cfg


def audio(cfg, name, content=b'first'):
    path = cfg.inbox/name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_cloud_rename_joins_existing_audio_and_keeps_actual_start_time(cfg, monkeypatch):
    path = audio(cfg, 'xiaomi/0/123/1791803078912/10-12-2026 13.52.m4a')
    atomic(cfg.root/'xiaomi-recording-names.json', {'names': {'123':'Kim 1.m4a'}})
    monkeypatch.setattr(meetings, 'probe', lambda *a: 877)
    groups = meetings.discover(cfg, refresh=False)
    assert len(groups) == 1 and groups[0]['day'] == '2026-10-12'
    assert groups[0]['members'][0]['provenance']['recorded_at'] == '2026-10-12T13:52:00+08:00'
    assert groups[0]['members'][0]['id'] == file_hash(path)
    assert recording_types.kind(cfg, path.name, 877, {'cloud_file_name':'Kim 1.m4a'}) == ('conversation','eligible')
    assert recording_types.kind(cfg, path.name, 877, {}) == ('ordinary','eligible')


def test_contact_day_order_dedup_and_filename_changes_do_not_repeat(cfg, monkeypatch):
    first = audio(cfg, 'Kim_20261012090000.wav')
    audio(cfg, 'Kim_20261012100000.wav', b'second')
    audio(cfg, 'copy/Kim_20261012090000.wav')
    audio(cfg, 'Kim_20261013090000.wav', b'next day')
    monkeypatch.setattr(meetings, 'probe', lambda *a: 10)
    groups = meetings.discover(cfg, refresh=False)
    assert [len(g['members']) for g in groups] == [2,1]
    assert groups[0]['members'][0]['id'] == file_hash(first)
    revision = groups[0]['revision']
    first.rename(first.with_name('Kim renamed_20261012090000.wav'))
    assert meetings.discover(cfg, refresh=False)[0]['revision'] == revision


def test_diarization_does_not_drop_provider_speaker_ids():
    text, sentences = asr.normalize({'transcripts':[{'sentences':[
        {'text':'Hello','begin_time':0,'end_time':100,'speaker_id':3},
        {'text':'Hi','begin_time':200,'end_time':300,'speaker_id':0}]}]})
    assert [s['speaker_id'] for s in sentences] == [3,0]


def test_names_are_resolved_from_evidence_not_channel_or_number(cfg, monkeypatch):
    store = Manifest(cfg, 'd'*64)
    turns = [{'id':0,'speaker_id':'9','text':'I am Kim.'}, {'id':1,'speaker_id':'2','text':'I am Lee.'}]
    def ask(c, schema, instruction, payload, validator):
        assert '左右声道' in instruction and '不是声道编号' in instruction
        return validator(meeting_audio.Names(speakers=[
            meeting_audio.SpeakerName(speaker_id='9',name='Kim',evidence='I am Kim.'),
            meeting_audio.SpeakerName(speaker_id='2',name='Lee',evidence='I am Lee.')]))
    monkeypatch.setattr(meeting_audio.routing, 'ask', ask)
    assert meeting_audio.resolve_names(cfg,store,turns,cfg.conversation_profiles['Kim']) == {'9':'Kim','2':'Lee'}
    monkeypatch.setattr(meeting_audio.routing,'ask',lambda *a:pytest.fail('completed mapping repeated'))
    assert meeting_audio.resolve_names(cfg,store,turns,cfg.conversation_profiles['Kim'])['9'] == 'Kim'


def test_completed_group_skips_and_partial_group_reuses_same_native_run(cfg, monkeypatch):
    group = {'id':'a'*64,'revision':'one','members':[]}
    store = Manifest(cfg, group['id'])
    store.update(completed_revision='one')
    monkeypatch.setattr(meetings, 'run_deployment', lambda *a,**k:pytest.fail('duplicate native submission'))
    assert meetings.submit(cfg,group,manual=True)['skipped'] == 'already_processed'
    store.update(completed_revision='old', submitted_revision='one', analysis_run='original')
    from async_workflow import ingest
    monkeypatch.setattr(ingest, 'continue_runs', lambda d: ['original'])
    assert meetings.submit(cfg,group,manual=True)['run_id'] == 'original'


def test_room_batch_cache_reused_when_same_day_gains_recording(cfg, monkeypatch):
    old = audio(cfg,'Kim_20261012090000.wav')
    new = audio(cfg,'Kim_20261012100000.wav', b'new')
    members = [{'id':file_hash(p),'path':str(p),'duration':10} for p in (old,new)]
    store = Manifest(cfg,'a'*64)
    batch_store = Manifest(cfg,'b'*64)
    batch = {'batch_id':'b'*64,'source_ids':[members[0]['id']], 'speakers':{'0':'Lee'},
             'boundaries':[{'id':members[0]['id'],'start_ms':0,'end_ms':10000}],
             'turns':[{'id':0,'speaker':'Lee','start_ms':10,'end_ms':100,'text':'First'}]}
    atomic(batch_store.folder/'room-transcript.json',batch)
    store.update(speech_batches=[{'batch_id':'b'*64,'source_ids':batch['source_ids']}])
    calls=[]
    def prepare(c, missing, profile):
        calls.append(missing)
        value = {**batch,'batch_id':'c'*64,'source_ids':[members[1]['id']],
                 'boundaries':[{'id':members[1]['id'],'start_ms':0,'end_ms':10000}],
                 'turns':[{'id':0,'speaker':'Kim','start_ms':20,'end_ms':300,'text':'Second'}],
                 'speakers':{'1':'Kim'}}
        atomic(Manifest(c,'c'*64).folder/'room-transcript.json',value)
        return value
    monkeypatch.setattr(meeting_audio,'prepare',prepare)
    group={'revision':'new','members':members}
    result=meetings.transcript(cfg,store,group,cfg.conversation_profiles['Kim'])
    assert calls == [[members[1]]]
    assert [t['text'] for t in result['turns']] == ['First','Second']
    assert [t['id'] for t in result['turns']] == [0,1]
    assert result['turns'][1]['start_ms'] == 10020


def test_failed_new_write_does_not_archive_old_meeting(cfg, monkeypatch):
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def connection(c):
        yield None
    monkeypatch.setattr(meetings.remnote,'connection',connection)
    async def resolve(*args): return {'parent':'meeting'}
    monkeypatch.setattr(meetings.remnote,'resolve_target',resolve)
    async def fail(*args,**kwargs): raise RuntimeError('write failed')
    monkeypatch.setattr(meetings.remnote,'publish',fail)
    async def forbidden(*args,**kwargs): pytest.fail('old output archived before verified replacement')
    monkeypatch.setattr(meetings.remnote,'call',forbidden)
    group={'id':'a'*64,'revision':'new','day':'2026-10-12','contact':'Kim'}
    plan={'obsolete':[{'root_id':'old','plugin_effect_key':'old-key'}], 'archived':{}}
    with pytest.raises(RuntimeError,match='write failed'):
        asyncio.run(meetings.publish(cfg,Manifest(cfg,group['id']),group,cfg.conversation_profiles['Kim'],
                                    '# Meeting',plan,cfg.root/'plan.json'))
    assert plan['archived'] == {}
