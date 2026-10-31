#!/usr/bin/env python3
"""Fail-closed Gmail Atom XML -> Gmail API filter deployer.

Runs fully offline for `validate` and `diff-xml`.
`plan`, `apply`, `verify` require a locally authenticated Gmail API session.
Message reclassification is separately approved; Action/Delete never deletes mail.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import random
import sys
import tempfile
import time
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
QUERY_V1_NORMALIZATION = "query-v1"
CURRENT_NORMALIZATION = "query-v2"
SUPPORTED_NORMALIZATIONS = ("legacy", QUERY_V1_NORMALIZATION, CURRENT_NORMALIZATION)
CURRENT_COMPILATION = "xml-fields-v1"
LEGACY_PLAN_SCHEMAS = ("gmail-filters-deploy-plan/v3", "gmail-filters-deploy-plan/v4")
QUERY_V1_PLAN_SCHEMA = "gmail-filters-deploy-plan/v5"
NATIVE_V1_PLAN_SCHEMA = "gmail-filters-deploy-plan/v6"
CURRENT_PLAN_SCHEMA = "gmail-filters-deploy-plan/v7"
SENDER_ADDRESS = r"[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,63}"
FROM_ATOM = "from:" + SENDER_ADDRESS


class DeploymentError(Exception):
    pass


# Only these fixed categories are aggregated; labels, IDs and rule content never
# become metric keys. Timings are overlapping views, not additive totals.
API_KINDS = ("labels.list", "filters.list", "labels.create", "filters.create", "filters.delete",
             "filters.batch.create", "filters.batch.delete", "messages.list", "messages.get",
             "messages.batchModify", "history.list", "getProfile")
OBSERVATION = ContextVar("deployment_observation", default=None)


class Observation:
    """Keep overlapping timing views and API counters for one command invocation."""

    def __init__(self, command, verbose=False):
        """Start an isolated measurement with no previous command's counters."""
        self.command = command
        self.verbose = verbose
        self.started = time.perf_counter()
        self.approval_wait = 0.0
        self.deployment_seconds = 0.0
        self.phase = None
        self.apis = {}
        self.batch_items = {}
        self.outcome = "完了"

    def finish_phase(self, status="完了"):
        """Close the active phase once and retain its deployment duration if applicable."""
        if self.phase is None:
            return
        label, started, deployment = self.phase
        seconds = time.perf_counter() - started
        if deployment:
            self.deployment_seconds += seconds
        if self.verbose:
            print(f"{timestamp_prefix()}[フェーズ{status}] {label} ({seconds:.2f}s)", flush=True)
        self.phase = None

    def start_phase(self, label, *, deployment=False):
        """Finish the previous phase before opening the next monotonic interval."""
        self.finish_phase()
        self.phase = (label, time.perf_counter(), deployment)
        if self.verbose:
            print(f"{timestamp_prefix()}[フェーズ開始] {label}", flush=True)

    def record_api(self, kind, seconds, succeeded):
        """Aggregate one API attempt under a fixed key, never a private operation label."""
        if kind not in API_KINDS:
            return
        row = self.apis.setdefault(kind, {"attempts": 0, "success": 0, "failure": 0,
                                          "seconds": 0.0, "max_seconds": 0.0})
        row["attempts"] += 1
        row["success" if succeeded else "failure"] += 1
        row["seconds"] += seconds
        row["max_seconds"] = max(row["max_seconds"], seconds)

    def report(self):
        """Print command totals and optional overlapping API and batch-response details."""
        total = time.perf_counter() - self.started
        title = "run全体（対話確認を含む）" if self.command == "run" else self.command + "全体"
        print(f"{timestamp_prefix()}[計測] {title}: {total:.2f}s / "
              f"承認入力待ち: {self.approval_wait:.2f}s / "
              f"承認入力待ち除外: {max(0.0, total - self.approval_wait):.2f}s / "
              f"配備区間: {self.deployment_seconds:.2f}s / 結果: {self.outcome}", flush=True)
        if not self.verbose:
            return
        for kind in API_KINDS:
            row = self.apis.get(kind)
            if row is None:
                continue
            print(f"[API集計] {kind}: 試行={row['attempts']} 成功={row['success']} "
                  f"失敗/中断={row['failure']} 合計={row['seconds']:.2f}s "
                  f"平均={row['seconds'] / row['attempts']:.2f}s 最大={row['max_seconds']:.2f}s",
                  flush=True)
            items = self.batch_items.get(kind)
            if items:
                print(f"  内部要求: 送出対象={items['submitted']} 成功応答={items['success']} "
                      f"失敗応答={items['failure']} 応答不明={items['unknown']} "
                      "（個別レイテンシは計測不可）", flush=True)
        if not self.apis:
            print("[API集計] Gmail API試行なし", flush=True)


@contextmanager
def observe_command(command, verbose=False):
    """Report every exit path and restore the prior context even after interruption."""
    observation = Observation(command, verbose)
    token = OBSERVATION.set(observation)
    try:
        yield observation
    except BaseException as exc:
        observation.outcome = "中断" if isinstance(exc, KeyboardInterrupt) else "例外停止"
        observation.finish_phase("停止")
        raise
    finally:
        try:
            observation.finish_phase()
            observation.report()
        finally:
            OBSERVATION.reset(token)


def start_phase(label, *, deployment=False):
    """Advance the current command's phase without creating global measurement state."""
    observation = OBSERVATION.get()
    if observation is not None:
        observation.start_phase(label, deployment=deployment)


def observation_outcome(outcome):
    """Record a nonexceptional terminal reason such as decline or bounded completion."""
    observation = OBSERVATION.get()
    if observation is not None:
        observation.outcome = outcome


def timestamp_prefix() -> str:
    """Use an offset-aware clock stamp; the Docker timezone may differ from the host."""
    return f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "


def timed_operation(label: str, call, verbose: bool = False, *, api_kind=None,
                    response_success=None):
    """Record wall-clock timestamps and monotonic duration without changing API semantics."""
    started = time.perf_counter()
    if verbose:
        print(f"{timestamp_prefix()}[処理開始] {label}", flush=True)
    succeeded = False
    try:
        result = call()
        succeeded = (response_success(result) if response_success and OBSERVATION.get() is not None
                     and api_kind in API_KINDS else True)
        return result
    finally:
        seconds = time.perf_counter() - started
        observation = OBSERVATION.get()
        if observation is not None:
            # Existing detailed operation labels may include private information;
            # match only the fixed API prefix, never retain the rest of the label.
            kind = api_kind or next((kind for kind in API_KINDS
                if label == f"Gmail {kind}" or label.startswith(f"Gmail {kind} ")), None)
            observation.record_api(kind, seconds, succeeded)
        if verbose:
            print(f"{timestamp_prefix()}[処理{'完了' if succeeded else '失敗'}] {label} "
                  f"({seconds:.2f}s)", flush=True)


# Three total attempts. SDK-level retries remain disabled: writes must reconcile first.
API_RETRY_DELAYS_SECONDS = (1, 2)
RECOVERY_STOP = ("保留計画・進捗ログを保持して停止しました。Gmail実設定を確認し、"
                 "同じMasterで run を再実行して再開してください。planの作り直しは不要です。")


