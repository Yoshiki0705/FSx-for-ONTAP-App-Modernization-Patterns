using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using DocIntake.Core;
using Microsoft.Extensions.Hosting;

namespace DocIntake.BackgroundWorker
{
    public sealed class ControlPlaneExecutionResult
    {
        public string Status { get; set; }
        public string ErrorClassification { get; set; }
        public string ClaimState { get; set; }
        public string Postcondition { get; set; }
    }

    public sealed class DocIntakeBackgroundService : BackgroundService
    {
        private readonly IFileStore _store;
        private readonly IClaimStore _claims;
        private readonly IControlPlaneRecorder _controlPlane;
        private readonly IWorkItemProcessor _processor;
        private readonly IClock _clock;
        private readonly BackgroundWorkerOptions _options;
        private readonly RetryPolicy _retryPolicy;
        private bool _cancellationRecorded;

        public DocIntakeBackgroundService(
            IFileStore store,
            IClaimStore claims,
            IControlPlaneRecorder controlPlane,
            IWorkItemProcessor processor,
            IClock clock,
            BackgroundWorkerOptions options,
            Func<TimeSpan, CancellationToken, Task> delayAsync = null)
        {
            _store = store ?? throw new ArgumentNullException("store");
            _claims = claims ?? throw new ArgumentNullException("claims");
            _controlPlane = controlPlane ?? throw new ArgumentNullException("controlPlane");
            _processor = processor ?? throw new ArgumentNullException("processor");
            _clock = clock ?? throw new ArgumentNullException("clock");
            _options = options ?? throw new ArgumentNullException("options");
            _options.Validate();
            _retryPolicy = new RetryPolicy(_options.MaxAttempts, _options.RetryDelay, delayAsync);
        }

        public override Task StartAsync(CancellationToken cancellationToken)
        {
            Record("startup", null, null, "not_applicable", "worker_start_requested", "not_applicable");
            return base.StartAsync(cancellationToken);
        }

        protected override async Task ExecuteAsync(CancellationToken stoppingToken)
        {
            try
            {
                while (!stoppingToken.IsCancellationRequested)
                {
                    foreach (var item in _store.List(StorePaths.InboxDir))
                    {
                        if (stoppingToken.IsCancellationRequested) break;
                        await ProcessItemAsync(item, stoppingToken).ConfigureAwait(false);
                    }
                    if (!stoppingToken.IsCancellationRequested)
                    {
                        await Task.Delay(_options.PollInterval, stoppingToken).ConfigureAwait(false);
                    }
                }
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                RecordCancellation();
            }
            finally
            {
                if (stoppingToken.IsCancellationRequested) RecordCancellation();
                Record("normal-stop", null, null, "worker_loop_stopped", "host_stop", "not_applicable");
            }
        }

