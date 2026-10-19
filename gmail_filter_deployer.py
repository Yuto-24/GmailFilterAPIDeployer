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


def select_master(local_dir: Path, explicit: Path | None = None) -> Path:
    """Choose latest numbered master by version/date; ignore patch and export files."""
    if explicit is not None:
        demand(explicit.is_file(), f"指定されたMaster XMLがありません: {explicit}")
        return explicit
    candidates = []
    for directory in (local_dir, local_dir / "masters"):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.xml"):
            stem = path.stem.lower()
            version = re.search(r"(?:^|[_-])v(\d+)\.(\d+)\.(\d+)(?:[_-]|$)", stem)
            if not version or "master" not in stem:
                continue
            if any(word in stem for word in ("patch", "export", "cumulative", "incremental", "baseline", "target")):
                continue
            dates = re.findall(r"(?<!\d)(20\d{6})(?!\d)", stem)
            score = (tuple(int(x) for x in version.groups()), max(dates) if dates else "")
            candidates.append((score, path))
    if not candidates:
        legacy = local_dir / "target.xml"
        demand(legacy.is_file(), f"Master XMLがありません。{local_dir}/masters/ に mailFilters_vX.Y.Z_*master*.xml を配置してください")
        return legacy
    candidates.sort(key=lambda x: x[0], reverse=True)
    leaders = [path for score, path in candidates if score == candidates[0][0]]
    demand(len(leaders) == 1, "同一バージョン・同一日付のMaster XMLが複数あります。--targetで指定してください: " + ", ".join(str(x) for x in leaders))
    return leaders[0]


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
            demand(all(p.get(flag) != "true" for flag in (
                "shouldTrash", "shouldMarkAsRead", "shouldStar",
                "shouldNeverSpam", "shouldNeverMarkAsImportant",
                "shouldAlwaysMarkAsImportant")), "Action/Deleteはラベル付与のみ許可されます")
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


def validate_delete_label_only(target: list[dict]) -> None:
    for row in target:
        if is_delete(row):
            demand(row["action"]["addLabelIds"] == ["label:Action/Delete"]
                   and not row["action"]["removeLabelIds"],
                   "Action/Deleteはラベル付与のみ許可されます（Trash・既読化等は禁止）")


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


def make_plan(base_path: Path | None, target_path: Path, live: list[dict], acct: str, labels: dict,
              extra_baselines: list[Path] | None = None) -> dict:
    """Build a pinned, fully reviewed diff from a live API snapshot by default."""
    target = compile_xml(target_path)
    validate_delete_label_only(target)
    if base_path is None:
        demand(not extra_baselines, "--also-baselineは--baseとともに指定してください")
        source_path = None
        base = list(live)
    else:
        baseline_paths = [base_path] + (extra_baselines or [])
        valid = [(p, compile_xml(p)) for p in baseline_paths]
        status = next(((p, b) for p, b in valid if counter(live) == counter(b)), None)
        if counter(live) == counter(target):
            status = (target_path, target)
        if status is None:
            p0, b0 = valid[0]
            adds, removes = delta(b0, live)
            raise DeploymentError(f"Gmail実設定が既知の基準XMLと一致しません（Gmailのみ: {len(adds)} / 基準のみ: {len(removes)}）。自動上書きしません")
        source_path, base = status
    adds, removes = delta(base, target)
    demand(all(x["action"]["addLabelIds"] == ["label:Action/Delete"] and
               not x["action"]["removeLabelIds"] for x in adds if is_delete(x)),
           "追加対象のAction/Deleteにラベル以外の処理が含まれています")
    demand(len(live) + len(adds) <= FILTER_LIMIT, f"追加先行時に{len(live)+len(adds)}件となり上限1000超過")
    existing_labels = set(labels)
    new_labels = sorted(set(x[6:] for t in adds for x in t["action"]["addLabelIds"] if x.startswith("label:")) - existing_labels)
    plan = {
        "schema": "gmail-filters-deploy-plan/v3",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "account": acct,
        "baseline_path": str(source_path.resolve()) if source_path else None,
        "baseline_sha256": file_hash(source_path) if source_path else None,
        "baseline_snapshot": base,
        "target_path": str(target_path.resolve()),
        "target_sha256": file_hash(target_path),
        "baseline_size": len(base),
        "target_size": len(target),
        "live_snapshot_hash": tokens_digest(live),
        "expected_label_names": sorted(existing_labels),
        "new_label_names": new_labels,
        "add": adds,
        "remove": removes,
        "current_delete_count": len([x for x in base if is_delete(x)]),
        "target_delete_count": len([x for x in target if is_delete(x)]),
        "current_trash_count": sum(is_trash(x) for x in base),
        "target_trash_count": sum(is_trash(x) for x in target),
        "trash_add_count": sum(is_trash(x) for x in adds),
        "trash_remove_count": sum(is_trash(x) for x in removes),
    }
    plan["approval_digest"] = json_hash({k: v for k, v in plan.items() if k not in {"created_utc", "approval_digest"}})
    return plan