def transient_api_error(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if isinstance(status, int):
        if status in (500, 502, 503, 504, 429):
            return True
        if status == 403:
            try:
                errors = json.loads(exc.content)["error"]["errors"]
                reasons = {entry["reason"] for entry in errors}
                return bool(reasons) and reasons <= {"rateLimitExceeded", "userRateLimitExceeded"}
            except (AttributeError, KeyError, TypeError, ValueError):
                return False
        return False
    # Do not classify arbitrary OSError/programming/auth errors as transient.
    if isinstance(exc, (TimeoutError, ConnectionError, http.client.IncompleteRead,
                        http.client.RemoteDisconnected)):
        return True
    try:
        from httplib2 import ServerNotFoundError
    except ImportError:
        return False
    return isinstance(exc, ServerNotFoundError)


def api_error_code(exc: Exception) -> str:
    status = getattr(getattr(exc, "resp", None), "status", None)
    return f"HTTP {status}" if isinstance(status, int) else type(exc).__name__


def api_error_details(exc: Exception) -> dict:
    """Preserve Google's diagnostic fields, never request headers or credentials.

    Messages may describe account-specific settings, so these details belong in
    the private local journal rather than public reports or source control.
    """
    details = {"code": api_error_code(exc), "exception_type": type(exc).__name__}
    try:
        payload = json.loads(exc.content)
        error = payload.get("error", {})
        if isinstance(error, dict):
            for key in ("code", "status", "message"):
                value = error.get(key)
                if isinstance(value, (str, int)):
                    details["google_" + key] = value[:4096] if isinstance(value, str) else value
            errors = error.get("errors", [])
            if isinstance(errors, list):
                details["errors"] = [{k: value[:4096] for k in ("reason", "domain", "message")
                                      if isinstance(value := row.get(k), str)}
                                     for row in errors[:25] if isinstance(row, dict)]
    except (AttributeError, TypeError, ValueError):
        pass
    return details


def retry_wait(label: str, attempt: int, exc: Exception, verbose: bool) -> None:
    delay = API_RETRY_DELAYS_SECONDS[attempt] + random.uniform(0, 1)
    if verbose:
        print(f"{timestamp_prefix()}[再試行] {label}: {api_error_code(exc)} / "
              f"{delay:.2f}s待機 / 次の試行 {attempt + 2}/{len(API_RETRY_DELAYS_SECONDS) + 1}",
              flush=True)
    time.sleep(delay)


def read_operation(label: str, call, verbose=False, *, retry=True):
    for attempt in range(len(API_RETRY_DELAYS_SECONDS) + 1):
        try:
            return timed_operation(label, call, verbose)
        except Exception as exc:
            if not retry or not transient_api_error(exc):
                raise
            if attempt == len(API_RETRY_DELAYS_SECONDS):
                raise DeploymentError(f"{label}: 一時障害（{api_error_code(exc)}）が継続。"
                                      + RECOVERY_STOP) from exc
            retry_wait(label, attempt, exc, verbose)


def recover_mutation(label: str, call, reconcile, record, *, verbose=False):
    """On an ambiguous write, reconcile before AND after waiting, never retry blindly.

    reconcile returns (applied, result). It must reject any state except the exact
    before/after snapshots. Recovery reads themselves are single-shot/fail-closed.
    """
    for attempt in range(len(API_RETRY_DELAYS_SECONDS) + 1):
        try:
            return timed_operation(label, call, verbose)
        except Exception as exc:
            if not transient_api_error(exc):
                raise
            details = {"attempt": attempt + 1, "error": api_error_code(exc)}
            record({"kind": "api_error", **details})
            for after_wait in (False, True):
                if after_wait:
                    if attempt == len(API_RETRY_DELAYS_SECONDS):
                        raise DeploymentError(f"{label}: 一時障害の再試行上限に到達。"
                                              + RECOVERY_STOP) from exc
                    retry_wait(label, attempt, exc, verbose)
                try:
                    applied, result = reconcile()
                except Exception as read_exc:
                    record({"kind": "reconciliation_failed", **details,
                            "read_error": api_error_code(read_exc)})
                    raise DeploymentError(f"{label}: 変更結果が不明、または外部変更を検出。"
                                          + RECOVERY_STOP) from read_exc
                if applied:
                    record({"kind": "reconciled_after_error", **details})
                    print(f"{timestamp_prefix()}{label}: エラー後の実設定照合で反映済みと確認。"
                          "同じ変更を再送せず進捗を確定します。", flush=True)
                    return result
                record({"kind": "reconciled_before_retry", **details,
                        "after_wait": after_wait})
    raise AssertionError("unreachable mutation retry")


def filter_snapshot(rows: list[dict]) -> tuple[dict[str, dict], Counter]:
    """Keep ID-less skipped rules immutable without blocking other approved work."""
    identified = {}
    unidentified = []
    for row in rows:
        fid = row.get("id")
        if isinstance(fid, str) and fid.strip():
            demand(fid not in identified, "フィルタIDが重複しています")
            identified[fid] = row["filter"]
        else:
            unidentified.append(row["filter"])
    return identified, counter(unidentified)


def prefix_counts(base: list[dict], plan: dict, adds: int, removes: int) -> Counter:
    expected = counter(base)
    expected.update(counter(plan["add"][:adds]))
    for row in plan["remove"][:removes]:
        key = json_hash(row)
        demand(expected[key] > 0, "内部削除カウンタ異常")
        expected[key] -= 1
        if not expected[key]:
            del expected[key]
    return expected


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
            f.flush()
            os.fsync(f.fileno())
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


def build_legacy_template(p: dict[str, str]) -> dict:
    """Preserve the original XML acceptance rules and query-building behavior."""
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


def build_template(p: dict[str, str], *, compilation=CURRENT_COMPILATION) -> dict:
    """Validate with the old compiler, then preserve native fields in the new mode."""
    demand(compilation in ("legacy", CURRENT_COMPILATION), "未知のXMLコンパイル方式です")
    template = build_legacy_template(p)
    if compilation == "legacy":
        return template
    # All original syntax/action checks above still run, including rejected OR
    # forms. Only the destination of accepted from/subject values changes.
    criteria = template["criteria"]
    criteria.pop("query", None)
    for key in ("from", "subject"):
        if p.get(key):
            criteria[key] = p[key].strip()
    if p.get("hasTheWord"):
        criteria["query"] = "(" + p["hasTheWord"].strip() + ")"
    return template


def plan_modes(plan: dict) -> tuple[str, str]:
    """Validate the exact schema/version pair before selecting compiler and normalizer."""
    if plan.get("schema") in LEGACY_PLAN_SCHEMAS:
        demand("normalization_version" not in plan and "compilation_version" not in plan,
               "旧計画に解釈versionが追加されています")
        return "legacy", "legacy"
    if plan.get("schema") == QUERY_V1_PLAN_SCHEMA:
        demand(plan.get("normalization_version") == QUERY_V1_NORMALIZATION and
               "compilation_version" not in plan, "v5計画の正規化/コンパイルversionが不正です")
        return "legacy", QUERY_V1_NORMALIZATION
    if plan.get("schema") == NATIVE_V1_PLAN_SCHEMA:
        demand(plan.get("normalization_version") == QUERY_V1_NORMALIZATION and
               plan.get("compilation_version") == CURRENT_COMPILATION,
               "v6計画の正規化/コンパイルversionが不正です")
        return CURRENT_COMPILATION, QUERY_V1_NORMALIZATION
    demand(plan.get("schema") == CURRENT_PLAN_SCHEMA and
           plan.get("normalization_version") == CURRENT_NORMALIZATION and
           plan.get("compilation_version") == CURRENT_COMPILATION,
           "未知または欠落した計画schema/解釈versionです。承認計画を自動変換せず停止します")
    return CURRENT_COMPILATION, CURRENT_NORMALIZATION


def normalization_for_plan(plan: dict) -> str:
    """Return the pinned normalizer only after validating both interpretation versions."""
    return plan_modes(plan)[1]


def simple_spam_query(body: str) -> bool:
    """Recognize one unwrapped ASCII spam query, excluding standalone operator words."""
    match = re.fullmatch(r"in:spam \{([A-Za-z0-9]+(?: [A-Za-z0-9]+)*)\}", body)
    return bool(match and not {"AND", "OR", "AROUND", "NOT"}.intersection(match.group(1).split(" ")))


def normalize_criteria(criteria: dict, *, normalization: str) -> tuple[dict, list[str]]:
    """Apply pinned notation rules; only query-v2 also normalizes narrow spam wrappers."""
    demand(normalization in SUPPORTED_NORMALIZATIONS, "未知の正規化方式です")
    result = dict(criteria)
    reasons = []
    if normalization == "legacy":
        return result, reasons
    query = result.get("query")
    if isinstance(query, str):
        body = query
        # Peel candidate whole wrappers, then require the complete remaining
        # string to match the small grammar. No partial rewrite survives failure.
        while body.startswith("(") and body.endswith(")"):
            body = body[1:-1]
        reason = "query_sender_or"
        if normalization == CURRENT_NORMALIZATION and simple_spam_query(body):
            replacement = "(" + body + ")"
            reason = "query_spam_outer_parentheses"
        elif re.fullmatch(FROM_ATOM + r"(?: OR " + FROM_ATOM + r")+", body):
            replacement = "{" + " ".join(body.split(" OR ")) + "}"
        elif re.fullmatch(r"\{" + FROM_ATOM + r"(?: " + FROM_ATOM + r")+\}", body):
            replacement = body
        else:
            replacement = query
        if replacement != query:
            result["query"] = replacement
            reasons.append(reason)
    subject = result.get("subject")
    if isinstance(subject, str):
        replacement = re.sub(" {2,}", " ", subject)
        if replacement != subject:
            result["subject"] = replacement
            reasons.append("subject_ascii_spaces")
    return result, reasons


def canonical_template(obj: dict, *, normalization=CURRENT_NORMALIZATION) -> dict:
    """Apply the pinned condition interpretation and the existing action cleanup."""
    criteria = dict(obj.get("criteria", {}))
    criteria = {k: v.strip() if isinstance(v, str) else v for k, v in criteria.items() if v not in (None, "", False)}
    criteria, _ = normalize_criteria(criteria, normalization=normalization)
    # API may return all optional empty arrays/false values. Normalize those only.
    action = obj.get("action", {})
    adds = sorted(set(action.get("addLabelIds") or []))
    removes = sorted(set(action.get("removeLabelIds") or []))
    forward = action.get("forward")
    demand(not forward, "予期しない転送フィルタです。自動配備しません")
    demand(not set(adds) & set(removes), "既存フィルタがラベルを追加・削除しています")
    return {"criteria": criteria, "action": {"addLabelIds": adds, "removeLabelIds": removes}}


def normalization_note(obj: dict, *, normalization: str) -> dict | None:
    """Retain changed criteria outside the semantic template for private diagnostics."""
    before = canonical_template(obj, normalization="legacy")["criteria"]
    after, reasons = normalize_criteria(before, normalization=normalization)
    if not reasons:
        return None
    return {"before_criteria": dict(obj.get("criteria", {})),
            "after_criteria": after, "reasons": reasons}


def compile_xml(path: Path, *, compilation=CURRENT_COMPILATION,
                normalization=CURRENT_NORMALIZATION, audit=None, compilation_audit=None) -> list[dict]:
    """Compile with pinned policies, keeping mapping provenance separate from normalization."""
    demand(compilation in ("legacy", CURRENT_COMPILATION), "未知のXMLコンパイル方式です")
    demand(normalization in SUPPORTED_NORMALIZATIONS, "未知の正規化方式です")
    entries = atom_entries(path)
    check_xml_rules(entries)
    out = []
    old_templates = []
    for index, properties in enumerate(entries, 1):
        raw = build_template(properties, compilation=compilation)
        out.append(canonical_template(raw, normalization=normalization))
        if audit is not None and (note := normalization_note(raw, normalization=normalization)):
            audit.append({"source": "xml", "entry": index, **note})
        if compilation != "legacy":
            old = canonical_template(build_template(properties, compilation="legacy"),
                                     normalization=normalization)
            old_templates.append(old)
            if compilation_audit is not None and old["criteria"] != out[-1]["criteria"]:
                compilation_audit.append({"source": "xml", "entry": index,
                    "before_compilation": "legacy", "after_compilation": compilation,
                    "before_criteria": old["criteria"], "after_criteria": out[-1]["criteria"]})
    # Keeping fields must not silently expand acceptance to a Master that the
    # previous compiler rejected because its compiled rules were duplicates.
    old_dups = [k for k, n in Counter(json_hash(x) for x in old_templates).items() if n > 1]
    demand(not old_dups, f"従来のAPI変換後に意味的重複: {len(old_dups)}種類。手動解決が必要")
    dups = [k for k, n in Counter(json_hash(x) for x in out).items() if n > 1]
    demand(not dups, f"API変換後に意味的重複: {len(dups)}種類。手動解決が必要")
    return out


def live_as_template(f: dict, names_by_id: dict[str, str], *, normalization=CURRENT_NORMALIZATION) -> dict:
    """Map live label IDs and interpret conditions in the active plan's mode."""
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
    return canonical_template({"criteria": c, "action": a}, normalization=normalization)


def live_row(f: dict, names_by_id: dict[str, str], *, normalization: str) -> dict:
    """Keep API criteria provenance beside, never inside, the comparable filter."""
    row = {"id": f.get("id"), "filter": live_as_template(f, names_by_id, normalization=normalization)}
    note = normalization_note(f, normalization=normalization)
    if note is not None:
        row["normalization"] = note
    return row


def to_api(t: dict, ids_by_name: dict[str, str], *, normalization=CURRENT_NORMALIZATION) -> dict:
    """Serialize only criteria/actions using the same mode as planning and readback."""
    t = canonical_template(t, normalization=normalization)
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


class SingleAttemptHttp:
    """googleapiclient transport with no hidden write replay on disconnect/401/redirect.

    httplib2 can replay BadStatusLine even when HttpRequest.num_retries is zero.
    AuthorizedSession supplies the same Google credentials over requests instead.
    """
    def __init__(self, credentials):
        from google.auth.transport.requests import AuthorizedSession
        from requests.adapters import HTTPAdapter
        self.session = AuthorizedSession(credentials, max_refresh_attempts=0)
        self.session.mount("https://", HTTPAdapter(max_retries=0))
        self.session.mount("http://", HTTPAdapter(max_retries=0))

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        import httplib2
        import requests
        try:
            response = self.session.request(method, uri, data=body, headers=headers,
                                            timeout=kwargs.get("timeout", 60), allow_redirects=False)
        except requests.exceptions.SSLError:
            raise  # Certificate failures must not be classified as retryable disconnects.
        except requests.exceptions.Timeout as exc:
            raise TimeoutError("Gmail HTTP request timed out") from exc
        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
            raise ConnectionError("Gmail HTTP response was interrupted") from exc
        # requests has already decompressed the body; expose the httplib2 response
        # contract expected by googleapiclient without stale compression headers.
        response_headers = {k.lower(): v for k, v in response.headers.items()
                            if k.lower() not in {"content-encoding", "content-length"}}
        response_headers.update(status=str(response.status_code),
                                **{"content-length": str(len(response.content))})
        return httplib2.Response(response_headers), response.content

    def close(self):
        self.session.close()


def get_client(client_secrets: Path, token_path: Path, allow_auth=True, *, scopes=None, expected_email=None):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:
        raise DeploymentError("依存ライブラリが未インストールです: pip install -r requirements.txt") from e
    creds = None
    required = scopes if scopes is not None else SCOPES
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), None if scopes is not None else SCOPES)
    if scopes is not None and creds and not creds.has_scopes(required):
        creds = None  # Keep the existing file until a verified replacement is ready.
    changed = False
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif allow_auth:
            demand(client_secrets.exists(), f"Google OAuth DesktopクライアントJSONが必要: {client_secrets}")
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), required)
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
        changed = True
    if scopes is not None:
        granted = creds.granted_scopes if creds.granted_scopes is not None else creds.scopes
        demand(granted is not None and set(required) <= set(granted), "メール操作用OAuth権限が不足しています")
        demand(expected_email and account_email(creds) == expected_email.lower(), "メール操作用OAuthアカウントが一致しません")
    if changed:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(token_path, json.loads(creds.to_json()))
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
    return build("gmail", "v1", http=SingleAttemptHttp(creds),
                 cache_discovery=False, static_discovery=True), creds


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


def filters_with_known_labels(client, labels_by_name: dict[str, str], *, verbose=False,
                              normalization=CURRENT_NORMALIZATION) -> list[dict]:
    """Read live filters using labels validated by the immediately preceding pre-delete read.

    The next pre-delete read and the final full verification still refresh labels.
    This cache never skips a live filters.list and never authorizes mutations.
    """
    names_by_id = {lid: name for name, lid in labels_by_name.items()}
    filters = read_operation(
        "Gmail filters.list", lambda: client.users().settings().filters().list(userId="me").execute(num_retries=0),
        verbose).get("filter", [])
    demand(len(filters) <= FILTER_LIMIT, f"Gmail側フィルタ数が異常: {len(filters)}")
    try:
        return [live_row(x, names_by_id, normalization=normalization) for x in filters]
    except DeploymentError as exc:
        if "不明なGmailラベルID" not in str(exc):
            raise
        # An unfamiliar system label or concurrent label change must never be
        # silently normalized against stale cache entries.
        if verbose:
            print(f"{timestamp_prefix()}[詳細] 未知のラベルIDを検出。"
                  "Gmailラベル・フィルタ一覧を完全再取得します", flush=True)
        current, _ = fetch_for_deploy(client, verbose=verbose, normalization=normalization)
        return current


