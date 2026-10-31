"""Message-only label replacement with immutable plans and durable, reversible intents.

Searches run before any writes. Settings compilation remains in the deployer;
this module never submits system labels, deletes messages, or creates labels.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from itertools import islice
import uuid

import gmail_filter_deployer as d

SCHEMA = "gmail-message-reclassification/v1"
DEFERRED_SCHEMA = "gmail-message-reclassification/v2"
SEARCH_VERSION = "message-search/v1"
RUN_AUTHORIZATION_POLICY = "replace-eligible-user-labels/v1"
EXCLUDED = frozenset({"SENT", "DRAFT", "SPAM", "TRASH", "CHAT"})
SYSTEM_IDS = EXCLUDED | {"INBOX", "UNREAD", "STARRED", "IMPORTANT", "DRAFTS", "ALL"}
PENDING = "messages_pending.json"
ATOM = r'[^\s{}()"\\:]+'
QUOTED = r'"[^"\\]*"'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def file_digest(path):
    """Hash arbitrarily large SQLite snapshots without reading them into memory."""
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def require(value, message):
    d.demand(value, message)


def sync_directory(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, value):
    d.save_json(path, value)
    sync_directory(Path(path).parent)


@contextmanager
def exclusive_lock(local_dir):
    """All CLI mutations share this lock; unsupported platforms fail closed."""
    import fcntl
    local_dir.mkdir(parents=True, exist_ok=True)
    with (local_dir / "run.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise d.DeploymentError("別の更新コマンドが進行中です") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def pending(local_dir):
    path = local_dir / PENDING
    if not path.exists():
        return None
    obj = json.loads(path.read_text())
    require(obj.get("mode") in ("apply", "restore"), "メール保留計画のmodeが不正です")
    run_directory(local_dir, obj.get("run_id", ""))
    return obj


def run_directory(local_dir, run_id):
    require(isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run_id),
            "不正な再分類run-idです")
    path = local_dir / "reclassification" / run_id
    require(not path.is_symlink(), "再分類ディレクトリのsymlinkは使用できません")
    return path


def balanced(value):
    """Check wrappers, not Gmail's search semantics. Never repair an expression."""
    stack, quoted = [], False
    require(isinstance(value, str) and value.strip() and not any(c in value for c in "\r\n\x00\\"),
            "検索条件が空、または未対応の制御文字・エスケープを含みます")
    for c in value:
        if c == '"':
            quoted = not quoted
        elif not quoted:
            if c in "({":
                stack.append(c)
            elif c in ")}":
                require(stack and stack.pop() == {")": "(", "}": "{"}[c],
                        "検索条件の括弧が対応していません")
    require(not stack and not quoted, "検索条件の括弧・引用符が閉じていません")


def native_query(field, value):
    """Accept a deliberately small native-field grammar, preserving each phrase."""
    value = value.strip()
    balanced(value)
    if re.fullmatch(QUOTED, value):
        return field + ":" + value
    require(not re.search(r"(?:^|\s)(?:AND|OR|NOT|AROUND)(?:\s|$)", value),
            f"{field}の未対応論理式です")
    require(re.fullmatch(rf"{ATOM}(?: +{ATOM})*", value), f"{field}の未対応構文です")
    require(not any(t.startswith(("-", "+", "*")) for t in value.split()),
            f"{field}の未対応接頭演算子です")
    # Native subject is a phrase; sender/recipient display names are phrases too.
    return field + ":" + (('"' + value + '"') if " " in value else value)


def search_spec(template):
    """AND of positive OR-groups, minus an unchanged negated-query result set."""
    criteria = template["criteria"]
    require(set(criteria) <= {"from", "to", "subject", "query", "negatedQuery"},
            "再分類で未対応のcriteriaです")
    groups = []
    if criteria.get("from"):
        choices = d.split_or_atoms(criteria["from"]) or [criteria["from"]]
        groups.append([native_query("from", v) for v in choices])
    if criteria.get("to"):
        groups.append([native_query(field, criteria["to"]) for field in ("to", "cc", "bcc")])
    if criteria.get("subject"):
        value = criteria["subject"].strip()
        if value.startswith("{"):
            balanced(value)
            item = rf"(?:{QUOTED}|{ATOM})"
            require(re.fullmatch(rf"\{{\s*{item}(?:\s+{item})*\s*\}}", value),
                    "件名のOR群は平坦な語・引用句だけに対応します")
            words = re.findall(item, value[1:-1])
            groups.append([native_query("subject", word) for word in words])
        else:
            groups.append([native_query("subject", value)])
    if criteria.get("query"):
        balanced(criteria["query"])
        groups.append([criteria["query"]])
    negative = criteria.get("negatedQuery")
    if negative:
        balanced(negative)
    require(groups or negative, "再分類の条件がありません")
    return {"all": groups, "exclude": negative}


def user_rules(templates):
    return [t for t in templates if any(x.startswith("label:") for x in t["action"]["addLabelIds"])]


class UnknownMessageLabels(d.DeploymentError):
    """Well-formed metadata with label IDs absent from the saved catalog."""
    def __init__(self, row):
        super().__init__("メッセージにラベル一覧にないIDがあります")
        self.row = row


