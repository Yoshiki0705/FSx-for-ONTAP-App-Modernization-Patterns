using System.Collections.Generic;
using System.IO;

namespace DocIntake.Core
{
    // The storage abstraction the Worker and Probe use. SmbFileStore implements it for stage 0.
    // The AIMF work plan adds NfsFileStore in stage 2 and S3FileStore as the final step
    // (confluence point #4). The shape mirrors Bob's Used Books Classic FileService local/S3 switch
    // so the AIMF observations MP-10 (a defect that shows only in one store mode) and MP-25 (a
    // config-key mismatch) apply.
    public interface IFileStore
    {
        // Enumerate the relative paths under a sub-path of the store root.
        IEnumerable<string> List(string relativeDir);

        // Read a file's bytes by relative path.
        byte[] Read(string relativePath);

        // Write bytes to a relative path, creating parent directories.
        void Write(string relativePath, byte[] content);

        // Open a file for exclusive editing. Used by the file-locking behavior.
        Stream OpenExclusive(string relativePath);

        // Report whether the current identity may write to a path, by evaluating ACLs BEFORE the
        // I/O. Used by the acl-evaluation behavior.
        bool CanWrite(string relativePath);
    }
}
