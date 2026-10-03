"""Composition root: build one application from concrete local adapters."""

from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

from mailarchive.application.activity import ActivityQueries
from mailarchive.application.credential_port import CredentialStore
from mailarchive.application.engine import ArchiveEngine
from mailarchive.application.events import EventLevel, RunProgress, ServiceEvent
from mailarchive.application.execution import ExecutionCoordinator
from mailarchive.application.profile import ProfileContext
from mailarchive.application.service import ArchiveService
from mailarchive.application.session import MailArchiveApplication
from mailarchive.infrastructure.activity_repository import SqliteActivityRepository
from mailarchive.infrastructure.diagnostics import ActivityLog
from mailarchive.infrastructure.oauth import authorize_account, parse_google_service_account_file
from mailarchive.infrastructure.output_files import LocalOutputFiles
from mailarchive.infrastructure.platform_integration import set_start_at_login
from mailarchive.infrastructure.profile_database import ProfileDatabase
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.infrastructure.profile_queries import SqliteProfileQueries
from mailarchive.infrastructure.providers.registry import MessageSourceRegistry
from mailarchive.infrastructure.updates import check_for_update

logger = logging.getLogger(__name__)


class LocalProfiles:
    def __init__(self, configuration: ConfigStore, credentials: CredentialStore) -> None:
        self.configuration = configuration
        self.credentials = credentials

    @property
    def path(self) -> Path:
        return self.configuration.state_database_path()

    def open(
        self,
        path: Path,
        on_event: Callable[[ServiceEvent], None],
        on_progress: Callable[[RunProgress], None],
    ) -> ProfileContext:
        path = path.expanduser().resolve()
        if path == self.configuration.path:
            state = ProfileDatabase(path, recover=True)
            settings = state.configuration.load_settings()
        else:
            state, settings = self.configuration.prepare_database(path)
        diagnostics = ActivityLog(
            state.connection, state.configuration.ensure_configuration_revision
        )

        def report(event: ServiceEvent) -> None:
            try:
                diagnostics.record(event)
            except Exception as exc:
                logger.exception("Could not save the diagnostic event.")
                on_event(ServiceEvent(EventLevel.WARNING, f"Could not save activity log: {exc}"))
            on_event(event)

        service = ArchiveService(
            state.configuration,
            state.discovery,
            state.operations,
            state.delivery,
            ArchiveEngine(
                state.delivery,
                state.operations,
                state.spool,
                state.plan_execution,
                LocalOutputFiles(),
            ),
            MessageSourceRegistry(self.credentials),
            event_handler=report,
        )
        execution = ExecutionCoordinator(
            service,
            lambda: deepcopy(context.settings),
            state.operations,
            polling_schedule=state.polling,
            automatic_monitoring_paused=settings.automatic_monitoring_paused,
            progress_handler=on_progress,
        )
        activity = ActivityQueries(SqliteActivityRepository(state.connection))
        context = ProfileContext(
            database_path=path,
            settings=settings,
            save=state.configuration.save_settings,
            execution=execution,
            activity=activity,
            diagnostics=diagnostics,
            queries=SqliteProfileQueries(state, activity),
            account_change=service.account_change,
            reset_scope=state.discovery.reset_scope_baseline,
            report=report,
        )
        return context

    def activate(self, path: Path) -> None:
        self.configuration.activate_database(path)


def create_application(
    config_store: ConfigStore, credential_store: CredentialStore
) -> MailArchiveApplication:
    return MailArchiveApplication(
        LocalProfiles(config_store, credential_store),
        credential_store,
        authorize=authorize_account,
        configure_startup=set_start_at_login,
        update_check=check_for_update,
        service_account_reader=parse_google_service_account_file,
    )
