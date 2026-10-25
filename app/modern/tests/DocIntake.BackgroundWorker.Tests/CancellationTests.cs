using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using DocIntake.BackgroundWorker;
using DocIntake.Core;
using Xunit;

namespace DocIntake.BackgroundWorker.Tests
{
    public sealed class CancellationTests
    {
        [Fact]
        public async Task Cancellation_stops_processing_releases_claim_and_does_not_retry()
        {
            using (var temp = new TempDirectory())
            {
                Directory.CreateDirectory(System.IO.Path.Combine(temp.Path, "seed", "inbox"));
                File.WriteAllText(System.IO.Path.Combine(temp.Path, "seed", "inbox", "item.txt"), "content");
                var entered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
                var processor = new ScriptedProcessor(async (item, token) =>
                {
                    entered.SetResult(true);
                    await Task.Delay(Timeout.InfiniteTimeSpan, token).ConfigureAwait(false);
                    return WorkItemResult.Success("unreachable");
                });
                var recorder = new RecordingControlPlaneRecorder();
                var service = TestFactory.Service(temp.Path, System.IO.Path.Combine(temp.Path, "events.jsonl"), processor, recorder);
                using (var cancellation = new CancellationTokenSource())
                {
                    var run = service.ProcessItemAsync("item.txt", cancellation.Token);
                    await entered.Task;
                    cancellation.Cancel();
                    var result = await run;
                    Assert.Equal("cancelled", result.Status);
                    Assert.Equal(1, result.AttemptCount);
                    Assert.True(result.ClaimReleased);
                    Assert.Equal(1, processor.Attempts);
                    Assert.True(processor.LastToken.IsCancellationRequested);
                }
                Assert.Null(new FileClaimStore(temp.Path).Read("item.txt"));
            }
        }

        [Fact]
        public async Task Default_processor_cancellation_is_observed_after_synchronous_store_call_returns()
        {
            var store = new BlockingReadFileStore();
            var processor = new DocIntakeWorkItemProcessor(store, "candidate-a");
            using (var cancellation = new CancellationTokenSource())
            {
                var run = Task.Run(() => processor.ProcessAsync("item.txt", cancellation.Token));
                await store.ReadEntered.Task;
                cancellation.Cancel();
                Assert.False(run.IsCompleted);
                store.ReleaseRead.TrySetResult(true);
                var result = await run;
                Assert.False(result.Succeeded);
                Assert.True(result.CancellationObserved);
                Assert.Equal("absent", result.OutputState);
                Assert.Equal("output_absent", result.Postcondition);
            }
        }

        [Fact]
        public async Task Cancellation_after_synchronous_report_write_records_present_output_and_reconciliation()
        {
            using (var cancellation = new CancellationTokenSource())
            {
                var store = new CancellingWriteFileStore(cancellation);
                var processor = new DocIntakeWorkItemProcessor(store, "candidate-a");

                var result = await processor.ProcessAsync("item.txt", cancellation.Token);

                Assert.False(result.Succeeded);
                Assert.True(result.CancellationObserved);
                Assert.Equal("present", result.OutputState);
                Assert.Equal("cancellation_after_output", result.ErrorClassification);
                Assert.Contains("output_present", result.Postcondition);
                Assert.Equal("reconcile_existing_report_before_retry_or_takeover", result.ReconciliationRule);
                Assert.True(store.WriteCompleted);
            }
        }

        private sealed class CancellingWriteFileStore : IFileStore
        {
            private readonly CancellationTokenSource _cancellation;

            public CancellingWriteFileStore(CancellationTokenSource cancellation)
            {
                _cancellation = cancellation;
            }

            public bool WriteCompleted { get; private set; }

            public System.Collections.Generic.IEnumerable<string> List(string relativeDir) { return new[] { "item.txt" }; }
            public byte[] Read(string relativePath) { return System.Text.Encoding.UTF8.GetBytes("content"); }
            public void Write(string relativePath, byte[] content)
            {
                _cancellation.Cancel();
                WriteCompleted = true;
            }
            public Stream OpenExclusive(string relativePath) { throw new NotSupportedException(); }
            public bool CanWrite(string relativePath) { return true; }
        }
    }
}
