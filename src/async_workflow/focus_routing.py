"""Route explicitly declared focus records with local, exact target bindings."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .core import Attention, digest, read_json


class Target(BaseModel):
    model_config=ConfigDict(extra='forbid')
    rem_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    daily_path: list[str] = Field(default_factory=list)


class Targets(BaseModel):
    model_config=ConfigDict(extra='forbid')
    schema_version: Literal[1] = 1
    document_id: str = Field(min_length=1)
    document_title: str = Field(min_length=1)
    categories: dict[Literal['goal','plan','todo','habit'],Target]


def plan(cfg, item, recorded_day, template_paths):
    category=item.get('focus_kind','')
    if not category: return None
    if category not in ('goal','plan','todo','habit') or item.get('kind')=='command':
        raise Attention('focus_type_invalid')
    evidence=item.get('focus_evidence','')
    raw=item.get('raw_text','')
    declaration=item.get('focus_declaration',{})
    direct=bool(evidence.strip()) and raw.lstrip().startswith(evidence.lstrip())
    inherited=(item.get('focus_from') is not None and bool(declaration.get('source_spans'))
               and declaration.get('evidence')==evidence and declaration.get('index',-1)<item.get('index',-1))
    if not direct and not inherited: raise Attention('focus_declaration_evidence_missing')
    scope=item.get('focus_scope','unspecified')
    if scope not in ('unspecified','today','other','date'):
        raise Attention('focus_scope_invalid')
    if scope!='unspecified' and (not item.get('focus_time_evidence','').strip()
                                or item['focus_time_evidence'] not in raw):
        raise Attention('focus_time_evidence_missing')
    today=scope=='today'
    if scope=='date':
        try: explicit=date.fromisoformat(item.get('focus_date','')).isoformat()
        except ValueError: raise Attention('focus_date_invalid') from None
        today=explicit==recorded_day
    try:
        targets=Targets.model_validate(read_json(cfg.root/'focus-targets.json',{}))
    except ValueError:
        raise Attention('focus_targets_not_synced') from None
    if category not in targets.categories: raise Attention('focus_category_not_bound')
    target=targets.categories[category]
    meta={'focus_kind':category,'focus_scope':scope,'focus_evidence':evidence,
          'focus_declaration':declaration,
          'focus_time_evidence':item.get('focus_time_evidence',''),
          'focus_targets_version':digest(targets.model_dump_json()),'review_eligible':False}
    if category!='habit' and today:
        if not target.daily_path or target.daily_path not in template_paths:
            raise Attention('focus_daily_branch_not_synced')
        return {**meta,'type':'daily','branch_path':target.daily_path,
                'reason':'declared_focus_item_for_recording_day'}
    return {**meta,'type':'document','parent_id':target.rem_id,
            'focus_document_id':targets.document_id,'focus_document_title':targets.document_title,
            'focus_branch_title':target.title,'reason':'habit_always_global' if category=='habit'
            else 'declared_focus_item_not_for_recording_day'}
