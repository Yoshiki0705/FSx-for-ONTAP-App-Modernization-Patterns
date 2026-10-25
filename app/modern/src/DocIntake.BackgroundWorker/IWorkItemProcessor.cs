using System;
using System.Threading;
using System.Threading.Tasks;

namespace DocIntake.BackgroundWorker
{
    public sealed class WorkItemResult
    {
        public bool Succeeded { get; set; }
        public bool Retryable { get; set; }
        public bool CancellationObserved { get; set; }
        public string ErrorClassification { get; set; }
        public string Postcondition { get; set; }
        public string OutputState { get; set; }
        public string ReconciliationRule { get; set; }

        public static WorkItemResult Success(string postcondition)
        {
            return new WorkItemResult
            {
                Succeeded = true,
                Postcondition = postcondition,
                OutputState = "present",
                ReconciliationRule = "reconcile_existing_report_before_retry_or_takeover"
            };
        }

        public static WorkItemResult Failure(string errorClassification, bool retryable, string postcondition)
        {
            return new WorkItemResult
            {
                Succeeded = false,
                Retryable = retryable,
                ErrorClassification = errorClassification,
                Postcondition = postcondition,
                OutputState = OutputStateFromPostcondition(postcondition),
                ReconciliationRule = "reconcile_existing_report_before_retry_or_takeover"
            };
        }

        public static WorkItemResult Cancelled(string errorClassification, string postcondition, string outputState)
        {
            return new WorkItemResult
            {
                Succeeded = false,
                Retryable = false,
                CancellationObserved = true,
                ErrorClassification = errorClassification,
                Postcondition = postcondition,
                OutputState = outputState,
                ReconciliationRule = "reconcile_existing_report_before_retry_or_takeover"
            };
        }

        private static string OutputStateFromPostcondition(string postcondition)
        {
            if (string.Equals(postcondition, "no_output_written", StringComparison.Ordinal)
                || string.Equals(postcondition, "output_absent", StringComparison.Ordinal)) return "absent";
            if (string.Equals(postcondition, "output_present", StringComparison.Ordinal)
                || (postcondition != null && postcondition.StartsWith("output_present;", StringComparison.Ordinal))
                || (postcondition != null && postcondition.StartsWith("report_written:", StringComparison.Ordinal))) return "present";
            return "unknown";
        }
    }

    public interface IWorkItemProcessor
    {
        Task<WorkItemResult> ProcessAsync(string item, CancellationToken cancellationToken);
    }

    public interface IClaimAwareWorkItemProcessor
    {
        Task<WorkItemResult> ProcessAsync(string item, CancellationToken cancellationToken, Func<bool> ownerFence);
    }
}
