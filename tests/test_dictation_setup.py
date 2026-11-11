import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('dictation_setup', Path(__file__).parents[1]/'scripts/dictation_setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def test_restore_keeps_credentials_and_timeout_until_native_provider_changes():
    config = {'ai': {'correction': {'provider': 'openai_compatible'},
                    'providers': {'openai_compatible': {'api_key': 'synthetic-local-token'}}},
              'voice_input_organize_level': 'strong',
              'features': {'model-overrides': {'voice_input': {'timeout_secs': 30}}}}
    original = {'correction': {'provider': 'openai'}, 'compatible': {'api_key': ''},
                'organize_level': 'light', 'voice_input_override': {}}
    assert not setup.restore_config(config, original, {'fast_model': {'configured_provider': 'openai_compatible'}})
    assert config['ai']['providers']['openai_compatible']['api_key'] == 'synthetic-local-token'
    assert config['features']['model-overrides']['voice_input']['timeout_secs'] == 30
    assert config['voice_input_organize_level'] == 'strong'
    assert setup.restore_config(config, original, {'fast_model': {'configured_provider': 'openai'}})
    assert config['ai']['providers']['openai_compatible']['api_key'] == ''
    assert config['features']['model-overrides']['voice_input'] == {}
    assert config['voice_input_organize_level'] == 'light'


def test_offline_restore_does_not_require_native_gateway():
    assert setup.restored_in_running_app(None, {'correction': {'provider': 'openai'}})
