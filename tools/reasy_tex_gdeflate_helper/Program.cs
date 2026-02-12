using System.Buffers.Binary;
using GDeflateNet;

namespace ReasyTexGDeflateHelper;

internal static class Program
{
    private const uint TexMagic = 0x00584554;
    private const int MhWildsVersion = 241106027;
    private const int MipHeaderStride = 16;
    private const int PackedHeaderStride = 8;
    private const int FirstMipHeaderOffset = 40;
    private const ushort GDeflateMagic = 0xFB04;

    private static int Main(string[] args)
    {
        try
        {
            var cli = Cli.Parse(args);
            var input = cli.ReadInput();
            var output = string.Equals(cli.Mode, "decompress-tex", StringComparison.OrdinalIgnoreCase)
                ? DecompressTex(input)
                : throw new ArgumentException("Unsupported mode. Use --mode decompress-tex");
            cli.WriteOutput(output);
            return 0;
        }
        catch (OperationCanceledException)
        {
            return 130;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.Message);
            return 1;
        }
    }

    private static byte[] DecompressTex(byte[] data)
    {
        if (data.Length < FirstMipHeaderOffset || ReadU32(data, 0) != TexMagic)
            throw new InvalidDataException("Input is not a valid TEX file");

        if (ReadI32(data, 4) < MhWildsVersion)
            return data;

        var imageCount = data[14];
        var mipHeaderSize = data[15];
        var mipCount = mipHeaderSize / MipHeaderStride;
        var totalMipEntries = mipCount * imageCount;
        if (totalMipEntries <= 0)
            return data;

        var mips = ReadMips(data, totalMipEntries);
        var firstMipOffset = checked((int)mips[0].Offset);
        var maxEndOffset = checked((int)mips.Max(m => checked(m.Offset + m.Size)));
        var packed = ReadPackedHeaders(data, totalMipEntries, firstMipOffset);

        var compressedPayloadStart = firstMipOffset + totalMipEntries * PackedHeaderStride;
        var outputLength = Math.Max(data.Length, maxEndOffset);
        var output = new byte[outputLength];
        Buffer.BlockCopy(data, 0, output, 0, data.Length);

        for (var i = 0; i < totalMipEntries; i++)
        {
            var mip = mips[i];
            var packedHeader = packed[i];

            var srcOffset = compressedPayloadStart + packedHeader.Offset;
            var src = SliceChecked(data, srcOffset, packedHeader.Size);
            var dstOffset = checked((int)mip.Offset);

            if (src.Length >= 2 && BinaryPrimitives.ReadUInt16LittleEndian(src) == GDeflateMagic)
            {
                var decoded = DecodeGDeflate(src.ToArray(), GetExpectedDecodeSize(src, mip.Size));
                output.AsSpan(dstOffset, mip.Size).Clear();
                Buffer.BlockCopy(decoded, 0, output, dstOffset, Math.Min(decoded.Length, mip.Size));
            }
            else
            {
                if (src.Length < mip.Size)
                    throw new InvalidDataException("TEX payload is truncated or malformed");
                src[..mip.Size].CopyTo(output.AsSpan(dstOffset, mip.Size));
            }
        }

        // re-entering packed-mip expansion. Packed mode is is keyed only by
        // version in the current parser (>= MHWILDS_TEX_VERSION).
        BinaryPrimitives.WriteInt32LittleEndian(output.AsSpan(4, 4), MhWildsVersion - 1);
        return output;
    }

    private static MipEntry[] ReadMips(byte[] data, int count)
    {
        var mips = new MipEntry[count];
        for (var i = 0; i < count; i++)
        {
            var off = FirstMipHeaderOffset + i * MipHeaderStride;
            mips[i] = new MipEntry(
                Offset: ReadI64(data, off),
                Size: ReadI32(data, off + 12));
        }
        return mips;
    }

    private static PackedEntry[] ReadPackedHeaders(byte[] data, int count, int startOffset)
    {
        var headers = new PackedEntry[count];
        for (var i = 0; i < count; i++)
        {
            var off = startOffset + i * PackedHeaderStride;
            headers[i] = new PackedEntry(
                Size: ReadI32(data, off),
                Offset: ReadI32(data, off + 4));
        }
        return headers;
    }


    private static int GetExpectedDecodeSize(ReadOnlySpan<byte> compressed, int fallbackSize)
    {
        if (compressed.Length < 8)
            return fallbackSize;

        var id = compressed[0];
        var magic = compressed[1];
        if (((byte)(0xFF ^ id) != magic) || id != 4)
            return fallbackSize;

        var numTiles = BinaryPrimitives.ReadUInt16LittleEndian(compressed.Slice(2, 2));
        var flags = BinaryPrimitives.ReadUInt32LittleEndian(compressed.Slice(4, 4));
        var lastTileSize = (int)((flags >> 2) & 0x3FFFFU);

        if (numTiles == 0)
            return fallbackSize;

        const int tileSize = 0x10000;
        var size = checked(numTiles * tileSize - (lastTileSize == 0 ? 0 : tileSize - lastTileSize));
        return size > 0 ? size : fallbackSize;
    }

    private static byte[] DecodeGDeflate(byte[] compressed, int expectedSize)
    {
        try
        {
            var decoded = new byte[expectedSize];
            if (!GDeflate.Decompress(compressed, decoded))
                throw new InvalidDataException("GDeflateNet decompression returned false");
            return decoded;
        }
        catch (Exception ex)
        {
            throw new InvalidDataException("Failed to decode gdeflate mip with GDeflateNet wrapper", ex);
        }
    }

    private static ReadOnlySpan<byte> SliceChecked(byte[] data, int offset, int size)
    {
        if (offset < 0 || size < 0 || offset + size > data.Length)
            throw new InvalidDataException("TEX payload is truncated or malformed");
        return data.AsSpan(offset, size);
    }


    private static uint ReadU32(byte[] data, int offset) => BinaryPrimitives.ReadUInt32LittleEndian(SliceChecked(data, offset, 4));
    private static int ReadI32(byte[] data, int offset) => BinaryPrimitives.ReadInt32LittleEndian(SliceChecked(data, offset, 4));
    private static long ReadI64(byte[] data, int offset) => BinaryPrimitives.ReadInt64LittleEndian(SliceChecked(data, offset, 8));

    private readonly record struct MipEntry(long Offset, int Size);
    private readonly record struct PackedEntry(int Size, int Offset);

    private sealed record Cli(string Mode, bool UseStdin, bool UseStdout, string? InputPath, string? OutputPath)
    {
        public static Cli Parse(IReadOnlyList<string> args)
        {
            string mode = "";
            var useStdin = false;
            var useStdout = false;
            string? inputPath = null;
            string? outputPath = null;

            for (var i = 0; i < args.Count; i++)
            {
                switch (args[i])
                {
                    case "--mode":
                        mode = Next(args, ref i, "--mode");
                        break;
                    case "--stdin":
                        useStdin = true;
                        break;
                    case "--stdout":
                        useStdout = true;
                        break;
                    case "--in":
                        inputPath = Next(args, ref i, "--in");
                        break;
                    case "--out":
                        outputPath = Next(args, ref i, "--out");
                        break;
                    default:
                        throw new ArgumentException($"Unknown argument: {args[i]}");
                }
            }

            if (string.IsNullOrWhiteSpace(mode))
                throw new ArgumentException("Missing --mode");
            if (useStdin && !string.IsNullOrWhiteSpace(inputPath))
                throw new ArgumentException("Use either --stdin or --in, not both");
            if (useStdout && !string.IsNullOrWhiteSpace(outputPath))
                throw new ArgumentException("Use either --stdout or --out, not both");

            return new Cli(mode, useStdin, useStdout, inputPath, outputPath);
        }

        public byte[] ReadInput()
        {
            if (UseStdin)
            {
                using var ms = new MemoryStream();
                Console.OpenStandardInput().CopyTo(ms);
                return ms.ToArray();
            }

            if (string.IsNullOrWhiteSpace(InputPath))
                throw new ArgumentException("Input source missing,use --stdin or --in <path>");

            return File.ReadAllBytes(InputPath);
        }

        public void WriteOutput(byte[] output)
        {
            if (UseStdout)
            {
                Console.OpenStandardOutput().Write(output);
                return;
            }

            if (string.IsNullOrWhiteSpace(OutputPath))
                throw new ArgumentException("Output target missing,use --stdout or --out <path>");

            File.WriteAllBytes(OutputPath, output);
        }

        private static string Next(IReadOnlyList<string> args, ref int i, string option)
        {
            if (i + 1 >= args.Count)
                throw new ArgumentException($"Missing value for {option}");
            i++;
            return args[i];
        }
    }
}
