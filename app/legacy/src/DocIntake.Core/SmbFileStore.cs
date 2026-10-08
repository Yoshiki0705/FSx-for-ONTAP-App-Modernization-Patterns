using System;
using System.Collections.Generic;
using System.IO;
using System.Security.AccessControl;

namespace DocIntake.Core
{
    // SMB-backed store for stage 0. The root is the UNC path to the share, e.g.
    // \\APPMODSVM01\appdata. Two of the five planted behaviors live here, each exactly once:
    //
    //   file-locking: OpenExclusive opens with FileShare.None, so a second client's open is refused
    //   while the first holds it. On SMB this is enforced; across SMB and NFS it is unverified.
    //
    //   acl-evaluation: CanWrite calls File.GetAccessControl and inspects the NTFS ACL BEFORE
    //   attempting the write. On modern .NET on Linux the GetAccessControl family is unavailable, so
    //   the pre-check and the actual NFS result can disagree.
    public class SmbFileStore : IFileStore
    {
        private readonly string _root;

        public SmbFileStore(string root)
        {
            _root = root;
        }

        private string Full(string relativePath)
        {
            // Deliberately uses StorePaths.Combine (the path-separator pitfall) rather than
            // Path.Combine, so the backslash behavior is exercised on every path this store builds.
            return StorePaths.Combine(_root, relativePath.Replace("/", "\\"));
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

        // file-locking pitfall: FileShare.None. A concurrent open by another client is refused on
        // SMB; the cross-protocol behavior is what the Probe measures.
        public Stream OpenExclusive(string relativePath)
        {
            return new FileStream(Full(relativePath), FileMode.OpenOrCreate, FileAccess.ReadWrite,
                FileShare.None);
        }

        // acl-evaluation pitfall: evaluate the NTFS ACL with GetAccessControl before the I/O. This
        // Windows-only API has no equivalent on Linux, so after migration the pre-check and the
        // real NFS result can disagree. The pitfall (the GetAccessControl family) stays here, in
        // this one method, exactly once (R5.3).
        //
        // When the target file does not exist yet, the writability of the location is governed by
        // the parent directory's ACL, so inspect Directory.GetAccessControl(parent) rather than
        // File.GetAccessControl(missing-file). Returning false for a missing file would be a false
        // pre-check: the actual write would then succeed on Windows+SMB, manufacturing a pre-check
        // vs I/O mismatch that R5.3 forbids on Windows+SMB. Existing files are evaluated with
        // File.GetAccessControl as before.
        public bool CanWrite(string relativePath)
        {
            try
            {
                var full = Full(relativePath);
                AuthorizationRuleCollection rules;
                if (File.Exists(full))
                {
                    rules = File.GetAccessControl(full)
                        .GetAccessRules(true, true, typeof(System.Security.Principal.NTAccount));
                }
                else
                {
                    var parent = Path.GetDirectoryName(full);
                    if (string.IsNullOrEmpty(parent))
                    {
                        return false;
                    }
                    rules = Directory.GetAccessControl(parent)
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
    }
}
