"""Check private production data without printing transcript or destination text."""
from pathlib import Path
import json
import httpx
from async_workflow.core import Settings
from async_workflow.dashboard import Inventory
from async_workflow.mirror import project

access = json.loads(Path('D:/AppData/RecordingView/access.json').read_text())
local = project(Inventory(Settings.load()))
with httpx.Client(timeout=30, trust_env=False, auth=('desktop', access['desktop'])) as client:
    response = client.get(access['url']+'api/recordings')
    response.raise_for_status()
    remote = response.json()
    assert {r['id'] for r in remote['recordings']} == {r['id'] for r in local['recordings']}
    eligible = [row for row in local['recordings'] if row.get('has_transcript') and row['status'] == 'verified']
    indexes = sorted({0, len(eligible)//4, len(eligible)//2, 3*len(eligible)//4, len(eligible)-1})
    for index in indexes:
        row = eligible[index]
        response = client.get(access['url']+'api/recordings/'+row['id'])
        response.raise_for_status()
        result = response.json()
        for key in ('name', 'transcript', 'parts', 'threads', 'asr'):
            assert result[key] == row[key], 'Cloud display differs from the source projection: '+key
    receipt = {'inventory_ids_match': True, 'total': remote['total'], 'details_verified': len(indexes),
               'synced_at': remote['synced_at'], 'fields': ['name','transcript','parts','threads','asr']}
Path('D:/Artifacts/AsyncWorkflow/recording-view-20261101/readback.json').write_text(json.dumps(receipt, indent=2))
print(json.dumps(receipt))