class Gmail:
    """One-attempt writes; bounded read retries, minimal message projections."""
    def __init__(self, client, verbose=False, read_batch_size=1):
        require(isinstance(read_batch_size, int) and 1 <= read_batch_size <= 50,
                "メール読取りバッチ件数は1〜50です")
        self.client, self.verbose = client, verbose
        self.read_batch_size = read_batch_size

    def read(self, kind, request):
        return d.read_operation("Gmail " + kind, lambda: request.execute(num_retries=0), self.verbose)

    def labels(self):
        rows = self.read("labels.list", self.client.users().labels().list(userId="me")).get("labels", [])
        result = {}
        names = set()
        for row in rows:
            lid, name, kind = row.get("id"), row.get("name"), str(row.get("type", "")).lower()
            require(isinstance(lid, str) and lid and isinstance(name, str) and name and
                    kind in ("user", "system") and lid not in result and name not in names,
                    "ラベル一覧のID・名前・種別が不正です")
            result[lid] = {"name": name, "type": kind}
            names.add(name)
        require(not any((lid in SYSTEM_IDS or lid.startswith("CATEGORY_")) and row["type"] != "system"
                        for lid, row in result.items()),
                "システムラベルの種別が不正です")
        return result

    def ids(self, query=None):
        token, seen = None, set()
        while True:
            args = {"userId": "me", "includeSpamTrash": True, "maxResults": 500,
                    "fields": "messages(id,threadId),nextPageToken"}
            if query is not None:
                args["q"] = query
            if token:
                args["pageToken"] = token
            result = self.read("messages.list", self.client.users().messages().list(**args))
            for row in result.get("messages", []):
                require(isinstance(row.get("id"), str) and row["id"], "messages.listのIDが不正です")
                yield row["id"]
            token = result.get("nextPageToken")
            if not token:
                break
            require(token not in seen, "messages.listのページが循環しています")
            seen.add(token)

    def message_request(self, mid):
        return self.client.users().messages().get(
            userId="me", id=mid, format="minimal", fields="id,threadId,labelIds,historyId")

    @staticmethod
    def metadata(mid, row, labels):
        lids = row.get("labelIds", [])
        require(row.get("id") == mid and isinstance(row.get("threadId"), str) and
                isinstance(lids, list) and all(isinstance(x, str) and x for x in lids)
                and len(lids) == len(set(lids)), "メッセージのID・ラベルが不正です")
        if set(lids) - labels.keys():
            raise UnknownMessageLabels(row)
        return {"id": mid, "threadId": row["threadId"], "labelIds": sorted(lids),
                "historyId": row.get("historyId")}

    def message(self, mid, labels):
        return self.metadata(mid, self.read("messages.get", self.message_request(mid)), labels)

    def snapshots(self, ids):
        """Fresh minimal GETs only; require every response before yielding a chunk.

        Whole-chunk retries are safe because this path never contains writes.
        Pace batches to at most 20 message GETs/second, leaving quota headroom.
        """
        remaining = iter(ids)
        while chunk := list(islice(remaining, self.read_batch_size)):
            if self.read_batch_size == 1:
                yield chunk[0], self.read("messages.get", self.message_request(chunk[0]))
                continue
            require(len(set(chunk)) == len(chunk), "メール読取りバッチのIDが重複しています")
            started = time.monotonic()

            def fetch():
                rows, failures, seen, invalid = {}, {}, set(), []
                def observed(mid, row, error):
                    if mid not in chunk or mid in seen:
                        invalid.append(mid)
                        return
                    seen.add(mid)
                    if error is not None:
                        failures[mid] = error
                    else:
                        rows[mid] = row
                batch = self.client.new_batch_http_request(callback=observed)
                for mid in chunk:
                    batch.add(self.message_request(mid), request_id=mid)
                batch.execute()
                require(not invalid and seen == set(chunk), "メール読取りバッチの応答が不足・重複しています")
                if failures:
                    raise next(iter(failures.values()))
                return rows

            rows = d.read_operation("Gmail messages.get batch", fetch, self.verbose)
            delay = len(chunk) / 20 - (time.monotonic() - started)
            if delay > 0:
                time.sleep(delay)
            for mid in chunk:
                yield mid, rows[mid]

    def history_start(self):
        row = self.read("getProfile", self.client.users().getProfile(userId="me"))
        require(isinstance(row.get("historyId"), str) and row["historyId"].isdigit(), "履歴位置が不正です")
        return row["historyId"]

    def changes(self, start):
        """Return added IDs and IDs with any event since the fixed initial cursor."""
        added, changed, seen, token = set(), set(), set(), None
        while True:
            args = {"userId": "me", "startHistoryId": start, "maxResults": 500}
            if token:
                args["pageToken"] = token
            row = self.read("history.list", self.client.users().history().list(**args))
            for event in row.get("history", []):
                for kind in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
                    for item in event.get(kind, []):
                        mid = item["message"]["id"]
                        changed.add(mid)
                        if kind == "messagesAdded":
                            added.add(mid)
                changed.update(x["id"] for x in event.get("messages", []))
            token = row.get("nextPageToken")
            if not token:
                return added, changed
            require(token not in seen, "history.listのページが循環しています")
            seen.add(token)

    def filters_match(self, rules, labels, *, normalization=d.CURRENT_NORMALIZATION):
        names = {lid: row["name"] for lid, row in labels.items()}
        rows = self.read("filters.list", self.client.users().settings().filters().list(userId="me"))
        live = [d.live_as_template(x, names, normalization=normalization)
                for x in rows.get("filter", [])]
        require(d.counter(user_rules(live)) == d.counter(user_rules(rules)),
                "ユーザーラベル用フィルタがMasterと一致しません。再分類を見送ります")

    def modify(self, ids, add, remove):
        return d.timed_operation("Gmail messages.batchModify", lambda:
            self.client.users().messages().batchModify(userId="me", body={
                "ids": ids, "addLabelIds": add, "removeLabelIds": remove}).execute(num_retries=0),
            self.verbose)