def planned_states(plan: dict):
    """Pin actual baseline and target to the approved plan, including resume."""
    target = compile_xml(Path(plan["target_path"]))
    validate_delete_label_only(target)
    demand(file_hash(plan["target_path"]) == plan["target_sha256"], "計画作成後にTarget XMLが変わりました")
    source = plan.get("baseline_path")
    if source:
        base = compile_xml(Path(source))
        demand(file_hash(source) == plan["baseline_sha256"], "計画作成後に基準XMLが変わりました")
    else:
        demand(plan.get("schema") == "gmail-filters-deploy-plan/v3", "旧版の配備計画は実行できません。実Gmailの状態を確認し、新しいrunで計画を作り直してください")
        base = [canonical_template(x) for x in plan["baseline_snapshot"]]
    demand(counter(base) == counter(plan["baseline_snapshot"]), "プランの基準スナップショットが変わりました")
    demand(tokens_digest(base) == plan["live_snapshot_hash"], "基準スナップショットのハッシュが不一致")
    demand(plan.get("schema") == "gmail-filters-deploy-plan/v3",
           "旧形式の配備計画は適用できません。途中状態を手動照合してください")
    demand(counter(base) + Counter(json_hash(x) for x in plan["add"]) - Counter(json_hash(x) for x in plan["remove"]) == counter(target),
           "計画とMasterの差分が不一致")
    demand(plan["current_delete_count"] == sum(is_delete(x) for x in base)
           and plan["target_delete_count"] == sum(is_delete(x) for x in target), "Delete件数の記録が改変されました")
    demand(plan.get("current_trash_count") == sum(is_trash(x) for x in base) and
           plan.get("target_trash_count") == sum(is_trash(x) for x in target) and
           plan.get("trash_add_count") == sum(is_trash(x) for x in plan["add"]) and
           plan.get("trash_remove_count") == sum(is_trash(x) for x in plan["remove"]),
           "Trash差分の承認計画が改変されています")
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


