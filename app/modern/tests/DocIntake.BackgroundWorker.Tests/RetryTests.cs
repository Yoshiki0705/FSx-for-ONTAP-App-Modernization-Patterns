using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using DocIntake.BackgroundWorker;
using Xunit;

namespace DocIntake.BackgroundWorker.Tests
{
    public sealed class RetryTests
    {
        [Fact]
        public async Task Transient_failure_retries_then_succeeds_with_injected_delay()
        {
            var delays = new List<TimeSpan>();
            var processor = new ScriptedProcessor(
                WorkItemResult.Failure("temporary_io", true, "not_written"),
                WorkItemResult.Failure("temporary_io", true, "not_written"),
                WorkItemResult.Success("written"));
            var policy = new RetryPolicy(3, TimeSpan.FromSeconds(2), (delay, token) =>
            {
                delays.Add(delay);
                return Task.CompletedTask;
            });

            var result = await policy.ExecuteAsync(processor, "item.txt", CancellationToken.None);

            Assert.Equal(3, result.AttemptCount);
            Assert.False(result.Cancelled);
            Assert.True(result.FinalResult.Succeeded);
            Assert.Equal(2, delays.Count);
            Assert.All(delays, delay => Assert.Equal(TimeSpan.FromSeconds(2), delay));
        }

        [Fact]
        public async Task Retry_limit_returns_the_last_error_without_an_extra_attempt()
        {
            var processor = new ScriptedProcessor(
                WorkItemResult.Failure("permanent_io", true, "unknown"),
                WorkItemResult.Failure("permanent_io", true, "unknown"));
            var policy = new RetryPolicy(2, TimeSpan.Zero, (delay, token) => Task.CompletedTask);

            var result = await policy.ExecuteAsync(processor, "item.txt", CancellationToken.None);

            Assert.Equal(2, result.AttemptCount);
            Assert.Equal(2, processor.Attempts);
            Assert.Equal("permanent_io", result.FinalResult.ErrorClassification);
            Assert.False(result.FinalResult.Succeeded);
        }

        [Fact]
        public async Task Cancellation_during_retry_delay_is_terminal_without_another_attempt()
        {
            var delayEntered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            var processor = new ScriptedProcessor(WorkItemResult.Failure("transient_io", true, "not_written"));
            using (var cancellation = new CancellationTokenSource())
            {
                var policy = new RetryPolicy(3, TimeSpan.FromMinutes(1), async (delay, token) =>
                {
                    delayEntered.SetResult(true);
                    await Task.Delay(Timeout.InfiniteTimeSpan, token).ConfigureAwait(false);
                });
                var run = policy.ExecuteAsync(processor, "item.txt", cancellation.Token);
                await delayEntered.Task;
                cancellation.Cancel();
                var result = await run;

                Assert.True(result.Cancelled);
                Assert.Equal(1, result.AttemptCount);
                Assert.Equal(1, processor.Attempts);
                Assert.Equal("retry_cancelled", result.FinalResult.Postcondition);
            }
        }

        [Fact]
        public async Task Null_processor_result_is_a_non_retryable_invalid_result()
        {
            var processor = new ScriptedProcessor((item, token) => Task.FromResult<WorkItemResult>(null));
            var result = await new RetryPolicy(3, TimeSpan.Zero).ExecuteAsync(processor, "item.txt", CancellationToken.None);

            Assert.Equal(1, result.AttemptCount);
            Assert.Equal("invalid_processor_result", result.FinalResult.ErrorClassification);
            Assert.False(result.FinalResult.Retryable);
            Assert.Equal(1, processor.Attempts);
        }
    }
}