def connect_db(path, *, readonly=False):
    if readonly:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        db = sqlite3.connect(path)
        path.chmod(0o600)
        db.execute("PRAGMA synchronous=FULL")
    db.row_factory = sqlite3.Row
    return db


def create_plan(api, local_dir, master, account, *, skip_unknown_labels=False):
    """Finish and hash a complete snapshot before making it eligible for approval."""
    require(pending(local_dir) is None, "未完了のメール更新があります。先に再開または復元してください")
    require(not (local_dir / "run_pending.json").exists(), "フィルタ配備が未完了です")
    original_hash = d.file_hash(master)
    templates = d.compile_xml(master)
    rules = user_rules(templates)
    specs = [search_spec(t) for t in rules]  # All unsupported native forms fail before API reads.
    labels = api.labels()
    api.filters_match(rules, labels)
    user_ids = {lid for lid, row in labels.items() if row["type"] == "user"}
    name_ids = {row["name"]: lid for lid, row in labels.items() if row["type"] == "user"}
    for t in rules:
        require(all(x[6:] in name_ids for x in t["action"]["addLabelIds"] if x.startswith("label:")),
                "Masterのユーザーラベルが存在しません")
    run_id = uuid.uuid4().hex
    directory = run_directory(local_dir, run_id)
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    copied = directory / "master.xml"
    with copied.open("xb") as f:
        f.write(master.read_bytes())
        f.flush()
        os.fsync(f.fileno())
    copied.chmod(0o600)
    require(d.file_hash(copied) == original_hash == d.file_hash(master), "Masterが計画作成中に変更されました")
    db = connect_db(directory / "plan.sqlite")
    try:
        db.executescript("""
            CREATE TABLE messages(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                before_labels TEXT NOT NULL, desired TEXT NOT NULL, matches INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE rules(number INTEGER PRIMARY KEY, template TEXT NOT NULL, spec TEXT NOT NULL);
            CREATE TABLE queries(query TEXT PRIMARY KEY);
            CREATE TABLE query_hits(query TEXT, message TEXT, PRIMARY KEY(query,message));
            CREATE TABLE matches(rule INTEGER, message TEXT, PRIMARY KEY(rule,message));
            CREATE TABLE desired_labels(message TEXT, label TEXT, PRIMARY KEY(message,label));
        """)
        if skip_unknown_labels:
            db.execute("CREATE TABLE deferred(id TEXT PRIMARY KEY, metadata TEXT NOT NULL)")
        deferred = 0
        start = api.history_start()
        first_ids = set(api.ids())
        added, changed = api.changes(start)
        require(not changed - added, "列挙中に既存メールが変更されました。計画を作り直してください")
        initial_ids = first_ids - added
        excluded = 0
        for n, (mid, raw) in enumerate(api.snapshots(sorted(initial_ids)), 1):
            try:
                row = api.metadata(mid, raw, labels)
            except UnknownMessageLabels as exc:
                if not skip_unknown_labels:
                    raise
                db.execute("INSERT INTO deferred VALUES(?,?)", (mid, encode(exc.row)))
                deferred += 1
                continue
            if set(row["labelIds"]) & EXCLUDED:
                excluded += 1
                continue
            db.execute("INSERT INTO messages VALUES(?,?,?,?,0)",
                       (mid, row["threadId"], encode(row["labelIds"]), "[]"))
            if n % 500 == 0:
                print(f"[メール計画] メタデータ取得 {n}/{len(initial_ids)}", flush=True)
        db.commit()

        def search(query):
            if not db.execute("SELECT 1 FROM queries WHERE query=?", (query,)).fetchone():
                for mid in api.ids(query):
                    db.execute("INSERT OR IGNORE INTO query_hits SELECT ?,id FROM messages WHERE id=?", (query, mid))
                db.execute("INSERT INTO queries VALUES(?)", (query,))
                db.commit()
            return {r[0] for r in db.execute("SELECT message FROM query_hits WHERE query=?", (query,))}

        target_ids = {r[0] for r in db.execute("SELECT id FROM messages")}
        for number, (rule, spec) in enumerate(zip(rules, specs), 1):
            matched = set(target_ids)
            for group in spec["all"]:
                hits = set()
                for query in group:
                    hits.update(search(query))
                matched.intersection_update(hits)
            if spec["exclude"]:
                matched.difference_update(search(spec["exclude"]))
            db.execute("INSERT INTO rules VALUES(?,?,?)", (number, encode(rule), encode(spec)))
            ids = [name_ids[x[6:]] for x in rule["action"]["addLabelIds"] if x.startswith("label:")]
            db.executemany("INSERT INTO matches VALUES(?,?)", ((number, mid) for mid in matched))
            db.executemany("UPDATE messages SET matches=matches+1 WHERE id=?", ((mid,) for mid in matched))
            db.executemany("INSERT OR IGNORE INTO desired_labels VALUES(?,?)",
                           ((mid, lid) for mid in matched for lid in ids))
            db.commit()
            if number % 50 == 0:
                print(f"[メール計画] ルール照合 {number}/{len(rules)}", flush=True)
        for mid in target_ids:
            desired = [r[0] for r in db.execute("SELECT label FROM desired_labels WHERE message=? ORDER BY label", (mid,))]
            db.execute("UPDATE messages SET desired=? WHERE id=?", (encode(desired), mid))
        second_ids = set(api.ids())
        new_ids, changed = api.changes(start)
        require(not changed - new_ids and second_ids - new_ids == initial_ids,
                "計画中に既存メールが変更・増減しました。計画を作り直してください")
        require(api.labels() == labels, "計画中にラベル定義が変更されました")
        api.filters_match(rules, labels)
        summary = {"target": len(target_ids), "changed": 0, "unchanged": 0, "unmatched": 0,
                   "multiple": 0, "excluded": excluded, "new_arrivals": len(new_ids),
                   "skipped_rules": len(templates) - len(rules), "unsupported": 0}
        if skip_unknown_labels:
            summary["deferred_unknown_labels"] = deferred
        additions, removals = Counter(), Counter()
        for row in db.execute("SELECT * FROM messages"):
            before, after = set(json.loads(row["before_labels"])) & user_ids, set(json.loads(row["desired"]))
            summary["changed" if before != after else "unchanged"] += 1
            summary["unmatched"] += row["matches"] == 0
            summary["multiple"] += row["matches"] > 1
            additions.update(after - before)
            removals.update(before - after)
        db.commit()
    finally:
        db.close()
    manifest = {"schema": DEFERRED_SCHEMA if skip_unknown_labels else SCHEMA, "search_version": SEARCH_VERSION, "run_id": run_id,
                "compilation_version": d.CURRENT_COMPILATION, "normalization_version": d.CURRENT_NORMALIZATION,
                "account": account, "labels": labels, "history_start": start,
                "files": {"master.xml": file_digest(copied), "plan.sqlite": file_digest(directory / "plan.sqlite")},
                "summary": summary, "additions": dict(additions), "removals": dict(removals)}
    manifest["digest"] = d.json_hash(manifest)
    save(directory / "manifest.json", manifest)  # Only complete plans have a manifest.
    (directory / "plan.sqlite").chmod(0o400)
    copied.chmod(0o400)
    return directory


