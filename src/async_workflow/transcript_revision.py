"""Accept a reviewed ASR receipt and resume only downstream native Prefect work."""
from pathlib import Path
import httpx
from filelock import FileLock
from .asr import normalize_funasr
from .core import Attention, Manifest, atomic, digest, file_hash, now, read_json


def verify_quiescent(cfg, before):
    for effect in before.get('effects', {}).values():
        if effect.get('session_id') or effect.get('thread_id') or effect.get('backend') == 'app-server':
            raise Attention('accepted_asr_requires_existing_thread_reconciliation')
        if effect.get('root_id'):
            if effect.get('parent') != cfg.trash_rem_id or not effect.get('held_output_receipt'):
                raise Attention('accepted_asr_requires_archived_old_outputs')
            held = read_json(Path(effect['held_output_receipt']), {})
            if isinstance(held, list):
                held = next((item for item in held if item.get('root_id') == effect['root_id']), {})
            if held.get('root_id') != effect['root_id'] or held.get('parent_id') != cfg.trash_rem_id or held.get('status') != 'verified':
                raise Attention('accepted_asr_archive_receipt_mismatch')
        elif effect.get('status') not in ('cancelled', 'not_started'):
            raise Attention('accepted_asr_unreconciled_effect')
    run_ids = [before.get('analysis_run'), *before.get('segment_runs', {}).values()]
    with httpx.Client(base_url=cfg.prefect_url, timeout=20) as client:
        for run_id in dict.fromkeys(r for r in run_ids if r):
            response = client.get('/flow_runs/' + run_id)
            response.raise_for_status()
            if response.json().get('state_type') not in ('COMPLETED', 'CANCELLED', 'FAILED', 'CRASHED'):
                raise Attention('accepted_asr_requires_stopped_old_runs')


def accept(cfg, recording_id, receipt, decision):
    if not decision.strip(): raise Attention('explicit_transcript_acceptance_required')
    raw = read_json(receipt)
    normalized = normalize_funasr(raw, recording_id)
    revision = digest(receipt.read_bytes())[:24]
    store = Manifest(cfg, recording_id)
    folder = store.folder / 'source-revisions' / revision
    with FileLock(str(cfg.root / 'locks' / ('repair-' + recording_id + '.lock'))):
        current = store.read()
        if current.get('source_revision') == revision:
            return {'revision':revision, 'source':current['sources'][0], 'reused':True}
        before = read_json(folder / 'before.json') or current
        if not before.get('sources') or file_hash(Path(before['sources'][0])) != recording_id:
            raise Attention('accepted_asr_audio_hash_mismatch')
        verify_quiescent(cfg, before)
        if not (folder / 'before.json').exists():
            for name in ('asr.json', 'transcript.txt', 'outline.md', 'analysis-version.json'):
                source = store.folder / name
                if source.exists(): atomic(folder / name, source.read_text(encoding='utf-8'), text=True)
            atomic(folder / 'before.json', before)
        atomic(folder / 'accepted-raw.json', raw)
        atomic(folder / 'acceptance.json', {'at':now(), 'decision':decision, 'receipt_path':str(receipt),
                                         'receipt_sha256':file_hash(receipt), 'source_sha256':recording_id})
        atomic(store.folder / 'asr.json', normalized)
        atomic(store.folder / 'transcript.txt', normalized['text'], text=True)
        store.update(source_revision=revision, segments=[], segment_runs={},
                     asr={'provider':'funasr','model':normalized['model'],'submission':'completed',
                          'raw_receipt':str(folder / 'accepted-raw.json'),'cleaned':True},
                     batch_hold={'review_required':False,'accepted_revision':revision,'decision':decision})
        return {'revision':revision, 'source':before['sources'][0], 'reused':False}
