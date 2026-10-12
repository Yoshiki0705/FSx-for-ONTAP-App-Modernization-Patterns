using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.Versioning;
using System.Security.AccessControl;

namespace DocIntake.Core
{
    // SMB/NFS/local-backed store. The root is the mount point or UNC path to the share, e.g.
    // \\APPMODSVM01\appdata (Windows) or /mnt/appdata (Linux NFS).
    //
    // Migration notes (DocIntake-modernized, Phase 3 Step 2):
    //
    //   AP-1 (path-separator): Full() no longer rewrites '/' to '\\'. Relative paths use '/' and are
    //   combined with Path.Combine, which emits the OS-native separator. On Linux a backslash is no
    //   longer injected into file names.
    //
    //   file-locking: OpenExclusive still opens with FileShare.None (unchanged behaviour).
    //
    //   AP-3 / acl-evaluation (ADR-3): CanWrite is now platform-split.
    //     - Windows: keep the NTFS ACL pre-check (File/Directory.GetAccessControl), including the
    //       "missing file → parent directory ACL" branch, under an OperatingSystem.IsWindows() guard.
    //     - Linux: the volume is NTFS security style and the NFS server (ONTAP) decides writability
    //       via name-mapped NTFS ACLs; POSIX mode bits are not a faithful mirror. So decide by a real
    //       I/O attempt: open an existing target for write, or CreateNew+delete a temp file in the
    //       parent directory. UnauthorizedAccessException / IOException (EACCES / EROFS) => not
    //       writable; any other exception is raised.
    public class SmbFileStore : IFileStore
    {
        private readonly string _root;

        public SmbFileStore(string root)
        {
            _root = root;
        }

        private string Full(string relativePath)
        {
            // AP-1: combine with the OS-native separator; do not hard-code backslash.
            return StorePaths.Combine(_root, relativePath);
        }

        public IEnumerable<string> List(string relativeDir)
        {
            var dir = Full(relativeDir);
            if (!Directory.Exists(dir))
            {
                return new List<string>();
            }
            var results = new List<string>();
            foreach (var path in Directory.GetFiles(dir))
            {
                results.Add(Path.GetFileName(path));
            }
            return results;
        }

        public byte[] Read(string relativePath)
        {
            return File.ReadAllBytes(Full(relativePath));
        }

        public void Write(string relativePath, byte[] content)
        {
            var full = Full(relativePath);
            var parent = Path.GetDirectoryName(full);
            if (!string.IsNullOrEmpty(parent) && !Directory.Exists(parent))
            {
                Directory.CreateDirectory(parent);
            }
            File.WriteAllBytes(full, content);
        }

        // file-locking: FileShare.None. A concurrent open by another client is refused on SMB;
        // the cross-protocol behaviour is what the Probe measures (AIMF-external, cross-host).
        public Stream OpenExclusive(string relativePath)
        {
            return new FileStream(Full(relativePath), FileMode.OpenOrCreate, FileAccess.ReadWrite,
                FileShare.None);
        }

        // AP-3 (ADR-3): platform-split write pre-check.
        public bool CanWrite(string relativePath)
        {
            var full = Full(relativePath);
            if (OperatingSystem.IsWindows())
            {
                return CanWriteWindows(full);
            }
            return CanWriteByTrial(full);
        }

        // Windows: NTFS ACL pre-check (unchanged from the Framework build), including the
        // missing-file → parent-directory ACL branch. Guarded so the Windows-only API is never
        // reached on Linux.
        [SupportedOSPlatform("windows")]
        private static bool CanWriteWindows(string full)
        {
            try
            {
                AuthorizationRuleCollection rules;
                if (File.Exists(full))
                {
                    rules = new FileInfo(full).GetAccessControl()
                        .GetAccessRules(true, true, typeof(System.Security.Principal.NTAccount));
                }
                else
                {
                    var parent = Path.GetDirectoryName(full);
                    if (string.IsNullOrEmpty(parent))
                    {
                        return false;
                    }
                    rules = new DirectoryInfo(parent).GetAccessControl()
                        .GetAccessRules(true, true, typeof(System.Security.Principal.NTAccount));
                }
                foreach (FileSystemAccessRule rule in rules)
                {
                    if (rule.AccessControlType == AccessControlType.Deny
                        && (rule.FileSystemRights & FileSystemRights.Write) == FileSystemRights.Write)
                    {
                        return false;
                    }
                }
                return true;
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }
        }

        // Linux (and any non-Windows): decide by a real write attempt. The NTFS-security-style volume
        // is evaluated by the NFS server via name-mapping, which POSIX mode bits do not mirror.
        private static bool CanWriteByTrial(string full)
        {
            try
            {
                if (File.Exists(full))
                {
                    // Open the existing target for write without truncating.
                    using (new FileStream(full, FileMode.Open, FileAccess.Write, FileShare.ReadWrite))
                    {
                    }
                    return true;
                }

                var parent = Path.GetDirectoryName(full);
                if (string.IsNullOrEmpty(parent))
                {
                    return false;
                }
                if (!Directory.Exists(parent))
                {
                    // The parent itself does not exist; Write() would create it. Probe the nearest
                    // existing ancestor instead.
                    parent = NearestExistingAncestor(parent);
                    if (parent == null)
                    {
                        return false;
                    }
                }
                var probe = Path.Combine(parent, ".docintake-canwrite-" + Guid.NewGuid().ToString("N"));
                try
                {
                    using (new FileStream(probe, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    {
                    }
                    return true;
                }
                finally
                {
                    try { File.Delete(probe); } catch (IOException) { } catch (UnauthorizedAccessException) { }
                }
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }
            catch (IOException)
            {
                // EACCES / EROFS and similar surface here as IOException on .NET.
                return false;
            }
        }

        private static string NearestExistingAncestor(string path)
        {
            var dir = path;
            while (!string.IsNullOrEmpty(dir))
            {
                if (Directory.Exists(dir))
                {
                    return dir;
                }
                dir = Path.GetDirectoryName(dir);
            }
            return null;
        }
    }
}
