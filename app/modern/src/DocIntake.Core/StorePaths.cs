using System.IO;

namespace DocIntake.Core
{
    // Path and name constants shared by the store implementations.
    //
    // Migration notes (DocIntake-modernized, Phase 3 Step 2):
    //
    //   AP-2 (case-sensitivity): ported from DocIntake edc67a0. The index is REFERENCED as
    //   "seed/Docs/Index.json" while the seeded real file is "seed/docs/index.json" (the positive
    //   control). On Windows + SMB (and Linux cifs) the two resolve to the same file; on a
    //   case-sensitive path (Linux/NFS, and S3 Access Points) they do not. The measurement only
    //   counts if the control resolves. Both are anchored under seed/ so the control is a real
    //   seeded file (edc67a0 fixed the pre-anchor spelling where IndexReal was "docs/index.json").
    //   The case difference is intentional and is what the Probe measures; it is NOT a bug to
    //   "normalize away" — normalising it would delete the measurement.
    //
    //   AP-1 (path-separator): Combine now uses System.IO.Path.Combine (fixed). The old build
    //   concatenated a literal backslash, which on Linux became part of the file name. The
    //   application must build paths with the OS-native separator; the Probe constructs its own
    //   explicit literal-separator path for the path-separator measurement (Step 4), so fixing the
    //   application path here does not remove the Probe's ability to measure the environment.
    public static class StorePaths
    {
        // AP-2: capital-D "Docs" and capital-I "Index" in the REFERENCE, under seed/ (forward slash;
        // SmbFileStore normalises separators per platform).
        public const string IndexReference = "seed/Docs/Index.json";

        // The real on-disk name (lower case) under seed/: the positive control. If it does not
        // resolve, nothing can be concluded about case sensitivity.
        public const string IndexReal = "seed/docs/index.json";

        public const string InboxDir = "seed/inbox";

        // AP-1: use the OS-native path separator instead of a hard-coded backslash.
        public static string Combine(string left, string right)
        {
            return Path.Combine(left, right);
        }
    }
}
