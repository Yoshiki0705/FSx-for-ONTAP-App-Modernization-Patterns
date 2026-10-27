using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using DocIntake.BackgroundWorker;
using Xunit;

namespace DocIntake.BackgroundWorker.Tests
{
    public sealed class ControlPlaneTimeoutTests
    {
        [Fact]
        public async Task Timeout_is_recorded_outside_appdata_with_observed_claim_state_and_drained_operation()
        {
            using (var temp = new TempDirectory())
            {
                var appData = System.IO.Path.Combine(temp.Path, "appdata");
                var external = System.IO.Path.Combine(temp.Path, "control", "events.jsonl");
                Directory.CreateDirectory(appData);
                var recorder = new FileControlPlaneRecorder(appData, external);
                var options = TestFactory.Options(appData, external);
                options.ControlPlaneTimeout = TimeSpan.FromMilliseconds(10);
                var service = TestFactory.Service(appData, external, new ScriptedProcessor(WorkItemResult.Success("unused")), recorder, options: options);
                var claims = new FileClaimStore(appData);
                var claim = claims.TryClaim("item.txt", "owner", DateTimeOffset.UtcNow, TimeSpan.FromMinutes(1));
                var completed = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);

                // The control-plane timeout is 10 ms. The operation delay must be far larger so the
                // timeout always fires first, regardless of CI scheduling jitter. A 50 ms delay
                // (5x the timeout) was flaky: under load the operation could complete before the
                // 10 ms timeout was observed, so result.Status came back "succeeded" instead of
                // "timeout". 2000 ms (200x) keeps the ordering deterministic. The test still waits
                // for the operation to finish afterward (completed.Task) and records its completion.
                var result = await service.ExecuteControlPlaneWithTimeoutAsync(
                    "item.txt",
                    token => Task.Run(async () =>
                    {
                        await Task.Delay(2000).ConfigureAwait(false);
                        completed.SetResult(true);
                    }),
                    CancellationToken.None);

                Assert.Equal("timeout", result.Status);
                Assert.Equal("control_plane_timeout", result.ErrorClassification);
                Assert.Equal("claim_present", result.ClaimState);
                Assert.Contains("claim_state_observed:claim_present", result.Postcondition);
                Assert.Contains("outside_appdata", result.Postcondition);
                await completed.Task;
                await WaitForFileEventAsync(external, "timeout-operation-complete");
                Assert.True(File.Exists(external));
                Assert.DoesNotContain(System.IO.Path.GetFullPath(appData) + System.IO.Path.DirectorySeparatorChar, System.IO.Path.GetFullPath(external));
                var records = File.ReadAllText(external);
                Assert.Contains("\"Event\":\"timeout\"", records);
                Assert.Contains("\"Event\":\"timeout-operation-complete\"", records);
                Assert.True(claim.Handle != null);
                Assert.True(claims.Release(claim.Handle));
            }
        }

        [Fact]
        public void Recorder_rejects_a_control_path_under_appdata()
        {
            using (var temp = new TempDirectory())
            {
                Assert.Throws<ArgumentException>(() => new FileControlPlaneRecorder(temp.Path, System.IO.Path.Combine(temp.Path, "events.jsonl")));
            }
        }

        [Fact]
        public void Recorder_rejects_a_symlink_alias_into_appdata()
        {
            using (var temp = new TempDirectory())
            {
                var appData = System.IO.Path.Combine(temp.Path, "appdata");
                var alias = System.IO.Path.Combine(temp.Path, "outside-alias");
                Directory.CreateDirectory(appData);
                Directory.CreateSymbolicLink(alias, appData);

                Assert.Throws<ArgumentException>(() => new FileControlPlaneRecorder(appData, System.IO.Path.Combine(alias, "events.jsonl")));
            }
        }

        private static async Task WaitForFileEventAsync(string path, string eventName)
        {
            for (var i = 0; i < 100; i++)
            {
                if (File.Exists(path) && File.ReadAllText(path).Contains("\"Event\":\"" + eventName + "\"")) return;
                await Task.Delay(1).ConfigureAwait(false);
            }
            Assert.True(File.Exists(path));
            Assert.Contains("\"Event\":\"" + eventName + "\"", File.ReadAllText(path));
        }
    }
}