def api_fetch(client, *, verbose: bool = False, retry_reads: bool = True,
              normalization=CURRENT_NORMALIZATION) -> tuple[list[dict], dict[str, str]]:
    """Read Gmail settings once per resource and normalize in the selected mode."""
    demand(normalization in SUPPORTED_NORMALIZATIONS, "未知の正規化方式です")
    labels = read_operation(
        "Gmail labels.list", lambda: client.users().labels().list(userId="me").execute(num_retries=0),
        verbose, retry=retry_reads).get("labels", [])
    # Include system and user labels for normalization; only user labels may be
    # created or referenced as names in new filter payloads.
    label_names = {x["name"]: x["id"] for x in labels if x.get("type") in ("user", "USER")}
    user_labels = [x for x in labels if x.get("type") in ("user", "USER")]
    demand(len(label_names) == len(user_labels)
           and len(set(label_names.values())) == len(user_labels)
           and all(isinstance(v, str) and v.strip() for v in label_names.values()),
           "ユーザーラベルの名前・IDが欠落または重複しています")
    names_by_id = {x["id"]: x["name"] for x in labels}
    filters = read_operation(
        "Gmail filters.list", lambda: client.users().settings().filters().list(userId="me").execute(num_retries=0),
        verbose, retry=retry_reads).get("filter", [])
    demand(len(filters) <= FILTER_LIMIT, f"Gmail側フィルタ数が異常: {len(filters)}")
    normalized = [live_row(x, names_by_id, normalization=normalization) for x in filters]
    return normalized, label_names


def fetch_for_deploy(client, verbose: bool = False, *, normalization=CURRENT_NORMALIZATION):
    """Read deployment state with the plan's pinned condition interpretation."""
    return api_fetch(client, verbose=verbose, normalization=normalization)


def compare_base_live(base: list[dict], target: list[dict], live: list[dict]) -> str:
    if counter(live) == counter(target):
        return "target"
    if counter(live) == counter(base):
        return "baseline"
    return "drift"


RULE_GROUPS = ("other", "delete", "trash")
RULE_GROUP_NAMES = {"trash": "Trash（実際のゴミ箱移動）",
                    "delete": "Action/Delete（ラベル付与）",
                    "other": "その他（Type・Service・Org等）"}


def filter_group(rule: dict) -> str:
    # Trash takes precedence for a live filter with multiple actions.
    if is_trash(rule):
        return "trash"
    if is_delete(rule):
        return "delete"
    return "other"


def selected_master_target(master: list[dict], live_base: list[dict],
                           selected: list[str]) -> list[dict]:
    """Approved categories follow Master. Declined categories retain live base."""
    demand(len(selected) == len(set(selected)) and
           all(name in RULE_GROUPS for name in selected), "無効な承認グループ")
    approved = set(selected)
    return ([row for row in master if filter_group(row) in approved] +
            [row for row in live_base if filter_group(row) not in approved])



def classify_deletion_conflicts(removals: list[dict], base: list[dict],
                                current: list[dict] | None = None) -> list[dict]:
    """List removals that cannot be mapped to exactly one live filter ID.

    The caller excludes these from an *approved* plan before any Gmail writes.
    For purely offline plan unit tests without current API objects, validate
    multiplicity only; the interactive run always passes current with IDs.
    """
    occurrences = Counter(json_hash(x) for x in base)
    matching_ids: dict[str, list] = {}
    if current is not None:
        for item in current:
            key = json_hash(item["filter"])
            matching_ids.setdefault(key, []).append(item.get("id"))
    result = []
    for rule in removals:
        key = json_hash(rule)
        count = occurrences[key] if current is None else len(matching_ids.get(key, []))
        ids = matching_ids.get(key, [])
        if count != 1:
            reason = "not_found" if count == 0 else "duplicate_matches"
        elif current is not None and (len(ids) != 1 or not isinstance(ids[0], str)
                                      or not ids[0].strip()):
            reason = "missing_filter_id"
        else:
            continue
        result.append({"rule": rule, "reason": reason, "matches": count})
    return result


def deletion_preview(plan: dict, current: list[dict]) -> list[dict]:
    """Inspect every requested deletion before requesting any approvals."""
    demand(counter(x["filter"] for x in current) == counter(plan["baseline_snapshot"]),
           "事前検証中にGmailのフィルタ設定が変化しました。差分を再取得してください")
    return classify_deletion_conflicts(plan["remove"], plan["baseline_snapshot"], current)


def select_plan_groups(plan: dict, selected: list[str],
                       current: list[dict] | None = None) -> dict:
    """Pin a subset of groups to the original live snapshot and XML hash."""
    compilation, normalization = plan_modes(plan)
    demand(plan.get("schema") in ("gmail-filters-deploy-plan/v3", QUERY_V1_PLAN_SCHEMA,
                                 NATIVE_V1_PLAN_SCHEMA, CURRENT_PLAN_SCHEMA) and
           not plan.get("selected_groups"), "新規の差分計画のみ選択できます")
    master = compile_xml(Path(plan["target_path"]), compilation=compilation, normalization=normalization)
    demand(file_hash(plan["target_path"]) == plan["target_sha256"],
           "承認前にMaster XMLが変更されました。再計画してください")
    base = [canonical_template(x, normalization=normalization) for x in plan["baseline_snapshot"]]
    intended = selected_master_target(master, base, selected)
    added, candidate_removals = delta(base, intended)
    skipped = classify_deletion_conflicts(candidate_removals, base, current)
    skipped_count = Counter(json_hash(row["rule"]) for row in skipped)
    removed = []
    for row in candidate_removals:
        key = json_hash(row)
        if skipped_count[key] > 0:
            skipped_count[key] -= 1
        else:
            removed.append(row)
    # Excluded deletions remain in Gmail; the approved effective target
    # therefore contains those original rule instances.
    effective = intended + [record["rule"] for record in skipped]
    demand(len(effective) <= FILTER_LIMIT,
           f"一意性判定で見送るルールを含めると、反映後{len(effective)}件となり"
           f"Gmailの上限{FILTER_LIMIT}件を超えます。最終承認前に処理を見直してください")
    from copy import deepcopy
    new_plan = deepcopy(plan)
    new_plan["schema"] = ("gmail-filters-deploy-plan/v4" if normalization == "legacy"
                          else plan["schema"])
    new_plan["skipped_deletions"] = skipped
    new_plan["selected_groups"] = sorted(selected)
    new_plan["target_size"] = len(effective)
    new_plan["add"] = added
    new_plan["remove"] = removed
    new_plan["new_label_names"] = sorted(
        {lab[6:] for row in added for lab in row["action"]["addLabelIds"]
         if lab.startswith("label:")} - set(plan["expected_label_names"]))
    new_plan["target_delete_count"] = sum(is_delete(x) for x in effective)
    new_plan["target_trash_count"] = sum(is_trash(x) for x in effective)
    new_plan["trash_add_count"] = sum(is_trash(x) for x in added)
    new_plan["trash_remove_count"] = sum(is_trash(x) for x in removed)
    new_plan["approval_digest"] = json_hash({
        k: v for k, v in new_plan.items() if k not in {"created_utc", "approval_digest"}})
    return new_plan


def interactive_group_selection(plan: dict) -> list[str]:
    """Basic changes are included by default; sensitive groups require consent."""
    approved = []
    basic_add = sum(filter_group(x) == "other" for x in plan["add"])
    basic_remove = sum(filter_group(x) == "other" for x in plan["remove"])
    if basic_add or basic_remove:
        approved.append("other")
    for group in ("delete", "trash"):
        added, removed = show_group_details(plan, group)
        if not (added or removed):
            continue
        prompt = f"{RULE_GROUP_NAMES[group]}の変更を適用しますか？ [y/N]: "
        if confirm_interactive(prompt):
            approved.append(group)
            print("  → 承認しました。", flush=True)
        else:
            print("  → 見送りました。Gmail側の現行ルールを維持します。", flush=True)
    return approved


