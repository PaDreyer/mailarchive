"""Legacy flat database view for regression tests only.

Production code uses ProfileDatabase repositories directly. This adapter preserves
old test call sites while those tests continue to exercise the real repositories.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from mailarchive.application.engine import ArchiveEngine
from mailarchive.application.errors import WorkspaceError
from mailarchive.application.service import ArchiveService
from mailarchive.infrastructure import output_files
from mailarchive.infrastructure.profile_database import ProfileDatabase
from mailarchive.infrastructure.spool import SpoolError


def make_service(state: ProfileDatabase, registry, **kwargs) -> ArchiveService:
    """Wire real application ports while preserving old test fixture access."""
    engine = ArchiveEngine(
        state.delivery,
        state.operations,
        state.spool,
        state.plan_execution,
        output_files.LocalOutputFiles(),
    )
    service = ArchiveService(
        state.configuration,
        state.discovery,
        state.operations,
        state.delivery,
        engine,
        registry,
        **kwargs,
    )
    service.state = state  # Existing regression assertions inspect repository state.
    return service


class WorkspaceStore(ProfileDatabase):
    @property
    def spool_dir(self) -> Path:
        return self.spool.path

    def read_work_copy(self, path: Path, max_bytes: int) -> bytes:
        try:
            return self.spool.read(path, max_bytes)
        except SpoolError as exc:
            raise WorkspaceError(str(exc)) from exc

    def discard_work_copy(self, path: Path) -> None:
        self.spool.discard(path)

    def _validate_spool_directory(self):
        try:
            return self.spool._validate_directory()
        except SpoolError as exc:
            raise WorkspaceError(str(exc)) from exc

    @contextmanager
    def _spool_directory_handle(self):
        try:
            with self.spool.directory_handle() as descriptor:
                yield descriptor
        except SpoolError as exc:
            raise WorkspaceError(str(exc)) from exc

    def load_settings(self, *args, **kwargs):
        return self.configuration.load_settings(*args, **kwargs)

    def ensure_configuration_revision(self, *args, **kwargs):
        return self.configuration.ensure_configuration_revision(*args, **kwargs)

    def save_settings(self, *args, **kwargs):
        return self.configuration.save_settings(*args, **kwargs)

    def normalize_source_ids(self, *args, **kwargs):
        return self.configuration.normalize_source_ids(*args, **kwargs)

    def source_values(self, *args, **kwargs):
        return self.configuration.source_values(*args, **kwargs)

    def replace_current_sources(self, *args, **kwargs):
        return self.configuration.replace_current_sources(*args, **kwargs)

    def remove_deselected_scopes(self, *args, **kwargs):
        return self.configuration.remove_deselected_scopes(*args, **kwargs)

    def prepare_run_settings(self, *args, **kwargs):
        return self.configuration.prepare_run_settings(*args, **kwargs)

    def configuration_revision(self, *args, **kwargs):
        return self.configuration.configuration_revision(*args, **kwargs)

    def unresolved_intakes(self, *args, **kwargs):
        return self.discovery.unresolved_intakes(*args, **kwargs)

    def intake_errors(self, *args, **kwargs):
        return self.discovery.intake_errors(*args, **kwargs)

    def intake_error_count(self, *args, **kwargs):
        return self.discovery.intake_error_count(*args, **kwargs)

    def intake_error_snapshot(self, *args, **kwargs):
        return self.discovery.intake_error_snapshot(*args, **kwargs)

    def pending_automatic_intakes(self, *args, **kwargs):
        return self.discovery.pending_automatic_intakes(*args, **kwargs)

    def cancel_intake(self, *args, **kwargs):
        return self.discovery.cancel_intake(*args, **kwargs)

    def scope(self, *args, **kwargs):
        return self.discovery.scope(*args, **kwargs)

    def source_monitoring_status(self, *args, **kwargs):
        return self.discovery.source_monitoring_status(*args, **kwargs)

    def prepare_scope_discovery(self, *args, **kwargs):
        return self.discovery.prepare_scope_discovery(*args, **kwargs)

    def finish_scope(self, *args, **kwargs):
        return self.discovery.finish_scope(*args, **kwargs)

    def pause_scope(self, *args, **kwargs):
        return self.discovery.pause_scope(*args, **kwargs)

    def record_scope_check_error(self, *args, **kwargs):
        return self.discovery.record_scope_check_error(*args, **kwargs)

    def reset_scope_baseline(self, *args, **kwargs):
        return self.discovery.reset_scope_baseline(*args, **kwargs)

    def baseline_message(self, *args, **kwargs):
        return self.discovery.baseline_message(*args, **kwargs)

    def intake_snapshot(self, *args, **kwargs):
        return self.discovery.intake_snapshot(*args, **kwargs)

    def release_intake(self, *args, **kwargs):
        return self.discovery.release_intake(*args, **kwargs)

    def mark_filtered(self, *args, **kwargs):
        return self.discovery.mark_filtered(*args, **kwargs)

    def discard_pending(self, *args, **kwargs):
        return self.discovery.discard_pending(*args, **kwargs)

    def pending_rechecks(self, *args, **kwargs):
        return self.discovery.pending_rechecks(*args, **kwargs)

    def intake_retry_deferred(self, *args, **kwargs):
        return self.discovery.intake_retry_deferred(*args, **kwargs)

    def reserve(self, *args, **kwargs):
        return self.discovery.reserve(*args, **kwargs)

    def mark_unmatched(self, *args, **kwargs):
        return self.discovery.mark_unmatched(*args, **kwargs)

    def mark_intake_error(self, *args, **kwargs):
        return self.discovery.mark_intake_error(*args, **kwargs)

    def reject_intake(self, *args, **kwargs):
        return self.discovery.reject_intake(*args, **kwargs)

    def create_manual_operation(self, *args, **kwargs):
        return self.operations.create_manual_operation(*args, **kwargs)

    def manual_operation(self, *args, **kwargs):
        return self.operations.manual_operation(*args, **kwargs)

    def manual_operation_sources(self, *args, **kwargs):
        return self.operations.manual_operation_sources(*args, **kwargs)

    def claim_manual_operation(self, *args, **kwargs):
        return self.operations.claim_manual_operation(*args, **kwargs)

    def interrupt_queued_manual_operations(self, *args, **kwargs):
        return self.operations.interrupt_queued_manual_operations(*args, **kwargs)

    def manual_operation_accepts_work(self, *args, **kwargs):
        return self.operations.manual_operation_accepts_work(*args, **kwargs)

    def manual_operation_accepts_outputs(self, *args, **kwargs):
        return self.operations.manual_operation_accepts_outputs(*args, **kwargs)

    def mark_operation_source(self, *args, **kwargs):
        return self.operations.mark_operation_source(*args, **kwargs)

    def request_stop_manual_operation(self, *args, **kwargs):
        return self.operations.request_stop_manual_operation(*args, **kwargs)

    def finalize_stop_manual_operation(self, *args, **kwargs):
        return self.operations.finalize_stop_manual_operation(*args, **kwargs)

    def finish_manual_operation(self, *args, **kwargs):
        return self.operations.finish_manual_operation(*args, **kwargs)

    def reconcile_manual_operation_for_plan(self, *args, **kwargs):
        return self.operations.reconcile_manual_operation_for_plan(*args, **kwargs)

    def start_run(self, *args, **kwargs):
        return self.operations.start_run(*args, **kwargs)

    def finish_run(self, *args, **kwargs):
        return self.operations.finish_run(*args, **kwargs)

    def cancel_run(self, *args, **kwargs):
        return self.operations.cancel_run(*args, **kwargs)

    def run_status(self, *args, **kwargs):
        return self.operations.run_status(*args, **kwargs)

    def run_operation_id(self, *args, **kwargs):
        return self.operations.run_operation_id(*args, **kwargs)

    def manual_run_for_source(self, *args, **kwargs):
        return self.operations.manual_run_for_source(*args, **kwargs)

    def manual_open_plans(self, *args, **kwargs):
        return self.operations.manual_open_plans(*args, **kwargs)

    def manual_output_failure_count(self, *args, **kwargs):
        return self.operations.manual_output_failure_count(*args, **kwargs)

    def interrupt_run(self, *args, **kwargs):
        return self.operations.interrupt_run(*args, **kwargs)

    def plan_operation_active(self, *args, **kwargs):
        return self.operations.plan_operation_active(*args, **kwargs)

    def incomplete_manual_runs(self, *args, **kwargs):
        return self.operations.incomplete_manual_runs(*args, **kwargs)

    def run_settings_snapshot(self, *args, **kwargs):
        return self.operations.run_settings_snapshot(*args, **kwargs)

    def restart_run(self, *args, **kwargs):
        return self.operations.restart_run(*args, **kwargs)

    def range_target_checkpoint(self, *args, **kwargs):
        return self.operations.range_target_checkpoint(*args, **kwargs)

    def update_range_target_checkpoint(self, *args, **kwargs):
        return self.operations.update_range_target_checkpoint(*args, **kwargs)

    def accept_plan(self, *args, **kwargs):
        return self.delivery.accept_plan(*args, **kwargs)

    def open_plans(self, *args, **kwargs):
        return self.delivery.open_plans(*args, **kwargs)

    def auto_resumable_plans(self, *args, **kwargs):
        return self.delivery.auto_resumable_plans(*args, **kwargs)

    def automatic_work_due(self, *args, **kwargs):
        return self.delivery.automatic_work_due(*args, **kwargs)

    def work_plans(self, *args, **kwargs):
        return self.delivery.work_plans(*args, **kwargs)

    def plan_targets(self, *args, **kwargs):
        return self.delivery.plan_targets(*args, **kwargs)

    def outputs(self, *args, **kwargs):
        return self.delivery.outputs(*args, **kwargs)

    def reserved_output_path(self, *args, **kwargs):
        return self.delivery.reserved_output_path(*args, **kwargs)

    def receipt(self, *args, **kwargs):
        return self.delivery.receipt(*args, **kwargs)

    def add_output(self, *args, **kwargs):
        return self.delivery.add_output(*args, **kwargs)

    def mark_target_no_output(self, *args, **kwargs):
        return self.delivery.mark_target_no_output(*args, **kwargs)

    def refresh_target_statuses(self, *args, **kwargs):
        return self.delivery.refresh_target_statuses(*args, **kwargs)

    def set_output_path(self, *args, **kwargs):
        return self.delivery.set_output_path(*args, **kwargs)

    def output_done(self, *args, **kwargs):
        return self.delivery.output_done(*args, **kwargs)

    def output_error(self, *args, **kwargs):
        return self.delivery.output_error(*args, **kwargs)

    def output_attempts(self, *args, **kwargs):
        return self.delivery.output_attempts(*args, **kwargs)

    def set_plan_error(self, *args, **kwargs):
        return self.delivery.set_plan_error(*args, **kwargs)

    def finish_plan_if_complete(self, *args, **kwargs):
        return self.delivery.finish_plan_if_complete(*args, **kwargs)

    def pause_plan(self, *args, **kwargs):
        return self.delivery.pause_plan(*args, **kwargs)

    def resume_plan(self, *args, **kwargs):
        return self.delivery.resume_plan(*args, **kwargs)

    def abort_plan(self, *args, **kwargs):
        return self.delivery.abort_plan(*args, **kwargs)

    def spool_usage(self, *args, **kwargs):
        return self.delivery.spool_usage(*args, **kwargs)

    def processing_history(self, *args, **kwargs):
        return self.delivery.processing_history(*args, **kwargs)

    def target_outputs(self, *args, **kwargs):
        return self.delivery.target_outputs(*args, **kwargs)
