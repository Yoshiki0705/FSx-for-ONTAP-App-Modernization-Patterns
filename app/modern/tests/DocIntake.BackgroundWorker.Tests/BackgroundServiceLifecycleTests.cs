using System;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using DocIntake.BackgroundWorker;
using DocIntake.Core;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Xunit;

namespace DocIntake.BackgroundWorker.Tests
{
    public sealed class BackgroundServiceLifecycleTests
    {
        [Fact]
        public async Task Default_processor_reads_inbox_and_writes_one_report()
        {
            using (var temp = new TempDirectory())
            {
                var inbox = System.IO.Path.Combine(temp.Path, "seed", "inbox");
                Directory.CreateDirectory(inbox);
                File.WriteAllText(System.IO.Path.Combine(inbox, "item.txt"), "first\nsecond");
                var processor = new DocIntakeWorkItemProcessor(new SmbFileStore(temp.Path), "candidate-a");

                var result = await processor.ProcessAsync("item.txt", CancellationToken.None);

                Assert.True(result.Succeeded);
                Assert.Equal("report_written:out/candidate-a/reports/item.txt.txt", result.Postcondition);
                Assert.Equal("length=12\nfirst-line=first\n", File.ReadAllText(
                    System.IO.Path.Combine(temp.Path, "out", "candidate-a", "reports", "item.txt.txt")));
            }
        }

        [Fact]
        public async Task Host_records_startup_cancellation_and_normal_stop_in_order()
        {
            using (var temp = new TempDirectory())
            {
                var controlPath = System.IO.Path.Combine(temp.Path, "external", "events.jsonl");
                var options = TestFactory.Options(temp.Path, controlPath);
                var recorder = new RecordingControlPlaneRecorder();
                var processor = new ScriptedProcessor(WorkItemResult.Success("not_used"));
                using (var host = new HostBuilder()
                    .ConfigureServices(services =>
                    {
                        services.AddSingleton(options);
                        services.AddSingleton<IClock>(new SystemClock());
                        services.AddSingleton<DocIntake.Core.IFileStore>(new DocIntake.Core.SmbFileStore(temp.Path));
                        services.AddSingleton<IClaimStore>(new FileClaimStore(temp.Path));
                        services.AddSingleton<IControlPlaneRecorder>(recorder);
                        services.AddSingleton<IWorkItemProcessor>(processor);
                        services.AddHostedService<DocIntakeBackgroundService>();
                    })
                    .Build())
                {
                    await host.StartAsync();
                    await Task.Delay(30);
                    await host.StopAsync();
                }

                var events = recorder.Events();
                Assert.Equal(new[] { "startup", "cancellation", "normal-stop" }, events);
                Assert.True(events.SequenceEqual(new[] { "startup", "cancellation", "normal-stop" }));
            }
        }
        [Fact]
        public async Task Hosted_service_enumerates_claims_processes_releases_and_polls()
        {
            using (var temp = new TempDirectory())
            {
                var controlPath = System.IO.Path.Combine(temp.Path, "external", "events.jsonl");
                var inbox = new OneShotInboxStore();
                var processed = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
                var processor = new ScriptedProcessor((item, token) =>
                {
                    processed.TrySetResult(true);
                    return Task.FromResult(WorkItemResult.Success("output_present"));
                });
                var recorder = new RecordingControlPlaneRecorder();
                var options = TestFactory.Options(temp.Path, controlPath);
                using (var host = new HostBuilder()
                    .ConfigureServices(services =>
                    {
                        services.AddSingleton(options);
                        services.AddSingleton<IClock>(new SystemClock());
                        services.AddSingleton<IFileStore>(inbox);
                        services.AddSingleton<IClaimStore>(new FileClaimStore(temp.Path));
                        services.AddSingleton<IControlPlaneRecorder>(recorder);
                        services.AddSingleton<IWorkItemProcessor>(processor);
                        services.AddHostedService<DocIntakeBackgroundService>();
                    })
                    .Build())
                {
                    await host.StartAsync();
                    await processed.Task;
                    await WaitForClaimAbsentAsync(temp.Path, "item.txt");
                    await WaitForListCallsAsync(inbox);
                    await host.StopAsync();
                }

                Assert.Equal(1, processor.Attempts);
                Assert.True(inbox.ListCalls >= 2);
                Assert.Contains("item-succeeded", recorder.Events());
                Assert.Equal(new[] { "startup", "item-succeeded", "cancellation", "normal-stop" }, recorder.Events());
            }
        }