def make_plan(base_path: Path | None, target_path: Path, live: list[dict], acct: str, labels: dict,
              extra_baselines: list[Path] | None = None, *, compilation=CURRENT_COMPILATION,
              normalization=CURRENT_NORMALIZATION, live_rows=None) -> dict:
    """Build a pinned, fully reviewed diff from a live API snapshot by default."""
    demand((compilation, normalization) in (("legacy", "legacy"), ("legacy", QUERY_V1_NORMALIZATION),
                                          (CURRENT_COMPILATION, QUERY_V1_NORMALIZATION),
                                          (CURRENT_COMPILATION, CURRENT_NORMALIZATION)),
           "未対応のコンパイル/正規化方式の組合せです")
    audit = []
    compilation_audit = []
    target = compile_xml(target_path, compilation=compilation, normalization=normalization,
                         audit=audit, compilation_audit=compilation_audit)
    raw_live = live
    if live_rows is not None:
        raw_live = [{"criteria": row.get("normalization", {}).get(
            "before_criteria", row["filter"]["criteria"]), "action": row["filter"]["action"]}
            for row in live_rows]
        demand(counter(canonical_template(row, normalization=normalization) for row in raw_live) ==
               counter(canonical_template(row, normalization=normalization) for row in live),
               "API条件の正規化診断情報がフィルタと一致しません")
    legacy_live = [canonical_template(row, normalization="legacy") for row in raw_live]
    live = [canonical_template(row, normalization=normalization) for row in live]
    if normalization != "legacy":
        for index, raw in enumerate(raw_live):
            if note := normalization_note(raw, normalization=normalization):
                source = {"filter_id": live_rows[index].get("id")} if live_rows is not None else {"entry": index + 1}
                audit.append({"source": "api", **source, **note})
    validate_delete_label_only(target)
    if base_path is None:
        demand(not extra_baselines, "--also-baselineは--baseとともに指定してください")
        source_path = None
        base = list(live)
    else:
        baseline_paths = [base_path] + (extra_baselines or [])
        valid = [(p, compile_xml(p, compilation=compilation, normalization=normalization)) for p in baseline_paths]
        status = next(((p, b) for p, b in valid if counter(live) == counter(b)), None)
        if counter(live) == counter(target):
            status = (target_path, target)
        if status is None:
            p0, b0 = valid[0]
            adds, removes = delta(b0, live)
            raise DeploymentError(f"Gmail実設定が既知の基準XMLと一致しません（Gmailのみ: {len(adds)} / 基準のみ: {len(removes)}）。自動上書きしません")
        source_path, base = status
    adds, removes = delta(base, target)
    demand(len(target) <= FILTER_LIMIT,
           f"Masterのフィルタ数{len(target)}件がGmailの上限{FILTER_LIMIT}件を超えます")
    demand(all(x["action"]["addLabelIds"] == ["label:Action/Delete"] and
               not x["action"]["removeLabelIds"] for x in adds if is_delete(x)),
           "追加対象のAction/Deleteにラベル以外の処理が含まれています")
    # For interactive run, capacity is checked on the approved subset, not all proposed groups.
    existing_labels = set(labels)
    new_labels = sorted(set(x[6:] for t in adds for x in t["action"]["addLabelIds"] if x.startswith("label:")) - existing_labels)
    plan = {
        "schema": (CURRENT_PLAN_SCHEMA if normalization == CURRENT_NORMALIZATION else
                   NATIVE_V1_PLAN_SCHEMA if compilation == CURRENT_COMPILATION else
                   "gmail-filters-deploy-plan/v3" if normalization == "legacy" else QUERY_V1_PLAN_SCHEMA),
        "operation_order": "remove_first",
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
    if normalization != "legacy":
        legacy_target = compile_xml(target_path, compilation=compilation, normalization="legacy")
        plan["normalization_version"] = normalization
        plan["normalization_audit"] = audit
        plan["normalization_matched"] = (sum((counter(live) & counter(target)).values()) -
                                         sum((counter(legacy_live) & counter(legacy_target)).values()))
    if compilation != "legacy":
        old_target = compile_xml(target_path, compilation="legacy", normalization=normalization)
        plan["compilation_version"] = compilation
        plan["compilation_audit"] = compilation_audit
        comparison = {"scope": "master_vs_live_at_planning", "normalization_version": normalization}
        for name, compiler, master in (("previous", "legacy", old_target), ("current", compilation, target)):
            proposed_add, proposed_remove = delta(live, master)
            comparison[name] = {"compilation_version": compiler,
                "matched": sum((counter(live) & counter(master)).values()),
                "add": len(proposed_add), "remove": len(proposed_remove)}
        plan["compilation_comparison"] = comparison
    plan["approval_digest"] = json_hash({k: v for k, v in plan.items() if k not in {"created_utc", "approval_digest"}})
    return plan


def planned_states(plan: dict):
    """Pin actual baseline and target to the approved plan, including resume."""
    compilation, normalization = plan_modes(plan)
    master = compile_xml(Path(plan["target_path"]), compilation=compilation, normalization=normalization)
    validate_delete_label_only(master)
    demand(file_hash(plan["target_path"]) == plan["target_sha256"], "計画作成後にTarget XMLが変わりました")
    source = plan.get("baseline_path")
    if source:
        base = compile_xml(Path(source), compilation=compilation, normalization=normalization)
        demand(file_hash(source) == plan["baseline_sha256"], "計画作成後に基準XMLが変わりました")
    else:
        base = [canonical_template(x, normalization=normalization) for x in plan["baseline_snapshot"]]
    demand(counter(base) == counter(plan["baseline_snapshot"]), "プランの基準スナップショットが変わりました")
    demand(tokens_digest(base) == plan["live_snapshot_hash"], "基準スナップショットのハッシュが不一致")
    demand(plan.get("operation_order", "add_first") in ("remove_first", "add_first"),
           "配備順序の指定が不正です")
    if plan.get("schema") == "gmail-filters-deploy-plan/v4" or "selected_groups" in plan:
        demand("selected_groups" in plan, "承認グループ情報がありません")
        intended = selected_master_target(master, base, plan["selected_groups"])
        skips = plan.get("skipped_deletions", [])
        demand(all(row.get("reason") in ("not_found", "duplicate_matches", "missing_filter_id")
                   and isinstance(row.get("rule"), dict)
                   and isinstance(row.get("matches"), int) for row in skips),
               "スキップ対象の記録が不正です")
        expected_removals = delta(base, intended)[1]
        expected_count = counter(expected_removals)
        for row in skips:
            key = json_hash(row["rule"])
            demand(expected_count[key] > 0,
                   "スキップ対象が元の削除計画に含まれていません")
            expected_count[key] -= 1
        target = intended + [row["rule"] for row in skips]
    else:
        target = master
    demand(counter(base) + Counter(json_hash(x) for x in plan["add"]) - Counter(json_hash(x) for x in plan["remove"]) == counter(target),
           "計画とMasterの差分が不一致")
    demand(plan["current_delete_count"] == sum(is_delete(x) for x in base)
           and plan["target_delete_count"] == sum(is_delete(x) for x in target), "Delete件数の記録が改変されました")
    demand(plan["target_size"] == len(target), "承認対象の最終件数が改変されています")
    demand(plan.get("current_trash_count") == sum(is_trash(x) for x in base) and
           plan.get("target_trash_count") == sum(is_trash(x) for x in target) and
           plan.get("trash_add_count") == sum(is_trash(x) for x in plan["add"]) and
           plan.get("trash_remove_count") == sum(is_trash(x) for x in plan["remove"]),
           "Trash差分の承認計画が改変されています")
    return base, target


def detect_position(base: list[dict], plan: dict, live: list[dict]) -> tuple[int, int]:
    """Resume only from an exact prefix of the plan's pinned operation order."""
    cur = counter(base)
    live_counts = counter(live)
    if cur == live_counts:
        return 0, 0

    def remove_one(item: dict):
        key = json_hash(item)
        demand(cur[key] >= 1, "不正な削除計画")
        cur[key] -= 1
        if cur[key] == 0:
            del cur[key]

    if plan.get("operation_order", "add_first") == "remove_first":
        for j, item in enumerate(plan["remove"], 1):
            remove_one(item)
            if cur == live_counts:
                return 0, j
        for i, item in enumerate(plan["add"], 1):
            cur[json_hash(item)] += 1
            if cur == live_counts:
                return i, len(plan["remove"])
    else:
        # Plans approved by the earlier add-first implementation may be
        # mid-flight; preserve their order for deterministic recovery.
        for i, item in enumerate(plan["add"], 1):
            cur[json_hash(item)] += 1
            if cur == live_counts:
                return i, 0
        for j, item in enumerate(plan["remove"], 1):
            remove_one(item)
            if cur == live_counts:
                return len(plan["add"]), j
    demand(False, "ライブ状態が承認計画の中間状態と一致しません。外部変更や失敗の可能性があります")
    raise AssertionError


def batch_progress(base, plan, current, log, labels=None):
    """Recognize only a journaled batch subset, including out-of-order success.

    The checkpoint is saved before sending any writes. Counts alone never
    authorize resuming a batch: unchanged IDs, contents and labels must match.
    """
    filter_snapshot(current)  # Reject duplicate IDs even for a fresh/legacy plan.
    prior = json.loads(log.read_text(encoding="utf-8")) if log.exists() else {}
    if prior:
        demand(prior.get("plan_digest") == plan["approval_digest"],
               "配備ログに別の計画が記録されています")
    checkpoint = prior.get("batch_checkpoint")
    if checkpoint is None:
        a, r = detect_position(base, plan, [row["filter"] for row in current])
        return set(range(a)), set(range(r))
    demand(checkpoint.get("schema") == 1, "バッチ進捗の形式が不正です")
    done = {}
    for kind in ("add", "remove"):
        steps = checkpoint[kind]
        demand(isinstance(steps, list) and all(type(i) is int and 0 <= i < len(plan[kind]) for i in steps)
               and len(set(steps)) == len(steps), "バッチ進捗の操作番号が不正です")
        done[kind] = set(steps)
    first, second = (("remove", "add") if plan.get("operation_order", "add_first") == "remove_first"
                     else ("add", "remove"))
    demand(not done[second] or len(done[first]) == len(plan[first]), "バッチ進捗の実行順序が不正です")
    expected = counter(base)
    expected.update(counter(plan["add"][i] for i in done["add"]))
    for i in done["remove"]:
        key = json_hash(plan["remove"][i])
        demand(expected[key] > 0, "バッチ進捗の削除記録が不正です")
        expected[key] -= 1
    expected = +expected
    before, unidentified = filter_snapshot(checkpoint["rows"])
    demand(counter(row["filter"] for row in checkpoint["rows"]) == expected,
           "バッチ進捗と承認計画が一致しません")
    actual, unknown = filter_snapshot(current)
    demand(unknown == unidentified, "ID不明フィルタの内容・件数が変わりました")
    if labels is not None:
        expected_labels = checkpoint["labels"]
        name = checkpoint.get("pending_label")
        if name is not None:
            demand(name in plan["new_label_names"] and name not in expected_labels
                   and not checkpoint.get("pending"), "作成中ラベルの記録が不正です")
            if name in labels:
                demand(labels[name] not in expected_labels.values(), "作成ラベルIDが重複しています")
                expected_labels = {**expected_labels, name: labels[name]}
        demand(labels == expected_labels, "バッチ照合中にラベル設定が変わりました")
    pending = checkpoint.get("pending")
    if pending is None:
        demand(actual == before, "バッチ進捗と実GmailのID・内容が一致しません")
        return done["add"], done["remove"]
    kind, steps = pending["kind"], pending["steps"]
    demand(kind in (first, second) and isinstance(steps, list) and 1 <= len(steps) <= 25,
           "送信中バッチの形式が不正です")
    remaining = [i for i in range(len(plan[kind])) if i not in done[kind]]
    demand(steps == remaining[:len(steps)] and
           (kind == first or len(done[first]) == len(plan[first])), "送信中バッチの順序が不正です")
    successes = pending.get("successes", {})
    demand(set(successes) <= {str(i) for i in steps}, "バッチ応答の操作番号が不正です")
    applied = set()
    if kind == "remove":
        ids = pending["ids"]
        demand(set(ids) == {str(i) for i in steps} and len(set(ids.values())) == len(steps),
               "バッチ削除IDが不正です")
        for i in steps:
            fid = ids[str(i)]
            demand(before.get(fid) == plan[kind][i] and
                   sum(rule == plan[kind][i] for rule in before.values()) == 1,
                   "バッチ削除対象が一意ではありません")
            if fid not in actual:
                applied.add(i)
        retained = {fid: rule for fid, rule in before.items()
                    if fid not in {ids[str(i)] for i in applied}}
        demand(actual == retained, "バッチ削除以外のフィルタ変更を検出しました")
        demand(all(int(i) in applied and fid == ids[i] for i, fid in successes.items()),
               "成功応答の削除が読戻しに反映されていません")
    else:
        demand(all(actual.get(fid) == rule for fid, rule in before.items()),
               "バッチ追加中に既存フィルタのID・内容が変わりました")
        candidates = {json_hash(plan[kind][i]): i for i in steps}
        demand(len(candidates) == len(steps) and not (set(candidates) & set(expected)),
               "バッチ追加対象が重複しています")
        created = {}
        for fid in set(actual) - set(before):
            i = candidates.get(json_hash(actual[fid]))
            demand(i is not None and i not in applied, "予定外・重複したフィルタ追加を検出しました")
            applied.add(i)
            created[str(i)] = fid
        demand(all(created.get(i) == fid for i, fid in successes.items()),
               "成功応答の追加IDが読戻しと一致しません")
    done[kind].update(applied)
    return done["add"], done["remove"]


def execute_filter_single(step, request, callback, *, api_kind=None):
    """Send one ordinary request with no replay; retain the journal callback."""
    try:
        response = timed_operation("単独HTTP要求", lambda: request.execute(num_retries=0),
                                   api_kind=api_kind)
    except Exception as exc:
        callback(str(step), None, exc)
    else:
        callback(str(step), response, None)


def execute_filter_batch(client, requests, callback, *, api_kind=None):
    """Use the SDK multipart codec, but permit exactly one HTTP transmission.

    BatchHttpRequest.execute can replay inner 401 responses, even though the
    ordinary HttpRequest retry count is zero. This transport refuses a second
    send; recovery always happens through our persisted journal and readback.
    """
    observation = OBSERVATION.get()
    items = None
    seen = set()
    request_ids = {str(step) for step, _ in requests}

    def observed_callback(request_id, response, exception):
        """Count each known response before passing it to the journal callback."""
        # Observe the response before the journal callback: a disk failure must
        # not turn a successful HTTP response into a failed API request.
        if items is not None and request_id in request_ids and request_id not in seen:
            seen.add(request_id)
            items["success" if exception is None else "failure"] += 1
            items["unknown"] -= 1
        callback(request_id, response, exception)

    class OneSend:
        sent = False

        def request(self, *args, **kwargs):
            """Measure one outer HTTP send and reject any SDK replay before transmission."""
            nonlocal items
            demand(not self.sent, "バッチの暗黙の再送を拒否しました")
            self.sent = True
            # Gmail does not promise parallel execution inside a batch. Keep
            # the old per-operation read budget, with a fixed connect timeout.
            kwargs["timeout"] = (60, 60 * len(requests))
            if observation is not None and api_kind in API_KINDS:
                items = observation.batch_items.setdefault(api_kind,
                    {"submitted": 0, "success": 0, "failure": 0, "unknown": 0})
                items["submitted"] += len(requests)
                items["unknown"] += len(requests)
            return timed_operation("バッチ外側HTTP要求", lambda: client._http.request(*args, **kwargs),
                                   api_kind=api_kind,
                                   response_success=lambda result: 200 <= int(result[0].status) < 300)

    batch = client.new_batch_http_request(callback=observed_callback)
    for step, request in requests:
        batch.add(request, request_id=str(step))
    batch.execute(http=OneSend())


def perform_batched(client, plan, base, target, current, labels, log, batch_size, verbose,
                    max_batches=None, readback_interval=50):
    """Persist each send/response, but reconcile successful writes at bounded intervals.

    batch_checkpoint.rows is the expected state (possibly response-confirmed only).
    completed_* counts are API-readback-confirmed; acknowledged_* counts can lead
    them until the next checkpoint. Resume always validates the expected state and
    any in-flight subset against a fresh API snapshot before sending anything.
    """
    normalization = normalization_for_plan(plan)
    adds, removes = batch_progress(base, plan, current, log, labels)
    state = json.loads(log.read_text(encoding="utf-8")) if log.exists() else {
        "account": plan["account"], "plan_digest": plan["approval_digest"], "events": []}
    # Keep the existing bounded, reconciled retries for fresh/legacy serial plans.
    # Old batch journals retain their stop-and-resume policy, even at size one.
    state.setdefault("serial_recovery", batch_size == 1 and "batch_checkpoint" not in state)
    state["readback_interval"] = readback_interval
    since_readback = 0
    sent_batches = 0

    def checkpoint(rows, now_labels, a, r, *, confirmed=False):
        """Persist expected rows and distinguish acknowledged from readback-confirmed progress."""
        state["batch_checkpoint"] = {"schema": 1, "rows": rows, "labels": dict(now_labels),
                                     "add": sorted(a), "remove": sorted(r), "pending": None,
                                     "readback_confirmed": confirmed}
        state.update(acknowledged_add=len(a), acknowledged_remove=len(r), verified=False)
        if confirmed:
            state.update(completed_add=len(a), completed_remove=len(r))
        save_json(log, state)

    def readback(*, failed=False):
        """Reconcile without writes; failed batches keep intent for exact-subset resume."""
        pending = state["batch_checkpoint"].get("pending")
        for attempt in range(len(READBACK_DELAYS_SECONDS) + 1):
            rows, now_labels = api_fetch(client, verbose=verbose, retry_reads=False, normalization=normalization)
            try:
                a, r = batch_progress(base, plan, rows, log, now_labels)
            except DeploymentError as exc:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                expected_rows = state["batch_checkpoint"]["rows"]
                expected_ids = {row["id"] for row in expected_rows if row.get("id")}
                observed_ids = {row["id"] for row in rows if row.get("id")}
                save_json(log.parent / "diagnostics" / f"batch_{stamp}.json", {
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "before_filters": state["batch_checkpoint"]["rows"],
                    "observed_filters": rows, "labels": now_labels, "pending": pending,
                    "expected_count": len(expected_rows), "observed_count": len(rows),
                    "missing_filter_ids": sorted(expected_ids - observed_ids),
                    "unexpected_filter_ids": sorted(observed_ids - expected_ids)})
                print(f"{timestamp_prefix()}バッチ読戻し不一致 ({attempt+1}/3): {exc}", flush=True)
                if attempt == len(READBACK_DELAYS_SECONDS):
                    raise DeploymentError("API読戻しが不一致です。" + RECOVERY_STOP) from exc
            else:
                if pending is not None:
                    observed, _ = filter_snapshot(rows)
                    kind = pending["kind"]
                    applied = (a - adds) if kind == "add" else (r - removes)
                    for i in applied:
                        pending["successes"][str(i)] = (pending["ids"][str(i)] if kind == "remove" else
                            next(fid for fid, rule in observed.items() if rule == plan[kind][i]))
                    save_json(log, state)
                if not failed or attempt == len(READBACK_DELAYS_SECONDS):
                    state.update(completed_add=len(a), completed_remove=len(r))
                    save_json(log, state)
                    if attempt:
                        print(f"再照合{attempt+1}/3回目で一致しました。", flush=True)
                    return rows, now_labels, a, r
            time.sleep(READBACK_DELAYS_SECONDS[attempt])
        raise AssertionError("unreachable readback")

    def confirm_checkpoint():
        """Refresh deployment state and persist it as fully readback-confirmed."""
        nonlocal current, labels, adds, removes, since_readback
        current, labels, adds, removes = readback()
        checkpoint(current, labels, adds, removes, confirmed=True)
        since_readback = 0
        print(f"旧ルール削除済み: {len(removes)}/{len(plan['remove'])}（全件照合済み） / "
              f"追加済み: {len(adds)}/{len(plan['add'])}（全件照合済み）", flush=True)

    def budget_exhausted():
        """Confirm outstanding writes before stopping at the requested batch limit."""
        if max_batches is not None and sent_batches >= max_batches:
            if since_readback:
                confirm_checkpoint()
            print("指定したバッチ数まで実行し照合しました。保留計画を保持して終了します。", flush=True)
            return True
        return False

    checkpoint(current, labels, adds, removes, confirmed=True)
    order = ("remove", "add") if plan.get("operation_order", "add_first") == "remove_first" else ("add", "remove")
    print(f"配備開始（最大{batch_size}件/HTTP、全件照合は{readback_interval}操作ごと）: "
          f"照合済み削除 {len(removes)}/{len(plan['remove'])} / 追加 {len(adds)}/{len(plan['add'])}", flush=True)
    if order[0] == "remove" and len(removes) < len(plan["remove"]):
        print("旧ルールを先に削除するため、新ルール追加まで一部の振り分け条件が一時的に無効になります。", flush=True)

    for kind in order:
        completed = adds if kind == "add" else removes
        remaining = [i for i in range(len(plan[kind])) if i not in completed]
        if not remaining:
            continue
        if budget_exhausted():
            return False
        if kind == "add":
            for name in plan["new_label_names"]:
                if name in labels:
                    continue
                before_labels = dict(labels)
                label_recovery_attempted = False

                def reconcile_label():
                    """Resolve a label creation outcome while rejecting unrelated filter or label changes."""
                    nonlocal label_recovery_attempted
                    label_recovery_attempted = True
                    rows, now = api_fetch(client, verbose=verbose, retry_reads=False, normalization=normalization)
                    demand(filter_snapshot(rows) == filter_snapshot(current),
                           "ラベル作成中にフィルタ設定が変更されました")
                    if name in now:
                        demand({k: v for k, v in now.items() if k != name} == before_labels
                               and now[name] not in before_labels.values(), "ラベル作成後の設定が不一致です")
                        return True, {"id": now[name]}
                    demand(now == before_labels, "ラベル作成前の設定が不一致です")
                    return False, None

                state["batch_checkpoint"]["pending_label"] = name
                save_json(log, state)

                def record(event):
                    """Persist a recovery event with the label creation context."""
                    state["events"].append({**event, "operation": "create_label", "name": name})
                    save_json(log, state)

                def send_label():
                    """Attempt label creation once and reset the recovery-read marker."""
                    nonlocal label_recovery_attempted
                    label_recovery_attempted = False
                    return client.users().labels().create(userId="me", body={
                        "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"
                    }).execute(num_retries=0)

                try:
                    result = recover_mutation(
                        f"Gmail labels.create {name}", send_label, reconcile_label, record, verbose=verbose)
                    demand(isinstance(result.get("id"), str) and result["id"].strip()
                           and result["id"] not in labels.values(), "作成ラベルIDが不正です")
                except Exception:
                    if not label_recovery_attempted:
                        readback()
                    raise
                labels[name] = result["id"]
                state["events"].append({"kind": "create_label", "name": name, "id": result["id"]})
                checkpoint(current, labels, adds, removes)
                since_readback += 1
                if since_readback == readback_interval:
                    confirm_checkpoint()
            # Validate new labels before using their IDs in filter creations.
            if since_readback:
                confirm_checkpoint()

        offset = 0
        while offset < len(remaining):
            if budget_exhausted():
                return False
            count = min(batch_size, readback_interval - since_readback)
            steps = remaining[offset:offset + count]
            ids, requests = {}, []
            for i in steps:
                rule = plan[kind][i]
                if kind == "remove":
                    matches = [row for row in current if row["filter"] == rule]
                    demand(len(matches) == 1 and isinstance(matches[0].get("id"), str)
                           and matches[0]["id"].strip(), "バッチ削除対象が一意ではありません")
                    ids[str(i)] = matches[0]["id"]
                    request = client.users().settings().filters().delete(userId="me", id=ids[str(i)])
                else:
                    demand(not any(row["filter"] == rule for row in current), "追加対象が既に存在します")
                    request = client.users().settings().filters().create(userId="me", body=to_api(rule, labels,
                                                                                              normalization=normalization))
                requests.append((i, request))
            pending = {"kind": kind, "steps": steps, "ids": ids, "successes": {}, "errors": {},
                       "error_details": {}, "transport": "single" if batch_size == 1 else "batch"}
            state["batch_checkpoint"]["pending"] = pending
            save_json(log, state)

            def callback(request_id, response, exception):
                """Validate a batch response ID and persist its success or failure before continuing."""
                demand(request_id in {str(i) for i in steps} and
                       request_id not in pending["successes"] and request_id not in pending["errors"],
                       "バッチ応答IDが不正です")
                if exception is not None:
                    pending["errors"][request_id] = api_error_code(exception)
                    pending["error_details"][request_id] = api_error_details(exception)
                else:
                    fid = ids[request_id] if kind == "remove" else (response or {}).get("id")
                    demand(isinstance(fid, str) and fid.strip(), "バッチ追加の応答IDが不正です")
                    if kind == "add":
                        demand(fid not in pending["successes"].values()
                               and all(row.get("id") != fid for row in current), "作成フィルタIDが重複しています")
                    pending["successes"][request_id] = fid
                state["acknowledged_add"] = len(adds) + (len(pending["successes"]) if kind == "add" else 0)
                state["acknowledged_remove"] = len(removes) + (len(pending["successes"]) if kind == "remove" else 0)
                save_json(log, state)
                if verbose:
                    print(f"{timestamp_prefix()}[詳細] {'削除' if kind == 'remove' else '追加'} [{int(request_id)+1}/{len(plan[kind])}] 応答確認 "
                          f"{pending['errors'].get(request_id, '成功')} / {format_filter_summary(plan[kind][int(request_id)])}",
                          flush=True)

            if batch_size == 1 and state["serial_recovery"]:
                # Preserve the legacy serial retry policy, but reconcile the entire
                # response-confirmed window before retrying this same operation.
                i, request = requests[0]
                recovery_attempted = False

                def reconcile_single():
                    """Resolve the current serial mutation against a fresh snapshot in the pinned mode."""
                    nonlocal recovery_attempted, since_readback
                    recovery_attempted = True
                    rows, now = api_fetch(client, verbose=verbose, retry_reads=False, normalization=normalization)
                    a, r = batch_progress(base, plan, rows, log, now)
                    state.update(completed_add=len(a), completed_remove=len(r))
                    save_json(log, state)
                    since_readback = 0
                    applied = i in (a if kind == "add" else r)
                    if not applied:
                        return False, None
                    fid = ids[str(i)] if kind == "remove" else next(
                        row["id"] for row in rows if row["filter"] == plan[kind][i])
                    return True, {"id": fid}

                def record_single(event):
                    """Persist a recovery event with its filter operation and step."""
                    state["events"].append({**event, "operation": "delete_filter" if kind == "remove" else "create_filter",
                                            "step": i + 1})
                    save_json(log, state)

                def send_single():
                    """Execute the serial request once without SDK retries."""
                    nonlocal recovery_attempted
                    recovery_attempted = False
                    return request.execute(num_retries=0)

                try:
                    response = recover_mutation(f"Gmail filters.{'delete' if kind == 'remove' else 'create'} [{i+1}/{len(plan[kind])}]",
                        send_single, reconcile_single, record_single, verbose=verbose)
                    callback(str(i), response, None)
                except Exception as exc:
                    pending["errors"][str(i)] = api_error_code(exc)
                    pending["error_details"][str(i)] = api_error_details(exc)
                    save_json(log, state)
                    # Recovery reads already fail closed. Permanent errors still
                    # need one readback to resolve the write before stopping.
                    if not recovery_attempted:
                        readback()
                    raise
                state["events"].append({"kind": "delete_filter" if kind == "remove" else "create_filter",
                                        "step": i + 1, "id": pending["successes"][str(i)],
                                        "sha256": json_hash(plan[kind][i])})
            else:
                try:
                    timed_operation(f"Gmail filters.{pending['transport']} {kind} "
                                    f"[{steps[0]+1}–{steps[-1]+1}/{len(plan[kind])}]",
                                    lambda: (execute_filter_single(*requests[0], callback,
                                        api_kind="filters.delete" if kind == "remove" else "filters.create")
                                        if batch_size == 1 else execute_filter_batch(client, requests, callback,
                                        api_kind="filters.batch.delete" if kind == "remove" else "filters.batch.create")), verbose)
                except Exception as exc:
                    pending["transport_error"] = api_error_code(exc)
                    pending["transport_error_details"] = api_error_details(exc)
                    save_json(log, state)
                failed = (bool(pending["errors"]) or "transport_error" in pending
                          or len(pending["successes"]) != len(steps))
                if failed:
                    rows, now_labels, a, r = readback(failed=True)
                    state["events"].append({"kind": "filter_batch", "operation": kind, "steps": steps,
                        "transport": pending["transport"], "successes": dict(pending["successes"]),
                        "errors": dict(pending["errors"]), "error_details": dict(pending["error_details"])})
                    save_json(log, state)
                    raise DeploymentError("バッチが一部失敗または応答不明です。実設定を読戻しました。" + RECOVERY_STOP)

            # All responses succeeded. Predict the next state, retaining its exact
            # IDs; this is not promoted to completed_* until an API readback passes.
            if kind == "remove":
                current = [row for row in current if row.get("id") not in ids.values()]
                removes.update(steps)
            else:
                current = current + [{"id": pending["successes"][str(i)], "filter": plan[kind][i]} for i in steps]
                adds.update(steps)
            state["events"].append({"kind": "filter_batch", "operation": kind, "steps": steps,
                "transport": pending["transport"], "successes": dict(pending["successes"]),
                "errors": {}, "error_details": {}, "timestamp_utc": datetime.now(timezone.utc).isoformat()})
            checkpoint(current, labels, adds, removes)
            since_readback += len(steps)
            sent_batches += 1
            offset += len(steps)
            print(f"応答確認済み: 削除 {len(removes)}/{len(plan['remove'])} / 追加 {len(adds)}/{len(plan['add'])} "
                  f"（全件照合済み: 削除 {state['completed_remove']} / 追加 {state['completed_add']}）", flush=True)
            if since_readback >= readback_interval or offset == len(remaining):
                if verbose and len(adds) == len(plan['add']) and len(removes) == len(plan['remove']):
                    print(f"{timestamp_prefix()}[詳細] 最終API照合中", flush=True)
                confirm_checkpoint()

    # The last phase's checkpoint is also the final readback; don't read twice.
    demand(len(adds) == len(plan["add"]) and len(removes) == len(plan["remove"])
           and counter(row["filter"] for row in current) == counter(target), "配備後の最終照合が一致しません")
    state["verified"] = True
    save_json(log, state)
    print(f"配備完了: {len(target)}件、API読戻し一致。Gmail UI exportも別途取得して照合してください")
    return {"rows": current, "labels": labels}


def validate_batch_limit(log, batch_size, max_batches):
    prior = json.loads(log.read_text(encoding="utf-8")) if log.exists() else {}
    demand(max_batches is None or max_batches > 0, "max-batchesは1以上で指定してください")
    demand(max_batches is None or batch_size > 1 or prior.get("batch_checkpoint") is not None,
           "max-batchesは複数件バッチまたは既存バッチ計画の再開専用です。"
           "新規の1件逐次処理では指定を外してください")
    return prior


def validate_before_final_approval(plan: dict, expected_email: str, log: Path,
                                   batch_size: int, current: list[dict], max_batches=None) -> None:
    """Check all locally decidable execution invariants before consent."""
    normalization = normalization_for_plan(plan)
    demand(expected_email.lower() == plan["account"].lower(),
           "承認対象のGmailアカウントが認証アカウントと異なります")
    demand(1 <= batch_size <= 25, "batch-sizeは1〜25件で指定してください")
    validate_batch_limit(log, batch_size, max_batches)
    demand(plan["approval_digest"] == json_hash({
        k:v for k,v in plan.items() if k not in {"created_utc", "approval_digest"}}),
        "承認計画のハッシュが一致しません")
    base, target = planned_states(plan)
    order = plan.get("operation_order", "add_first")
    peak = max(len(base), len(target)) if order == "remove_first" else len(base) + len(plan["add"])
    demand(peak <= FILTER_LIMIT,
           f"配備途中の最大件数{peak}件がGmailの上限{FILTER_LIMIT}件を超えます")
    added, removed = batch_progress(base, plan, current, log)
    # Every *remaining* deletion must refer to exactly one current filter ID.
    for i, rule in enumerate(plan["remove"]):
        if i in removed:
            continue
        matching = [x for x in current if x["filter"] == rule]
        demand(len(matching) == 1 and isinstance(matching[0].get("id"), str)
               and matching[0]["id"].strip(),
               f"削除対象が最終承認前に一意でなくなりました: "
               f"{format_filter_summary(rule)} / 一致{len(matching)}件。"
               "差分取得から再実行してください")
    if log.exists():
        prior = json.loads(log.read_text(encoding="utf-8"))
        demand(prior.get("plan_digest") == plan["approval_digest"],
               "配備ログに別の計画が記録されています")
    # Validate prospective API payloads without calling a mutating endpoint.
    labels = {name: "pending_" + name for name in plan["new_label_names"]}
    labels.update({name: name for name in plan["expected_label_names"]})
    for i, rule in enumerate(plan["add"]):
        if i not in added:
            to_api(rule, labels, normalization=normalization)


def perform(client, plan: dict, expected: str, digest: str, log: Path, batch_size=1, verbose=False,
            max_batches=None, readback_interval=50):
    """Validate the approved plan and resume deployment using its pinned interpretation."""
    normalization = normalization_for_plan(plan)
    demand(expected.lower() == plan["account"].lower(), "対象アカウントが承認計画と不一致")
    demand(digest == plan["approval_digest"], "承認ハッシュが一致しません")
    demand(plan["approval_digest"] == json_hash(
        {k: v for k, v in plan.items() if k not in {"created_utc", "approval_digest"}}),
        "承認済み計画が改変されています")
    demand(1 <= batch_size <= 25, "batch-sizeは1〜25件で指定してください")
    demand(type(readback_interval) is int and readback_interval > 0, "照合間隔は正の整数で指定してください")
    validate_batch_limit(log, batch_size, max_batches)
    base, target = planned_states(plan)
    order = plan.get("operation_order", "add_first")
    demand(order in ("remove_first", "add_first"), "配備順序が不正です")
    peak = max(len(base), len(target)) if order == "remove_first" else len(base) + len(plan["add"])
    demand(peak <= FILTER_LIMIT,
           f"配備中の最大フィルタ数{peak}件がGmail上限{FILTER_LIMIT}件を超えます")
    current, labels = fetch_for_deploy(client, verbose=verbose, normalization=normalization)
    return perform_batched(client, plan, base, target, current, labels, log, batch_size, verbose,
                           max_batches, readback_interval)


READBACK_DELAYS_SECONDS = (2, 5)  # Exactly three reads, no writes between reads.


def verify_partial(client, base: list[dict], plan: dict, add_count: int, remove_count: int,
                   *, verbose=False, label_cache: dict[str, str] | None = None,
                   expected_filter_ids: set[str] | None = None,
                   diagnostic_dir: Path | None = None):
    """Read back the expected progress in the plan mode without issuing mutations."""
    normalization = normalization_for_plan(plan)
    expected = prefix_counts(base, plan, add_count, remove_count)

    # A mismatching Gmail list may be transient immediately after a mutation.
    # Re-read only; never perform another mutation while the state is uncertain.
    expected_templates = content_by_hash(base + plan["add"])
    attempts = len(READBACK_DELAYS_SECONDS) + 1
    for attempt in range(attempts):
        if label_cache is None:
            current, _ = fetch_for_deploy(client, verbose=verbose, normalization=normalization)
        else:
            current = filters_with_known_labels(client, label_cache, verbose=verbose, normalization=normalization)
        actual = counter([x["filter"] for x in current])
        if expected == actual:
            if attempt:
                print(f"{timestamp_prefix()}API読戻しは再照合{attempt + 1}/{attempts}回目で一致しました。"
                      "後続処理を再開します。", flush=True)
            return True

        missing = expected - actual
        unexpected = actual - expected
        missing_count = sum(missing.values())
        unexpected_count = sum(unexpected.values())
        actual_ids = {row["id"] for row in current if row.get("id")}
        missing_ids = sorted(expected_filter_ids - actual_ids) if expected_filter_ids is not None else []
        unexpected_ids = sorted(actual_ids - expected_filter_ids) if expected_filter_ids is not None else []
        if diagnostic_dir is not None:
            # Retain the *first* inconsistent response, even if later reads recover.
            # The directory is beside the local journal; local/ is gitignored.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            snapshot = diagnostic_dir / (
                f"readback_{stamp}_a{add_count}_r{remove_count}_{attempt+1}.json")
            save_json(snapshot, {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "attempt": attempt + 1,
                "attempts": attempts,
                "completed_add": add_count,
                "completed_remove": remove_count,
                "expected_count": sum(expected.values()),
                "observed_count": sum(actual.values()),
                "missing_rule_hashes": dict(missing),
                "unexpected_rule_hashes": dict(unexpected),
                "missing_filter_ids": missing_ids,
                "unexpected_filter_ids": unexpected_ids,
                "observed_filters": current,
            })
            print(f"{timestamp_prefix()}[診断保存] {snapshot}", flush=True)
        print(f"{timestamp_prefix()}API読戻し不一致 ({attempt + 1}/{attempts}): "
              f"expected={sum(expected.values())} / actual={sum(actual.values())} / "
              f"不足={missing_count} / 予定外={unexpected_count}", flush=True)
        if verbose:
            if expected_filter_ids is not None:
                print(f"{timestamp_prefix()}[詳細] フィルタID差分: "
                      f"不足={missing_ids[:10]} / 予定外={unexpected_ids[:10]}", flush=True)
            for sha in sorted(missing)[:5]:
                rule = expected_templates.get(sha)
                print(f"{timestamp_prefix()}[詳細] 不足 SHA={sha[:16]} "
                      f"{format_filter_summary(rule) if rule else '(ルール特定不可)'}", flush=True)
            for sha in sorted(unexpected)[:5]:
                matching = [row for row in current if json_hash(row["filter"]) == sha]
                ids = [row.get("id") for row in matching]
                print(f"{timestamp_prefix()}[詳細] 予定外 SHA={sha[:16]} id={ids} "
                      f"{format_filter_summary(matching[0]['filter']) if matching else ''}", flush=True)
        if attempt < len(READBACK_DELAYS_SECONDS):
            delay = READBACK_DELAYS_SECONDS[attempt]
            if verbose:
                print(f"{timestamp_prefix()}[詳細] Gmail実設定の再照合まで{delay}s待機（書込みなし）", flush=True)
            time.sleep(delay)
        else:
            raise DeploymentError(
                f"API読戻しが不一致（{attempts}回の読み取り後も継続）。"
                f"expected={sum(expected.values())} / actual={sum(actual.values())} / "
                f"不足={missing_count} / 予定外={unexpected_count}。"
                "後続操作を停止。Gmailを変更せず診断してください")
    raise AssertionError("unreachable readback state")