def perform(client, plan: dict, expected: str, digest: str, log: Path, batch_size=20, verbose=False):
    demand(expected.lower() == plan["account"].lower(), "対象アカウントが承認計画と不一致")
    demand(digest == plan["approval_digest"], "承認ハッシュが一致しません")
    demand(plan["approval_digest"] == json_hash({k: v for k, v in plan.items() if k not in {"created_utc", "approval_digest"}}), "承認済み計画が改変されています")
    demand(1 <= batch_size <= 25, "batch-sizeは1〜25件で指定してください")
    base, target = planned_states(plan)
    current, labels = api_fetch(client)
    live = [x["filter"] for x in current]
    a_done, r_done = detect_position(base, plan, live)
    print(f"配備開始: 追加 {len(plan['add'])}件（完了 {a_done}件） / 削除 {len(plan['remove'])}件（完了 {r_done}件）", flush=True)
    if verbose:
        print(f"[詳細] 既存フィルタ照合: {len(live)}件一致 / 変更対象Master: {plan['target_path']}", flush=True)
        print(f"[詳細] 進捗記録: {log}", flush=True)
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
                if verbose:
                    print(f"[詳細] ラベル作成: {name}", flush=True)
                state["events"].append({"kind": "create_label", "name": name, "id": created["id"]})
                save_json(log, state)
        for i in range(a_done, len(plan["add"])):
            templ = plan["add"][i]
            result = client.users().settings().filters().create(userId="me", body=to_api(templ, labels)).execute()
            demand(result.get("id"), "フィルタ作成APIからIDが返りませんでした。停止します")
            state["events"].append({"kind": "create_filter", "step": i + 1, "id": result["id"], "sha256": json_hash(templ)})
            state["completed_add"] = i + 1
            save_json(log, state)
            if verbose:
                print(f"[詳細] 追加 [{i+1}/{len(plan['add'])}] id={result['id']} {format_filter_summary(templ)}", flush=True)
            # Confirm via a fresh live list after each small batch.
            if (i + 1) % batch_size == 0 or (i + 1) == len(plan["add"]):
                if verbose:
                    print(f"[詳細] Gmail API再取得・追加フィルタ照合中: {i+1}/{len(plan['add'])}", flush=True)
                verify_partial(client, base, plan, state["completed_add"], state["completed_remove"])
                print(f"追加済み: {i+1}/{len(plan['add'])}件、Gmail再取得一致", flush=True)
    # All additions have been read back before any old filters are deleted.
    for j in range(r_done, len(plan["remove"])):
        current, _ = api_fetch(client)
        old = plan["remove"][j]
        matches = [x for x in current if x["filter"] == old]
        demand(len(matches) == 1, f"削除対象が一意ではありません: {json_hash(old)} ({len(matches)}件)")
        client.users().settings().filters().delete(userId="me", id=matches[0]["id"]).execute()
        state["events"].append({"kind": "delete_filter", "step": j+1, "id": matches[0]["id"], "sha256": json_hash(old)})
        state["completed_remove"] = j+1
        save_json(log, state)
        if verbose:
            print(f"[詳細] 削除 [{j+1}/{len(plan['remove'])}] id={matches[0]['id']} {format_filter_summary(old)}", flush=True)
        verify_partial(client, base, plan, len(plan["add"]), j+1)
        print(f"旧ルール削除済み: {j+1}/{len(plan['remove'])}", flush=True)
    if verbose:
        print("[詳細] 最終API照合中（配備後の全フィルタ）", flush=True)
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
    target_path = select_master(args.local_dir, args.target)
    target = compile_xml(target_path)
    report = {
        "target": str(target_path), "target_sha256": file_hash(target_path),
        "target_health": check_xml_rules(atom_entries(target_path)),
        "api_compatible_compilation": True,
        "action_delete_label_only": True,
    }
    if args.base:
        base = compile_xml(args.base)
        added, removed = delta(base, target)
        demand(len(base) + len(added) <= FILTER_LIMIT, "追加先行時にGmail上限1000件を超えます")
        report.update({"base": str(args.base), "base_sha256": file_hash(args.base),
                       "base_health": check_xml_rules(atom_entries(args.base)),
                       "add": len(added), "remove": len(removed),
                       "expected_intermediate_count": len(base)+len(added),
                       "delete_add": sum(is_delete(x) for x in added),
                       "delete_remove": sum(is_delete(x) for x in removed),
                       "trash_add": sum(is_trash(x) for x in added),
                       "trash_remove": sum(is_trash(x) for x in removed)})
    if args.out:
        save_json(args.out, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def cmd_plan(args):
    target_path = select_master(args.local_dir, args.target)
    client, creds = get_client(args.client_secrets, args.token)
    acct = account_email(creds)
    demand(acct == args.expected_email.lower(), f"Googleアカウント不一致: {acct} != {args.expected_email}")
    current, labels = api_fetch(client)
    plan = make_plan(args.base, target_path, [x["filter"] for x in current], acct, labels, args.also_baseline or [])
    save_json(args.out, plan)
    print(json.dumps({"account": acct, "current": len(current), "target": plan["target_size"],
                      "master": str(target_path), "add": len(plan["add"]), "remove": len(plan["remove"]),
                      "new_labels": plan["new_label_names"], "approval_digest": plan["approval_digest"],
                      "plan_file": str(args.out), "requires_allow_removals": bool(plan["remove"]),
                      "delete_add": sum(is_delete(x) for x in plan["add"]),
                      "delete_remove": sum(is_delete(x) for x in plan["remove"]),
                      "trash_add": plan["trash_add_count"],
                      "trash_remove": plan["trash_remove_count"]}, ensure_ascii=False, indent=2))
    print("ドライランのみ。Gmailは変更していません。削除対象はplan.jsonで必ず確認してください")


def cmd_apply(args):
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    client, creds = get_client(args.client_secrets, args.token, allow_auth=False)
    acct = account_email(creds)
    demand(acct == args.expected_email.lower(), f"認証アカウントが違います: {acct}")
    demand(not plan["remove"] or args.allow_removals,
           f"旧フィルタ{len(plan['remove'])}件の削除予定があります。plan.jsonを確認し、承認する場合だけ--allow-removalsを指定してください")
    perform(client, plan, acct, args.approve, args.log, args.batch_size, verbose=args.verbose)


def cmd_verify(args):
    client, creds = get_client(args.client_secrets, args.token, allow_auth=False)
    acct = account_email(creds)
    demand(acct == args.expected_email.lower(), "認証アカウント不一致")
    target = compile_xml(select_master(args.local_dir, args.target))
    current, _ = api_fetch(client)
    actual = [x["filter"] for x in current]
    adds, removes = delta(target, actual)
    report = {"account": acct, "target": len(target), "live": len(actual),
              "live_only": len(adds), "master_only": len(removes), "match": not adds and not removes,
              "delete_live": sum(is_delete(x) for x in actual),
              "delete_master": sum(is_delete(x) for x in target)}
    if args.out:
        save_json(args.out, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    demand(report["match"], "現行フィルタはMasterと一致しません")


def format_filter_summary(row: dict) -> str:
    criteria = json.dumps(row["criteria"], ensure_ascii=False, sort_keys=True)
    action = json.dumps(row["action"], ensure_ascii=False, sort_keys=True)
    return f"条件={criteria} / 処理={action}"


def show_run_plan(plan: dict, plan_path: Path) -> None:
    """Preview EVERY change before a single final confirmation."""
    adds, removes = plan["add"], plan["remove"]
    print(f"\nGmailフィルタ配備計画: {plan['account']}")
    print(f"Master: {plan['target_path']}")
    print(f"現行 {plan['baseline_size']}件 → Master {plan['target_size']}件")
    print(f"追加 {len(adds)}件 / 既存ルール削除 {len(removes)}件 / 新規ラベル {len(plan['new_label_names'])}件")
    print(f"Action/Delete: 現行 {plan['current_delete_count']}件 → Master {plan['target_delete_count']}件")
    print(f"Action/Deleteの追加 {sum(is_delete(x) for x in adds)}件 / 削除 {sum(is_delete(x) for x in removes)}件")
    print(f"実際のゴミ箱移動（TRASH）: 現行 {plan['current_trash_count']}件 → Master {plan['target_trash_count']}件")
    print(f"TRASHアクションの追加 {plan['trash_add_count']}件 / 削除 {plan['trash_remove_count']}件")
    if plan['trash_add_count'] or plan['trash_remove_count']:
        print("\n【重要：ゴミ箱移動アクションの変更があります】")
        print("  TRASHへの追加は、条件に一致する今後のメールを実際にゴミ箱へ移動させます。")
        print("  以下の全フィルタの条件と処理を確認してください。")
    if plan['new_label_names']:
        print("\n【新規作成するラベル：全件】")
        for name in plan['new_label_names']:
            print("  + " + name)
    # Group unambiguous replacements by identical resulting action/label.
    # Do not guess when multiple distinct rules share one action.
    from collections import defaultdict
    by_action_add = defaultdict(list)
    by_action_remove = defaultdict(list)
    for row in adds:
        by_action_add[json_hash(row["action"])].append(row)
    for row in removes:
        by_action_remove[json_hash(row["action"])].append(row)
    replaced_add, replaced_remove, replacements = set(), set(), []
    for key in sorted(set(by_action_add) & set(by_action_remove)):
        old, new = by_action_remove[key], by_action_add[key]
        if len(old) == len(new) == 1:
            replaced_remove.add(json_hash(old[0]))
            replaced_add.add(json_hash(new[0]))
            replacements.append((old[0], new[0]))
    print(f"\n【条件変更（旧→新）：{len(replacements)}組】")
    for i, (old, new) in enumerate(replacements, 1):
        print(f"  ~ {i}. 旧 {format_filter_summary(old)}")
        print(f"         新 {format_filter_summary(new)}")
    print(f"\n【削除する既存フィルタ：{len(removes) - len(replaced_remove)}件】")
    for i, row in enumerate((x for x in removes if json_hash(x) not in replaced_remove), 1):
        print(f"  - {i}. {format_filter_summary(row)}")
    print(f"\n【追加するフィルタ：{len(adds) - len(replaced_add)}件】")
    for i, row in enumerate((x for x in adds if json_hash(x) not in replaced_add), 1):
        print(f"  + {i}. {format_filter_summary(row)}")
    print(f"\n配備計画全文: {plan_path}")
    print("Master XMLに合わせて上記を追加・削除・置換します。")
    print("Action/DeleteはGmailラベルの付与であり、メールをゴミ箱には移動しません。")


def confirm_interactive(prompt: str = "上記の変更をGmailに反映します。進めていいですか？ [y/N]: ") -> bool:
    # No implicit approval through piped input or background/scheduled runs.
    demand(sys.stdin.isatty(), "対話端末が必要です。docker compose run（-Tなし）で実行してください")
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nキャンセルしました。Gmailは変更していません。")
        return False
    return answer in {"y", "yes"}


def read_gmail_filters_preflight(client):
    """Report API configuration/read errors before any Gmail writes can occur."""
    try:
        return api_fetch(client)
    except Exception as exc:
        message = str(exc)
        if "accessNotConfigured" in message or "Gmail API has not been used" in message:
            project_match = re.search(r"project\s+(\d+)", message)
            project = project_match.group(1) if project_match else None
            url = "https://console.cloud.google.com/apis/library/gmail.googleapis.com"
            if project:
                url += f"?project={project}"
            raise DeploymentError(
                "Google Cloud側でGmail APIが無効です。次のURLでGmail APIを有効化してください: "
                f"{url}。有効化後は同じコマンドで再実行できます。"
                "エラーはフィルタの読取段階で発生しており、Gmail設定は一切変更していません。"
            ) from exc
        raise DeploymentError(
            "Gmailのラベル・フィルタ取得に失敗しました。"
            "配備前の読取段階で停止したため、Gmail設定は変更していません。"
            f" 原因: {message}"
        ) from exc


def cmd_run(args):
    """One command: auth, preflight, plan, interactive consent, apply, verify.

    A pending approved plan is resumed rather than replanning from a partial
    live state. Separate per-plan journals allow indefinitely many releases.
    """
    from contextlib import contextmanager

    @contextmanager
    def exclusive_lock(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+b') as lockfile:
            try:
                import fcntl
                fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeploymentError("別のrunが進行中です。二重実行を防ぐため停止しました") from exc
            except ImportError:
                # Docker/Linux is the reference runtime; Windows native falls
                # back to API state verification and must not be used concurrently.
                pass
            try:
                yield
            finally:
                try:
                    import fcntl
                    fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)
                except ImportError:
                    pass

    with exclusive_lock(args.local_dir / 'run.lock'):
        print("[1/6] 最新Master XMLを選択・検証中...", flush=True)
        target_path = select_master(args.local_dir, args.target)
        target = compile_xml(target_path)  # Offline preflight before OAuth.
        print(f"      検証完了: {target_path.name}（{len(target)}件）", flush=True)
        if args.verbose:
            print(f"[詳細] Master SHA-256: {file_hash(target_path)}", flush=True)
        print("[2/6] Google OAuth認証とアカウント照合中...", flush=True)
        client, creds = get_client(args.client_secrets, args.token)
        acct = account_email(creds)
        demand(acct == args.expected_email, f"認証Googleアカウント不一致: {acct} != {args.expected_email}")
        print(f"      認証済み: {acct}", flush=True)
        print("[3/6] Gmailの現行ラベル・フィルタを取得中...", flush=True)
        current, labels = read_gmail_filters_preflight(client)
        live = [x['filter'] for x in current]
        print(f"      取得完了: {len(live)}フィルタ / {len(labels)}ラベル", flush=True)
        print("[4/6] 変更差分と保護ルールを照合中...", flush=True)
        pending_file = args.local_dir / 'run_pending.json'
        plan_file = args.local_dir / 'run_plan.json'
        if pending_file.exists():
            plan = json.loads(pending_file.read_text(encoding='utf-8'))
            demand(plan.get('schema') == 'gmail-filters-deploy-plan/v3', '旧版の配備計画が残っています。以前の配備状態を確認してから手動で整理してください')
            demand(plan['account'] == acct, '途中計画のアカウントが一致しません')
            demand(plan['target_path'] == str(target_path.resolve()), '途中計画の対象Masterと現行Masterが異なります。再開を停止しました')
            base, old_target = planned_states(plan)
            demand(file_hash(target_path) == plan["target_sha256"], '途中計画のMasterが変更されています')
            a_done, r_done = detect_position(base, plan, live)
            if a_done == len(plan['add']) and r_done == len(plan['remove']):
                print(f"前回の配備は既に反映済みです。MasterとAPI一致: {len(old_target)}件", flush=True)
                pending_file.unlink()
                return
            print(f"前回の中断計画を検出しました（追加済 {a_done}/{len(plan['add'])}、削除済 {r_done}/{len(plan['remove'])}）。")
        else:
            plan = make_plan(None, target_path, live, acct, labels)
            if not plan['add'] and not plan['remove']:
                print(f"変更不要：{acct} の{len(target)}フィルタが最新Masterに一致しています。", flush=True)
                return
            save_json(plan_file, plan)
        if args.verbose:
            print(f"[詳細] 追加 {len(plan['add'])}件 / 削除 {len(plan['remove'])}件 / 新規ラベル {len(plan['new_label_names'])}件", flush=True)
            print(f"[詳細] 計画SHA-256: {plan['approval_digest']}", flush=True)
        # The full plan is always available before accepting any modification.
        show_run_plan(plan, plan_file if not pending_file.exists() else pending_file)
        print("[5/6] 事前確認完了。上記の全変更内容を確認してください。", flush=True)
        if not confirm_interactive():
            print("承認されなかったためGmailは変更していません。")
            return
        if not pending_file.exists():
            save_json(pending_file, plan)  # Write before any API mutation.
        journal = args.local_dir / 'logs' / (plan['approval_digest'] + '.json')
        print("[6/6] Gmailへ配備し、APIの読戻しで検証中...", flush=True)
        perform(client, plan, acct, plan['approval_digest'], journal, args.batch_size, verbose=args.verbose)
        pending_file.unlink()
        print("API照合完了。Gmail UIのfresh exportと新着メール分類の確認は別途必要です。", flush=True)


def resolve_expected_email(cli_email: str | None) -> str:
    """Require an account pin, preferring the CLI flag over environment."""
    configured = cli_email if cli_email is not None else os.environ.get("GMAIL_EXPECTED_EMAIL", "")
    email = configured.strip().lower()
    demand(bool(email), "対象メールアドレスが未設定です。.env の GMAIL_EXPECTED_EMAIL を設定するか --expected-email を指定してください")
    demand(bool(re.fullmatch(r"[^@\s]+@[^@\s]+", email)), "--expected-email / GMAIL_EXPECTED_EMAIL の形式が不正です")
    return email


def main(argv=None):
    root = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="安全なGmail filter XML/API配備ツール（標準は読み取り専用）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # No renaming: latest versioned master is discovered in local/masters/ or local/.
    default_local = root / "local"
    p = sub.add_parser("run", help="検証→差分表示→対話承認→配備→API照合を1コマンドで実行")
    p.add_argument("--target", type=Path, help="任意: デフォルトは最新Masterを自動選択")
    p.add_argument("--batch-size", type=int, default=20, help="API読戻しの間隔。1〜25、既定20")
    p.add_argument("-v", "--verbose", action="store_true", help="フィルタ単位の処理・API照合の詳細を表示")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("validate", help="オフライン: 最新Master XMLを検証（--baseは任意）")
    p.add_argument("--base", type=Path, help="任意の厳格な比較元XML")
    p.add_argument("--target", type=Path, help="指定しなければ最新のversioned masterを選ぶ")
    p.add_argument("--out", type=Path)
    p.set_defaults(func=cmd_validate)
    p = sub.add_parser("plan", help="Gmailの実設定から差分計画を生成。読み取り専用")
    p.add_argument("--base", type=Path, help="任意: 既知MasterとGmail実設定が一致するか厳格照合")
    p.add_argument("--also-baseline", type=Path, action="append", help="--base指定時のみ追加の既知Master")
    p.add_argument("--target", type=Path, help="指定しなければ最新版を自動選択")
    p.add_argument("--out", type=Path, default=default_local / "plan.json")
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser("apply", help="承認ハッシュ必須。旧ルール削除には別途--allow-removals")
    p.add_argument("--plan", type=Path, default=default_local / "plan.json")
    p.add_argument("--approve", required=True, help="planが表示したapproval_digest")
    p.add_argument("--allow-removals", action="store_true", help="既存非保護ルールの削除を明示的に承認")
    p.add_argument("--log", type=Path, default=default_local / "deploy_log.json")
    p.add_argument("--batch-size", type=int, default=20)
    p.add_argument("-v", "--verbose", action="store_true", help="フィルタ単位の配備・API照合詳細を表示")
    p.set_defaults(func=cmd_apply)
    p = sub.add_parser("verify", help="Gmail API実設定と最新版Masterの完全照合")
    p.add_argument("--target", type=Path, help="指定しなければ最新版を自動選択")
    p.add_argument("--out", type=Path, default=default_local / "verify.json")
    p.set_defaults(func=cmd_verify)
    for name in ("validate", "plan", "apply", "verify", "run"):
        sub.choices[name].add_argument("--local-dir", type=Path, default=default_local,
                                       help="Master XML保存フォルダ。既定はlocal/")
    for name in ("plan", "apply", "verify", "run"):
        p = sub.choices[name]
        p.add_argument("--expected-email", help="対象Gmailアドレス。省略時はGMAIL_EXPECTED_EMAILを使用")
        p.add_argument("--client-secrets", type=Path, default=default_local / "credentials.json")
        p.add_argument("--token", type=Path, default=default_local / "token.json")
    args = ap.parse_args(argv)
    try:
        if args.cmd in ("plan", "apply", "verify", "run"):
            args.expected_email = resolve_expected_email(args.expected_email)
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