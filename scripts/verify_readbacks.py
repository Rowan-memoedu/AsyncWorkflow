"""Compare independently captured RemNote trees with saved workflow results."""
import argparse
import hashlib
import json
from pathlib import Path

from async_workflow.core import Settings, atomic, now, read_json
from async_workflow.markdown import parse_outline


def compare(expected, actual):
    assert expected['text'] == actual['title'], (expected['text'], actual['title'])
    assert not actual.get('cardDirection'), actual['remId']
    children = actual.get('children', actual.get('contentStructured', []))
    assert len(expected['children']) == len(children), actual['remId']
    return 1 + sum(compare(left, right) for left, right in zip(expected['children'], children))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('recording_id')
    parser.add_argument('--snapshots', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    cfg = Settings.load()
    folder = cfg.artifacts / args.recording_id
    manifest = read_json(folder / 'manifest.json')
    results = []
    for segment in manifest['segments']:
        if segment.get('route') != 'remnote' or segment.get('cancelled'):
            continue
        effect = manifest['effects'][segment['key']]
        assert effect['status'] == 'verified'
        observed = read_json(args.snapshots / (effect['root_id'] + '.json'))
        assert observed['remId'] == effect['root_id']
        assert observed['parentRemId'] == effect['parent']
        count = compare(parse_outline(segment['markdown']), observed)
        result = {'key': segment['key'], 'root_id': effect['root_id'],
                  'parent_id': effect['parent'], 'parent_title': observed.get('parentTitle'),
                  'outline_nodes_matched': count, 'content_only': True}
        if effect.get('separators'):
            pair=effect['separators']
            siblings=read_json(args.snapshots/('siblings-'+effect['parent']+'.json'))['children']
            ids=[node['remId'] for node in siblings]
            index=ids.index(effect['root_id'])
            assert index>0 and index+1<len(ids)
            assert ids[index-1]==pair['before_id'] and ids[index+1]==pair['after_id']
            assert siblings[index-1]['title']==siblings[index+1]['title']==''
            result['blank_siblings_verified']=True
        if effect.get('created_document_id'):
            document = read_json(args.snapshots / (effect['created_document_id'] + '.json'))
            assert document['remType'] == 'document'
            assert document['title'] == effect['routing']['title']
            assert document.get('parentRemId') == effect['routing'].get('parent_id')
            result['document_verified'] = True
        results.append(result)
    receipt = {'at': now(), 'recording_id': args.recording_id,
               'independent_readback_passed': True, 'results': results}
    if args.baseline:
        baseline = next(x for x in read_json(args.baseline)['records']
                        if x['recording_id'] == args.recording_id)
        for name, digest in baseline['files'].items():
            assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest, name
        assert manifest['asr']['task_id'] == baseline['asr_task_id']
        receipt['asr_and_original_outline_unchanged'] = True
        unchanged = 0
        for segment in manifest['segments']:
            old = baseline['outlines'].get(segment.get('reuses_effect', segment['key']))
            if old is None:
                old = next((x for x in baseline['outlines'].values()
                            if x['raw'] == segment['raw_text']), None)
            if old and not segment.get('control_spans'):
                assert old['markdown'] == segment['markdown']
                assert old['root_id'] == manifest['effects'][segment['key']]['root_id']
                unchanged += 1
        receipt['accepted_segment_outlines_reused'] = unchanged
    atomic(args.output, receipt)
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
