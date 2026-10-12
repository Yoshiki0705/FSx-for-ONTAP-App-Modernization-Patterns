using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace DocIntake.Worker
{
    // AP-7: index.json model for System.Text.Json. Property names are lower-case to match the
    // Framework build's output keys ("documents", "name", "size"). Per ADR-4 the serialised bytes
    // are compact (STJ default); keys, values and order are unchanged.
    public sealed class IndexDocument
    {
        [JsonPropertyName("documents")]
        public List<IndexEntry> Documents { get; set; }
    }

    public sealed class IndexEntry
    {
        [JsonPropertyName("name")]
        public string Name { get; set; }

        [JsonPropertyName("size")]
        public int Size { get; set; }
    }

    // Source-generated context: avoids reflection-based serialisation (no trim/AOT warnings) and is
    // the modern STJ idiom.
    [JsonSerializable(typeof(IndexDocument))]
    internal sealed partial class IndexJsonContext : JsonSerializerContext
    {
    }
}
