"""Read the shared, already verified hotword release without copying user terms."""
from pathlib import Path
from .core import Attention, file_hash, read_json


def parameters(cfg, model):
    path = cfg.asr_preference_profile
    if path is None:
        return {}, None
    profile = read_json(path, {})
    words = profile.get('hotwords') if profile.get('schema_version') == 2 else profile.get('doubao', {}).get('hotwords')
    if not isinstance(words, list) or not words:
        raise Attention('shared_hotword_profile_invalid')
    profile_hash = file_hash(path)
    if model == 'fun-asr':
        receipt = read_json(Path(path).parent/'current-release.json', {})
        matches = [table for table in receipt.get('tables', []) if table.get('model') == model and table.get('verified')]
        if receipt.get('profile_sha256') != profile_hash or len(matches) != 1 or matches[0].get('count') != len(words):
            raise Attention('shared_hotword_release_not_verified')
        table = matches[0]
        return {'vocabulary_id': table['vocabulary_id']}, {'profile_sha256': profile_hash, **table}
    vocabulary = {}
    for item in words:
        if not isinstance(item, dict) or not isinstance(item.get('word'), str) or type(item.get('weight')) is not int or not 1 <= item['weight'] <= 5:
            raise Attention('shared_hotword_profile_invalid')
        vocabulary[item['word']] = item['weight']
    return {'vocabulary': vocabulary}, {'profile_sha256': profile_hash, 'count': len(vocabulary), 'mode': 'inline'}
