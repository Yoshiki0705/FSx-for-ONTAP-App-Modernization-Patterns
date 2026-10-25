using System;
using System.IO;
using System.Text;
using System.Text.Json;

namespace DocIntake.BackgroundWorker
{
    public sealed class FileControlPlaneRecorder : IControlPlaneRecorder
    {
        private readonly string _appDataRoot;
        private readonly string _recordPath;
        private readonly object _sync = new object();

        public FileControlPlaneRecorder(string appDataRoot, string recordPath)
        {
            if (string.IsNullOrWhiteSpace(appDataRoot)) throw new ArgumentException("appDataRoot is required", "appDataRoot");
            if (string.IsNullOrWhiteSpace(recordPath)) throw new ArgumentException("recordPath is required", "recordPath");
            _appDataRoot = ResolveExistingPath(appDataRoot);
            _recordPath = Path.GetFullPath(recordPath);
            EnsureOutsideAppData(_recordPath);
        }

        public string RecordPath { get { return _recordPath; } }

        public void Record(ControlPlaneRecord record)
        {
            if (record == null) throw new ArgumentNullException("record");
            lock (_sync)
            {
                EnsureOutsideAppData(_recordPath);
                var parent = Path.GetDirectoryName(_recordPath);
                if (!string.IsNullOrEmpty(parent)) Directory.CreateDirectory(parent);
                EnsureOutsideAppData(_recordPath);
                var line = JsonSerializer.Serialize(record) + Environment.NewLine;
                File.AppendAllText(_recordPath, line, Encoding.UTF8);
            }
        }

        private void EnsureOutsideAppData(string path)
        {
            if (IsInside(ResolveExistingPath(path), _appDataRoot))
            {
                throw new ArgumentException("control-plane path must be outside appdata", "recordPath");
            }
        }

        private static bool IsInside(string path, string root)
        {
            var fullPath = Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            var fullRoot = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            var comparison = OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;
            return fullPath.Equals(fullRoot, comparison) || fullPath.StartsWith(fullRoot + Path.DirectorySeparatorChar, comparison);
        }

        private static string ResolveExistingPath(string path)
        {
            var full = Path.GetFullPath(path);
            var pending = new System.Collections.Generic.Stack<string>();
            var current = full;
            while (!File.Exists(current) && !Directory.Exists(current))
            {
                var name = Path.GetFileName(current);
                if (string.IsNullOrEmpty(name)) break;
                pending.Push(name);
                current = Path.GetDirectoryName(current);
                if (string.IsNullOrEmpty(current)) break;
            }

            if (string.IsNullOrEmpty(current)) current = Path.GetPathRoot(full);
            FileSystemInfo info = Directory.Exists(current)
                ? (FileSystemInfo)new DirectoryInfo(current)
                : new FileInfo(current);
            var resolved = info.ResolveLinkTarget(true);
            var result = resolved == null ? info.FullName : resolved.FullName;
            while (pending.Count > 0) result = Path.Combine(result, pending.Pop());
            return Path.GetFullPath(result);
        }
    }
}
