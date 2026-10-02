"""SQLite-backed profile status used by the application facade."""

from mailarchive.application.activity import ActivityQueries
from mailarchive.application.profile import ApplicationStatus, MonitoringStatus, PausedScope
from mailarchive.domain.configuration import MailProvider
from mailarchive.infrastructure.profile_database import ProfileDatabase


class SqliteProfileQueries:
    def __init__(self, state: ProfileDatabase, activity: ActivityQueries) -> None:
        self.state = state
        self.activity = activity

    def status(self) -> ApplicationStatus:
        size = self.state.spool.usage_bytes()
        return ApplicationStatus(len(self.activity.current()), size)

    def monitoring_status(self, source_id: str, folders: list[str]) -> MonitoringStatus:
        with self.state.connection() as db:
            source = db.execute("SELECT provider FROM source WHERE id=?", (source_id,)).fetchone()
        if source is None:
            return MonitoringStatus("setting_up")
        return MonitoringStatus(
            self.state.discovery.source_monitoring_status(
                source_id, MailProvider(source["provider"]), folders
            )
        )

    def paused_scopes(self, account_id: str) -> tuple[PausedScope, ...]:
        with self.state.connection() as db:
            rows = db.execute(
                "SELECT s.id, sc.scope_key, sc.error FROM source s "
                "JOIN source_scope sc ON sc.source_id=s.id "
                "WHERE s.account_id=? AND sc.status='paused' ORDER BY s.id, sc.scope_key",
                (account_id,),
            ).fetchall()
        return tuple(PausedScope(row["id"], row["scope_key"], row["error"]) for row in rows)