def cmd_validate(args):
    """Validate XML offline with current compilation and normalization policies."""
    target_path = select_master(args.local_dir, args.target)
    target = compile_xml(target_path, compilation=CURRENT_COMPILATION, normalization=CURRENT_NORMALIZATION)
    report = {
        "target": str(target_path), "target_sha256": file_hash(target_path),
        "target_health": check_xml_rules(atom_entries(target_path)),
        "api_compatible_compilation": True,
        "action_delete_label_only": True,
    }
    if args.base:
        base = compile_xml(args.base, compilation=CURRENT_COMPILATION, normalization=CURRENT_NORMALIZATION)
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
    """Save a read-only plan with current policies and separate compilation/normalization audits."""
    target_path = select_master(args.local_dir, args.target)
    client, creds = get_client(args.client_secrets, args.token)
    acct = account_email(creds)
    demand(acct == args.expected_email.lower(), f"Googleアカウント不一致: {acct} != {args.expected_email}")
    current, labels = api_fetch(client, normalization=CURRENT_NORMALIZATION)
    plan = make_plan(args.base, target_path, [x["filter"] for x in current], acct, labels,
                     args.also_baseline or [], compilation=CURRENT_COMPILATION,
                     normalization=CURRENT_NORMALIZATION, live_rows=current)
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
    show_normalization_summary(plan)


