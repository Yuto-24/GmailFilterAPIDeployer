"""Single-send deployment with durable deferrals and conservative partial reads."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import gmail_filter_deployer as d


DELAYS = (30, 60)
FINAL_DELAY = 30


def api_failure(exc):
    """Do not turn programming, validation, or local persistence failures into deferrals."""
    return isinstance(getattr(getattr(exc, 'resp', None), 'status', None), int) or d.transient_api_error(exc)


def require_access(exc):
    """Authentication and account-wide permission failures cannot be isolated to one rule."""
    status = getattr(getattr(exc, 'resp', None), 'status', None)
    d.demand(status != 401 and (status != 403 or d.transient_api_error(exc)),
             'Gmailの認証・権限を確認できないため停止します。保留計画とログを保持してください')


def key(operation):
    """Identify an approved operation independently of its execution order."""
    return f"{operation['kind']}:{operation['step']}"


def operations(checkpoint):
    """Include a crash-interrupted send as an unresolved, never replayed intent."""
    result = deepcopy(checkpoint.get('deferred', {}))
    pending = checkpoint.get('pending')
    if pending:
        d.demand(key(pending) not in result, '保留操作と送信中操作が重複しています')
        result[key(pending)] = deepcopy(pending)
    return result


def validate(plan, state):
    """Reject corrupt new journals before interpreting observations or sending writes."""
    d.demand(state.get('plan_digest') == plan['approval_digest'], '配備ログに別の計画が記録されています')
    cp = state['batch_checkpoint']
    d.demand(cp.get('schema') == 2, '継続モードの進捗形式が不正です')
    expected = d.counter(plan['baseline_snapshot'])
    for kind in ('add', 'remove'):
        steps = cp[kind]
        d.demand(isinstance(steps, list) and len(set(steps)) == len(steps)
                 and all(type(i) is int and 0 <= i < len(plan[kind]) for i in steps), '継続モードの操作番号が不正です')
        for i in steps:
            digest = d.json_hash(plan[kind][i])
            expected[digest] += 1 if kind == 'add' else -1
            d.demand(expected[digest] >= 0, '継続モードの削除記録が不正です')
    first, second = (('remove', 'add') if plan.get('operation_order', 'add_first') == 'remove_first'
                     else ('add', 'remove'))
    d.demand(not cp[second] or len(cp[first]) == len(plan[first]), '継続モードの工程順序が不正です')
    d.demand(d.counter(row['filter'] for row in cp['rows']) == +expected, '継続モードの進捗と承認計画が一致しません')
    identified, _ = d.filter_snapshot(cp['rows'])
    labels = cp['labels']
    d.demand(isinstance(labels, dict) and len(set(labels.values())) == len(labels)
             and all(isinstance(v, str) and v.strip() for v in labels.values()), '継続モードのラベルIDが不正です')
    d.demand(len(set(cp.get('removed_ids', {}).values())) == len(cp.get('removed_ids', {})),
             '削除済みIDの操作番号が重複しています')
    for fid, step in cp.get('removed_ids', {}).items():
        d.demand(isinstance(fid, str) and fid not in identified and type(step) is int
                 and step in cp['remove'], '削除済みIDの記録が不正です')
    for name, op in operations(cp).items():
        kind, step = op.get('kind'), op.get('step')
        d.demand(name == key(op) and op.get('status') in ('unknown', 'failed', 'blocked')
                 and type(op.get('attempted')) is bool
                 and (op['status'] == 'blocked') == (not op['attempted']), '保留操作の形式が不正です')
        if kind == 'label':
            d.demand(step in plan['new_label_names'] and step not in labels, '保留ラベルが承認計画と一致しません')
        else:
            d.demand(not op['attempted'] or kind != second or len(cp[first]) == len(plan[first]),
                     '送信済み保留操作の工程順序が不正です')
            d.demand(kind in ('add', 'remove') and type(step) is int and 0 <= step < len(plan[kind])
                     and step not in cp[kind], '保留操作が承認計画と一致しません')
            if kind == 'remove' and (op['attempted'] or 'id' in op):
                d.demand(identified.get(op.get('id')) == plan[kind][step], '保留削除IDが承認計画と一致しません')
    return cp


def reconcile(plan, state, rows, labels, direct=None):
    """Accept positive evidence only; list absence never proves a deletion succeeded."""
    result = deepcopy(state)
    cp = validate(plan, result)
    held = operations(cp)
    cp['pending'] = None
    cp['read_available'] = True
    before, unknown = d.filter_snapshot(cp['rows'])
    actual, observed_unknown = d.filter_snapshot(rows)
    d.demand(unknown == observed_unknown, 'ID不明フィルタの内容・件数が変わりました')
    expected_labels = dict(cp['labels'])
    for name in set(labels) - set(expected_labels):
        op = held.get('label:' + name)
        d.demand(op and op['attempted'] and labels[name] not in expected_labels.values(), '予定外のラベル追加を検出しました')
        expected_labels[name] = labels[name]
        del held['label:' + name]
    d.demand(labels == expected_labels, '継続モードのラベル設定が変わりました')
    cp['labels'] = expected_labels
    available = set(actual) & set(before)
    d.demand(all(actual[fid] == before[fid] for fid in available), '既存フィルタのID・内容が変わりました')
    removed_ids = cp.setdefault('removed_ids', {})
    lagging = {fid for fid in set(actual) - set(before)
               if fid in removed_ids and actual[fid] == plan['remove'][removed_ids[fid]]}
    for fid in set(actual) - set(before) - lagging:
        matches = [op for op in held.values() if op['kind'] == 'add' and op['attempted']
                   and plan['add'][op['step']] == actual[fid]]
        d.demand(len(matches) == 1, '予定外・重複したフィルタ追加を検出しました')
        op = matches[0]
        cp['add'].append(op['step'])
        cp['rows'].append(next(row for row in rows if row.get('id') == fid))
        before[fid] = actual[fid]
        available.add(fid)
        del held[key(op)]
    for fid, observation in (direct or {}).items():
        if fid in actual or fid not in before:
            continue
        if observation is None:  # Only an actual individual GET 404 supplies this proof.
            matches = [op for op in held.values() if op['kind'] == 'remove' and op['attempted']
                       and op.get('id') == fid]
            d.demand(len(matches) == 1, '未送信のフィルタ削除・外部変更を検出しました')
            op = matches[0]
            cp['remove'].append(op['step'])
            removed_ids[fid] = op['step']
            cp['rows'] = [row for row in cp['rows'] if row.get('id') != fid]
            del before[fid]
            del held[key(op)]
        else:
            d.demand(observation == before[fid], '個別取得したフィルタが承認内容と異なります')
            available.add(fid)
    for op in held.values():
        if op['kind'] == 'remove' and op.get('id') in available and op['attempted']:
            op['status'] = 'failed'  # Present with the original ID/content: retry only on next approved run.
    cp['deferred'] = held
    cp['available_ids'] = sorted(available)
    cp['lagging_removed_ids'] = sorted(lagging)
    cp['unconfirmed_ids'] = sorted((set(before) - available) | lagging)
    cp['add'].sort()
    cp['remove'].sort()
    full = actual == before and not lagging
    cp['readback_confirmed'] = full
    result.update(acknowledged_add=len(cp['add']), acknowledged_remove=len(cp['remove']), verified=False)
    if full:
        result.update(completed_add=len(cp['add']), completed_remove=len(cp['remove']))
    validate(plan, result)
    return result


def progress(plan, state, rows, labels):
    """Expose reconciled operation sets to preflight without modifying the journal."""
    cp = reconcile(plan, state, rows, labels)['batch_checkpoint']
    return set(cp['add']), set(cp['remove'])


def perform(client, plan, current, labels, log, *, verbose=False, max_batches=None, readback_interval=50):
    """Attempt independent operations once per invocation and retain every unresolved intent."""
    prior = json.loads(log.read_text()) if log.exists() else {}
    if prior.get('batch_checkpoint', {}).get('schema') == 2:
        state = reconcile(plan, prior, current, labels)
    else:
        added, removed = d.batch_progress(plan['baseline_snapshot'], plan, current, log, labels)
        state = {**prior, 'account': plan['account'], 'plan_digest': plan['approval_digest'],
                 'events': prior.get('events', []), 'verified': False,
                 'completed_add': len(added), 'completed_remove': len(removed),
                 'acknowledged_add': len(added), 'acknowledged_remove': len(removed),
                 'batch_checkpoint': {'schema': 2, 'rows': current, 'labels': dict(labels),
                     'add': sorted(added), 'remove': sorted(removed), 'pending': None,
                     'deferred': {}, 'available_ids': [r['id'] for r in current if r.get('id')],
                     'unconfirmed_ids': [], 'readback_confirmed': True}}
    normalization = d.normalization_for_plan(plan)
    attempted = set()
    sent = 0
    since_readback = 0

    def save():
        validate(plan, state)
        d.save_json(log, state)

    def event(kind, **fields):
        state['events'].append({'kind': kind, 'timestamp_utc': datetime.now(timezone.utc).isoformat(), **fields})
        save()

    def get_one(fid):
        try:
            raw = d.read_operation('Gmail filters.get',
                lambda: client.users().settings().filters().get(userId='me', id=fid).execute(num_retries=0),
                verbose, retry=False)
        except Exception as exc:
            if not api_failure(exc):
                raise
            if getattr(getattr(exc, 'resp', None), 'status', None) == 404:
                return True, None
            event('deferred_get_error', id=fid, error=d.api_error_details(exc))
            require_access(exc)
            return False, None
        d.demand(raw.get('id') == fid, '個別取得の応答IDが一致しません')
        names = {value: name for name, value in state['batch_checkpoint']['labels'].items()}
        return True, d.live_as_template(raw, names, normalization=normalization)

    def readback(*, final=False):
        nonlocal state, current, labels, since_readback
        if final:
            print(f'{d.timestamp_prefix()}最終照合まで{FINAL_DELAY}秒待機します。', flush=True)
            d.time.sleep(FINAL_DELAY)
        for attempt in range(len(DELAYS) + 1):
            try:
                rows, now_labels = d.api_fetch(client, verbose=verbose, retry_reads=False, normalization=normalization)
            except Exception as exc:
                if not api_failure(exc):
                    raise
                event('deferred_read_error', error=d.api_error_details(exc), attempt=attempt + 1)
                require_access(exc)
                state['batch_checkpoint']['available_ids'] = []
                state['batch_checkpoint']['read_available'] = False
                state['batch_checkpoint']['readback_confirmed'] = False
                save()
            else:
                # Preserve every incomplete observation; reconcile fails closed on positive external drift.
                stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                d.save_json(log.parent / 'diagnostics' / f'deferred_{stamp}.json',
                            {'rows': rows, 'labels': now_labels, 'checkpoint': state['batch_checkpoint']})
                candidate = reconcile(plan, state, rows, now_labels)
                direct = {}
                for op in candidate['batch_checkpoint']['deferred'].values():
                    if op['kind'] == 'remove' and op['attempted'] and op.get('id') in candidate['batch_checkpoint']['unconfirmed_ids']:
                        known, value = get_one(op['id'])
                        if known:
                            direct[op['id']] = value
                state = reconcile(plan, state, rows, now_labels, direct)
                current, labels = rows, now_labels
                save()
                if state['batch_checkpoint']['readback_confirmed']:
                    since_readback = 0
                    return
            if attempt < len(DELAYS):
                print(f'{d.timestamp_prefix()}一覧が未確定のため{DELAYS[attempt]}秒待って再取得します。', flush=True)
                d.time.sleep(DELAYS[attempt])
        since_readback = 0
        d.demand(not state['batch_checkpoint'].get('lagging_removed_ids'),
                 '削除成功済みのIDが再取得後も残っています。外部変更の可能性があるため停止します')
        event('deferred_readback', unconfirmed_ids=state['batch_checkpoint'].get('unconfirmed_ids', []))

    def hold(op, reason):
        state['batch_checkpoint']['deferred'][key(op)] = {**op, 'reason': reason}
        event('deferred_operation', operation=key(op), reason=reason)
        print(f"保留: {key(op)} / {reason}", flush=True)

    def send(op, request):
        nonlocal sent, since_readback
        cp = state['batch_checkpoint']
        cp['deferred'].pop(key(op), None)
        cp['pending'] = {**op, 'attempted': True, 'status': 'unknown'}
        save()  # Durable intent precedes the only HTTP transmission.
        attempted.add(key(op))
        sent += int(op['kind'] != 'label')
        since_readback += 1
        try:
            response = d.timed_operation('継続モード ' + key(op), lambda: request.execute(num_retries=0), verbose,
                api_kind='labels.create' if op['kind'] == 'label' else 'filters.' + ('create' if op['kind'] == 'add' else 'delete'))
        except Exception as exc:
            if not api_failure(exc):
                raise
            intent = cp['pending']
            status = getattr(getattr(exc, 'resp', None), 'status', None)
            # A rejected create may be retried on a later approved invocation. Lost/5xx responses stay unknown.
            if op['kind'] in ('add', 'label') and status is not None and 400 <= status < 500 and status not in (408,):
                intent['status'] = 'failed'
            intent['error'] = d.api_error_details(exc)
            cp['deferred'][key(op)] = intent
            cp['pending'] = None
            event('deferred_write_error', operation=key(op), error=intent['error'])
            require_access(exc)
            print(f"保留: {key(op)} / {d.api_error_code(exc)}。同じ操作はこの起動中に再送しません。", flush=True)
            readback()
            return
        if op['kind'] == 'remove':
            cp.setdefault('removed_ids', {})[op['id']] = op['step']
            cp['remove'].append(op['step'])
            cp['rows'] = [r for r in cp['rows'] if r.get('id') != op['id']]
            cp['available_ids'] = [fid for fid in cp['available_ids'] if fid != op['id']]
        else:
            fid = response.get('id') if isinstance(response, dict) else None
            existing = set(cp['labels'].values()) if op['kind'] == 'label' else {r.get('id') for r in cp['rows']}
            if not isinstance(fid, str) or not fid.strip() or fid in existing:
                cp['deferred'][key(op)] = cp['pending']
                cp['pending'] = None
                event('deferred_invalid_response', operation=key(op))
                readback()
                return
            if op['kind'] == 'label':
                cp['labels'][op['step']] = fid
            else:
                cp['add'].append(op['step'])
                cp['rows'].append({'id': fid, 'filter': plan['add'][op['step']]})
                cp['available_ids'].append(fid)
        cp['add'].sort()
        cp['remove'].sort()
        cp['pending'] = None
        cp['readback_confirmed'] = False
        state.update(acknowledged_add=len(cp['add']), acknowledged_remove=len(cp['remove']))
        event('continued_operation', operation=key(op))
        print(f"応答確認済み: 削除 {len(cp['remove'])}/{len(plan['remove'])} / 追加 {len(cp['add'])}/{len(plan['add'])}", flush=True)

    save()
    # Missing IDs from an interrupted DELETE need direct evidence before resume can skip that operation.
    if state['batch_checkpoint']['deferred'] or state['batch_checkpoint'].get('unconfirmed_ids'):
        readback()
    first, second = (('remove', 'add') if plan.get('operation_order', 'add_first') == 'remove_first' else ('add', 'remove'))
    for kind in (first, second):
        if kind == second and len(state['batch_checkpoint'][first]) != len(plan[first]):
            for i in range(len(plan[kind])):
                if i not in state['batch_checkpoint'][kind]:
                    hold({'kind': kind, 'step': i, 'attempted': False, 'status': 'blocked'}, '先行工程の未完了に依存')
            break
        if kind == 'add':
            for name in plan['new_label_names']:
                cp = state['batch_checkpoint']
                if name in cp['labels']:
                    continue
                old = cp['deferred'].get('label:' + name)
                if old and old['attempted'] and old['status'] == 'unknown':
                    continue
                if not cp.get('read_available', True):
                    hold({'kind': 'label', 'step': name, 'attempted': False, 'status': 'blocked'}, 'ラベル一覧を取得できません')
                    continue
                if max_batches is not None and sent >= max_batches:
                    readback()
                    return False
                send({'kind': 'label', 'step': name}, client.users().labels().create(userId='me', body={
                    'name': name, 'labelListVisibility': 'labelShow', 'messageListVisibility': 'show'}))
                if since_readback >= readback_interval:
                    readback()
            if since_readback:
                readback()
        for i, rule in enumerate(plan[kind]):
            cp = state['batch_checkpoint']
            if i in cp[kind]:
                continue
            op = {'kind': kind, 'step': i}
            old = cp['deferred'].get(key(op))
            if key(op) in attempted or (old and old['attempted'] and old['status'] == 'unknown'):
                continue
            if max_batches is not None and sent >= max_batches:
                readback()
                return False
            if not cp.get('read_available', True):
                hold({**op, 'attempted': False, 'status': 'blocked'}, '一覧取得に失敗したため状態を確認できません')
                continue
            if kind == 'remove':
                matches = [r for r in cp['rows'] if r['filter'] == rule]
                d.demand(len(matches) == 1 and matches[0].get('id'), '削除対象IDが一意ではありません')
                op['id'] = matches[0]['id']
                if op['id'] not in cp['available_ids']:
                    known, value = get_one(op['id'])
                    if not known:
                        hold({**op, 'attempted': False, 'status': 'blocked'}, '対象IDを確認できません')
                        continue
                    d.demand(value == rule, '未送信の削除対象が外部変更されています')
                    cp['available_ids'].append(op['id'])
                request = client.users().settings().filters().delete(userId='me', id=op['id'])
            else:
                missing_labels = [v[6:] for v in rule['action']['addLabelIds'] if v.startswith('label:') and v[6:] not in cp['labels']]
                uncertain_adds = sum(o['kind'] == 'add' and o['attempted'] and o['status'] == 'unknown' for o in operations(cp).values())
                if missing_labels or len(cp['rows']) + uncertain_adds >= d.FILTER_LIMIT:
                    hold({**op, 'attempted': False, 'status': 'blocked'}, '必要ラベル・確認済み状態・容量が未確定')
                    continue
                request = client.users().settings().filters().create(userId='me', body=d.to_api(rule, cp['labels'], normalization=normalization))
            send(op, request)
            if since_readback >= readback_interval:
                readback()
        if since_readback:
            readback()
    readback(final=True)
    cp = state['batch_checkpoint']
    complete = (len(cp['add']) == len(plan['add']) and len(cp['remove']) == len(plan['remove'])
                and cp['readback_confirmed'] and not cp['deferred']
                and d.counter(r['filter'] for r in current) == d.counter(d.planned_states(plan)[1]))
    state['verified'] = complete
    save()
    if not complete:
        print(f"一部完了: 照合済み削除 {state['completed_remove']}/{len(plan['remove'])} / "
              f"追加 {state['completed_add']}/{len(plan['add'])} / 保留 {len(cp['deferred'])}件 / "
              f"未確認ID {len(cp.get('unconfirmed_ids', []))}件。計画とログを保持します。", flush=True)
        statuses = {'unknown': '成否不明', 'failed': '未反映', 'blocked': '未送信'}
        for op in cp['deferred'].values():
            title = ('ラベル作成 ' + op['step'] if op['kind'] == 'label' else
                     ('追加' if op['kind'] == 'add' else '削除') + f" {op['step'] + 1}/{len(plan[op['kind']])}")
            reason = op.get('reason') or op.get('error', {}).get('code', '応答結果が未確定')
            print(f"  保留: {title} / {statuses[op['status']]} / {reason}", flush=True)
    return {'rows': current, 'labels': cp['labels'], 'verified': complete, 'deferred': cp['deferred']}