        public async Task<WorkItemRunResult> ProcessItemAsync(string item, CancellationToken cancellationToken)
        {
            var claim = _claims.TryClaim(item, _options.OwnerId, _clock.UtcNow, _options.ClaimLease);
            if (claim.Status == ClaimStatus.DuplicateContention)
            {
                Record("claim-contention", item, "duplicate_contention", "claim_retained_by_other_owner", "fresh_claim_exists", "claim_present", "unknown", "reconcile_existing_report_before_retry_or_takeover");
                return new WorkItemRunResult { Status = "duplicate_contention", AttemptCount = 0, ClaimReleased = false, OutputState = "unknown", ReconciliationRule = "reconcile_existing_report_before_retry_or_takeover" };
            }
            if (claim.Status == ClaimStatus.StorageFailure)
            {
                Record("claim-store-failure", item, claim.ErrorClassification, "claim_not_acquired", "claim_store_unavailable", "claim_state_unknown", "unknown", "reconcile_existing_report_before_retry_or_takeover");
                return new WorkItemRunResult
                {
                    Status = "claim_store_failure",
                    AttemptCount = 0,
                    ClaimReleased = false,
                    ErrorClassification = claim.ErrorClassification,
                    Postcondition = "claim_not_acquired",
                    OutputState = "unknown",
                    ReconciliationRule = "reconcile_existing_report_before_retry_or_takeover"
                };
            }

            var leaseLost = false;
            using (var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken))
            {
                var ownerFence = new Func<bool>(() =>
                {
                    var current = IsCurrent(claim.Handle);
                    if (!current) leaseLost = true;
                    return current;
                });
                var heartbeat = RunLeaseHeartbeatAsync(claim.Handle, linked.Token, () =>
                {
                    leaseLost = true;
                    linked.Cancel();
                });
                RetryExecutionResult execution;
                try
                {
                    execution = await _retryPolicy.ExecuteAsync(
                        new FencedWorkItemProcessor(_processor, ownerFence),
                        item,
                        linked.Token).ConfigureAwait(false);
                }
                catch (Exception ex)
                {
                    execution = new RetryExecutionResult
                    {
                        AttemptCount = 0,
                        FinalResult = WorkItemResult.Failure("worker_exception:" + ex.GetType().Name, false, "processing_exception")
                    };
                }
                finally
                {
                    linked.Cancel();
                    try
                    {
                        await heartbeat.ConfigureAwait(false);
                    }
                    catch (Exception)
                    {
                        leaseLost = true;
                    }
                }

                var final = execution.FinalResult ?? WorkItemResult.Failure("invalid_processor_result", false, "processing_result_invalid");
                if (leaseLost)
                {
                    final.Succeeded = false;
                    final.Retryable = false;
                    final.ErrorClassification = "claim_fenced";
                    final.Postcondition = AppendPostcondition(final.Postcondition, "claim_fenced;processing_stopped");
                    final.ReconciliationRule = "reconcile_existing_report_before_retry_or_takeover";
                }

                var released = false;
                try
                {
                    released = _claims.Release(claim.Handle);
                }
                catch (Exception ex)
                {
                    if (!leaseLost)
                    {
                        final = WorkItemResult.Failure("claim_release_exception:" + ex.GetType().Name, false, "claim_release_failed");
                    }
                }

                var status = leaseLost
                    ? "claim_fenced"
                    : (execution.Cancelled ? "cancelled" : (final.Succeeded ? "succeeded" : "failed"));
                if (!released && !leaseLost)
                {
                    status = "claim_release_failed";
                    final = WorkItemResult.Failure("claim_release_failed", false, "claim_release_not_confirmed");
                }
                Record(
                    "item-" + status,
                    item,
                    final.ErrorClassification,
                    final.Postcondition,
                    final.Succeeded ? "processing_succeeded" : "processing_failed",
                    released ? "claim_released" : "claim_retained_or_unknown",
                    final.OutputState,
                    final.ReconciliationRule);
                return new WorkItemRunResult
                {
                    Status = status,
                    AttemptCount = execution.AttemptCount,
                    ClaimReleased = released,
                    ErrorClassification = final.ErrorClassification,
                    Postcondition = final.Postcondition,
                    OutputState = final.OutputState,
                    ReconciliationRule = final.ReconciliationRule
                };
            }
        }

        public async Task<ControlPlaneExecutionResult> ExecuteControlPlaneWithTimeoutAsync(
            string item,
            Func<CancellationToken, Task> operation,
            CancellationToken cancellationToken)
        {
            if (operation == null) throw new ArgumentNullException("operation");
            var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            Task operationTask;
            try
            {
                operationTask = operation(linked.Token);
                if (operationTask == null) throw new InvalidOperationException("control-plane operation returned null");
            }
            catch (Exception ex)
            {
                linked.Dispose();
                var failed = new ControlPlaneExecutionResult
                {
                    Status = "failed",
                    ErrorClassification = ex.GetType().Name,
                    ClaimState = ObserveClaimState(item),
                    Postcondition = "control_plane_operation_failed"
                };
                Record("control-plane-failure", item, failed.ErrorClassification, failed.Postcondition, ex.Message, failed.ClaimState);
                return failed;
            }

            var timeoutTask = Task.Delay(_options.ControlPlaneTimeout, CancellationToken.None);
            var completed = await Task.WhenAny(operationTask, timeoutTask).ConfigureAwait(false);
            if (completed != operationTask)
            {
                linked.Cancel();
                var claimState = ObserveClaimState(item);
                var result = new ControlPlaneExecutionResult
                {
                    Status = "timeout",
                    ErrorClassification = "control_plane_timeout",
                    ClaimState = claimState,
                    Postcondition = "operation_cancel_requested;operation_completion_unconfirmed;claim_state_observed:" + claimState + ";timeout_recorded_outside_appdata"
                };
                Record("timeout", item, result.ErrorClassification, result.Postcondition, "operation_not_confirmed", claimState);
                _ = ObserveTimedOutOperationAsync(operationTask, linked, item);
                return result;
            }

            try
            {
                await operationTask.ConfigureAwait(false);
                linked.Dispose();
                var result = new ControlPlaneExecutionResult
                {
                    Status = "succeeded",
                    ClaimState = ObserveClaimState(item),
                    Postcondition = "control_plane_operation_completed"
                };
                Record("control-plane-complete", item, null, result.Postcondition, "operation_completed", result.ClaimState);
                return result;
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                linked.Dispose();
                var result = new ControlPlaneExecutionResult
                {
                    Status = "cancelled",
                    ErrorClassification = "cancellation",
                    ClaimState = ObserveClaimState(item),
                    Postcondition = "control_plane_operation_cancelled"
                };
                Record("cancellation", item, result.ErrorClassification, result.Postcondition, "operation_cancelled", result.ClaimState);
                return result;
            }
            catch (Exception ex)
            {
                linked.Dispose();
                var result = new ControlPlaneExecutionResult
                {
                    Status = "failed",
                    ErrorClassification = ex.GetType().Name,
                    ClaimState = ObserveClaimState(item),
                    Postcondition = "control_plane_operation_failed"
                };
                Record("control-plane-failure", item, result.ErrorClassification, result.Postcondition, ex.Message, result.ClaimState);
                return result;
            }
        }

