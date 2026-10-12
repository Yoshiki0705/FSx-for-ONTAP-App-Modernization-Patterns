namespace DocIntake.Core
{
    // Path and name constants shared by the store implementations.
    //
    // Two of the five planted behaviors live here, each exactly once:
    //
    //   case-sensitivity: the index is REFERENCED as "seed\\Docs\\Index.json" while the seeded real
    //   file is "seed\\docs\\index.json" (the positive control). On Windows + SMB the two resolve to
    //   the same file; on a case-sensitive path (Linux/NFS) they do not. The measurement only
    //   counts if the control resolves. Planted once, here.
    //
    //   path-separator: Combine joins with a literal backslash by string concatenation instead of
    //   Path.Combine, so on Linux the backslash becomes part of the file name rather than a
    //   separator. Planted once, here.
    public static class StorePaths
    {
        // case-sensitivity pitfall: capital-D "Docs" and capital-I "Index" in the REFERENCE, under seed\.
        public const string IndexReference = "seed\\Docs\\Index.json";

        // The real on-disk name (lower case) under seed\: the positive control. If it does not
        // resolve, nothing can be concluded about case sensitivity.
        public const string IndexReal = "seed\\docs\\index.json";

        public const string InboxDir = "seed/inbox";

        // path-separator pitfall: a backslash is concatenated as the separator. On Windows this is a
        // path separator; on Linux it is an ordinary character in the file name.
        public static string Combine(string left, string right)
        {
            return left + "\\" + right;
        }
    }
}