def cmd_apply(args):
    """Apply a pinned approved plan with authentication and deployment timing phases."""
    start_phase("[1/3] 承認計画の読込み")
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    normalization_for_plan(plan)  # Reject unknown interpretations before OAuth/API access.
    start_phase("[2/3] Google OAuth認証とアカウント照合")
    client, creds = get_client(args.client_secrets, args.token, allow_auth=False)
    acct = account_email(creds)
    demand(acct == args.expected_email.lower(), f"認証アカウントが違います: {acct}")
    demand(not plan["remove"] or args.allow_removals,
           f"旧フィルタ{len(plan['remove'])}件の削除予定があります。plan.jsonを確認し、承認する場合だけ--allow-removalsを指定してください")
    start_phase("[3/3] 承認済み計画の配備・API読戻し", deployment=True)
    result = perform(client, plan, acct, args.approve, args.log, args.batch_size, verbose=args.verbose,
                     max_batches=getattr(args, "max_batches", None))
    if result is False:
        observation_outcome("途中終了（保留計画を保持）")


def cmd_verify(args):
    """Compare live settings with Master using the current compiler and normalizer."""
    client, creds = get_client(args.client_secrets, args.token, allow_auth=False)
    acct = account_email(creds)
    demand(acct == args.expected_email.lower(), "認証アカウント不一致")
    target = compile_xml(select_master(args.local_dir, args.target), compilation=CURRENT_COMPILATION,
                         normalization=CURRENT_NORMALIZATION)
    current, _ = api_fetch(client, normalization=CURRENT_NORMALIZATION)
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


