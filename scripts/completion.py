"""Task outcomes. Report validity, bounded execution and quantity are separate facts."""
from core import digest, read_json
import re


def target_scope(store):
    return digest({k:store.config.get(k) for k in ('topic','scope','max_followers','min_ratio',
                  'search_days','baseline_count','min_baseline_count','comparison_window_days','min_age_days')})


def run_identity(store):
    # Legacy snapshots have no UUID. Preserve their original topic/time identity.
    return store.state.get('run_id') or digest({
        'as_of': store.state['as_of'], 'topic': store.config['topic'],
        'scope': store.config['scope'], 'keywords': store.config['keywords']})


def target_policy(store):
    requested = store.config.get('delivery_target_count', 20)
    approval = read_json(store.root / 'delivery-target-approval.json', {})
    scope = target_scope(store)
    accepted = (bool(re.fullmatch('[a-f0-9]{32}',str(store.state.get('run_id',''))))
                and type(requested) is int and 1 <= requested <= 30
                and approval.get('schema') == 'delivery-target-approval-v1'
                and approval.get('run_identity') == run_identity(store)
                and approval.get('scope_sha256') == scope
                and approval.get('target') == requested
                and isinstance(approval.get('authorization_reference'), str)
                and bool(approval['authorization_reference'].strip()))
    # A copied config/one-time acceptance is never a standing lower target.
    return {'default_min': 20, 'default_max': 30, 'requested': requested,
            'effective_min': requested if accepted else 20,
            'exception_applied': accepted and requested != 20,
            'unscoped_override_ignored': requested != 20 and not accepted}


def outcome(assessment, demo=False):
    if demo and assessment['report']['ready']:
        return 'done', '合成示例报告已生成，不代表真实研究目标已达成。'
    if assessment['overall_goal_met']:
        return 'done', '报告已生成，本轮检索与数量目标已核实。'
    if assessment['report']['ready']:
        return 'report_ready', '报告已生成，可先阅读已有成果；本轮研究目标尚未全部达成。'
    return 'needs_analysis', '已保存阶段结果，本轮研究尚未完成。'
