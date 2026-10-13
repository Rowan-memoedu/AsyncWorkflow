"""Narrow client for XiaomiAlbumSyncer 0.19.1's documented local HTTP API."""
from __future__ import annotations
import secrets
from contextlib import contextmanager
import httpx
from .auth import access, protect
from .core import Settings, Attention, atomic, now, read_json


def request(client, method, path, **kwargs):
    try:
        reply = client.request(method, path, **kwargs)
    except httpx.HTTPError:
        raise Attention("xiaomi_local_api_unavailable") from None
    if reply.status_code >= 400:
        raise Attention("xiaomi_api_http_" + str(reply.status_code))
    return reply.json() if reply.content else None


@contextmanager
def connection(cfg: Settings):
    with httpx.Client(base_url=cfg.xiaomi_url, timeout=120, follow_redirects=False) as client:
        values = access(cfg.credential_file)
        initialized = request(client, "GET", "/api/system-config")["init"]
        if not initialized:
            # Persist first so an interrupted initialization doesn't lose its local password.
            values.setdefault("XIAOMI_LOCAL_PASSWORD", secrets.token_urlsafe(32))
            protect(cfg.credential_file, values)
            request(client, "POST", "/api/system-config", json={"password":values["XIAOMI_LOCAL_PASSWORD"]})
        if not values.get("XIAOMI_LOCAL_PASSWORD"):
            raise Attention("xiaomi_existing_password_not_bound")
        request(client, "GET", "/api/token", params={"password":values["XIAOMI_LOCAL_PASSWORD"]})
        yield client


def status(cfg):
    with connection(cfg) as client:
        accounts = request(client, "GET", "/api/account")
        jobs = request(client, "GET", "/api/crontab")
        return {"account_count":len(accounts), "job_count":len(jobs), "version":request(client, "GET", "/api/system-config/info")["appVersion"]}


def refresh_recording_names(cfg):
    """Cloud renames do not rename already downloaded files; join by stable asset ID."""
    binding = read_json(cfg.root / 'xiaomi-binding.json', {})
    if not binding:
        return {}
    with connection(cfg) as client:
        albums = request(client, 'GET', f"/api/album/latest/{binding['account_id']}")
        names = {}
        for album in albums:
            if str(album.get('remoteId')) == '-1' and not album.get('shadow'):
                for asset in request(client, 'GET', f"/api/asset/{album['id']}/latest"):
                    if asset.get('type') == 'AUDIO':
                        names[str(asset['id'])] = asset['fileName']
    atomic(cfg.root / 'xiaomi-recording-names.json', {'at': now(), 'names': names})
    return names


def configure(cfg, account_id=None):
    """Baseline metadata before any download; never enable production consumption here."""
    with connection(cfg) as client:
        accounts = request(client, "GET", "/api/account")
        binding = read_json(cfg.root / "xiaomi-binding.json", {})
        selected = account_id if account_id is not None else binding.get("account_id")
        candidates = accounts if selected is None else [a for a in accounts if a["id"] == selected]
        if len(candidates) != 1:
            raise Attention("xiaomi_account_missing_or_ambiguous")
        account = candidates[0]["id"]
        if binding and binding.get("account_id") != account:
            raise Attention("xiaomi_bound_account_changed")
        albums = request(client, "GET", f"/api/album/latest/{account}")
        # 0.19.1 utils/extendFunctions.kt: Album.isAudioAlbum() == remoteId -1.
        audio_albums = [a for a in albums if str(a.get("remoteId")) == "-1" and not a.get("shadow")]
        if not audio_albums:
            raise Attention("xiaomi_audio_album_schema_requires_live_check")
        baseline_path = cfg.root / "xiaomi-baseline.json"
        if baseline_path.exists() and read_json(baseline_path).get("account_id") != account:
            raise Attention("xiaomi_baseline_account_changed")
        if not baseline_path.exists():
            assets = []
            for album in audio_albums:
                assets.extend(request(client, "GET", f"/api/asset/{album['id']}/latest"))
            if any(a.get("type") != "AUDIO" for a in assets):
                raise Attention("xiaomi_non_audio_album_rejected")
            atomic(baseline_path, {"at":now(), "account_id":account, "asset_ids":[str(a["id"]) for a in assets]})
        name = "AsyncWorkflow recordings"
        previous = [j for j in request(client, "GET", "/api/crontab") if j["name"] == name]
        if len(previous) > 1:
            raise Attention("xiaomi_duplicate_jobs")
        config = {"expression":"0 */5 * * * ?", "timeZone":"Asia/Shanghai",
            "targetPath":cfg.inbox.as_posix() + "/xiaomi/${recordingTypeId}/${assetId}/${takenEpochMillis}/${fileName}",
            "downloadImages":False, "downloadVideos":False, "downloadAudios":True,
            "rewriteExifTime":False, "rewriteExifTimeZone":None, "skipExistingFile":True,
            "rewriteFileSystemTime":False, "checkSha1":True, "fetchFromDbSize":2,
            "downloaders":2, "verifiers":1, "exifProcessors":1, "fileTimeWorkers":1, "notify":False}
        album_ids = [a["id"] for a in audio_albums]
        def matches(job):
            # The server omits nullable fields when serializing the config.
            actual_config = {"rewriteExifTimeZone":None, **job["config"]}
            return (actual_config == config and job.get("accountId") == account
                and sorted(job.get("albumIds", [])) == sorted(album_ids)
                and job.get("syncMode") == "FULL" and job.get("enabled") is True)
        if previous:
            job = previous[0]
            if not matches(job):
                raise Attention("xiaomi_existing_job_differs")
        else:
            job = request(client, "POST", "/api/crontab", json={"name":name, "description":"Audio only; original basename and source provenance; historical baseline retained.",
                "enabled":True, "syncMode":"FULL", "accountId":account,
                "albumIds":album_ids, "config":config})
        actual = [j for j in request(client, "GET", "/api/crontab") if j["id"] == job["id"]]
        if len(actual) != 1 or not matches(actual[0]):
            raise Attention("xiaomi_config_readback_mismatch")
        atomic(cfg.root / "xiaomi-binding.json", {"job_id":job["id"], "account_id":account, "at":now()})
        return {"job_id":job["id"], "baseline_count":len(read_json(baseline_path)["asset_ids"]), "configured":True}
