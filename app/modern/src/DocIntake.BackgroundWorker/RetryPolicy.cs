using System;
using System.Threading;
using System.Threading.Tasks;

namespace DocIntake.BackgroundWorker
{
    public sealed class RetryExecutionResult
    {
        public int AttemptCount { get; set; }
        public bool Cancelled { get; set; }
        public WorkItemResult FinalResult { get; set; }
    }

    public sealed class RetryPolicy
    {
        private readonly int _maxAttempts;
        private readonly TimeSpan _delay;
        private readonly Func<TimeSpan, CancellationToken, Task> _delayAsync;

        public RetryPolicy(int maxAttempts, TimeSpan delay, Func<TimeSpan, CancellationToken, Task> delayAsync = null)
        {
            if (maxAttempts < 1) throw new ArgumentOutOfRangeException("maxAttempts");
            if (delay < TimeSpan.Zero) throw new ArgumentOutOfRangeException("delay");
            _maxAttempts = maxAttempts;
            _delay = delay;
            _delayAsync = delayAsync ?? Task.Delay;
        }

        public async Task<RetryExecutionResult> ExecuteAsync(IWorkItemProcessor processor, string item, CancellationToken cancellationToken)
        {
            if (processor == null) throw new ArgumentNullException("processor");
            var result = new RetryExecutionResult();
            for (var attempt = 1; attempt <= _maxAttempts; attempt++)
            {
                if (cancellationToken.IsCancellationRequested)
                {
                    result.Cancelled = true;
                    result.FinalResult = WorkItemResult.Cancelled("cancellation", "output_absent", "absent");
                    return result;
                }
                result.AttemptCount = attempt;
                WorkItemResult current;
                try
                {
                    current = await processor.ProcessAsync(item, cancellationToken).ConfigureAwait(false);
                    current = Normalize(current);
                }
                catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
                {
                    result.Cancelled = true;
                    result.FinalResult = WorkItemResult.Cancelled("cancellation", "output_state_unknown;reconcile_before_retry_or_takeover", "unknown");
                    return result;
                }
                catch (Exception ex)
                {
                    current = WorkItemResult.Failure("processor_exception:" + ex.GetType().Name, false, "processing_exception");
                }

                result.FinalResult = current;
                if (current.CancellationObserved)
                {
                    result.Cancelled = true;
                    return result;
                }
                if (current.Succeeded || !current.Retryable || attempt == _maxAttempts)
                {
                    return result;
                }

                try
                {
                    await _delayAsync(_delay, cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
                {
                    result.Cancelled = true;
                    result.FinalResult = WorkItemResult.Failure("cancellation", false, "retry_cancelled");
                    return result;
                }
            }
            return result;
        }

        private static WorkItemResult Normalize(WorkItemResult result)
        {
            if (result == null)
            {
                return WorkItemResult.Failure("invalid_processor_result", false, "processing_result_invalid");
            }
            if (result.Succeeded && string.IsNullOrWhiteSpace(result.Postcondition))
            {
                return WorkItemResult.Failure("invalid_processor_result", false, "processing_result_invalid");
            }
            if (!result.Succeeded && string.IsNullOrWhiteSpace(result.ErrorClassification))
            {
                return WorkItemResult.Failure("invalid_processor_result", false, "processing_result_invalid");
            }
            return result;
        }
    }
}
