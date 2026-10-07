from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path

from .core import Settings, Attention, Manifest, now, read_json, resolve_codex_path


async def doctor(cfg):
    from .auth import access
    from .remnote import connection, call
    import httpx
    credentials = access(cfg.credential_file)
    result = {"enabled": cfg.enabled, "ffprobe": cfg.ffprobe.exists(), "codex": resolve_codex_path(cfg.codex).exists(), "credential_available": {k: bool(credentials.get(k)) for k in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET")}}
    try:
        async with connection(cfg) as session:
            result["remnote"] = await call(session, "status")
    except Exception as exc:
        result["remnote"] = {"error_type": type(exc).__name__}
    try:
        result["prefect"] = httpx.get(cfg.prefect_url + "/health", timeout=5).status_code == 200
    except httpx.HTTPError:
        result["prefect"] = False
    return result


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    commands.add_parser("serve")
    commands.add_parser("watch")
    commands.add_parser("status")
    commands.add_parser("disable")
    commands.add_parser("enable")
    commands.add_parser("sync-template")
    commands.add_parser("sync-targets")
    repair = commands.add_parser("repair")
    repair.add_argument("recording_id")
    repair.add_argument("--from", dest="stage", choices=("split","content","routing","publish","meeting"), required=True)
    feedback_command = commands.add_parser('feedback')
    feedback_command.add_argument('recording_id')
    accepted = commands.add_parser('accept-asr')
    accepted.add_argument('recording_id')
    accepted.add_argument('--receipt', type=Path, required=True)
    accepted.add_argument('--decision', required=True)
    retry = commands.add_parser("retry")
    retry.add_argument("run_id")
    verify = commands.add_parser("verify-effect")
    verify.add_argument("recording_id")
    verify.add_argument("key")
    verify.add_argument("--evidence", required=True)
    proposal = commands.add_parser("reanalyze")
    proposal.add_argument("recording_id")
    submit = commands.add_parser("submit")
    submit.add_argument("path")
    submit.add_argument("--canary", action="store_true")
    batch = commands.add_parser('batch')
    batch.add_argument('--latest', type=int, required=True)
    batch.add_argument('--cutoff')
    batch.add_argument('--manual', action='store_true')
    resume = commands.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--answer", required=True)
    resume.add_argument("--destination", default="")
    resume.add_argument("--approve", action="store_true")
    resume.add_argument("--cancel", action="store_true")
    args = parser.parse_args()
    cfg = Settings.load()
    cfg.prepare()
    os.environ.setdefault("PREFECT_API_URL", cfg.prefect_url)
    os.environ.setdefault("PREFECT_HOME", str(cfg.root / "prefect"))
    os.environ.setdefault("PREFECT_LOCAL_STORAGE_PATH", str(cfg.root / "prefect-results"))
    os.environ.setdefault("PREFECT_SERVER_ANALYTICS_ENABLED", "false")
    if args.command == "doctor":
        print(json.dumps(asyncio.run(doctor(cfg)), ensure_ascii=False, indent=2))
    elif args.command == "serve":
        from .flows import serve_local
        serve_local()
    elif args.command == "watch":
        from .ingest import watch
        watch(cfg)
    elif args.command == "sync-template":
        from .remnote import sync_template
        print(json.dumps(asyncio.run(sync_template(cfg)),ensure_ascii=False,indent=2))
    elif args.command == "sync-targets":
        from .remnote import sync_targets
        print(json.dumps(asyncio.run(sync_targets(cfg)),ensure_ascii=False,indent=2))
    elif args.command == "repair":
        from .repairs import identity
        from prefect.deployments import run_deployment
        operation=identity(cfg,args.recording_id,args.stage)
        run=run_deployment('repair-recording/local',parameters={'recording_id':args.recording_id,'stage':args.stage,'operation_id':operation},timeout=0,idempotency_key='repair:'+args.recording_id+':'+operation,as_subflow=False)
        print(json.dumps({'run_id':str(run.id),'operation_id':operation,'stage':args.stage}))
    elif args.command == 'feedback':
        from .feedback import submit
        run_id=submit(Manifest(cfg,args.recording_id))
        print(json.dumps({'run_id':run_id,'stage':'workflow-feedback'}))
    elif args.command == 'accept-asr':
        from .transcript_revision import accept
        from prefect.deployments import run_deployment
        result=accept(cfg,args.recording_id,args.receipt,args.decision)
        run=run_deployment('analyze-recording/local',parameters={'path':result['source'],'canary':True},
                           timeout=0,idempotency_key='accepted-asr:'+args.recording_id+':'+result['revision'],as_subflow=False)
        Manifest(cfg,args.recording_id).update(analysis_run=str(run.id))
        print(json.dumps({**result,'run_id':str(run.id)},ensure_ascii=False))
    elif args.command in ("enable", "disable"):
        if args.command == "enable":
            from .routing import snapshot
            snapshot(cfg)
            health=asyncio.run(doctor(cfg))
            if not health.get('remnote',{}).get('connected'):
                raise Attention('independent_remnote_plugin_not_connected')
            from .ingest import baseline
            baseline(cfg)
            cfg.enabled, cfg.enabled_at = True, now()
        else:
            cfg.enabled = False
        cfg.save()
        print(json.dumps({"enabled": cfg.enabled, "enabled_at": cfg.enabled_at}))
    elif args.command == 'batch':
        from datetime import datetime
        from .batch import recent, run
        if not 1 <= args.latest <= 100:
            raise Attention('batch_count_out_of_range')
        cutoff=datetime.fromisoformat(args.cutoff) if args.cutoff else None
        if cutoff and cutoff.tzinfo is None:
            raise Attention('batch_cutoff_timezone_required')
        print(json.dumps({'receipt':str(run(cfg,recent(cfg,args.latest,cutoff),manual=args.manual))}))
    elif args.command == "submit":
        from .ingest import submit_file
        print(json.dumps(submit_file(cfg, Path(args.path), canary=args.canary), default=str))
    elif args.command == "reanalyze":
        from .proposals import reanalyze
        print(json.dumps(reanalyze(cfg,args.recording_id)))
    elif args.command == "verify-effect":
        if not args.evidence.strip():
            raise Attention("independent_verification_evidence_required")
        store = Manifest(cfg, args.recording_id)
        saved = store.effect(args.key)
        if not saved or saved["status"] != "completed_unverified":
            raise Attention("verification_requires_observed_execution_result")
        store.save_effect(args.key, {**saved, "status":"verified", "verification":{"at":now(),"evidence":args.evidence}})
        print(json.dumps({"effect":args.key,"verified":True}))
    elif args.command == "retry":
        from prefect.client.orchestration import get_client
        from prefect.states import Scheduled
        from uuid import UUID
        with get_client(sync_client=True) as client:
            run = client.read_flow_run(UUID(args.run_id))
            if not (run.state.is_failed() or run.state.is_crashed()):
                raise Attention("retry_requires_failed_or_crashed_run")
            client.set_flow_run_state(run.id, Scheduled(name="AwaitingRetry"), force=True)
        print(json.dumps({"retry_scheduled": args.run_id}))
    elif args.command == "resume":
        if not args.answer.strip():
            raise Attention("answer_required")
        from prefect.flow_runs import resume_flow_run
        from uuid import UUID
        resume_flow_run(UUID(args.run_id), run_input={"answer": args.answer, "destination": args.destination, "approved": args.approve, "cancel": args.cancel})
        print(json.dumps({"resumed": args.run_id}))
    else:
        # Only references and outcomes, not private transcripts, are printed.
        records = []
        for path in cfg.artifacts.glob("*/manifest.json"):
            data = read_json(path)
            records.append({"recording_id": data.get("recording_id"), "eligibility": data.get("eligibility"), "analysis_run": data.get("analysis_run"), "segment_runs": data.get("segment_runs"), "effects": {k: v["status"] for k, v in data.get("effects", {}).items()}})
        print(json.dumps(records, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