        [Fact]
        public async Task Competing_hosts_renew_active_claim_and_record_one_post_write_cancellation()
        {
            using (var temp = new TempDirectory())
            {
                var controlPath = System.IO.Path.Combine(temp.Path, "external", "events.jsonl");
                var store = new CountingInboxStore();
                var ownerRecorder = new RecordingControlPlaneRecorder();
                var successorRecorder = new RecordingControlPlaneRecorder();
                var ownerProcessor = new CancellationAfterWriteProcessor(store, store.WriteCompleted);
                var successorProcessor = new CancellationAfterWriteProcessor(store, new TaskCompletionSource<bool>());
                var ownerOptions = TestFactory.Options(temp.Path, controlPath, "owner-a");
                ownerOptions.ClaimLease = TimeSpan.FromMilliseconds(500);
                ownerOptions.PollInterval = TimeSpan.FromMilliseconds(1);
                var successorOptions = TestFactory.Options(temp.Path, controlPath, "owner-b");
                successorOptions.ClaimLease = ownerOptions.ClaimLease;
                successorOptions.PollInterval = ownerOptions.PollInterval;

                using (var ownerHost = BuildHost(temp.Path, store, ownerRecorder, ownerProcessor, ownerOptions))
                using (var successorHost = BuildHost(temp.Path, store, successorRecorder, successorProcessor, successorOptions))
                {
                    await ownerHost.StartAsync();
                    await store.WriteCompleted.Task;
                    var initialClaim = new FileClaimStore(temp.Path).Read("item.txt");
                    Assert.NotNull(initialClaim);

                    await Task.Delay(ownerOptions.ClaimLease + TimeSpan.FromMilliseconds(250));
                    var renewedClaim = new FileClaimStore(temp.Path).Read("item.txt");
                    Assert.NotNull(renewedClaim);
                    Assert.True(renewedClaim.LastRenewedAt.HasValue);
                    Assert.True(renewedClaim.LastRenewedAt.Value > initialClaim.AcquiredAt);

                    await successorHost.StartAsync();
                    await WaitForEventAsync(successorRecorder, "claim-contention");
                    Assert.Equal(1, store.WriteCount);

                    await successorHost.StopAsync();
                    await ownerHost.StopAsync();
                }

                Assert.Equal(1, store.WriteCount);
                Assert.Contains("item-cancelled", ownerRecorder.Events());
                var cancelled = ownerRecorder.Records.Find(record => record.Event == "item-cancelled");
                Assert.NotNull(cancelled);
                Assert.Equal("present", cancelled.OutputState);
                Assert.Equal("cancellation_after_output", cancelled.ErrorClassification);
                Assert.Equal("reconcile_existing_report_before_retry_or_takeover", cancelled.ReconciliationRule);
                Assert.Null(new FileClaimStore(temp.Path).Read("item.txt"));
            }
        }

        private static IHost BuildHost(
            string root,
            IFileStore store,
            RecordingControlPlaneRecorder recorder,
            IWorkItemProcessor processor,
            BackgroundWorkerOptions options)
        {
            return new HostBuilder()
                .ConfigureServices(services =>
                {
                    services.AddSingleton(options);
                    services.AddSingleton<IClock>(new SystemClock());
                    services.AddSingleton(store);
                    services.AddSingleton<IClaimStore>(new FileClaimStore(root));
                    services.AddSingleton<IControlPlaneRecorder>(recorder);
                    services.AddSingleton(processor);
                    services.AddHostedService<DocIntakeBackgroundService>();
                })
                .Build();
        }

        private static async Task WaitForEventAsync(RecordingControlPlaneRecorder recorder, string eventName)
        {
            for (var i = 0; i < 500; i++)
            {
                if (recorder.Events().Contains(eventName)) return;
                await Task.Delay(1).ConfigureAwait(false);
            }
            Assert.Contains(eventName, recorder.Events());
        }

        private static async Task WaitForListCallsAsync(OneShotInboxStore inbox)
        {
            for (var i = 0; i < 100; i++)
            {
                if (inbox.ListCalls >= 2) return;
                await Task.Delay(1).ConfigureAwait(false);
            }
            Assert.True(inbox.ListCalls >= 2);
        }

        private static async Task WaitForClaimAbsentAsync(string root, string item)
        {
            for (var i = 0; i < 100; i++)
            {
                if (new FileClaimStore(root).Read(item) == null) return;
                await Task.Delay(1).ConfigureAwait(false);
            }
            Assert.Null(new FileClaimStore(root).Read(item));
        }
    }
}