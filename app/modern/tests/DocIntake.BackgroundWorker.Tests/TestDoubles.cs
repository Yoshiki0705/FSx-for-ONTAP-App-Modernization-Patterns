using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using DocIntake.BackgroundWorker;
using DocIntake.Core;

namespace DocIntake.BackgroundWorker.Tests
{
    internal sealed class TempDirectory : IDisposable
    {
        public string Path { get; private set; }

        public TempDirectory()
        {
            Path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "candidate-a-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(Path);
        }

        public void Dispose()
        {
            if (Directory.Exists(Path)) Directory.Delete(Path, true);
        }
    }

    internal sealed class ManualClock : IClock
    {
        public DateTimeOffset UtcNow { get; set; }
    }

    internal sealed class RecordingControlPlaneRecorder : IControlPlaneRecorder
    {
        public List<ControlPlaneRecord> Records { get; } = new List<ControlPlaneRecord>();

        public void Record(ControlPlaneRecord record)
        {
            lock (Records) Records.Add(record);
        }

        public string[] Events()
        {
            lock (Records)
            {
                var result = new string[Records.Count];
                for (var i = 0; i < Records.Count; i++) result[i] = Records[i].Event;
                return result;
            }
        }
    }

    internal sealed class ScriptedProcessor : IWorkItemProcessor
    {
        private readonly ConcurrentQueue<WorkItemResult> _results = new ConcurrentQueue<WorkItemResult>();
        private readonly Func<string, CancellationToken, Task<WorkItemResult>> _handler;
        public int Attempts { get; private set; }
        public CancellationToken LastToken { get; private set; }

        public ScriptedProcessor(params WorkItemResult[] results)
        {
            foreach (var result in results) _results.Enqueue(result);
        }

        public ScriptedProcessor(Func<string, CancellationToken, Task<WorkItemResult>> handler)
        {
            _handler = handler;
        }

        public async Task<WorkItemResult> ProcessAsync(string item, CancellationToken cancellationToken)
        {
            Attempts++;
            LastToken = cancellationToken;
            if (_handler != null) return await _handler(item, cancellationToken).ConfigureAwait(false);
            WorkItemResult result;
            if (!_results.TryDequeue(out result)) throw new InvalidOperationException("script exhausted");
            return result;
        }
    }

    internal sealed class BlockingReadFileStore : IFileStore
    {
        public TaskCompletionSource<bool> ReadEntered { get; } = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource<bool> ReleaseRead { get; } = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);

        public IEnumerable<string> List(string relativeDir) { return new[] { "item.txt" }; }

        public byte[] Read(string relativePath)
        {
            ReadEntered.TrySetResult(true);
            ReleaseRead.Task.GetAwaiter().GetResult();
            return System.Text.Encoding.UTF8.GetBytes("content");
        }

        public void Write(string relativePath, byte[] content) { }

        public Stream OpenExclusive(string relativePath) { throw new NotSupportedException(); }

        public bool CanWrite(string relativePath) { return true; }
    }

    internal sealed class OneShotInboxStore : IFileStore
    {
        private int _listCalls;

        public int ListCalls { get { return _listCalls; } }

        public IEnumerable<string> List(string relativeDir)
        {
            var call = Interlocked.Increment(ref _listCalls);
            return call == 1 ? new[] { "item.txt" } : Array.Empty<string>();
        }

        public byte[] Read(string relativePath) { return System.Text.Encoding.UTF8.GetBytes("content"); }
        public void Write(string relativePath, byte[] content) { }
        public Stream OpenExclusive(string relativePath) { throw new NotSupportedException(); }
        public bool CanWrite(string relativePath) { return true; }
    }

    internal sealed class CountingInboxStore : IFileStore
    {
        private int _writeCount;

        public TaskCompletionSource<bool> WriteCompleted { get; } = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        public int WriteCount { get { return Volatile.Read(ref _writeCount); } }

        public IEnumerable<string> List(string relativeDir) { return new[] { "item.txt" }; }
        public byte[] Read(string relativePath) { return System.Text.Encoding.UTF8.GetBytes("content"); }
        public void Write(string relativePath, byte[] content)
        {
            Interlocked.Increment(ref _writeCount);
            WriteCompleted.TrySetResult(true);
        }
        public Stream OpenExclusive(string relativePath) { throw new NotSupportedException(); }
        public bool CanWrite(string relativePath) { return true; }
    }

    internal sealed class CancellationAfterWriteProcessor : IWorkItemProcessor, IClaimAwareWorkItemProcessor
    {
        private readonly IFileStore _store;
        private readonly TaskCompletionSource<bool> _writeCompleted;

        public CancellationAfterWriteProcessor(IFileStore store, TaskCompletionSource<bool> writeCompleted)
        {
            _store = store;
            _writeCompleted = writeCompleted;
        }

        public Task<WorkItemResult> ProcessAsync(string item, CancellationToken cancellationToken)
        {
            return ProcessAsync(item, cancellationToken, null);
        }

        public async Task<WorkItemResult> ProcessAsync(string item, CancellationToken cancellationToken, Func<bool> ownerFence)
        {
            if (ownerFence != null && !ownerFence()) return WorkItemResult.Failure("claim_fenced", false, "output_absent");
            _store.Read(StorePaths.InboxDir + "/" + item);
            if (ownerFence != null && !ownerFence()) return WorkItemResult.Failure("claim_fenced", false, "output_absent");
            _store.Write("out/candidate-a/reports/" + item + ".txt", System.Text.Encoding.UTF8.GetBytes("report"));
            _writeCompleted.TrySetResult(true);
            try
            {
                await Task.Delay(Timeout.InfiniteTimeSpan, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                return WorkItemResult.Cancelled(
                    "cancellation_after_output",
                    "output_present;idempotency_reconciliation_required",
                    "present");
            }
            return WorkItemResult.Failure("unexpected_processor_completion", false, "output_present");
        }
    }

    internal static class TestFactory
    {
        public static BackgroundWorkerOptions Options(string root, string controlPath, string owner = "owner-a")
        {
            return new BackgroundWorkerOptions
            {
                AppDataRoot = root,
                ControlPlanePath = controlPath,
                Stage = "candidate-a",
                OwnerId = owner,
                ClaimLease = TimeSpan.FromMinutes(5),
                MaxAttempts = 3,
                RetryDelay = TimeSpan.Zero,
                PollInterval = TimeSpan.FromMilliseconds(1),
                ControlPlaneTimeout = TimeSpan.FromMilliseconds(20)
            };
        }

        public static DocIntakeBackgroundService Service(
            string root,
            string controlPath,
            IWorkItemProcessor processor,
            IControlPlaneRecorder recorder,
            IClock clock = null,
            BackgroundWorkerOptions options = null)
        {
            var selected = options ?? Options(root, controlPath);
            return new DocIntakeBackgroundService(
                new SmbFileStore(root),
                new FileClaimStore(root),
                recorder,
                processor,
                clock ?? new SystemClock(),
                selected,
                (delay, token) => Task.CompletedTask);
        }
    }
}
