#!/usr/bin/env python3
"""Fail-closed Gmail Atom XML -> Gmail API filter deployer.

Runs fully offline for `validate` and `diff-xml`.
`plan`, `apply`, `verify` require a locally authenticated Gmail API session.
No action touches individual messages; Action/Delete rules are immutable.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import xml.etree.ElementTree as ET

ATOM = "http://www.w3.org/2005/Atom"
APPS = "http://schemas.google.com/apps/2006"
NS = {"a": ATOM, "apps": APPS}
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.labels",
]
KNOWN = {
    "from", "to", "subject", "hasTheWord", "doesNotHaveTheWord",
    "label", "shouldNeverSpam", "shouldNeverMarkAsImportant",
    "shouldAlwaysMarkAsImportant", "shouldMarkAsRead", "shouldStar",
    "shouldTrash", "sizeOperator", "sizeUnit",
}
FILTER_LIMIT = 1000


class DeploymentError(Exception):
    pass


def demand(ok: bool, message: str) -> None:
    if not ok:
        raise DeploymentError(message)


def save_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".tmp_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.remove(name)


def json_hash(obj) -> str:
    data = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def file_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atom_entries(path: str | Path) -> list[dict[str, str]]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise DeploymentError(f"XMLを読み取れません: {path}: {exc}") from exc
    demand(root.tag == f"{{{ATOM}}}feed", f"Atom feedではありません: {path}")
    entries = []
    for idx, entry in enumerate(root.findall(f"{{{ATOM}}}entry"), 1):
        props = {}
        for prop in entry.findall(f"{{{APPS}}}property"):
            key = prop.attrib.get("name")
            val = prop.attrib.get("value", "")
            demand(key in KNOWN, f"未知のXML property {key!r}（{idx}件目）")
            demand(key not in props, f"重複XML property {key!r}（{idx}件目）")
            props[key] = val
        demand(bool(props), f"空のfilter（{idx}件目）")
        entries.append(props)
    return entries


def xml_semantic_key(p: dict[str, str]) -> str:
    return json_hash({k: v for k, v in p.items() if k not in {"sizeOperator", "sizeUnit"}})


def check_xml_rules(entries: list[dict[str, str]], allow_spam_exception=True) -> dict:
    keys = [xml_semantic_key(e) for e in entries]
    dup = [k for k, n in Counter(keys).items() if n > 1]
    demand(not dup, f"意味的に重複したXMLフィルタがあります: {len(dup)}種類")
    no_label = 0
    spam_defense = 0
    delete_keys = []
    for i, p in enumerate(entries, 1):
        for k, v in p.items():
            if k.startswith("should"):
                demand(v in {"true", "false"}, f"不正な真偽値: {i}件目 {k}={v!r}")
        demand(not (p.get("shouldAlwaysMarkAsImportant") == "true" and p.get("shouldNeverMarkAsImportant") == "true"), f"重要マークが矛盾: {i}")
        demand(p.get("shouldArchive") != "true", f"自動アーカイブは禁止: {i}")
        label = p.get("label", "")
        if label == "Action/Delete":
            delete_keys.append(xml_semantic_key(p))
            demand(p.get("shouldTrash") != "true", "Action/DeleteがTrashします")
            demand(p.get("shouldMarkAsRead") != "true", "Action/Deleteで既読化する仕様は保留")
        if not label:
            no_label += 1
            demand(allow_spam_exception and p.get("shouldTrash") == "true" and "in:spam" in p.get("hasTheWord", ""), f"通常メール用processing-onlyフィルタ禁止: {i}")
        if p.get("shouldTrash") == "true":
            spam_defense += 1
            demand(allow_spam_exception and not label and "in:spam" in p.get("hasTheWord", ""), f"未承認Trashフィルタ: {i}")
    demand(no_label == spam_defense and spam_defense <= 1, "例外Spam防御フィルタ数が異常です")
    return {"total": len(entries), "duplicates": 0, "trash": spam_defense, "unlabeled": no_label, "action_delete": len(delete_keys)}


def split_or_atoms(expr: str) -> list[str] | None:
    # A safe, limited transformation: XML from="addr OR addr" => Gmail query {from:addr from:addr}.
    # Refuse complex nested/quoted boolean expressions rather than silently changing meaning.
    if " OR " not in expr:
        return None
    choices = expr.split(" OR ")
    demand(all(re.fullmatch(r"[^\s{}()\"']+", x) for x in choices), f"複雑な送信者条件は安全に変換できません: {expr[:120]}")
    return choices


def query_atom(key: str, value: str) -> str:
    return f"{key}:{value}"


def build_template(p: dict[str, str]) -> dict:
    criteria = {}
    fragments = []
    if p.get("from"):
        expr = p["from"].strip()
        choices = split_or_atoms(expr)
        if choices:
            fragments.append("{" + " ".join(query_atom("from", x) for x in choices) + "}")
        else:
            # Gmail API accepts a single address/domain as a direct criteria.
            demand("{" not in expr and "}" not in expr, f"fromの複雑な構文は未対応: {expr[:120]}")
            criteria["from"] = expr
    if p.get("to"):
        criteria["to"] = p["to"].strip()
    if p.get("subject"):
        subj = p["subject"].strip()
        if subj.startswith("{") and subj.endswith("}"):
            fragments.append("subject:" + subj)
        elif " OR " in subj:
            demand(False, f"件名の複雑なOR式は未対応: {subj[:140]}")
        else:
            criteria["subject"] = subj
    if p.get("hasTheWord"):
        fragments.append("(" + p["hasTheWord"].strip() + ")")
    if fragments:
        criteria["query"] = " ".join(fragments)
    if p.get("doesNotHaveTheWord"):
        criteria["negatedQuery"] = p["doesNotHaveTheWord"].strip()
    # Gmail UI exports frequently contain these placeholders, without 'size'.
    # They are UI state, not a size criterion, and must not be submitted to the API.
    if "sizeOperator" in p or "sizeUnit" in p:
        demand(p.get("sizeOperator", "s_sl") == "s_sl" and p.get("sizeUnit", "s_smb") == "s_smb", "明示的なサイズ条件は未対応")
    demand(criteria, "条件なしの全受信メールフィルタは禁止")
    adds, removes = [], []
    if p.get("label"):
        adds.append("label:" + p["label"])
    if p.get("shouldNeverSpam") == "true":
        removes.append("SPAM")
    if p.get("shouldMarkAsRead") == "true":
        removes.append("UNREAD")
    if p.get("shouldNeverMarkAsImportant") == "true":
        removes.append("IMPORTANT")
    if p.get("shouldAlwaysMarkAsImportant") == "true":
        adds.append("IMPORTANT")
    if p.get("shouldStar") == "true":
        adds.append("STARRED")
    if p.get("shouldTrash") == "true":
        adds.append("TRASH")
    demand(len([x for x in adds if x.startswith("label:")]) <= 1, "APIは1フィルタあたりユーザー定義ラベル1個まで")
    demand(not set(adds) & set(removes), "追加と削除のラベルが矛盾します")
    demand(bool(adds or removes), "処理内容なしのフィルタ")
    return {"criteria": criteria, "action": {"addLabelIds": sorted(set(adds)), "removeLabelIds": sorted(set(removes))}}


def canonical_template(obj: dict) -> dict:
    criteria = dict(obj.get("criteria", {}))
    criteria = {k: v.strip() if isinstance(v, str) else v for k, v in criteria.items() if v not in (None, "", False)}
    # API may return all optional empty arrays/false values. Normalize those only.
    action = obj.get("action", {})
    adds = sorted(set(action.get("addLabelIds") or []))
    removes = sorted(set(action.get("removeLabelIds") or []))
    forward = action.get("forward")
    demand(not forward, "予期しない転送フィルタです。自動配備しません")
    demand(not set(adds) & set(removes), "既存フィルタがラベルを追加・削除しています")
    return {"criteria": criteria, "action": {"addLabelIds": adds, "removeLabelIds": removes}}


def compile_xml(path: Path) -> list[dict]:
    entries = atom_entries(path)
    check_xml_rules(entries)
    out = [canonical_template(build_template(p)) for p in entries]
    dups = [k for k, n in Counter(json_hash(x) for x in out).items() if n > 1]
    demand(not dups, f"API変換後に意味的重複: {len(dups)}種類。手動解決が必要")
    return out


def live_as_template(f: dict, names_by_id: dict[str, str]) -> dict:
    c = dict(f.get("criteria", {}))
    a = dict(f.get("action", {}))
    mapped = []
    for lid in a.get("addLabelIds", []):
        if lid in {"IMPORTANT", "STARRED", "TRASH", "INBOX", "UNREAD", "SPAM"}:
            mapped.append(lid)
        else:
            demand(lid in names_by_id, f"不明なGmailラベルID {lid}：ラベル一覧再取得が必要")
            mapped.append("label:" + names_by_id[lid])
    a["addLabelIds"] = mapped
    return canonical_template({"criteria": c, "action": a})


def to_api(t: dict, ids_by_name: dict[str, str]) -> dict:
    t = canonical_template(t)
    a = dict(t["action"])
    new = []
    for x in a["addLabelIds"]:
        if x.startswith("label:"):
            name = x[len("label:"):]
            demand(name in ids_by_name, f"ラベルが未作成です: {name}")
            new.append(ids_by_name[name])
        else:
            new.append(x)
    a["addLabelIds"] = new
    return {"criteria": t["criteria"], "action": a}


def tokens_digest(rows: list[dict]) -> str:
    return json_hash(sorted(json_hash(x) for x in rows))


def counter(rows: list[dict]) -> Counter:
    return Counter(json_hash(x) for x in rows)


def content_by_hash(rows: list[dict]) -> dict:
    return {json_hash(x): x for x in rows}


def delta(before: list[dict], after: list[dict]) -> tuple[list[dict], list[dict]]:
    old, new = counter(before), counter(after)
    o, n = content_by_hash(before), content_by_hash(after)
    adds = [n[k] for k in sorted(new - old) for _ in range((new - old)[k])]
    removes = [item for k in sorted(old - new) for item in [o[k]] * (old - new)[k]]
    return adds, removes


def is_delete(t: dict) -> bool:
    return "label:Action/Delete" in t["action"]["addLabelIds"]


def is_trash(t: dict) -> bool:
    return "TRASH" in t["action"]["addLabelIds"]


def protect(base: list[dict], target: list[dict], actual: list[dict] | None = None) -> None:
    demand(counter([x for x in base if is_delete(x)]) == counter([x for x in target if is_delete(x)]), "Deleteルールがbaseとtargetで異なるため停止")
    demand(counter([x for x in base if is_trash(x)]) == counter([x for x in target if is_trash(x)]), "Trashルールに変化があるため停止")
    if actual is not None:
        demand(counter([x for x in base if is_delete(x)]) == counter([x for x in actual if is_delete(x)]), "Gmail実設定のDeleteがbaseと不一致")
        demand(counter([x for x in base if is_trash(x)]) == counter([x for x in actual if is_trash(x)]), "Gmail実設定のTrashがbaseと不一致")


def get_client(client_secrets: Path, token_path: Path, allow_auth=True):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:
        raise DeploymentError("依存ライブラリが未インストールです: pip install -r requirements.txt") from e
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif allow_auth:
            demand(client_secrets.exists(), f"Google OAuth DesktopクライアントJSONが必要: {client_secrets}")
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
            # Docker: print the URL, bind inside the container, and publish a
            # fixed loopback port on the host. Local (non-Docker) defaults stay unchanged.
            host = os.environ.get("GMAIL_OAUTH_HOST", "localhost")
            bind_addr = os.environ.get("GMAIL_OAUTH_BIND_ADDR")
            port = int(os.environ.get("GMAIL_OAUTH_PORT", "0"))
            open_browser = os.environ.get("GMAIL_OAUTH_OPEN_BROWSER", "1").lower() not in {"0", "false", "no"}
            demand(0 <= port <= 65535, "不正なGMAIL_OAUTH_PORTです")
            creds = flow.run_local_server(
                host=host, bind_addr=bind_addr, port=port,
                open_browser=open_browser, prompt="consent"
            )
        else:
            demand(False, "認証が必要。先に plan を実行してください")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
    return build("gmail", "v1", credentials=creds, cache_discovery=False), creds


def account_email(creds) -> str:
    # Requires only openid/email; avoids scope to read message content.
    try:
        from google.auth.transport.requests import AuthorizedSession
        response = AuthorizedSession(creds).get("https://openidconnect.googleapis.com/v1/userinfo", timeout=20)
        response.raise_for_status()
        data = response.json()
        demand(data.get("email_verified") is True, "Google OAuth emailが未認証")
        return str(data["email"]).lower()
    except Exception as e:
        if isinstance(e, DeploymentError):
            raise
        raise DeploymentError(f"認証アカウントのemailを照合できません: {e}") from e


def api_fetch(client) -> tuple[list[dict], dict[str, str]]:
    labels = client.users().labels().list(userId="me").execute().get("labels", [])
    names_by_id = {x["id"]: x["name"] for x in labels}
    filters = client.users().settings().filters().list(userId="me").execute().get("filter", [])
    demand(len(filters) <= FILTER_LIMIT, f"Gmail側フィルタ数が異常: {len(filters)}")
    normalized = [{"id": x.get("id"), "filter": live_as_template(x, names_by_id)} for x in filters]
    return normalized, {x["name"]: x["id"] for x in labels if x.get("type") == "user" or x.get("type") == "USER"}


def compare_base_live(base: list[dict], target: list[dict], live: list[dict]) -> str:
    if counter(live) == counter(target):
        return "target"
    if counter(live) == counter(base):
        return "baseline"
    return "drift"


def make_plan(base_path: Path, target_path: Path, live: list[dict], acct: str, labels: dict, extra_baselines: list[Path] | None = None) -> dict:
    target = compile_xml(target_path)
    baseline_paths = [base_path] + (extra_baselines or [])
    valid = [(p, compile_xml(p)) for p in baseline_paths]
    status = next(((p, b) for p, b in valid if counter(live) == counter(b)), None)
    if counter(live) == counter(target):
        status = (target_path, target)
    if status is None:
        p0, b0 = valid[0]
        adds, removes = delta(b0, live)
        raise DeploymentError(f"Gmail実設定が既知の基準XMLと一致しません（Gmailのみ: {len(adds)} / 基準のみ: {len(removes)}）。差分を調査し再ベース化してください。自動上書きしません")
    source_path, base = status
    protect(base, target, live)
    adds, removes = delta(base, target)
    demand(not any(is_delete(x) or is_trash(x) for x in adds + removes), "Delete/Trash追加・削除は禁止")
    demand(len(live) + len(adds) <= FILTER_LIMIT, f"一時的な追加後に{len(live)+len(adds)}件になり上限1000超過。安全な順序を再設計してください")
    demand(len(adds) <= 200 and len(removes) <= 30, "差分が想定範囲を超えます。コード側の安全上限を変更する前にレビューしてください")
    existing_labels = set(labels)
    new_labels = sorted(set(x[6:] for t in adds for x in t["action"]["addLabelIds"] if x.startswith("label:")) - existing_labels)
    plan = {
        "schema": "gmail-filters-deploy-plan/v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "account": acct,
        "baseline_path": str(source_path.resolve()),
        "baseline_sha256": file_hash(source_path),
        "target_path": str(target_path.resolve()),
        "target_sha256": file_hash(target_path),
        "baseline_size": len(base),
        "target_size": len(target),
        "live_snapshot_hash": tokens_digest(live),
        "expected_label_names": sorted(existing_labels),
        "new_label_names": new_labels,
        "add": adds,
        "remove": removes,
        "protected_delete_count": len([x for x in base if is_delete(x)]),
        "protected_trash_count": len([x for x in base if is_trash(x)]),
    }
    plan["approval_digest"] = json_hash({k: v for k, v in plan.items() if k not in {"created_utc", "approval_digest"}})
    return plan


def planned_states(plan: dict):
    """Return valid intermediate stages: all additions first, then exact removals."""
    base = compile_xml(Path(plan["baseline_path"]))
    target = compile_xml(Path(plan["target_path"]))
    demand(file_hash(plan["baseline_path"]) == plan["baseline_sha256"], "計画作成後に基準XMLが変わりました")
    demand(file_hash(plan["target_path"]) == plan["target_sha256"], "計画作成後にTarget XMLが変わりました")
    demand(counter(base) + Counter(json_hash(x) for x in plan["add"]) - Counter(json_hash(x) for x in plan["remove"]) == counter(target), "計画ファイルが対象XMLと不一致")
    protect(base, target)
    return base, target


def detect_position(base: list[dict], plan: dict, live: list[dict]) -> tuple[int, int]:
    """Resume only if the live set is exactly one prefix of this plan's ordered operations."""
    cur = counter(base)
    live_counts = counter(live)
    if cur == live_counts:
        return 0, 0
    for i, t in enumerate(plan["add"], 1):
        cur[json_hash(t)] += 1
        if cur == live_counts:
            return i, 0
    for j, t in enumerate(plan["remove"], 1):
        k = json_hash(t)
        demand(cur[k] >= 1, "不正な削除計画")
        cur[k] -= 1
        if cur[k] == 0:
            del cur[k]
        if cur == live_counts:
            return len(plan["add"]), j
    demand(False, "ライブ状態が承認計画の中間状態と一致しません。外部変更や失敗の可能性があります")
    raise AssertionError


