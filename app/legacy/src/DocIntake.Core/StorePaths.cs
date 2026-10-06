namespace DocIntake.Core
{
    // Path and name constants shared by the store implementations.
    //
    // Two of the five planted behaviors live here, each exactly once:
    //
    //   case-sensitivity: the index is REFERENCED as "Docs\\Index.json" while the Worker writes the
    //   real file as "docs\\index.json". On Windows + SMB the two resolve to the same file; on a
    //   case-sensitive path (Linux/NFS) they do not. Planted once, here.
    //
    //   path-separator: Combine joins with a literal backslash by string concatenation instead of
    //   Path.Combine, so on Linux the backslash becomes part of the file name rather than a
    //   separator. Planted once, here.
    public static class StorePaths
    {
        // case-sensitivity pitfall: capital-D "Docs" and capital-I "Index" in the REFERENCE.
        public const string IndexReference = "Docs\\Index.json";

        // The real on-disk name the Worker writes (lower case).
        public const string IndexReal = "docs/index.json";

        public const string InboxDir = "seed/inbox";

        // path-separator pitfall: a backslash is concatenated as the separator. On Windows this is a
        // path separator; on Linux it is an ordinary character in the file name.
        public static string Combine(string left, string right)
        {
            return left + "\\" + right;
        }
    }
}
