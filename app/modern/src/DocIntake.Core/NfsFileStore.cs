namespace DocIntake.Core
{
    // NFS-mounted store. On Linux the .NET file APIs used by SmbFileStore (List/Read/Write/
    // OpenExclusive/CanWrite) behave identically whether the root is an SMB (cifs) mount, an NFS
    // mount, or a local directory — the difference is the mount, not the code. So NfsFileStore
    // reuses SmbFileStore's filesystem semantics unchanged, including:
    //   - AP-1: Path.Combine (OS-native separator)
    //   - file-locking: FileShare.None on OpenExclusive
    //   - AP-3 (ADR-3): CanWrite is a real-I/O trial on Linux (NTFS-security-style volume evaluated
    //     by ONTAP via name-mapping; POSIX mode bits are not a faithful mirror)
    //
    // It exists as a distinct type so the Worker/Probe can select "nfs" explicitly and so the store
    // kind is named in diagnostics. The cross-protocol / cross-host behaviours (file-locking
    // contention, write-visibility lag, case-sensitivity on a real NFS mount) are measured
    // AIMF-externally; AIMF-internal verification uses a temp directory root.
    public sealed class NfsFileStore : SmbFileStore
    {
        public NfsFileStore(string root) : base(root)
        {
        }
    }
}