def perform(client, plan: dict, expected: str, digest: str, log: Path, batch_size=20):
    demand(expected.lower() == plan["account"].lower(), "対象アカウントが承認計画と不一致")
    demand(digest == plan["approval_digest"], "承認ハッシュが一致しません")
    demand(plan["approval_digest"] == json_hash({k: v for k, v in plan.items() if k not in {"created_utc", "approval_digest"}}), "承認済み計画が改変されています")
    demand(1 <= batch_size <= 25, "batch-sizeは1〜25件で指定してください")
    base, target = planned_states(plan)
    current, labels = api_fetch(client)
    live = [x["filter"] for x in current]
    protect(base, target, live if counter(live) in (counter(base), counter(target)) else None)
    a_done, r_done = detect_position(base, plan, live)
    state = {"account": expected, "plan_digest": digest, "completed_add": a_done, "completed_remove": r_done, "events": []}
    if log.exists():
        previous = json.loads(log.read_text(encoding="utf-8"))
        demand(previous.get("plan_digest") == digest, "他の配備ログが存在します。別パスを指定してください")
        state["events"] = previous.get("events", [])
    save_json(log, state)
    if a_done < len(plan["add"]):
        # Newly introduced labels must exist before creating their filters.
        # Do not delete or alter existing labels.
        for name in plan["new_label_names"]:
            if name not in labels:
                created = client.users().labels().create(userId="me", body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}).execute()
                labels[name] = created["id"]
                state["events"].append({"kind": "create_label", "name": name, "id": created["id"]})
                save_json(log, state)
        for i in range(a_done, len(plan["add"])):
            templ = plan["add"][i]
            result = client.users().settings().filters().create(userId="me", body=to_api(templ, labels)).execute()
            demand(result.get("id"), "フィルタ作成APIからIDが返りませんでした。停止します")
            state["events"].append({"kind": "create_filter", "step": i + 1, "id": result["id"], "sha256": json_hash(templ)})
            state["completed_add"] = i + 1
            save_json(log, state)
            # Confirm via a fresh live list after each small batch.
            if (i + 1) % batch_size == 0 or (i + 1) == len(plan["add"]):
                verify_partial(client, base, plan, state["completed_add"], state["completed_remove"])
                print(f"追加済み: {i+1}/{len(plan['add'])}件、Gmail再取得一致")
    # All additions have been read back before any old filters are deleted.
    for j in range(r_done, len(plan["remove"])):
        current, _ = api_fetch(client)
        old = plan["remove"][j]
        matches = [x for x in current if x["filter"] == old]
        demand(len(matches) == 1, f"削除対象が一意ではありません: {json_hash(old)} ({len(matches)}件)")
        demand(not is_delete(old) and not is_trash(old), "保護されたフィルタは削除できません")
        client.users().settings().filters().delete(userId="me", id=matches[0]["id"]).execute()
        state["events"].append({"kind": "delete_filter", "step": j+1, "id": matches[0]["id"], "sha256": json_hash(old)})
        state["completed_remove"] = j+1
        save_json(log, state)
        verify_partial(client, base, plan, len(plan["add"]), j+1)
        print(f"旧ルール削除済み: {j+1}/{len(plan['remove'])}")
    verify_partial(client, base, plan, len(plan["add"]), len(plan["remove"]))
    demand(counter([x["filter"] for x in api_fetch(client)[0]]) == counter(target), "配備後の最終照合が一致しません")
    state["verified"] = True
    save_json(log, state)
    print(f"配備完了: {len(target)}件、API読戻し一致。Gmail UI exportも別途取得して照合してください")