class Plan:
    """A verified immutable plan; never recompiles Master during resume/restore."""
    def __init__(self, directory, account):
        self.directory = directory
        path = directory / "manifest.json"
        require(path.is_file(), "完全なメール計画・バックアップがありません")
        self.manifest = json.loads(path.read_text())
        m = self.manifest
        require(m.get("schema") in (SCHEMA, DEFERRED_SCHEMA) and m.get("search_version") == SEARCH_VERSION,
                "未対応のメール計画versionです")
        require(m.get("compilation_version") == "xml-fields-v1" and m.get("normalization_version") == "query-v2",
                "未対応のメール計画コンパイル・正規化versionです")
        require(m.get("account") == account and m.get("run_id") == directory.name,
                "メール計画のアカウント・run-idが一致しません")
        require(m.get("digest") == d.json_hash({k: v for k, v in m.items() if k != "digest"}),
                "メール計画manifestのdigest不一致")
        require(set(m["files"]) == {"master.xml", "plan.sqlite"}, "バックアップ構成が不正です")
        self.verify_files()
        self.db = connect_db(directory / "plan.sqlite", readonly=True)
        try:
            require(self.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "計画DBが破損しています")
            require(self.db.execute("SELECT count(*) FROM messages").fetchone()[0] == m["summary"]["target"],
                    "バックアップ件数不一致")
            self.deferred_count = 0
            if m["schema"] == DEFERRED_SCHEMA:
                for row in self.db.execute("SELECT * FROM deferred"):
                    metadata = json.loads(row["metadata"])
                    lids = metadata.get("labelIds")
                    require(metadata.get("id") == row["id"] and isinstance(metadata.get("threadId"), str)
                            and isinstance(lids, list) and all(isinstance(x, str) and x for x in lids)
                            and len(lids) == len(set(lids)) and set(lids) - m["labels"].keys()
                            and not self.db.execute("SELECT 1 FROM messages WHERE id=?", (row["id"],)).fetchone(),
                            "保留メールの記録が不正です")
                    self.deferred_count += 1
                require(self.deferred_count == m["summary"].get("deferred_unknown_labels"),
                        "保留メールの件数不一致")
            self.labels = m["labels"]
            self.users = {lid for lid, row in self.labels.items() if row["type"] == "user"}
            self.rules = [json.loads(row[0]) for row in self.db.execute("SELECT template FROM rules ORDER BY number")]
            for row in self.rows():
                require(set(json.loads(row["before_labels"])) <= self.labels.keys() and
                        set(json.loads(row["desired"])) <= self.users and not self.before(row) & EXCLUDED,
                        "バックアップに不正なラベル・対象メールがあります")
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def verify_files(self):
        manifest_path = self.directory / "manifest.json"
        require(manifest_path.is_file() and not manifest_path.is_symlink()
                and json.loads(manifest_path.read_text()) == self.manifest,
                "メール計画manifestが変更・削除されました")
        for name, digest in self.manifest["files"].items():
            path = self.directory / name
            require(path.is_file() and not path.is_symlink() and file_digest(path) == digest,
                    "メールバックアップのハッシュ不一致: " + name)

    def rows(self):
        return self.db.execute("SELECT * FROM messages ORDER BY id")

    def row(self, mid):
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        require(row is not None, "進捗に計画外のメッセージがあります")
        return row

    def before(self, row):
        return set(json.loads(row["before_labels"]))

    def desired(self, row):
        return set(json.loads(row["desired"]))

    def changed(self, row):
        return self.before(row) & self.users != self.desired(row)

    def summary(self):
        print("【既存メールの再分類計画】 " + encode(self.manifest["summary"]), flush=True)
        for lid in sorted(set(self.manifest["additions"]) | set(self.manifest["removals"])):
            print(f"  {self.labels[lid]['name']}: 解除 {self.manifest['removals'].get(lid, 0)} / "
                  f"付与 {self.manifest['additions'].get(lid, 0)}", flush=True)
        print(f"バックアップ: {self.directory}\n計画digest: {self.manifest['digest']}", flush=True)


