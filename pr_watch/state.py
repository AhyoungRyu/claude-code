from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import Binding, ClassifiedEvent, HostSyncItem, InboxItem, NotificationItem, QueueItem
from .util import dumps, loads_dict, normalize_repo_full_name, random_id, utc_now


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists bindings (
                  binding_id text primary key,
                  repo_owner text not null,
                  repo_name text not null,
                  pr_number integer not null,
                  pr_url text not null,
                  role text not null,
                  agent text not null,
                  session_id text not null,
                  cwd text not null default '',
                  branch text not null default '',
                  host text,
                  confidence text not null,
                  confirmed integer not null,
                  active integer not null default 1,
                  confirmation_source text not null,
                  evidence_json text not null,
                  created_at text not null,
                  updated_at text not null,
                  last_event_at text not null default ''
                );
                create index if not exists idx_bindings_pr
                  on bindings(repo_owner, repo_name, pr_number, role);

                create table if not exists events (
                  event_id text primary key,
                  dedupe_key text not null unique,
                  repo_owner text not null,
                  repo_name text not null,
                  pr_number integer not null,
                  pr_url text not null,
                  role text not null,
                  event_type text not null,
                  summary text not null,
                  actor text not null,
                  actionable integer not null,
                  status text not null,
                  delivery_status text not null,
                  binding_id text,
                  confidence text not null,
                  evidence_json text not null,
                  payload_json text not null,
                  recovery_command text not null default '',
                  error text not null default '',
                  created_at text not null,
                  updated_at text not null
                );
                create index if not exists idx_events_status on events(status);

                create table if not exists queue (
                  queue_id text primary key,
                  event_id text not null,
                  command_json text not null,
                  prompt text not null,
                  status text not null,
                  created_at text not null,
                  updated_at text not null
                );

                create table if not exists notifications (
                  notification_id text primary key,
                  event_id text not null,
                  channel text not null,
                  title text not null,
                  message text not null,
                  target_url text not null,
                  status text not null,
                  error text not null default '',
                  created_at text not null,
                  updated_at text not null,
                  unique(event_id, channel)
                );
                create index if not exists idx_notifications_status
                  on notifications(status);

                create table if not exists host_syncs (
                  sync_id text primary key,
                  event_id text not null,
                  host text not null,
                  target_id text not null,
                  status text not null,
                  external_id text not null default '',
                  error text not null default '',
                  created_at text not null,
                  updated_at text not null,
                  unique(event_id, host, target_id)
                );
                create index if not exists idx_host_syncs_event
                  on host_syncs(event_id);

                create table if not exists watch_repos (
                  repo_full_name text primary key,
                  created_at text not null,
                  updated_at text not null
                );
                """
            )
            self._migrate_bindings(conn)

    def _migrate_bindings(self, conn: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in conn.execute("pragma table_info(bindings)").fetchall()}
        if "active" not in columns:
            conn.execute("alter table bindings add column active integer not null default 1")

    def upsert_binding(self, binding: Binding) -> Binding:
        now = utc_now()
        created_at = binding.created_at or now
        updated = Binding(
            binding_id=binding.binding_id,
            repo_owner=binding.repo_owner,
            repo_name=binding.repo_name,
            pr_number=binding.pr_number,
            pr_url=binding.pr_url,
            role=binding.role,
            agent=binding.agent,
            session_id=binding.session_id,
            cwd=binding.cwd,
            branch=binding.branch,
            host=binding.host,
            confidence=binding.confidence,
            confirmed=binding.confirmed,
            active=binding.active,
            confirmation_source=binding.confirmation_source,
            evidence=binding.evidence,
            created_at=created_at,
            updated_at=now,
            last_event_at=binding.last_event_at,
        )
        with self.connect() as conn:
            conn.execute(
                """
                insert into bindings (
                  binding_id, repo_owner, repo_name, pr_number, pr_url, role,
                  agent, session_id, cwd, branch, host, confidence, confirmed,
                  active, confirmation_source, evidence_json, created_at, updated_at, last_event_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(binding_id) do update set
                  agent=excluded.agent,
                  session_id=excluded.session_id,
                  cwd=excluded.cwd,
                  branch=excluded.branch,
                  host=excluded.host,
                  confidence=excluded.confidence,
                  confirmed=excluded.confirmed,
                  active=excluded.active,
                  confirmation_source=excluded.confirmation_source,
                  evidence_json=excluded.evidence_json,
                  updated_at=excluded.updated_at,
                  last_event_at=excluded.last_event_at
                """,
                (
                    updated.binding_id,
                    updated.repo_owner,
                    updated.repo_name,
                    updated.pr_number,
                    updated.pr_url,
                    updated.role,
                    updated.agent,
                    updated.session_id,
                    updated.cwd,
                    updated.branch,
                    updated.host,
                    updated.confidence,
                    int(updated.confirmed),
                    int(updated.active),
                    updated.confirmation_source,
                    json.dumps(updated.evidence),
                    updated.created_at,
                    updated.updated_at,
                    updated.last_event_at,
                ),
            )
        return updated

    def create_binding(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        pr_url: str,
        role: str,
        agent: str,
        session_id: str,
        cwd: str = "",
        branch: str = "",
        host: Optional[str] = None,
        confidence: str = "high",
        confirmed: bool = False,
        active: bool = True,
        confirmation_source: str = "inferred_candidate",
        evidence: Optional[List[str]] = None,
        binding_id: Optional[str] = None,
    ) -> Binding:
        return self.upsert_binding(
            Binding(
                binding_id=binding_id or random_id("bind"),
                repo_owner=repo_owner,
                repo_name=repo_name,
                pr_number=pr_number,
                pr_url=pr_url,
                role=role,
                agent=agent,
                session_id=session_id,
                cwd=cwd,
                branch=branch,
                host=host,
                confidence=confidence,
                confirmed=confirmed,
                active=active,
                confirmation_source=confirmation_source,
                evidence=evidence or [],
            )
        )

    def confirm_binding(self, binding_id: str, source: str = "user_confirmed") -> Binding:
        binding = self.get_binding(binding_id)
        if binding is None:
            raise KeyError(f"binding not found: {binding_id}")
        confirmed = Binding(
            binding_id=binding.binding_id,
            repo_owner=binding.repo_owner,
            repo_name=binding.repo_name,
            pr_number=binding.pr_number,
            pr_url=binding.pr_url,
            role=binding.role,
            agent=binding.agent,
            session_id=binding.session_id,
            cwd=binding.cwd,
            branch=binding.branch,
            host=binding.host,
            confidence="high",
            confirmed=True,
            active=True,
            confirmation_source=source,
            evidence=binding.evidence + ["user confirmed first PR/session binding"],
            created_at=binding.created_at,
            last_event_at=binding.last_event_at,
        )
        self.deactivate_confirmed_bindings(
            confirmed.repo_owner,
            confirmed.repo_name,
            confirmed.pr_number,
            confirmed.role,
            except_binding_id=confirmed.binding_id,
        )
        return self.upsert_binding(confirmed)

    def deactivate_confirmed_bindings(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        role: str,
        except_binding_id: Optional[str] = None,
    ) -> int:
        query = """
            update bindings
            set active = 0, updated_at = ?
            where repo_owner = ? and repo_name = ? and pr_number = ?
              and role = ? and confirmed = 1 and active = 1
        """
        params: List[Any] = [utc_now(), repo_owner, repo_name, pr_number, role]
        if except_binding_id:
            query += " and binding_id <> ?"
            params.append(except_binding_id)
        with self.connect() as conn:
            cursor = conn.execute(query, tuple(params))
        return cursor.rowcount

    def get_binding(self, binding_id: Optional[str]) -> Optional[Binding]:
        if not binding_id:
            return None
        with self.connect() as conn:
            row = conn.execute("select * from bindings where binding_id = ?", (binding_id,)).fetchone()
        return _binding_from_row(row) if row else None

    def find_confirmed_binding(self, event: ClassifiedEvent) -> Optional[Binding]:
        with self.connect() as conn:
            row = conn.execute(
                """
                select * from bindings
                where repo_owner = ? and repo_name = ? and pr_number = ?
                  and role = ? and confirmed = 1 and active = 1
                order by updated_at desc
                limit 1
                """,
                (event.pr.owner, event.pr.repo, event.pr.number, event.role),
            ).fetchone()
        return _binding_from_row(row) if row else None

    def find_binding_for_session(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        role: str,
        session_id: str,
    ) -> Optional[Binding]:
        with self.connect() as conn:
            row = conn.execute(
                """
                select * from bindings
                where repo_owner = ? and repo_name = ? and pr_number = ?
                  and role = ? and session_id = ?
                order by updated_at desc
                limit 1
                """,
                (repo_owner, repo_name, pr_number, role, session_id),
            ).fetchone()
        return _binding_from_row(row) if row else None

    def list_bindings(self) -> List[Binding]:
        with self.connect() as conn:
            rows = conn.execute("select * from bindings order by updated_at desc").fetchall()
        return [_binding_from_row(row) for row in rows]

    def add_watch_repo(self, repo: str) -> str:
        normalized = normalize_repo_full_name(repo)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                insert into watch_repos (repo_full_name, created_at, updated_at)
                values (?, ?, ?)
                on conflict(repo_full_name) do update set updated_at = excluded.updated_at
                """,
                (normalized, now, now),
            )
        return normalized

    def remove_watch_repo(self, repo: str) -> bool:
        normalized = normalize_repo_full_name(repo)
        with self.connect() as conn:
            cursor = conn.execute("delete from watch_repos where repo_full_name = ?", (normalized,))
        return cursor.rowcount > 0

    def list_watch_repos(self) -> List[str]:
        with self.connect() as conn:
            rows = conn.execute("select repo_full_name from watch_repos order by repo_full_name").fetchall()
        return [str(row["repo_full_name"]) for row in rows]

    def clear_watch_repos(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute("delete from watch_repos")
        return cursor.rowcount

    def upsert_event(
        self,
        event: ClassifiedEvent,
        status: str,
        delivery_status: str,
        binding_id: Optional[str],
        confidence: str,
        evidence: Iterable[str],
    ) -> InboxItem:
        now = utc_now()
        existing = self.find_event_by_dedupe(event.dedupe_key)
        event_id = existing.event_id if existing else random_id("evt")
        created_at = existing.created_at if existing else now
        next_status = status
        next_delivery_status = delivery_status
        next_binding_id = binding_id
        next_confidence = confidence
        next_evidence = list(evidence)
        if existing and _should_preserve_delivery_state(existing):
            next_status = existing.status
            next_delivery_status = existing.delivery_status
            next_binding_id = existing.binding_id
            next_confidence = existing.confidence
            next_evidence = existing.evidence
        with self.connect() as conn:
            conn.execute(
                """
                insert into events (
                  event_id, dedupe_key, repo_owner, repo_name, pr_number, pr_url,
                  role, event_type, summary, actor, actionable, status, delivery_status,
                  binding_id, confidence, evidence_json, payload_json, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(dedupe_key) do update set
                  status=excluded.status,
                  delivery_status=excluded.delivery_status,
                  binding_id=excluded.binding_id,
                  confidence=excluded.confidence,
                  evidence_json=excluded.evidence_json,
                  payload_json=excluded.payload_json,
                  updated_at=excluded.updated_at
                """,
                (
                    event_id,
                    event.dedupe_key,
                    event.pr.owner,
                    event.pr.repo,
                    event.pr.number,
                    event.pr.url,
                    event.role,
                    event.event_type,
                    event.summary,
                    event.actor,
                    int(event.actionable),
                    next_status,
                    next_delivery_status,
                    next_binding_id,
                    next_confidence,
                    json.dumps(next_evidence),
                    dumps(event.payload),
                    created_at,
                    now,
                ),
            )
        return self.get_event(event_id)

    def find_event_by_dedupe(self, dedupe_key: str) -> Optional[InboxItem]:
        with self.connect() as conn:
            row = conn.execute("select * from events where dedupe_key = ?", (dedupe_key,)).fetchone()
        return _event_from_row(row) if row else None

    def get_event(self, event_id: str) -> InboxItem:
        with self.connect() as conn:
            row = conn.execute("select * from events where event_id = ?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(f"event not found: {event_id}")
        return _event_from_row(row)

    def list_events(self, include_done: bool = False) -> List[InboxItem]:
        query = "select * from events"
        params: tuple[Any, ...] = ()
        if not include_done:
            query += " where status not in ('delivered', 'dismissed', 'dropped')"
        query += " order by updated_at desc"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_event_from_row(row) for row in rows]

    def update_event(
        self,
        event_id: str,
        status: Optional[str] = None,
        delivery_status: Optional[str] = None,
        binding_id: Optional[str] = None,
        confidence: Optional[str] = None,
        evidence: Optional[List[str]] = None,
        recovery_command: Optional[str] = None,
        error: Optional[str] = None,
    ) -> InboxItem:
        current = self.get_event(event_id)
        values = {
            "status": status if status is not None else current.status,
            "delivery_status": delivery_status if delivery_status is not None else current.delivery_status,
            "binding_id": binding_id if binding_id is not None else current.binding_id,
            "confidence": confidence if confidence is not None else current.confidence,
            "evidence_json": json.dumps(evidence if evidence is not None else current.evidence),
            "recovery_command": recovery_command if recovery_command is not None else current.recovery_command,
            "error": error if error is not None else current.error,
            "updated_at": utc_now(),
            "event_id": event_id,
        }
        with self.connect() as conn:
            conn.execute(
                """
                update events set
                  status = :status,
                  delivery_status = :delivery_status,
                  binding_id = :binding_id,
                  confidence = :confidence,
                  evidence_json = :evidence_json,
                  recovery_command = :recovery_command,
                  error = :error,
                  updated_at = :updated_at
                where event_id = :event_id
                """,
                values,
            )
        return self.get_event(event_id)

    def dismiss_stale_open_pr_events(self, repo: str, open_pr_numbers: Iterable[int]) -> int:
        normalized = normalize_repo_full_name(repo)
        repo_owner, repo_name = normalized.split("/", 1)
        live_numbers = sorted({int(number) for number in open_pr_numbers})
        params: List[Any] = [repo_owner, repo_name]
        query = """
            select event_id from events
            where lower(repo_owner) = ? and lower(repo_name) = ?
              and status in ('pending', 'needs_confirmation', 'queued')
        """
        if live_numbers:
            placeholders = ", ".join("?" for _ in live_numbers)
            query += f" and pr_number not in ({placeholders})"
            params.extend(live_numbers)

        now = utc_now()
        error = (
            f"PR is no longer in the open PR list for {normalized}; "
            "it may have been merged or closed."
        )
        with self.connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            if not rows:
                return 0
            event_ids = [row["event_id"] for row in rows]
            conn.executemany(
                """
                update events set
                  status = 'dismissed',
                  delivery_status = 'stale_pr_not_open',
                  recovery_command = '',
                  error = ?,
                  updated_at = ?
                where event_id = ?
                """,
                [(error, now, event_id) for event_id in event_ids],
            )
            conn.executemany(
                """
                update queue set
                  status = 'stale_pr_not_open',
                  updated_at = ?
                where event_id = ? and status = 'queued'
                """,
                [(now, event_id) for event_id in event_ids],
            )
        return len(rows)

    def enqueue(self, event_id: str, command: List[str], prompt: str) -> QueueItem:
        now = utc_now()
        queue_id = random_id("queue")
        with self.connect() as conn:
            conn.execute(
                """
                insert into queue (queue_id, event_id, command_json, prompt, status, created_at, updated_at)
                values (?, ?, ?, ?, 'queued', ?, ?)
                """,
                (queue_id, event_id, json.dumps(command), prompt, now, now),
            )
        return self.get_queue_item(queue_id)

    def get_queue_item(self, queue_id: str) -> QueueItem:
        with self.connect() as conn:
            row = conn.execute("select * from queue where queue_id = ?", (queue_id,)).fetchone()
        if row is None:
            raise KeyError(f"queue item not found: {queue_id}")
        return _queue_from_row(row)

    def list_queue(self) -> List[QueueItem]:
        with self.connect() as conn:
            rows = conn.execute("select * from queue where status = 'queued' order by created_at").fetchall()
        return [_queue_from_row(row) for row in rows]

    def upsert_notification(
        self,
        event_id: str,
        channel: str,
        title: str,
        message: str,
        target_url: str,
        status: str,
        error: str = "",
    ) -> NotificationItem:
        now = utc_now()
        existing = self.get_notification(event_id, channel)
        notification_id = existing.notification_id if existing else random_id("note")
        created_at = existing.created_at if existing else now
        with self.connect() as conn:
            conn.execute(
                """
                insert into notifications (
                  notification_id, event_id, channel, title, message,
                  target_url, status, error, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(event_id, channel) do update set
                  title=excluded.title,
                  message=excluded.message,
                  target_url=excluded.target_url,
                  status=excluded.status,
                  error=excluded.error,
                  updated_at=excluded.updated_at
                """,
                (
                    notification_id,
                    event_id,
                    channel,
                    title,
                    message,
                    target_url,
                    status,
                    error,
                    created_at,
                    now,
                ),
            )
        return self.get_notification(event_id, channel)

    def get_notification(self, event_id: str, channel: str) -> Optional[NotificationItem]:
        with self.connect() as conn:
            row = conn.execute(
                "select * from notifications where event_id = ? and channel = ?",
                (event_id, channel),
            ).fetchone()
        return _notification_from_row(row) if row else None

    def list_notifications(self, include_done: bool = False) -> List[NotificationItem]:
        query = "select * from notifications"
        if not include_done:
            query += " where status in ('pending', 'failed')"
        query += " order by updated_at desc"
        with self.connect() as conn:
            rows = conn.execute(query).fetchall()
        return [_notification_from_row(row) for row in rows]

    def get_notification_by_id(self, notification_id: str) -> NotificationItem:
        with self.connect() as conn:
            row = conn.execute("select * from notifications where notification_id = ?", (notification_id,)).fetchone()
        if row is None:
            raise KeyError(f"notification not found: {notification_id}")
        return _notification_from_row(row)

    def update_notification_status(
        self,
        notification_id: str,
        status: str,
        error: Optional[str] = None,
    ) -> NotificationItem:
        current = self.get_notification_by_id(notification_id)
        with self.connect() as conn:
            conn.execute(
                """
                update notifications set
                  status = ?,
                  error = ?,
                  updated_at = ?
                where notification_id = ?
                """,
                (
                    status,
                    current.error if error is None else error,
                    utc_now(),
                    notification_id,
                ),
            )
        return self.get_notification_by_id(notification_id)

    def get_host_sync(self, event_id: str, host: str, target_id: str) -> Optional[HostSyncItem]:
        with self.connect() as conn:
            row = conn.execute(
                """
                select * from host_syncs
                where event_id = ? and host = ? and target_id = ?
                """,
                (event_id, host, target_id),
            ).fetchone()
        return _host_sync_from_row(row) if row else None

    def upsert_host_sync(
        self,
        event_id: str,
        host: str,
        target_id: str,
        status: str,
        external_id: str = "",
        error: str = "",
    ) -> HostSyncItem:
        now = utc_now()
        existing = self.get_host_sync(event_id, host, target_id)
        sync_id = existing.sync_id if existing else random_id("sync")
        created_at = existing.created_at if existing else now
        with self.connect() as conn:
            conn.execute(
                """
                insert into host_syncs (
                  sync_id, event_id, host, target_id, status, external_id,
                  error, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(event_id, host, target_id) do update set
                  status=excluded.status,
                  external_id=excluded.external_id,
                  error=excluded.error,
                  updated_at=excluded.updated_at
                """,
                (
                    sync_id,
                    event_id,
                    host,
                    target_id,
                    status,
                    external_id,
                    error,
                    created_at,
                    now,
                ),
            )
        synced = self.get_host_sync(event_id, host, target_id)
        if synced is None:  # pragma: no cover - sqlite insert failure would raise first
            raise RuntimeError("host sync was not recorded")
        return synced


def _binding_from_row(row: sqlite3.Row) -> Binding:
    return Binding(
        binding_id=row["binding_id"],
        repo_owner=row["repo_owner"],
        repo_name=row["repo_name"],
        pr_number=int(row["pr_number"]),
        pr_url=row["pr_url"],
        role=row["role"],
        agent=row["agent"],
        session_id=row["session_id"],
        cwd=row["cwd"],
        branch=row["branch"],
        host=row["host"],
        confidence=row["confidence"],
        confirmed=bool(row["confirmed"]),
        active=bool(row["active"]),
        confirmation_source=row["confirmation_source"],
        evidence=json.loads(row["evidence_json"] or "[]"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_event_at=row["last_event_at"],
    )


def _event_from_row(row: sqlite3.Row) -> InboxItem:
    return InboxItem(
        event_id=row["event_id"],
        dedupe_key=row["dedupe_key"],
        repo_owner=row["repo_owner"],
        repo_name=row["repo_name"],
        pr_number=int(row["pr_number"]),
        pr_url=row["pr_url"],
        role=row["role"],
        event_type=row["event_type"],
        summary=row["summary"],
        actor=row["actor"],
        actionable=bool(row["actionable"]),
        status=row["status"],
        delivery_status=row["delivery_status"],
        binding_id=row["binding_id"],
        confidence=row["confidence"],
        evidence=json.loads(row["evidence_json"] or "[]"),
        payload=loads_dict(row["payload_json"] or "{}"),
        recovery_command=row["recovery_command"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _should_preserve_delivery_state(existing: InboxItem) -> bool:
    user_decision_states = {
        "busy_needs_decision",
        "delivered",
        "dropped",
        "failed",
        "awaiting_first_binding_confirmation",
        "awaiting_rebind_confirmation",
        "notify_only",
        "queued",
    }
    final_statuses = {"delivered", "dismissed", "dropped", "queued"}
    return existing.status in final_statuses or existing.delivery_status in user_decision_states


def _queue_from_row(row: sqlite3.Row) -> QueueItem:
    return QueueItem(
        queue_id=row["queue_id"],
        event_id=row["event_id"],
        command=json.loads(row["command_json"]),
        prompt=row["prompt"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _notification_from_row(row: sqlite3.Row) -> NotificationItem:
    return NotificationItem(
        notification_id=row["notification_id"],
        event_id=row["event_id"],
        channel=row["channel"],
        title=row["title"],
        message=row["message"],
        target_url=row["target_url"],
        status=row["status"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _host_sync_from_row(row: sqlite3.Row) -> HostSyncItem:
    return HostSyncItem(
        sync_id=row["sync_id"],
        event_id=row["event_id"],
        host=row["host"],
        target_id=row["target_id"],
        status=row["status"],
        external_id=row["external_id"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