def show_normalization_summary(plan: dict, *, verbose=False) -> None:
    """Distinguish field-preserving compilation from established notation equivalence."""
    normalization = normalization_for_plan(plan)
    if normalization == "legacy":
        return
    compilation_audit = plan.get("compilation_audit", [])
    if compilation_audit and verbose:
        print(f"XMLのfrom・subjectを元のフィールドに保持: {len(compilation_audit)}件"
              "（フィールド間の同等性は判定していません）", flush=True)
    matched = plan.get("normalization_matched", 0)
    if matched:
        print(f"表記を統一して一致したルール: {matched}件（変更不要）", flush=True)
    uncertain = False
    for row in plan["add"] + plan["remove"]:
        criteria = row["criteria"]
        query = criteria.get("query")
        known_query = query and (re.fullmatch(r"\{" + FROM_ATOM + r"(?: " + FROM_ATOM + r")+\}", query)
            or normalization == CURRENT_NORMALIZATION and query.startswith("(") and query.endswith(")")
            and simple_spam_query(query[1:-1]))
        if (query and not known_query
                or criteria.get("negatedQuery")
                or " OR " in criteria.get("from", "")
                or "{" in criteria.get("subject", "")):
            uncertain = True
    if uncertain:
        print("同等性未確認: 対象外のquery式・否定・from/subject等のフィールド間移動は"
              "自動判定しません。追加と削除をそれぞれ確認してください。", flush=True)
    if verbose:
        if compilation_audit:
            comparison = plan["compilation_comparison"]
            before, after = comparison["previous"], comparison["current"]
            print(f"[XML変換] 計画作成時のMaster全体とGmailの比較（旧方式→今回）: "
                  f"一致 {before['matched']}→{after['matched']} / "
                  f"追加 {before['add']}→{after['add']} / 削除 {before['remove']}→{after['remove']}", flush=True)
        for note in compilation_audit:
            print(f"[XML変換] 項目={note['entry']} / "
                  f"方式={note['before_compilation']}→{note['after_compilation']} / "
                  f"旧条件={json.dumps(note['before_criteria'], ensure_ascii=False, sort_keys=True)} / "
                  f"今回条件={json.dumps(note['after_criteria'], ensure_ascii=False, sort_keys=True)}", flush=True)
        for note in plan.get("normalization_audit", []):
            print(f"[正規化] {note['source']} / 理由={','.join(note['reasons'])} / "
                  f"変換前={json.dumps(note['before_criteria'], ensure_ascii=False, sort_keys=True)} / "
                  f"変換後={json.dumps(note['after_criteria'], ensure_ascii=False, sort_keys=True)}", flush=True)


def show_group_details(plan: dict, group: str) -> tuple[int, int]:
    """Display EVERY affected sensitive rule, with criteria and API actions."""
    additions = [x for x in plan["add"] if filter_group(x) == group]
    removals = [x for x in plan["remove"] if filter_group(x) == group]
    print(f"\n【{RULE_GROUP_NAMES[group]}】", flush=True)
    print(f"追加 {len(additions)}件 / 削除 {len(removals)}件", flush=True)
    if group == "trash" and (additions or removals):
        print("※ TRASHはAction/Deleteラベルとは異なり、条件に合うメールを実際にゴミ箱へ移動します。", flush=True)
    if not (additions or removals):
        print("変更なし（承認不要）", flush=True)
        return 0, 0
    # A modified condition appears as the removal of the old API filter and
    # creation of a new one. Display both, rather than guessing pairings.
    print("削除する既存ルール（全件）:", flush=True)
    for i, row in enumerate(removals, 1):
        print(f"  - [{i}/{len(removals)}] {format_filter_summary(row)}", flush=True)
    print("新たに作成するルール（全件）:", flush=True)
    for i, row in enumerate(additions, 1):
        print(f"  + [{i}/{len(additions)}] {format_filter_summary(row)}", flush=True)
    return len(additions), len(removals)


def show_run_plan(plan: dict, plan_path: Path, verbose: bool = False,
                  conflicts: list[dict] | None = None) -> None:
    """Show the basic summary first; sensitive rule details follow in review."""
    adds, removes = plan["add"], plan["remove"]
    basic_add = [x for x in adds if filter_group(x) == "other"]
    basic_remove = [x for x in removes if filter_group(x) == "other"]
    print(f"\nGmailフィルタ配備計画: {plan['account']}", flush=True)
    print(f"Master: {plan['target_path']}", flush=True)
    print(f"現行 {plan['baseline_size']}件 → 適用目標 {plan['target_size']}件", flush=True)
    print(f"全差分: 追加 {len(adds)}件 / 削除 {len(removes)}件", flush=True)
    print("\n【基本（Type・Service・Org等）】", flush=True)
    print(f"追加 {len(basic_add)}件 / 削除 {len(basic_remove)}件", flush=True)
    print("最終承認があれば反映します（独立した承認なし）。", flush=True)
    if verbose:
        print("削除する既存ルール:", flush=True)
        for i, row in enumerate(basic_remove, 1):
            print(f"  - [{i}/{len(basic_remove)}] {format_filter_summary(row)}", flush=True)
        print("新たに作成するルール:", flush=True)
        for i, row in enumerate(basic_add, 1):
            print(f"  + [{i}/{len(basic_add)}] {format_filter_summary(row)}", flush=True)
    else:
        print("基本ルールの全件詳細は -v で表示できます。", flush=True)
    if conflicts:
        print(f"\n【削除対象の事前検証：スキップ候補 {len(conflicts)}件】", flush=True)
        print("対応するGmailフィルタを一意に特定できないため、"
              "承認対象となったグループでも以下は変更しません。", flush=True)
        for i, row in enumerate(conflicts, 1):
            print(f"  ~ [{i}/{len(conflicts)}] 理由={row['reason']} / "
                  f"一致件数={row['matches']} / {format_filter_summary(row['rule'])}", flush=True)
    print(f"\n全件の差分・承認計画: {plan_path}", flush=True)


def confirm_interactive(prompt: str = "上記の変更をGmailに反映します。進めていいですか？ [y/N]: ") -> bool:
    """Read explicit terminal consent while counting only time spent waiting for input."""
    # No implicit approval through piped input or background/scheduled runs.
    demand(sys.stdin.isatty(), "対話端末が必要です。docker compose run（-Tなし）で実行してください")
    try:
        started = time.perf_counter()
        try:
            answer = input(prompt).strip().lower()
        finally:
            observation = OBSERVATION.get()
            if observation is not None:
                observation.approval_wait += time.perf_counter() - started
    except (EOFError, KeyboardInterrupt):
        print("\nキャンセルしました。この確認での追加の変更は行いません。")
        return False
    return answer in {"y", "yes"}