class Journal:
    """Persist ownership before sending; no observed match can invent ownership."""
    def __init__(self, plan, *, create=False):
        self.plan = plan
        path = plan.directory / "progress.sqlite"
        require(create or path.is_file(), "メール進捗がありません。計画から開始してください")
        self.db = connect_db(path)
        if create:
            require(not self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone(),
                    "既存のメール進捗を上書きできません")
            self.db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE progress(message TEXT PRIMARY KEY,status TEXT NOT NULL);
                CREATE TABLE intents(seq INTEGER PRIMARY KEY,mode TEXT NOT NULL,ids TEXT NOT NULL,
                    add_labels TEXT NOT NULL,remove_labels TEXT NOT NULL,systems TEXT NOT NULL);
                CREATE TABLE events(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,detail TEXT NOT NULL);
            """)
            self.db.executemany("INSERT INTO meta VALUES(?,?)", [("digest", plan.manifest["digest"]),
                ("mode", "apply"), ("complete", "false")])
            self.db.executemany("INSERT INTO progress VALUES(?, 'new')",
                               ((row["id"],) for row in plan.rows() if plan.changed(row)))
            self.db.commit()
            sync_directory(plan.directory)
        try:
            self.validate()
        except BaseException:
            self.db.close()
            raise

    def validate(self):
        require(self.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "進捗DBが破損しています")
        require(self.meta("digest") == self.plan.manifest["digest"], "メール進捗の計画digest不一致")
        require(self.meta("mode") in ("apply", "restore"), "メール進捗modeが不正です")
        states = dict(self.db.execute("SELECT message,status FROM progress"))
        require(set(states) == {r["id"] for r in self.plan.rows() if self.plan.changed(r)}, "進捗の対象件数不一致")
        sent, restored = set(), set()
        for intent in self.db.execute("SELECT * FROM intents ORDER BY seq"):
            ids = json.loads(intent["ids"])
            add, remove = set(json.loads(intent["add_labels"])), set(json.loads(intent["remove_labels"]))
            require(intent["mode"] in ("apply", "restore") and ids and len(set(ids)) == len(ids)
                    and set(ids) <= states.keys(), "メールintentが不正です")
            require(not add & remove and add | remove <= self.plan.users, "intentにユーザーラベル以外があります")
            systems = json.loads(intent["systems"])
            require(set(systems) == set(ids), "intentのシステム状態が欠落しています")
            for mid in ids:
                row = self.plan.row(mid)
                old, new = self.plan.before(row) & self.plan.users, self.plan.desired(row)
                require(set(systems[mid]) <= self.plan.labels.keys() - self.plan.users
                        and not set(systems[mid]) & EXCLUDED, "intentのシステム状態が不正です")
                if intent["mode"] == "restore":
                    require(mid in sent, "更新intentなしの復元です")
                    old, new = new, old
                    restored.add(mid)
                else:
                    require(mid not in restored, "復元後に再分類intentがあります")
                    sent.add(mid)
                require(add == new - old and remove == old - new, "intentの差分が計画と一致しません")
        for mid, status in states.items():
            require(status in {"new", "pending", "applied", "restore_pending", "restored"}, "不正なメール進捗です")
            require(status == "new" or mid in sent, "所有権を確認できないメール進捗です")
            require(status not in {"restore_pending", "restored"} or mid in restored, "復元intentがありません")

    def close(self):
        self.db.close()

    def meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        require(row is not None, "メール進捗のmetaが欠落しています")
        return row[0]

    def set_meta(self, key, value):
        self.db.execute("UPDATE meta SET value=? WHERE key=?", (value, key))

    def state(self, mid):
        row = self.db.execute("SELECT status FROM progress WHERE message=?", (mid,)).fetchone()
        return row[0] if row else "unchanged"

    def set_state(self, mid, status):
        self.db.execute("UPDATE progress SET status=? WHERE message=?", (status, mid))

    def event(self, kind, detail):
        self.db.execute("INSERT INTO events(kind,detail) VALUES(?,?)", (kind, encode(detail)))

    def report(self):
        counts = dict(self.db.execute("SELECT status,count(*) FROM progress GROUP BY status"))
        print("[メール進捗] " + encode({"mode": self.meta("mode"), "verified": self.meta("complete") == "true",
              "保留（未知のラベル）": self.plan.deferred_count,
              "適用確認済み": counts.get("applied", 0), "未適用": counts.get("new", 0),
              "不明": counts.get("pending", 0) + counts.get("restore_pending", 0),
              "復元確認済み": counts.get("restored", 0),
              "競合検出累計": self.db.execute("SELECT count(*) FROM events WHERE kind='conflict'").fetchone()[0]}), flush=True)


def observe(api, plan, journal, row, mode, *, now=None):
    """Resolve only an owned intent; exact new-state observation alone is insufficient."""
    mid = row["id"]
    status = journal.state(mid) if journal else "new"
    if mode == "restore" and status in ("new", "unchanged"):
        return None
    now = now if now is not None else api.message(mid, plan.labels)
    actual = set(now["labelIds"])
    before, desired = plan.before(row), plan.desired(row)
    require(now["threadId"] == row["thread_id"] and not actual & EXCLUDED, "対象メールの所属が変更されています: " + mid)
    if mode == "apply":
        require(actual - plan.users == before - plan.users, "システム状態の外部変更: " + mid)
    old = before & plan.users
    current = actual & plan.users
    if status in ("new", "unchanged", "restored"):
        require(current == old, "未変更メールのユーザーラベル競合: " + mid)
    elif status == "applied":
        require(current == desired, "更新済みメールのユーザーラベル競合: " + mid)
    elif status in ("pending", "restore_pending"):
        require(current in (old, desired), "結果不明メールのユーザーラベル競合: " + mid)
        if status == "pending":
            journal.set_state(mid, "applied" if current == desired else "new")
        else:
            journal.set_state(mid, "restored" if current == old else "applied")
    return now


def validate_labels(api, plan):
    """Permit unrelated new user definitions, never missing or changed saved labels.

    Message reads deliberately retain the saved catalog: a newly created label
    attached to a target is an external user-label change and must stop recovery.
    Planning itself still requires an exact catalog throughout all searches.
    """
    live = api.labels()
    require(all(live.get(lid) == row for lid, row in plan.labels.items())
            and all(row["type"] == "user" for lid, row in live.items() if lid not in plan.labels),
            "ラベル定義がバックアップと一致しません")


def reconcile(api, plan, journal, mode):
    validate_labels(api, plan)
    if mode == "apply":
        api.filters_match(plan.rules, plan.labels, normalization=plan.manifest["normalization_version"])
    for mid, raw in api.snapshots(row["id"] for row in plan.rows()):
        row = plan.row(mid)
        try:
            observe(api, plan, journal, row, mode, now=api.metadata(mid, raw, plan.labels))
        except BaseException as exc:
            if journal:
                journal.event("conflict", {"message": row["id"], "error": str(exc)})
                journal.db.commit()
            raise
    if journal:
        journal.db.commit()


def execute(api, plan, local_dir, *, mode="apply", batch_size=50, max_batches=None, authorization=None):
    """Validate, ask once for this invocation, send durable intents, and read back."""
    require(mode in ("apply", "restore"), "メール更新modeが不正です")
    require(1 <= batch_size <= 1000 and (max_batches is None or max_batches > 0), "メールバッチ上限が不正です")
    active = pending(local_dir)
    require(not active or active["run_id"] == plan.directory.name, "別のメール更新が未完了です")
    require(not active or active.get("digest") == plan.manifest["digest"], "保留メール計画のdigest不一致")
    require(not (local_dir / "run_pending.json").exists(), "フィルタ配備が未完了です")
    journal = Journal(plan) if (plan.directory / "progress.sqlite").exists() else None
    try:
        plan.verify_files()
        if authorization is not None:
            require(mode == "apply" and isinstance(authorization, dict)
                    and authorization.get("policy") == RUN_AUTHORIZATION_POLICY
                    and authorization.get("account") == plan.manifest["account"]
                    and authorization.get("master_sha256") == plan.manifest["files"]["master.xml"]
                    and authorization.get("excluded_labels") == sorted(EXCLUDED),
                    "起動時の既存メール承認が固定計画と一致しません")
        require(mode != "restore" or journal is not None, "変更履歴がないため復元できません")
        require(not journal or mode == "restore" or journal.meta("mode") == "apply",
                "復元を開始した計画は再分類へ戻せません")
        reconcile(api, plan, journal, mode)
        plan.summary()
        if journal:
            journal.report()
        if authorization is None and not sys.stdin.isatty():
            print("対話端末がないため、メールを更新しません。", flush=True)
            d.observation_outcome("メール更新見送り（非対話）")
            return False
        question = "バックアップのユーザーラベルへ復元しますか？" if mode == "restore" else "上記の計画で既存メールのユーザーラベルを置き換えますか？"
        if authorization is None and not d.confirm_interactive(question + " [y/N]: "):
            print("メール更新を見送りました。", flush=True)
            d.observation_outcome("メール更新承認辞退")
            return False
        # Reconcile again after interactive waiting; never use a stale approval snapshot.
        plan.verify_files()
        reconcile(api, plan, journal, mode)
        if journal is None:
            journal = Journal(plan, create=True)
        journal.set_meta("mode", mode)
        journal.set_meta("complete", "false")
        journal.db.execute("INSERT OR REPLACE INTO meta VALUES('eligible_verified', 'false')")
        journal.event("approval", {"mode": mode, "digest": plan.manifest["digest"],
                                  "source": "run_upfront" if authorization is not None else "interactive",
                                  "authorization": authorization})
        journal.db.commit()
        save(local_dir / PENDING, {"run_id": plan.directory.name, "mode": mode,
                                  "digest": plan.manifest["digest"]})
        groups = {}
        for row in plan.rows():
            status = journal.state(row["id"])
            if status != ("new" if mode == "apply" else "applied"):
                continue
            old, new = plan.before(row) & plan.users, plan.desired(row)
            if mode == "restore":
                old, new = new, old
            key = (tuple(sorted(new - old)), tuple(sorted(old - new)))
            groups.setdefault(key, []).append(row["id"])
        batches = 0
        for (add, remove), all_ids in sorted(groups.items()):
            for offset in range(0, len(all_ids), batch_size):
                if max_batches is not None and batches >= max_batches:
                    print("メールバッチ上限で停止。保留計画から再開できます。", flush=True)
                    d.observation_outcome("メール更新途中終了（保留計画を保持）")
                    return False
                ids = all_ids[offset:offset + batch_size]
                validate_labels(api, plan)
                if mode == "apply":
                    api.filters_match(plan.rules, plan.labels, normalization=plan.manifest["normalization_version"])
                systems = {}
                for mid, raw in api.snapshots(ids):
                    now = observe(api, plan, journal, plan.row(mid), mode,
                                  now=api.metadata(mid, raw, plan.labels))
                    systems[mid] = sorted(set(now["labelIds"]) - plan.users)
                journal.db.execute("INSERT INTO intents(mode,ids,add_labels,remove_labels,systems) VALUES(?,?,?,?,?)",
                                   (mode, encode(ids), encode(add), encode(remove), encode(systems)))
                for mid in ids:
                    journal.set_state(mid, "pending" if mode == "apply" else "restore_pending")
                journal.db.commit()  # Durable intent BEFORE any HTTP mutation.
                failure = None
                try:
                    api.modify(ids, list(add), list(remove))
                except BaseException as exc:
                    failure = exc
                    journal.event("write_failed", d.api_error_details(exc))
                    journal.db.commit()
                # Even a successful empty batch response is not a per-message acknowledgement.
                for mid, raw in api.snapshots(ids):
                    now = api.metadata(mid, raw, plan.labels)
                    require(sorted(set(now["labelIds"]) - plan.users) == systems[mid],
                            "送信中にシステム状態が変更されました: " + mid)
                    observe(api, plan, journal, plan.row(mid), mode, now=now)
                journal.db.commit()
                validate_labels(api, plan)
                if failure is not None:
                    raise d.DeploymentError("メール更新の応答が失敗・不明です。読戻しを保存し停止しました。再開で未反映分を確認してください") from failure
                expected = "applied" if mode == "apply" else "restored"
                require(all(journal.state(mid) == expected for mid in ids), "メール更新が一部未反映です。再開してください")
                batches += 1
                print(f"[メール更新] {batches}バッチ読戻し確認済み", flush=True)
        reconcile(api, plan, journal, mode)
        expected = {"applied"} if mode == "apply" else {"new", "restored"}
        require(all(row[0] in expected for row in journal.db.execute("SELECT status FROM progress")),
                "メール更新の未確認進捗が残っています")
        complete = mode == "restore" or plan.deferred_count == 0
        journal.set_meta("complete", "true" if complete else "false")
        journal.db.execute("INSERT OR REPLACE INTO meta VALUES('eligible_verified', 'true')")
        journal.event("verified", {"mode": mode, "deferred_unknown_labels": plan.deferred_count})
        journal.db.commit()
        (local_dir / PENDING).unlink()
        sync_directory(local_dir)
        if not complete:
            print(f"既知のラベルを持つ対象メールは照合済み。未知のラベルを持つ{plan.deferred_count}通は未変更で保留です。"
                  "保留IDは計画DBのdeferred表に保存しました。解消後は新しい計画で確認してください。", flush=True)
        d.observation_outcome("メール復元全件照合済み" if mode == "restore" else
                              "メール再分類全件照合済み" if complete else "メール再分類は一部保留（未知のラベル）")
        return complete
    except BaseException as exc:
        if journal:
            journal.set_meta("complete", "false")
            journal.db.execute("INSERT OR REPLACE INTO meta VALUES('eligible_verified', 'false')")
            journal.event("stopped", {"error": str(exc), "type": type(exc).__name__})
            journal.db.commit()
        raise
    finally:
        if journal:
            journal.report()
            journal.close()


def message_client(args):
    cached = getattr(args, "_message_api", None)
    if cached is not None:
        require(getattr(args, "_message_api_account", None) == args.expected_email,
                "起動時認証のアカウントが変わりました")
        return cached
    token = getattr(args, "message_token", None) or args.local_dir / "message_token.json"
    require(token.resolve() != args.token.resolve() and token.resolve() != args.client_secrets.resolve(),
            "メール用トークンは配備用トークン・クライアント秘密情報と別のファイルにしてください")
    client, _ = d.get_client(args.client_secrets, token, allow_auth=sys.stdin.isatty(),
        scopes=d.SCOPES + ["https://www.googleapis.com/auth/gmail.modify"], expected_email=args.expected_email)
    return Gmail(client, getattr(args, "verbose", False), getattr(args, "message_read_batch_size", 1))


def authorize_run(args):
    """Obtain explicit message scope approval and OAuth before a long filter deployment."""
    if not getattr(args, "reclassify_existing", False):
        return True
    require(sys.stdin.isatty(), "既存メールを含む起動時承認には対話端末が必要です")
    target = d.select_master(args.local_dir, args.target)
    for rule in user_rules(d.compile_xml(target)):
        search_spec(rule)
    digest = d.file_hash(target)
    active = pending(args.local_dir)
    if active:
        require(active["mode"] == "apply", "メール復元が未完了です。再分類の自動実行より先に復元を完了してください")
        saved = Plan(run_directory(args.local_dir, active["run_id"]), args.expected_email)
        try:
            require(saved.manifest["files"]["master.xml"] == digest,
                    "未完了のメール計画と対象Masterが異なります。固定済み計画で再開してください")
        finally:
            saved.close()
    print("【フィルタ配備と既存メール再分類の一括実行】", flush=True)
    print(f"アカウント: {args.expected_email} / Master: {target.name}", flush=True)
    print("配備・照合後、受信トレイとアーカイブ済みメールをバックアップし、"
          "手動分を含むユーザーラベルをMasterの分類へ置き換えます。", flush=True)
    print("一致なしはユーザーラベルなし、複数一致はすべて付与します。"
          "送信済み・下書き・迷惑メール・ゴミ箱・チャットとシステムラベルは対象外です。", flush=True)
    if getattr(args, "skip_unknown_message_labels", False):
        print("計画時に未知のラベルIDが付くメールは未変更で保留し、他を進めます。保留ありは終了コード4です。", flush=True)
    if not d.confirm_interactive("このMasterで既存メール再分類まで自動実行することを承認しますか？ [y/N]: "):
        d.observation_outcome("既存メールを含む実行の承認辞退")
        return False
    args._message_authorization = {"policy": RUN_AUTHORIZATION_POLICY, "account": args.expected_email,
        "master_sha256": digest, "excluded_labels": sorted(EXCLUDED),
        "approved_at_utc": datetime.now(timezone.utc).isoformat()}
    args._message_api = message_client(args)
    args._message_api_account = args.expected_email
    return True


def command(args):
    """Called under the common CLI lock."""
    require(not (args.local_dir / "run_pending.json").exists(), "フィルタ配備が未完了です")
    authorization = getattr(args, "_message_authorization", None)
    if not getattr(args, "plan_only", False) and authorization is None and not sys.stdin.isatty():
        print("非対話実行ではメール再分類・復元を開始しません。読取り計画には --plan-only を使用してください。", flush=True)
        d.observation_outcome("メール更新見送り（非対話）")
        return
    mode = "restore" if args.cmd == "restore-labels" else "apply"
    run_id = getattr(args, "run_id", None) or getattr(args, "resume", None)
    active = pending(args.local_dir)
    require(not active or run_id == active["run_id"], "未完了のメール更新があります。保存済みrun-idで再開してください")
    plan = None
    try:
        if run_id:
            plan = Plan(run_directory(args.local_dir, run_id), args.expected_email)  # Before OAuth.
        api = message_client(args)
        if plan is None:
            directory = create_plan(api, args.local_dir, d.select_master(args.local_dir, args.target), args.expected_email,
                                    skip_unknown_labels=getattr(args, "skip_unknown_message_labels", False))
            plan = Plan(directory, args.expected_email)
        if getattr(args, "plan_only", False):
            plan.summary()
            d.observation_outcome("メール計画のみ作成（未適用）")
            return
        complete = execute(api, plan, args.local_dir, mode=mode, batch_size=args.message_batch_size,
                           max_batches=args.max_message_batches, authorization=authorization)
        if not complete and (authorization is not None or plan.deferred_count):
            args.exit_code = 4
    finally:
        if plan:
            plan.close()


def offer_after_deploy(args, target_path, current):
    """No extra OAuth or message reads unless the user opts into preparation."""
    d.start_phase("既存メール再分類の案内")
    if not sys.stdin.isatty() and getattr(args, "_message_authorization", None) is None:
        print("非対話実行のため、既存メールの再分類を見送ります。", flush=True)
        return
    master = d.compile_xml(target_path)
    if d.counter(user_rules([r["filter"] for r in current])) != d.counter(user_rules(master)):
        print("ユーザーラベル用フィルタがMasterと一致しないため、既存メールの再分類を見送ります。", flush=True)
        if getattr(args, "reclassify_existing", False):
            args.exit_code = 4
            d.observation_outcome("フィルタが未一致のためメール再分類は未実施")
        return
    if getattr(args, "reclassify_existing", False):
        require(getattr(args, "_message_authorization", None) is not None,
                "既存メールの起動時承認がありません")
        command(args)
    elif d.confirm_interactive("既存メールの再分類を準備しますか？ [y/N]: "):
        command(args)


def resume_before_deploy(args):
    active = pending(args.local_dir)
    if active is None:
        return False
    print(f"未完了のメール更新: {active['run_id']} / {active['mode']}。先に照合・再開します。", flush=True)
    from copy import copy
    resume_args = copy(args)
    resume_args.cmd = "restore-labels" if active["mode"] == "restore" else "reclassify"
    resume_args.run_id = active["run_id"]
    command(resume_args)
    if getattr(resume_args, "exit_code", 0):
        args.exit_code = resume_args.exit_code
    return True