        private async Task RunLeaseHeartbeatAsync(ClaimHandle handle, CancellationToken cancellationToken, Action leaseLost)
        {
            var intervalTicks = Math.Max(TimeSpan.FromMilliseconds(1).Ticks, _options.ClaimLease.Ticks / 3);
            var interval = new TimeSpan(Math.Min(intervalTicks, _options.ClaimLease.Ticks));
            while (!cancellationToken.IsCancellationRequested)
            {
                try
                {
                    await Task.Delay(interval, cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
                {
                    return;
                }
                if (cancellationToken.IsCancellationRequested) return;
                if (!_claims.Renew(handle, _clock.UtcNow, _options.ClaimLease))
                {
                    leaseLost();
                    return;
                }
            }
        }

        private bool IsCurrent(ClaimHandle handle)
        {
            try
            {
                return _claims.IsCurrent(handle, _clock.UtcNow);
            }
            catch (Exception)
            {
                return false;
            }
        }

        private async Task ObserveTimedOutOperationAsync(Task operationTask, CancellationTokenSource linked, string item)
        {
            try
            {
                await operationTask.ConfigureAwait(false);
                var claimState = ObserveClaimState(item);
                Record("timeout-operation-complete", item, null, "operation_completed_after_timeout", "operation_completed_after_timeout", claimState);
            }
            catch (OperationCanceledException)
            {
                var claimState = ObserveClaimState(item);
                Record("timeout-operation-cancelled", item, "cancellation", "operation_cancelled_after_timeout", "operation_cancelled_after_timeout", claimState);
            }
            catch (Exception ex)
            {
                var claimState = ObserveClaimState(item);
                Record("timeout-operation-failure", item, ex.GetType().Name, "operation_failed_after_timeout", ex.Message, claimState);
            }
            finally
            {
                linked.Dispose();
            }
        }

        private string ObserveClaimState(string item)
        {
            try
            {
                return _claims.Read(item) == null ? "claim_absent" : "claim_present";
            }
            catch (Exception ex)
            {
                return "claim_state_unavailable:" + ex.GetType().Name;
            }
        }

        private void RecordCancellation()
        {
            if (_cancellationRecorded) return;
            _cancellationRecorded = true;
            Record("cancellation", null, "cancellation", "item claims are released in the item-finally path", "host_cancellation_observed", "not_applicable", "unknown", "reconcile_existing_report_before_retry_or_takeover");
        }

        private void Record(
            string eventName,
            string item,
            string error,
            string postcondition,
            string detail,
            string claimState,
            string outputState = null,
            string reconciliationRule = null)
        {
            _controlPlane.Record(new ControlPlaneRecord
            {
                Event = eventName,
                OccurredAt = _clock.UtcNow,
                Item = item,
                ErrorClassification = error,
                ClaimState = claimState,
                Postcondition = postcondition,
                Detail = detail,
                OutputState = outputState,
                ReconciliationRule = reconciliationRule
            });
        }

        private static string AppendPostcondition(string current, string addition)
        {
            return string.IsNullOrWhiteSpace(current) ? addition : current + ";" + addition;
        }

        private sealed class FencedWorkItemProcessor : IWorkItemProcessor
        {
            private readonly IWorkItemProcessor _inner;
            private readonly Func<bool> _ownerFence;
            private readonly IClaimAwareWorkItemProcessor _claimAware;

            public FencedWorkItemProcessor(IWorkItemProcessor inner, Func<bool> ownerFence)
            {
                _inner = inner;
                _ownerFence = ownerFence;
                _claimAware = inner as IClaimAwareWorkItemProcessor;
            }

            public Task<WorkItemResult> ProcessAsync(string item, CancellationToken cancellationToken)
            {
                if (!_ownerFence()) return Task.FromResult(WorkItemResult.Failure("claim_fenced", false, "output_absent"));
                return _claimAware == null
                    ? _inner.ProcessAsync(item, cancellationToken)
                    : _claimAware.ProcessAsync(item, cancellationToken, _ownerFence);
            }
        }
    }

    public sealed class WorkItemRunResult
    {
        public string Status { get; set; }
        public int AttemptCount { get; set; }
        public bool ClaimReleased { get; set; }
        public string ErrorClassification { get; set; }
        public string Postcondition { get; set; }
        public string OutputState { get; set; }
        public string ReconciliationRule { get; set; }
    }
}