def verify_partial(client, base: list[dict], plan: dict, add_count: int, remove_count: int):
    expected = counter(base)
    for x in plan["add"][:add_count]:
        expected[json_hash(x)] += 1
    for x in plan["remove"][:remove_count]:
        k = json_hash(x)
        demand(expected[k] > 0, "内部削除カウンタ異常")
        expected[k] -= 1
        if expected[k] == 0:
            del expected[k]
    current, _ = api_fetch(client)
    actual = counter([x["filter"] for x in current])
    demand(expected == actual, f"API読戻しが不一致。expected={sum(expected.values())} / actual={sum(actual.values())}。後続操作を停止")
    return True


def cmd_validate(args):
    base = compile_xml(args.base)
    target = compile_xml(args.target)
    protect(base, target)
    added, removed = delta(base, target)
    demand(not any(is_delete(x) or is_trash(x) for x in added+removed), "Action/DeleteまたはTrashの変更が検出されました")
    report = {
        "base": str(args.base), "base_sha256": file_hash(args.base),
        "target": str(args.target), "target_sha256": file_hash(args.target),
        "base_health": check_xml_rules(atom_entries(args.base)),
        "target_health": check_xml_rules(atom_entries(args.target)),
        "api_compatible_compilation": True,
        "add": len(added), "remove": len(removed),
        "expected_intermediate_count": len(base)+len(added),
        "within_1000_limit": len(base)+len(added) <= FILTER_LIMIT,
        "changes_delete": False,
        "changes_trash": False,
    }
    demand(report["within_1000_limit"], "追加先行でフィルタ数が1000を超えます")
    if args.out:
        save_json(args.out, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def cmd_plan(args):
    client, creds = get_client(args.client_secrets, args.token)
    acct = account_email(creds)
    demand(acct == args.expected_email.lower(), f"Googleアカウント不一致: {acct} != {args.expected_email}")
    current, labels = api_fetch(client)
    plan = make_plan(args.base, args.target, [x["filter"] for x in current], acct, labels, args.also_baseline or [])
    save_json(args.out, plan)
    print(json.dumps({"account": acct, "current": len(current), "target": plan["target_size"], "add": len(plan["add"]), "remove": len(plan["remove"]), "new_labels": plan["new_label_names"], "approval_digest": plan["approval_digest"], "plan_file": str(args.out)}, ensure_ascii=False, indent=2))
    print("これはドライランです。Gmailの設定は変更していません")


def cmd_apply(args):
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    client, creds = get_client(args.client_secrets, args.token, allow_auth=False)
    acct = account_email(creds)
    demand(acct == args.expected_email.lower(), f"認証アカウントが違います: {acct}")
    perform(client, plan, acct, args.approve, args.log, args.batch_size)


def cmd_verify(args):
    client, creds = get_client(args.client_secrets, args.token, allow_auth=False)
    acct = account_email(creds)
    demand(acct == args.expected_email.lower(), "認証アカウント不一致")
    target = compile_xml(args.target)
    current, _ = api_fetch(client)
    actual = [x["filter"] for x in current]
    adds, removes = delta(target, actual)
    report = {"account": acct, "target": len(target), "live": len(actual), "live_only": len(adds), "master_only": len(removes), "match": not adds and not removes}
    if args.out:
        save_json(args.out, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    demand(report["match"], "現行フィルタはMasterと一致しません")


def main(argv=None):
    root = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="安全なGmail filter XML/API配備ツール（標準は読み取り専用）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # Production masters are intentionally never bundled with the public project.
    default_base = root / "local" / "baseline.xml"
    default_target = root / "local" / "target.xml"
    p = sub.add_parser("validate", help="完全オフライン: XMLとDelete保護・API変換を検査")
    p.add_argument("--base", type=Path, default=default_base)
    p.add_argument("--target", type=Path, default=default_target)
    p.add_argument("--out", type=Path)
    p.set_defaults(func=cmd_validate)
    p = sub.add_parser("plan", help="Gmail設定読取・差分計画生成のみ、書込なし")
    p.add_argument("--base", type=Path, default=default_base)
    p.add_argument("--also-baseline", type=Path, action="append", help="既知の中間master（例 v0.13）")
    p.add_argument("--target", type=Path, default=default_target)
    p.add_argument("--out", type=Path, default=root / "local" / "plan.json")
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser("apply", help="承認hashが必要。追加・確認後に旧フィルタ削除")
    p.add_argument("--plan", type=Path, default=root / "local" / "plan.json")
    p.add_argument("--approve", required=True, help="plan出力のapproval_digestを完全一致で指定")
    p.add_argument("--log", type=Path, default=root / "local" / "deploy_log.json")
    p.add_argument("--batch-size", type=int, default=20)
    p.set_defaults(func=cmd_apply)
    p = sub.add_parser("verify", help="現行APIルールとMasterの完全照合（読取専用）")
    p.add_argument("--target", type=Path, default=default_target)
    p.add_argument("--out", type=Path, default=root / "local" / "verify.json")
    p.set_defaults(func=cmd_verify)
    for name in ("plan", "apply", "verify"):
        p = sub.choices[name]
        p.add_argument("--expected-email", required=True, help="一致が必要なGmailアドレス")
        p.add_argument("--client-secrets", type=Path, default=root / "local" / "credentials.json")
        p.add_argument("--token", type=Path, default=root / "local" / "token.json")
    args = ap.parse_args(argv)
    try:
        args.func(args)
    except DeploymentError as exc:
        print("STOP: " + str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print("STOP: 予期しないエラーです。途中まで反映された可能性があります。planを再実行せず状態照合してください: " + str(exc), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())