def read_gmail_filters_preflight(client, *, normalization=CURRENT_NORMALIZATION):
    """Report API configuration/read errors before any Gmail writes can occur."""
    try:
        return api_fetch(client, normalization=normalization)
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
    import gmail_message_reclassifier as messages
    with messages.exclusive_lock(args.local_dir):
        if not messages.authorize_run(args):
            return
        if messages.resume_before_deploy(args):
            return
        pending_file = args.local_dir / 'run_pending.json'
        pending_plan = json.loads(pending_file.read_text(encoding='utf-8')) if pending_file.exists() else None
        compilation, normalization = (plan_modes(pending_plan) if pending_plan is not None else
                                      (CURRENT_COMPILATION, CURRENT_NORMALIZATION))
        start_phase("[1/6] 最新Master XMLを選択・検証")
        print("[1/6] 最新Master XMLを選択・検証中...", flush=True)
        target_path = select_master(args.local_dir, args.target)
        target = compile_xml(target_path, compilation=compilation, normalization=normalization)  # Before OAuth.
        print(f"      検証完了: {target_path.name}（{len(target)}件）", flush=True)
        if args.verbose:
            print(f"{timestamp_prefix()}[詳細] Master SHA-256: {file_hash(target_path)}", flush=True)
        start_phase("[2/6] Google OAuth認証とアカウント照合")
        print("[2/6] Google OAuth認証とアカウント照合中...", flush=True)
        client, creds = get_client(args.client_secrets, args.token)
        acct = account_email(creds)
        demand(acct == args.expected_email, f"認証Googleアカウント不一致: {acct} != {args.expected_email}")
        print(f"      認証済み: {acct}", flush=True)
        start_phase("[3/6] Gmailの現行ラベル・フィルタ取得")
        print("[3/6] Gmailの現行ラベル・フィルタを取得中...", flush=True)
        current, labels = timed_operation(
            "Gmail初期フィルタ・ラベル取得",
            lambda: read_gmail_filters_preflight(client, normalization=normalization), args.verbose)
        live = [x['filter'] for x in current]
        print(f"      取得完了: {len(live)}フィルタ / {len(labels)}ラベル", flush=True)
        start_phase("[4/6] 変更差分と保護ルールの照合")
        print("[4/6] 変更差分と保護ルールを照合中...", flush=True)
        plan_file = args.local_dir / 'run_plan.json'
        if pending_plan is not None:
            plan = pending_plan
            demand(plan['account'] == acct, '途中計画のアカウントが一致しません')
            demand(plan['target_path'] == str(target_path.resolve()), '途中計画の対象Masterと現行Masterが異なります。再開を停止しました')
            base, old_target = planned_states(plan)
            demand(file_hash(target_path) == plan["target_sha256"], '途中計画のMasterが変更されています')
            journal = args.local_dir / 'logs' / (plan['approval_digest'] + '.json')
            added, removed = batch_progress(base, plan, current, journal, labels)
            a_done, r_done = len(added), len(removed)
            if a_done == len(plan['add']) and r_done == len(plan['remove']):
                state = json.loads(journal.read_text(encoding='utf-8')) if journal.exists() else {
                    "account": acct, "plan_digest": plan['approval_digest'], "events": []}
                state.update(verified=True, completed_add=a_done, completed_remove=r_done,
                             acknowledged_add=a_done, acknowledged_remove=r_done)
                if "batch_checkpoint" in state:
                    state["batch_checkpoint"] = {"schema": 1, "rows": current, "labels": dict(labels),
                        "add": sorted(added), "remove": sorted(removed), "pending": None,
                        "readback_confirmed": True}
                save_json(journal, state)
                print(f"前回の配備は既に反映済みです。MasterとAPI一致: {len(old_target)}件", flush=True)
                pending_file.unlink()
                observation_outcome("前回配備の反映済みを照合")
                messages.offer_after_deploy(args, target_path, current)
                return
            print(f"前回の中断計画を検出しました（追加済 {a_done}/{len(plan['add'])}、削除済 {r_done}/{len(plan['remove'])}）。")
        else:
            plan = make_plan(None, target_path, live, acct, labels, compilation=compilation,
                             normalization=normalization, live_rows=current)
            show_normalization_summary(plan, verbose=args.verbose)
            if not plan['add'] and not plan['remove']:
                if normalization != "legacy":
                    save_json(plan_file, plan)  # Preserve the audit even when no writes are needed.
                print(f"変更不要：{acct} の{len(target)}フィルタが最新Masterに一致しています。", flush=True)
                observation_outcome("変更不要")
                messages.offer_after_deploy(args, target_path, current)
                return
            save_json(plan_file, plan)
        if args.verbose:
            print(f"{timestamp_prefix()}[詳細] 追加 {len(plan['add'])}件 / 削除 {len(plan['remove'])}件 / 新規ラベル {len(plan['new_label_names'])}件", flush=True)
            print(f"{timestamp_prefix()}[詳細] 計画SHA-256: {plan['approval_digest']}", flush=True)
        conflicts = [] if pending_file.exists() else deletion_preview(plan, current)
        start_phase("[5/6] 差分内容の確認と承認")
        print("[5/6] 差分内容の確認と承認...", flush=True)
        show_run_plan(plan, plan_file if not pending_file.exists() else pending_file,
                      verbose=args.verbose, conflicts=conflicts)
        if pending_file.exists():
            # Resume a previously approved, pinned set. Never silently include
            # categories declined during the original review.
            for group in ("delete", "trash"):
                show_group_details(plan, group)
            print("\n以前に承認した配備の続きを再開します。", flush=True)
            print(f"対象: 追加 {len(plan['add'])}件 / 削除 {len(plan['remove'])}件", flush=True)
        else:
            selected = interactive_group_selection(plan)
            if not selected:
                print("承認された変更がないため、Gmail設定は変更しません。", flush=True)
                observation_outcome("承認辞退")
                return
            plan = select_plan_groups(plan, selected, current=current)
            # Validate the approved subset, labels, and live snapshot BEFORE
            # the final question. Keep this check read-only.
            planned_states(plan)
            refreshed, _ = api_fetch(client, verbose=args.verbose, normalization=normalization)
            demand(counter(x["filter"] for x in refreshed) == counter(live),
                   "承認操作中にGmail側でフィルタ設定が変更されました。再実行して差分を更新してください")
            skipped = plan["skipped_deletions"]
            if skipped:
                print(f"\n【スキップ予定：{len(skipped)}件】", flush=True)
                for i, row in enumerate(skipped, 1):
                    print(f"  ~ [{i}/{len(skipped)}] 理由={row['reason']} "
                          f"一致件数={row['matches']} / {format_filter_summary(row['rule'])}",
                          flush=True)
            print("\n【最終確認】", flush=True)
            print("反映対象: " + ", ".join(RULE_GROUP_NAMES[x] for x in RULE_GROUPS if x in selected), flush=True)
            print("見送り対象: " + (
                ", ".join(RULE_GROUP_NAMES[x] for x in RULE_GROUPS if x not in selected) or "なし"), flush=True)
            print(f"実行件数: 追加 {len(plan['add'])}件 / 削除 {len(plan['remove'])}件"
                  f" / スキップ {len(plan['skipped_deletions'])}件", flush=True)
        # Every deterministic precondition is checked BEFORE final [y/N].
        latest = api_fetch(client, verbose=args.verbose, normalization=normalization)[0] if pending_file.exists() else refreshed
        journal = args.local_dir / 'logs' / (plan['approval_digest'] + '.json')
        validate_before_final_approval(plan, acct, journal, args.batch_size, latest,
                                       max_batches=getattr(args, "max_batches", None))
        print("事前検証: 計画・削除ID・容量・進捗ログ・API payloadを確認済み。", flush=True)
        if not confirm_interactive("上記の内容でGmailのフィルタ設定を変更しますか？ [y/N]: "):
            print("最終承認されなかったため、Gmail設定は変更していません。", flush=True)
            observation_outcome("承認辞退")
            return
        if not pending_file.exists():
            save_json(plan_file, plan)
            save_json(pending_file, plan)  # Save only after final approval, before API mutation.
        start_phase("[6/6] Gmailへ配備・API読戻し検証", deployment=True)
        print("[6/6] Gmailへ配備し、APIの読戻しで検証中...", flush=True)
        result = timed_operation(
            "承認済み計画の配備（削除・追加・検証）",
            lambda: perform(client, plan, acct, plan['approval_digest'],
                            journal, args.batch_size, verbose=args.verbose,
                            max_batches=getattr(args, "max_batches", None)),
            args.verbose)
        if result is False:
            observation_outcome("途中終了（保留計画を保持）")
            return
        pending_file.unlink()
        # perform's final confirmed snapshot already includes skipped rules.
        actual = result["rows"]
        master = compile_xml(target_path, compilation=compilation, normalization=normalization)
        master_only, gmail_only = delta([x["filter"] for x in actual], master)
        skipped = plan.get("skipped_deletions", [])
        print("\n【配備結果】", flush=True)
        print(f"  削除成功: {len(plan['remove'])}件", flush=True)
        print(f"  追加成功: {len(plan['add'])}件", flush=True)
        journal_events = json.loads(journal.read_text(encoding="utf-8")).get("events", [])
        already_absent = sum(x.get("kind") == "already_absent" for x in journal_events)
        print(f"  スキップ（未適用）: {len(skipped)}件", flush=True)
        if already_absent:
            print(f"  処理不要（既に削除済みと照合）: {already_absent}件", flush=True)
        for i, entry in enumerate(skipped, 1):
            print(f"  ~ [{i}/{len(skipped)}] {entry['reason']} / "
                  f"{format_filter_summary(entry['rule'])}", flush=True)
        if master_only or gmail_only:
            print(f"結果: 一部未適用（Masterのみ {len(master_only)}件 / "
                  f"Gmailのみ {len(gmail_only)}件）。次回runで再提案します。", flush=True)
        else:
            print("結果: Master XMLと完全一致しました。", flush=True)
        print("Gmail UIのfresh exportと新着メール分類の確認は別途必要です。", flush=True)
        messages.offer_after_deploy(args, target_path, actual)


def resolve_expected_email(cli_email: str | None) -> str:
    """Require an account pin, preferring the CLI flag over environment."""
    configured = cli_email if cli_email is not None else os.environ.get("GMAIL_EXPECTED_EMAIL", "")
    email = configured.strip().lower()
    demand(bool(email), "対象メールアドレスが未設定です。.env の GMAIL_EXPECTED_EMAIL を設定するか --expected-email を指定してください")
    demand(bool(re.fullmatch(r"[^@\s]+@[^@\s]+", email)), "--expected-email / GMAIL_EXPECTED_EMAIL の形式が不正です")
    return email


def main(argv=None):
    """Dispatch CLI commands, summarize run/apply measurements, and classify failures."""
    root = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="安全なGmail filter XML/API配備ツール（標準は読み取り専用）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # No renaming: latest versioned master is discovered in local/masters/ or local/.
    default_local = root / "local"
    p = sub.add_parser("run", help="検証→差分表示→対話承認→配備→API照合を1コマンドで実行")
    p.add_argument("--target", type=Path, help="任意: デフォルトは最新Masterを自動選択")
    p.add_argument("--batch-size", type=int, default=1, help="削除・追加のHTTPバッチ件数。1〜25、既定1。1は逐次処理")
    p.add_argument("-v", "--verbose", action="store_true", help="フィルタ単位の処理・API照合の詳細を表示")
    p.add_argument("--reclassify-existing", action="store_true",
                   help="起動時に既存メールのラベル置換を明示承認し、配備・照合後にバックアップと再分類を自動実行")
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
    p.add_argument("--batch-size", type=int, default=1, help="削除・追加のHTTPバッチ件数。1〜25、既定1")
    p.add_argument("-v", "--verbose", action="store_true", help="フィルタ単位の配備・API照合詳細を表示")
    p.set_defaults(func=cmd_apply)
    p = sub.add_parser("verify", help="Gmail API実設定と最新版Masterの完全照合")
    p.add_argument("--target", type=Path, help="指定しなければ最新版を自動選択")
    p.add_argument("--out", type=Path, default=default_local / "verify.json")
    p.set_defaults(func=cmd_verify)
    import gmail_message_reclassifier as messages
    p = sub.add_parser("reclassify", help="既存メールのユーザーラベル再分類（専用対話承認）")
    p.add_argument("--target", type=Path)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--plan-only", action="store_true", help="完全なバックアップ・計画の作成のみ")
    group.add_argument("--resume", help="固定済みメール計画のrun-id")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=messages.command)
    p = sub.add_parser("restore-labels", help="指定run-idで変更したユーザーラベルをバックアップから復元")
    p.add_argument("--run-id", required=True)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=messages.command)
    for name in ("validate", "plan", "apply", "verify", "run", "reclassify", "restore-labels"):
        sub.choices[name].add_argument("--local-dir", type=Path, default=default_local,
                                       help="Master XML保存フォルダ。既定はlocal/")
    for name in ("plan", "apply", "verify", "run", "reclassify", "restore-labels"):
        p = sub.choices[name]
        p.add_argument("--expected-email", help="対象Gmailアドレス。省略時はGMAIL_EXPECTED_EMAILを使用")
        p.add_argument("--client-secrets", type=Path, default=default_local / "credentials.json")
        p.add_argument("--token", type=Path, default=default_local / "token.json")
    for name in ("run", "reclassify"):
        sub.choices[name].add_argument("--skip-unknown-message-labels", action="store_true",
            help="計画時にラベル一覧にないIDを持つメールを記録・保留し、確認できるメールを処理（保留ありは終了コード4）")
    for name in ("run", "reclassify", "restore-labels"):
        p = sub.choices[name]
        p.add_argument("--message-token", type=Path, help="メール専用OAuthトークン（既定: local-dir/message_token.json）")
        p.add_argument("--message-batch-size", type=int, default=50, help="メール更新件数: 1〜1000、既定50")
        p.add_argument("--max-message-batches", type=int, help="指定バッチ数のメール更新・読戻し後に停止")
    for name in ("run", "apply"):
        sub.choices[name].add_argument("--max-batches", type=int,
                                       help="指定数のバッチを照合後、保留計画を保持して終了（既定は全件）")
    args = ap.parse_args(argv)
    try:
        with observe_command(args.cmd, args.verbose) if args.cmd in ("run", "apply", "reclassify", "restore-labels") else nullcontext():
            max_batches = getattr(args, "max_batches", None)
            demand(max_batches is None or max_batches > 0, "max-batchesは1以上で指定してください")
            if args.cmd in ("plan", "apply", "verify", "run", "reclassify", "restore-labels"):
                args.expected_email = resolve_expected_email(args.expected_email)
            if hasattr(args, "message_batch_size"):
                demand(1 <= args.message_batch_size <= 1000, "message-batch-sizeは1〜1000です")
                demand(args.max_message_batches is None or args.max_message_batches > 0, "max-message-batchesは1以上です")
            with messages.exclusive_lock(args.local_dir) if args.cmd in ("apply", "reclassify", "restore-labels") else nullcontext():
                if args.cmd == "apply":
                    demand(messages.pending(args.local_dir) is None, "メール更新が未完了です。先に再開・復元してください")
                args.func(args)
    except DeploymentError as exc:
        print("STOP: " + str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print("STOP: 予期しないエラーです。途中まで反映された可能性があります。planを再実行せず状態照合してください: " + str(exc), file=sys.stderr)
        return 3
    return getattr(args, "exit_code", 0)


if __name__ == "__main__":
    # The message module shares exceptions, observation context and helpers with this CLI instance.
    sys.modules["gmail_filter_deployer"] = sys.modules[__name__]
    sys.exit(main())
