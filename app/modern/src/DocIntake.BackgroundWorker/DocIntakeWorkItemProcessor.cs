using System;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using DocIntake.Core;

namespace DocIntake.BackgroundWorker
{
    public sealed class DocIntakeWorkItemProcessor : IWorkItemProcessor, IClaimAwareWorkItemProcessor
    {
        private readonly IFileStore _store;
        private readonly string _stage;

        public DocIntakeWorkItemProcessor(IFileStore store, string stage)
        {
            _store = store ?? throw new ArgumentNullException("store");
            if (string.IsNullOrWhiteSpace(stage)) throw new ArgumentException("stage is required", "stage");
            _stage = stage;
        }

        // IFileStore is a synchronous contract. Cancellation is therefore cooperative at the
        // boundaries before the read, between read and write, and after the write; an in-flight
        // NFS read/write cannot be interrupted by this process until the store call returns.
        public Task<WorkItemResult> ProcessAsync(string item, CancellationToken cancellationToken)
        {
            return ProcessAsync(item, cancellationToken, null);
        }

        public Task<WorkItemResult> ProcessAsync(
            string item,
            CancellationToken cancellationToken,
            Func<bool> ownerFence)
        {
            var outputWriteAttempted = false;
            var outputWriteCompleted = false;
            try
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (ownerFence != null && !ownerFence())
                {
                    return Task.FromResult(WorkItemResult.Failure("claim_fenced", false, "output_absent"));
                }
                if (string.IsNullOrWhiteSpace(item) || item.IndexOf('/') >= 0 || item.IndexOf('\\') >= 0)
                {
                    return Task.FromResult(WorkItemResult.Failure("invalid_item", false, "no_output_written"));
                }

                var content = _store.Read(StorePaths.InboxDir + "/" + item);
                cancellationToken.ThrowIfCancellationRequested();
                if (ownerFence != null && !ownerFence())
                {
                    return Task.FromResult(WorkItemResult.Failure("claim_fenced", false, "output_absent"));
                }
                var text = Encoding.UTF8.GetString(content);
                var firstLine = text.Split('\n')[0];
                var summary = "length=" + text.Length + "\nfirst-line=" + firstLine + "\n";
                var report = "out/" + _stage + "/reports/" + item + ".txt";
                outputWriteAttempted = true;
                _store.Write(report, Encoding.UTF8.GetBytes(summary));
                outputWriteCompleted = true;

                if (cancellationToken.IsCancellationRequested)
                {
                    return Task.FromResult(WorkItemResult.Cancelled(
                        "cancellation_after_output",
                        "output_present;idempotency_reconciliation_required",
                        "present"));
                }
                if (ownerFence != null && !ownerFence())
                {
                    return Task.FromResult(WorkItemResult.Failure(
                        "claim_fenced_after_output",
                        false,
                        "output_present;idempotency_reconciliation_required"));
                }
                return Task.FromResult(WorkItemResult.Success("report_written:" + report));
            }
            catch (OperationCanceledException)
            {
                if (outputWriteCompleted)
                {
                    return Task.FromResult(WorkItemResult.Cancelled(
                        "cancellation_after_output",
                        "output_present;idempotency_reconciliation_required",
                        "present"));
                }
                if (outputWriteAttempted)
                {
                    return Task.FromResult(WorkItemResult.Cancelled(
                        "cancellation_during_output_write",
                        "output_state_unknown;reconcile_before_retry_or_takeover",
                        "unknown"));
                }
                return Task.FromResult(WorkItemResult.Cancelled(
                    "cancellation",
                    "output_absent",
                    "absent"));
            }
            catch (FileNotFoundException)
            {
                return Task.FromResult(WorkItemResult.Failure("permanent_missing_inbox", false, "no_output_written"));
            }
            catch (DirectoryNotFoundException)
            {
                return Task.FromResult(WorkItemResult.Failure("permanent_missing_inbox", false, "no_output_written"));
            }
            catch (UnauthorizedAccessException)
            {
                return Task.FromResult(WorkItemResult.Failure("permanent_access_denied", false, "output_postcondition_unknown"));
            }
            catch (IOException)
            {
                // Generic I/O remains explicitly retryable. Missing paths and access failures are
                // classified above so a permanent local defect is not retried as an outage.
                return Task.FromResult(WorkItemResult.Failure("transient_io", true, "output_postcondition_unknown"));
            }
            catch (Exception ex)
            {
                return Task.FromResult(WorkItemResult.Failure("permanent_processor_error:" + ex.GetType().Name, false, "output_postcondition_unknown"));
            }
        }
    }
}
