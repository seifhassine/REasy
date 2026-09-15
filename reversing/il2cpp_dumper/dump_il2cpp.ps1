<#
.SYNOPSIS
    Standalone RE Engine IL2CPP/TDB dumper.

.DESCRIPTION
    Produces a standalone il2cpp_dump JSON from a running RE Engine title.
    It uses PROCESS_VM_READ only and includes runtime-patched method addresses,
    native type names, RSZ data, deserializer chains, and reflection metadata.

.EXAMPLE
    # Start the game normally, wait until the title screen, then run:
    pwsh -File .\dump_il2cpp.ps1

.EXAMPLE
    # Select an executable explicitly when the folder contains several:
    pwsh -File .\dump_il2cpp.ps1 -GamePath .\re4.exe

.EXAMPLE
    # Small validation dump containing TDB type IDs 0 through 99:
    pwsh -File .\dump_il2cpp.ps1 -MaxTypes 100 -OutputPath .\il2cpp_test.json

.NOTES
    Supports RE Engine TDB versions 66, 67, and 69 through 84. Legacy TDB 49
    builds are not supported. Update-sensitive runtime addresses and layouts
    are derived from the executable and running process.
#>

[CmdletBinding()]
param(
    [string] $GamePath = '',

    [string] $OutputPath = (Join-Path $PSScriptRoot 'il2cpp_dump_standalone.json'),

    [int] $ProcessId = 0,

    [ValidateRange(0, 3600)]
    [int] $WaitSeconds = 180,

    [ValidateRange(0, 1000000)]
    [int] $MaxTypes = 0,

    [int[]] $TypeId = @(),

    [switch] $CoreOnly,

    [switch] $Compact,

    [switch] $Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw 'PowerShell 7 or newer is required. Run this script with pwsh.exe.'
}

$OutputPath = [IO.Path]::GetFullPath($OutputPath)

if (-not [string]::IsNullOrWhiteSpace($GamePath)) {
    $GamePath = [IO.Path]::GetFullPath($GamePath)
    if (-not [IO.File]::Exists($GamePath)) {
        throw "Game executable not found: $GamePath"
    }
}

if ([IO.File]::Exists($OutputPath) -and -not $Force) {
    throw "Output already exists: $OutputPath`nUse -Force to replace it, or choose another -OutputPath."
}

$outputDirectory = [IO.Path]::GetDirectoryName($OutputPath)
if (-not [IO.Directory]::Exists($outputDirectory)) {
    [IO.Directory]::CreateDirectory($outputDirectory) | Out-Null
}

if (-not ('ReEngineStandaloneDump.Entry' -as [type])) {
    $csharp = @'
#nullable enable
using System;
using System.Buffers.Binary;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Threading;

namespace ReEngineStandaloneDump
{
    public sealed class Options
    {
        public string GamePath { get; set; } = "";
        public string SearchDirectory { get; set; } = "";
        public string OutputPath { get; set; } = "";
        public int ProcessId { get; set; }
        public int WaitSeconds { get; set; } = 180;
        public ulong[] TypeListCandidateRvas { get; set; } = Array.Empty<ulong>();
        public int MaxTypes { get; set; }
        public int[] TypeIds { get; set; } = Array.Empty<int>();
        public bool CoreOnly { get; set; }
        public bool Compact { get; set; }
    }

    internal sealed class TdbLayout
    {
        public readonly uint Version;
        public readonly int NumTypes, NumMethods, NumFields, NumTypeImpl, NumFieldImpl, NumMethodImpl;
        public readonly int NumPropertyImpl, NumProperties, NumParams, NumInitData, NumModules;
        public readonly int StringPoolSize, BytePoolSize;
        public readonly int Modules, Types, TypeImpls, Methods, MethodImpls, Fields, FieldImpls;
        public readonly int Properties, PropertyImpls, Params, InitData, StringPool, BytePool;
        public readonly int ModuleStride, TypeStride, MethodStride, FieldStride, PropertyStride, MethodFunctionOffset, TypeBits;

        public bool EncodedMethods => Version >= 71;
        public bool SplitMetadata => Version >= 69;
        public bool PackedTypeImplNames => Version >= 83;
        public uint TypeMask => (1U << TypeBits) - 1U;

        private TdbLayout(uint version)
        {
            Version = version;
            ModuleStride = version >= 81 ? 0x40 : 0x58;
            TypeStride = version < 69 ? 0x78 : version >= 74 ? 0x50 : version >= 71 ? 0x48 : 0x50;
            MethodStride = version < 69 ? 0x20 : version >= 71 ? 0x0C : 0x10;
            FieldStride = version == 66 ? 0x14 : version < 69 ? 0x18 : 0x08;
            PropertyStride = version < 69 ? 0x10 : 0x08;
            MethodFunctionOffset = version < 69 ? 0x18 : 0x08;
            TypeBits = version >= 71 ? 19 : version >= 69 ? 18 : version == 67 ? 17 : 16;

            if (version < 69)
            {
                NumTypes = 0x0C; NumMethods = 0x10; NumFields = 0x14;
                NumTypeImpl = NumFieldImpl = NumMethodImpl = NumPropertyImpl = NumParams = -1;
                NumProperties = 0x18; NumInitData = 0x2C; NumModules = 0x34;
                StringPoolSize = 0x40; BytePoolSize = 0x44;
                Modules = 0x48; Types = 0x50; Methods = 0x58; Fields = 0x60;
                Properties = 0x68; InitData = 0x88; StringPool = 0x98; BytePool = 0xA0;
                TypeImpls = MethodImpls = FieldImpls = PropertyImpls = Params = -1;
            }
            else if (version < 74)
            {
                NumTypes = version == 73 ? 0x08 : 0x0C;
                NumMethods = 0x10; NumFields = 0x14;
                NumTypeImpl = 0x18; NumFieldImpl = 0x1C; NumMethodImpl = 0x20;
                NumPropertyImpl = 0x24; NumProperties = 0x28; NumParams = 0x30;
                NumInitData = 0x38; NumModules = 0x44;
                StringPoolSize = 0x50; BytePoolSize = 0x54;
                Modules = 0x58; Types = 0x60; TypeImpls = 0x68;
                Methods = 0x70; MethodImpls = 0x78; Fields = 0x80; FieldImpls = 0x88;
                Properties = 0x90; PropertyImpls = 0x98; Params = 0xA8; InitData = 0xB8;
                StringPool = version == 69 ? 0xC8 : 0xD0;
                BytePool = version == 69 ? 0xD0 : 0xD8;
            }
            else
            {
                NumTypes = 0x08; NumMethods = 0x14; NumFields = 0x18;
                NumTypeImpl = 0x1C; NumFieldImpl = 0x20; NumMethodImpl = 0x24;
                NumPropertyImpl = 0x28; NumProperties = 0x2C; NumParams = 0x34;
                NumInitData = 0x3C;
                NumModules = version >= 82 ? 0x4C : 0x48;
                StringPoolSize = version >= 82 ? 0x58 : 0x54;
                BytePoolSize = version >= 82 ? 0x5C : 0x58;
                Modules = 0x60; Types = 0x68; TypeImpls = 0x70;
                Methods = 0x78; MethodImpls = 0x80; Fields = 0x88; FieldImpls = 0x90;
                Properties = 0x98; PropertyImpls = 0xA0; Params = 0xB0; InitData = 0xC0;
                StringPool = 0xD8; BytePool = 0xE0;
            }
        }

        public static bool Supports(uint version) => version == 66 || version == 67 || (version >= 69 && version <= 84);

        public static TdbLayout For(uint version)
        {
            if (!Supports(version))
                throw new NotSupportedException($"TDB {version} is unsupported; supported versions are 66, 67, and 69 through 84.");
            return new TdbLayout(version);
        }
    }

    internal sealed class PeImage : IDisposable
    {
        internal sealed class Section
        {
            public string Name = "";
            public uint VirtualAddress;
            public uint VirtualSize;
            public uint RawAddress;
            public uint RawSize;
            public uint Characteristics;
        }

        private readonly FileStream _stream;
        private readonly List<Section> _sections = new List<Section>();
        public ulong PreferredBase { get; private set; }
        public uint ImageSize { get; private set; }
        public uint TimeDateStamp { get; private set; }

        public PeImage(string path)
        {
            _stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite, 1 << 20, FileOptions.RandomAccess);
            var dos = ReadFile(0, 0x40);
            if (U16(dos, 0) != 0x5A4D) throw new InvalidDataException("Not a PE file (missing MZ header).");
            int pe = checked((int)U32(dos, 0x3C));
            var coff = ReadFile(pe, 0x18);
            if (U32(coff, 0) != 0x00004550) throw new InvalidDataException("Not a PE file (missing PE header).");
            int sectionCount = U16(coff, 6);
            TimeDateStamp = U32(coff, 8);
            int optionalSize = U16(coff, 20);
            var optional = ReadFile(pe + 0x18, optionalSize);
            if (U16(optional, 0) != 0x20B) throw new InvalidDataException("The game executable is not PE32+ (64-bit).");
            PreferredBase = U64(optional, 0x18);
            ImageSize = U32(optional, 0x38);
            long sectionOffset = pe + 0x18L + optionalSize;
            for (int i = 0; i < sectionCount; i++)
            {
                var s = ReadFile(sectionOffset + i * 40L, 40);
                int end = Array.IndexOf(s, (byte)0, 0, 8);
                if (end < 0) end = 8;
                _sections.Add(new Section
                {
                    Name = Encoding.ASCII.GetString(s, 0, end),
                    VirtualSize = U32(s, 8),
                    VirtualAddress = U32(s, 12),
                    RawSize = U32(s, 16),
                    RawAddress = U32(s, 20),
                    Characteristics = U32(s, 36)
                });
            }
        }

        private byte[] ReadFile(long offset, int count)
        {
            if (offset < 0 || count < 0 || offset + count > _stream.Length) throw new EndOfStreamException();
            var result = new byte[count];
            _stream.Position = offset;
            int done = 0;
            while (done < count)
            {
                int n = _stream.Read(result, done, count - done);
                if (n <= 0) throw new EndOfStreamException();
                done += n;
            }
            return result;
        }

        private long VaToFile(ulong address, int count)
        {
            if (address < PreferredBase) throw new InvalidDataException($"VA 0x{address:x} is below the preferred image base.");
            ulong rva64 = address - PreferredBase;
            if (rva64 > uint.MaxValue) throw new InvalidDataException($"VA 0x{address:x} is outside the image.");
            uint rva = (uint)rva64;
            foreach (var s in _sections)
            {
                uint span = Math.Max(s.VirtualSize, s.RawSize);
                if (rva >= s.VirtualAddress && (ulong)rva + (uint)count <= (ulong)s.VirtualAddress + span)
                {
                    ulong delta = rva - s.VirtualAddress;
                    if (delta + (uint)count > s.RawSize) throw new EndOfStreamException($"VA 0x{address:x} is in a virtual-only part of section {s.Name}.");
                    return s.RawAddress + (long)delta;
                }
            }
            if ((ulong)rva + (uint)count <= 0x1000) return rva;
            throw new InvalidDataException($"VA 0x{address:x} does not map to a PE section.");
        }

        private uint FileToRva(long fileOffset)
        {
            foreach (var s in _sections)
            {
                if (fileOffset >= s.RawAddress && fileOffset < (long)s.RawAddress + s.RawSize)
                    return checked(s.VirtualAddress + (uint)(fileOffset - s.RawAddress));
            }
            if (fileOffset >= 0 && fileOffset < 0x1000) return (uint)fileOffset;
            throw new InvalidDataException($"File offset 0x{fileOffset:x} does not map to a PE section.");
        }

        public byte[] Read(ulong address, int count) => ReadFile(VaToFile(address, count), count);

        public ulong FindTdbRva()
        {
            var preferred = _sections.OrderBy(s => s.Name == ".link" ? 0 : 1).ThenBy(s => s.RawAddress).ToList();
            const int chunkSize = 4 * 1024 * 1024;
            foreach (var section in preferred)
            {
                if (section.RawSize < 0xF0) continue;
                long sectionEnd = (long)section.RawAddress + section.RawSize;
                long pos = section.RawAddress;
                byte[] overlap = Array.Empty<byte>();
                while (pos < sectionEnd)
                {
                    int take = (int)Math.Min(chunkSize, sectionEnd - pos);
                    var current = ReadFile(pos, take);
                    var scan = new byte[overlap.Length + current.Length];
                    Buffer.BlockCopy(overlap, 0, scan, 0, overlap.Length);
                    Buffer.BlockCopy(current, 0, scan, overlap.Length, current.Length);
                    long scanBase = pos - overlap.Length;
                    for (int i = 0; i + 0xF0 <= scan.Length; i++)
                    {
                        if (scan[i] != (byte)'T' || scan[i + 1] != (byte)'D' || scan[i + 2] != (byte)'B' || scan[i + 3] != 0) continue;
                        uint version = U32(scan, i + 4);
                        if (!TdbLayout.Supports(version)) continue;
                        var layout = TdbLayout.For(version);
                        uint types = U32(scan, i + layout.NumTypes);
                        uint methods = U32(scan, i + layout.NumMethods);
                        uint fields = U32(scan, i + layout.NumFields);
                        ulong typePtr = U64(scan, i + layout.Types);
                        if (types < 10000 || types > 1000000 || methods < types || methods > 10000000 || fields > 10000000) continue;
                        if (typePtr == 0) continue;
                        return FileToRva(scanBase + i);
                    }
                    int keep = Math.Min(0xEF, scan.Length);
                    overlap = new byte[keep];
                    Buffer.BlockCopy(scan, scan.Length - keep, overlap, 0, keep);
                    pos += take;
                }
            }
            throw new InvalidDataException("Could not locate a supported RE Engine TDB header in the executable (versions 66, 67, and 69 through 84).");
        }

        private IEnumerable<long> FindPattern(byte[] pattern) => FindPattern(pattern.Select(value => (byte?)value).ToArray());

        private IEnumerable<long> FindPattern(byte?[] pattern)
        {
            const int Chunk = 4 * 1024 * 1024;
            var found = new HashSet<long>();
            foreach (var section in _sections)
            {
                if ((section.Characteristics & 0x20000000U) == 0) continue;
                long end = (long)section.RawAddress + section.RawSize;
                byte[] overlap = Array.Empty<byte>();
                for (long pos = section.RawAddress; pos < end; pos += Chunk)
                {
                    var current = ReadFile(pos, (int)Math.Min(Chunk, end - pos));
                    var bytes = new byte[overlap.Length + current.Length];
                    Buffer.BlockCopy(overlap, 0, bytes, 0, overlap.Length);
                    Buffer.BlockCopy(current, 0, bytes, overlap.Length, current.Length);
                    long origin = pos - overlap.Length;
                    for (int i = 0; i + pattern.Length <= bytes.Length; i++)
                    {
                        int n = 0;
                        while (n < pattern.Length && (!pattern[n].HasValue || bytes[i + n] == pattern[n]!.Value)) n++;
                        if (n == pattern.Length) found.Add(origin + i);
                    }
                    int keep = Math.Min(pattern.Length - 1, bytes.Length);
                    overlap = bytes.AsSpan(bytes.Length - keep, keep).ToArray();
                }
            }
            return found;
        }

        public ulong[] FindTypeListRvas(uint tdbVersion)
        {
            var result = new HashSet<ulong>();

            void AddRipTarget(long instruction, int displacementOffset, int instructionLength)
            {
                var bytes = ReadFile(instruction + displacementOffset, 4);
                long target = FileToRva(instruction) + instructionLength + I32(bytes, 0);
                if (target >= 0x1000 && target + 0x10 <= ImageSize && (target & 7) == 0)
                    result.Add((ulong)target);
            }

            if (tdbVersion < 73)
            {
                var directReference = new byte?[] {
                    0x48,0x8D,0x0D,null,null,null,null, 0xE8,null,null,null,null,
                    0x48,0x8D,0x05,null,null,null,null, 0x48,0x89,0x03
                };
                foreach (long reference in FindPattern(directReference)) AddRipTarget(reference, 3, 7);

                foreach (long typeInfoNone in FindPattern(new byte[] { 0xBA, 0xAE, 0xE7, 0xF7, 0x08 }))
                {
                    var section = _sections.FirstOrDefault(s => typeInfoNone >= s.RawAddress && typeInfoNone < (long)s.RawAddress + s.RawSize);
                    if (section == null) continue;
                    int searchSize = (int)Math.Min(0x100, (long)section.RawAddress + section.RawSize - typeInfoNone);
                    var registration = ReadFile(typeInfoNone, searchSize);
                    for (int i = 0; i + 8 <= registration.Length; i++)
                    {
                        if (registration[i] != 0x48 || registration[i + 1] != 0x8B || registration[i + 2] != 0xCB || registration[i + 3] != 0xE8) continue;
                        long callRva = FileToRva(typeInfoNone + i) + 8L + I32(registration, i + 4);
                        if (callRva < 0x1000 || callRva >= ImageSize) continue;
                        long functionFile;
                        try { functionFile = VaToFile(PreferredBase + (ulong)callRva, 1); }
                        catch { continue; }
                        var functionSection = _sections.FirstOrDefault(s => functionFile >= s.RawAddress && functionFile < (long)s.RawAddress + s.RawSize);
                        if (functionSection == null) continue;
                        int functionSize = (int)Math.Min(0x200, (long)functionSection.RawAddress + functionSection.RawSize - functionFile);
                        var function = ReadFile(functionFile, functionSize);
                        for (int n = 0; n + 7 <= function.Length; n++)
                        {
                            if (function[n] == 0x4C && function[n + 1] == 0x8B && function[n + 2] == 0x05)
                                AddRipTarget(functionFile + n, 3, 7);
                        }
                    }
                }
            }
            {
                var anchors = FindPattern(new byte[] { 0xBA, 0x55, 0xFD, 0x09, 0xD2 })
                    .Concat(FindPattern(new byte[] { 0xC7, 0xC2, 0x55, 0xFD, 0x09, 0xD2 }));
                foreach (long anchor in anchors)
                {
                    var section = _sections.FirstOrDefault(s => anchor >= s.RawAddress && anchor < (long)s.RawAddress + s.RawSize);
                    if (section == null) continue;
                    long start = Math.Max(section.RawAddress, anchor - 0x80);
                    int count = (int)Math.Min(0x580, (long)section.RawAddress + section.RawSize - start);
                    var code = ReadFile(start, count);
                    for (int i = 0; i + 7 <= code.Length; i++)
                    {
                        bool rex = (code[i] & 0xF0) == 0x40;
                        if (!rex || (code[i + 1] != 0x8D && code[i + 1] != 0x8B) || (code[i + 2] & 0xC7) != 0x05) continue;
                        long instructionRva = FileToRva(start + i);
                        long target = instructionRva + 7L + I32(code, i + 3);
                        if (target >= 0x1000 && target + 0x10 <= ImageSize && (target & 7) == 0)
                            result.Add((ulong)target);
                    }
                }
            }
            return result.OrderBy(x => x).ToArray();
        }

        public void Dispose() => _stream.Dispose();

        internal static ushort U16(byte[] b, int o) => BinaryPrimitives.ReadUInt16LittleEndian(b.AsSpan(o, 2));
        internal static short I16(byte[] b, int o) => BinaryPrimitives.ReadInt16LittleEndian(b.AsSpan(o, 2));
        internal static uint U32(byte[] b, int o) => BinaryPrimitives.ReadUInt32LittleEndian(b.AsSpan(o, 4));
        internal static int I32(byte[] b, int o) => BinaryPrimitives.ReadInt32LittleEndian(b.AsSpan(o, 4));
        internal static ulong U64(byte[] b, int o) => BinaryPrimitives.ReadUInt64LittleEndian(b.AsSpan(o, 8));
    }

    internal sealed class ProcessMemory : IDisposable
    {
        private const uint PROCESS_VM_READ = 0x0010;
        private const uint PROCESS_QUERY_INFORMATION = 0x0400;
        private const uint MEM_COMMIT = 0x1000;
        private const uint READABLE_PROTECTION = 0xEE;
        private const uint PAGE_GUARD = 0x100;
        private const int PageSize = 0x1000;
        private const int MaxCachedPages = 8192;

        [StructLayout(LayoutKind.Sequential)]
        private struct MemoryBasicInformation
        {
            public IntPtr BaseAddress;
            public IntPtr AllocationBase;
            public uint AllocationProtect;
            public UIntPtr RegionSize;
            public uint State;
            public uint Protect;
            public uint Type;
        }

        internal readonly struct Range
        {
            public readonly ulong Start;
            public readonly ulong Size;
            public ulong End => Start + Size;
            public Range(ulong start, ulong size) { Start = start; Size = size; }
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint access, bool inheritHandle, uint processId);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool ReadProcessMemory(IntPtr process, IntPtr address, [Out] byte[] buffer, UIntPtr size, out UIntPtr read);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern UIntPtr VirtualQueryEx(IntPtr process, IntPtr address, out MemoryBasicInformation buffer, UIntPtr length);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);

        private readonly IntPtr _handle;
        private readonly Dictionary<ulong, byte[]> _pages = new Dictionary<ulong, byte[]>();
        private readonly Queue<ulong> _pageOrder = new Queue<ulong>();
        public ulong ModuleBase { get; }
        public ulong PreferredBase { get; }
        public uint ImageSize { get; }

        public ProcessMemory(Process process, ulong preferredBase, uint imageSize)
        {
            _handle = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, false, (uint)process.Id);
            if (_handle == IntPtr.Zero)
                throw new InvalidOperationException($"OpenProcess failed for PID {process.Id} (Win32 error {Marshal.GetLastWin32Error()}). Try running pwsh as administrator.");
            if (process.MainModule == null) throw new InvalidOperationException("Could not query the game module.");
            ModuleBase = unchecked((ulong)process.MainModule.BaseAddress.ToInt64());
            PreferredBase = preferredBase;
            ImageSize = imageSize;
        }

        private byte[] ReadDirect(ulong address, int count)
        {
            var result = new byte[count];
            if (!ReadProcessMemory(_handle, unchecked((IntPtr)(long)address), result, (UIntPtr)(uint)count, out var got) || got.ToUInt64() != (ulong)count)
                throw new InvalidOperationException($"ReadProcessMemory failed at 0x{address:x} for 0x{count:x} bytes (Win32 error {Marshal.GetLastWin32Error()}).");
            return result;
        }

        private byte[] GetPage(ulong page)
        {
            if (_pages.TryGetValue(page, out var cached)) return cached;
            var bytes = ReadDirect(page, PageSize);
            _pages[page] = bytes;
            _pageOrder.Enqueue(page);
            while (_pageOrder.Count > MaxCachedPages)
            {
                ulong old = _pageOrder.Dequeue();
                _pages.Remove(old);
            }
            return bytes;
        }

        public byte[] Read(ulong address, int count)
        {
            if (count < 0) throw new ArgumentOutOfRangeException(nameof(count));
            if (count >= PageSize * 2) return ReadDirect(address, count);
            var result = new byte[count];
            int done = 0;
            while (done < count)
            {
                ulong at = address + (ulong)done;
                ulong page = at & ~(ulong)(PageSize - 1);
                int within = (int)(at - page);
                int take = Math.Min(count - done, PageSize - within);
                try
                {
                    var bytes = GetPage(page);
                    Buffer.BlockCopy(bytes, within, result, done, take);
                }
                catch
                {
                    var bytes = ReadDirect(at, take);
                    Buffer.BlockCopy(bytes, 0, result, done, take);
                }
                done += take;
            }
            return result;
        }

        public bool TryRead(ulong address, int count, out byte[] bytes)
        {
            try { bytes = Read(address, count); return true; }
            catch { bytes = Array.Empty<byte>(); return false; }
        }

        public bool TryReadBlock(ulong address, int count, out byte[] bytes)
        {
            try { bytes = ReadDirect(address, count); return true; }
            catch { bytes = Array.Empty<byte>(); return false; }
        }

        internal IReadOnlyList<Range> ReadableImageRanges()
        {
            ulong imageEnd = ModuleBase + ImageSize;
            ulong address = ModuleBase;
            var result = new List<Range>();
            UIntPtr infoSize = (UIntPtr)(uint)Marshal.SizeOf<MemoryBasicInformation>();

            while (address < imageEnd)
            {
                if (VirtualQueryEx(_handle, unchecked((IntPtr)(long)address), out var info, infoSize) == UIntPtr.Zero) break;
                ulong regionStart = unchecked((ulong)info.BaseAddress.ToInt64());
                ulong regionSize = info.RegionSize.ToUInt64();
                if (regionSize == 0 || regionStart + regionSize <= address) break;

                ulong start = Math.Max(regionStart, ModuleBase);
                ulong end = Math.Min(regionStart + regionSize, imageEnd);
                bool readable = info.State == MEM_COMMIT &&
                                (info.Protect & READABLE_PROTECTION) != 0 &&
                                (info.Protect & PAGE_GUARD) == 0;
                if (readable && start < end) result.Add(new Range(start, end - start));
                address = regionStart + regionSize;
            }

            return result;
        }

        public ulong Normalize(ulong address)
        {
            if (address >= ModuleBase && address < ModuleBase + ImageSize)
                return PreferredBase + (address - ModuleBase);
            return address;
        }

        public string ReadCString(ulong address, int maxBytes = 16384)
        {
            if (address < 0x10000) return "";
            var bytes = new List<byte>(Math.Min(maxBytes, 256));
            for (int offset = 0; offset < maxBytes; offset += 256)
            {
                int count = Math.Min(256, maxBytes - offset);
                if (!TryRead(address + (ulong)offset, count, out var part)) break;
                int zero = Array.IndexOf(part, (byte)0);
                if (zero >= 0) { bytes.AddRange(part.Take(zero)); break; }
                bytes.AddRange(part);
            }
            return Encoding.UTF8.GetString(bytes.ToArray());
        }

        public void ClearCache()
        {
            _pages.Clear();
            _pageOrder.Clear();
        }

        public void Dispose()
        {
            if (_handle != IntPtr.Zero) CloseHandle(_handle);
        }
    }

    internal sealed class LiveImageScanner
    {
        private readonly ProcessMemory _memory;
        private readonly IReadOnlyList<ProcessMemory.Range> _ranges;

        public LiveImageScanner(ProcessMemory memory)
        {
            _memory = memory;
            _ranges = memory.ReadableImageRanges();
            if (_ranges.Count == 0) throw new InvalidOperationException("The running game image has no readable memory regions.");
        }

        private static byte?[] Mask(byte[] values) => values.Select(value => (byte?)value).ToArray();

        private bool TryReadWindow(ulong address, int maximum, out byte[] bytes)
        {
            var range = _ranges.FirstOrDefault(item => address >= item.Start && address < item.End);
            int count = range.Size == 0 ? 0 : (int)Math.Min((ulong)maximum, range.End - address);
            if (count > 0) return _memory.TryReadBlock(address, count, out bytes);
            bytes = Array.Empty<byte>();
            return false;
        }

        private List<ulong> FindPattern(byte[] pattern) => FindPatterns(new[] { Mask(pattern) })[0];

        private List<ulong>[] FindPatterns(IReadOnlyList<byte?[]> patterns)
        {
            const int Block = 4 * 1024 * 1024;
            const int SubBlock = 64 * 1024;
            const int Page = 4 * 1024;
            if (patterns.Count == 0 || patterns.Any(pattern => pattern.Length == 0))
                throw new ArgumentException("Search patterns cannot be empty.", nameof(patterns));

            var found = patterns.Select(_ => new HashSet<ulong>()).ToArray();
            int maximumLength = patterns.Max(pattern => pattern.Length);
            var probes = patterns.Select(pattern =>
            {
                for (int i = 0; i < pattern.Length; i++)
                    if (pattern[i].HasValue) return i;
                return 0;
            }).ToArray();
            byte[] overlap = Array.Empty<byte>();
            ulong expectedAddress = 0;

            void Consume(ulong address, byte[] current)
            {
                if (address != expectedAddress) overlap = Array.Empty<byte>();
                var bytes = new byte[overlap.Length + current.Length];
                Buffer.BlockCopy(overlap, 0, bytes, 0, overlap.Length);
                Buffer.BlockCopy(current, 0, bytes, overlap.Length, current.Length);
                ulong origin = address - (ulong)overlap.Length;

                for (int p = 0; p < patterns.Count; p++)
                {
                    var pattern = patterns[p];
                    int probe = probes[p];
                    for (int i = 0; i + pattern.Length <= bytes.Length; i++)
                    {
                        if (pattern[probe].HasValue && bytes[i + probe] != pattern[probe]!.Value) continue;
                        int n = 0;
                        while (n < pattern.Length && (!pattern[n].HasValue || bytes[i + n] == pattern[n]!.Value)) n++;
                        if (n == pattern.Length) found[p].Add(origin + (ulong)i);
                    }
                }

                int keep = Math.Min(maximumLength - 1, bytes.Length);
                overlap = bytes.AsSpan(bytes.Length - keep, keep).ToArray();
                expectedAddress = address + (ulong)current.Length;
            }

            void ReadRange(ulong start, ulong size)
            {
                for (ulong offset = 0; offset < size;)
                {
                    int count = (int)Math.Min((ulong)Block, size - offset);
                    ulong address = start + offset;
                    if (_memory.TryReadBlock(address, count, out var block))
                    {
                        Consume(address, block);
                    }
                    else
                    {
                        for (int sub = 0; sub < count; sub += SubBlock)
                        {
                            int take = Math.Min(SubBlock, count - sub);
                            ulong at = address + (ulong)sub;
                            if (_memory.TryReadBlock(at, take, out var part))
                            {
                                Consume(at, part);
                                continue;
                            }
                            for (int page = 0; page < take; page += Page)
                            {
                                int pageSize = Math.Min(Page, take - page);
                                ulong pageAddress = at + (ulong)page;
                                if (_memory.TryReadBlock(pageAddress, pageSize, out var pageBytes)) Consume(pageAddress, pageBytes);
                                else expectedAddress = 0;
                            }
                        }
                    }
                    offset += (uint)count;
                }
            }

            foreach (var range in _ranges) ReadRange(range.Start, range.Size);
            return found.Select(matches => matches.OrderBy(address => address).ToList()).ToArray();
        }

        public ulong FindTdbRva()
        {
            var magic = new byte[] { (byte)'T', (byte)'D', (byte)'B', 0 };
            foreach (ulong address in FindPattern(magic))
            {
                if (!TryReadWindow(address, 0xF0, out var h) || h.Length < 0xF0) continue;
                uint version = PeImage.U32(h, 4);
                if (!TdbLayout.Supports(version)) continue;
                var layout = TdbLayout.For(version);
                uint types = PeImage.U32(h, layout.NumTypes);
                uint methods = PeImage.U32(h, layout.NumMethods);
                uint fields = PeImage.U32(h, layout.NumFields);
                ulong typeTable = PeImage.U64(h, layout.Types);
                ulong methodTable = PeImage.U64(h, layout.Methods);
                bool tables = typeTable >= _memory.ModuleBase && typeTable < _memory.ModuleBase + _memory.ImageSize &&
                              methodTable >= _memory.ModuleBase && methodTable < _memory.ModuleBase + _memory.ImageSize;
                if (types < 10000 || types > 1000000 || methods < types || methods > 10000000 || fields > 10000000 || !tables) continue;
                return address - _memory.ModuleBase;
            }
            throw new InvalidDataException("Could not locate a supported RE Engine TDB in the running game image (versions 66, 67, and 69 through 84).");
        }

        public ulong[] FindTypeListRvas(uint tdbVersion)
        {
            var result = new HashSet<ulong>();
            var directReference = new byte?[] {
                0x48,0x8D,0x0D,null,null,null,null, 0xE8,null,null,null,null,
                0x48,0x8D,0x05,null,null,null,null, 0x48,0x89,0x03
            };
            var typeInfoNone = Mask(new byte[] { 0xBA, 0xAE, 0xE7, 0xF7, 0x08 });
            var viaObjectA = Mask(new byte[] { 0xBA, 0x55, 0xFD, 0x09, 0xD2 });
            var viaObjectB = Mask(new byte[] { 0xC7, 0xC2, 0x55, 0xFD, 0x09, 0xD2 });
            var patterns = tdbVersion < 73
                ? new[] { directReference, typeInfoNone, viaObjectA, viaObjectB }
                : new[] { viaObjectA, viaObjectB };
            var matches = FindPatterns(patterns);

            void AddRipTarget(ulong instruction, int displacementOffset, int instructionLength)
            {
                if (!_memory.TryReadBlock(instruction + (ulong)displacementOffset, 4, out var bytes)) return;
                long target = unchecked((long)instruction + instructionLength + PeImage.I32(bytes, 0));
                long rva = target - (long)_memory.ModuleBase;
                if (rva >= 0x1000 && rva + 0x10 <= _memory.ImageSize && (rva & 7) == 0)
                    result.Add((ulong)rva);
            }

            if (tdbVersion < 73)
            {
                foreach (ulong reference in matches[0]) AddRipTarget(reference, 3, 7);

                foreach (ulong anchor in matches[1])
                {
                    if (!TryReadWindow(anchor, 0x100, out var registration)) continue;
                    for (int i = 0; i + 8 <= registration.Length; i++)
                    {
                        if (registration[i] != 0x48 || registration[i + 1] != 0x8B || registration[i + 2] != 0xCB || registration[i + 3] != 0xE8) continue;
                        ulong functionAddress = unchecked((ulong)((long)anchor + i + 8L + PeImage.I32(registration, i + 4)));
                        if (!TryReadWindow(functionAddress, 0x200, out var function)) continue;
                        for (int n = 0; n + 7 <= function.Length; n++)
                            if (function[n] == 0x4C && function[n + 1] == 0x8B && function[n + 2] == 0x05)
                                AddRipTarget(functionAddress + (ulong)n, 3, 7);
                    }
                }
            }

            int viaIndex = tdbVersion < 73 ? 2 : 0;
            var anchors = matches[viaIndex].Concat(matches[viaIndex + 1]);
            foreach (ulong anchor in anchors)
            {
                ulong start = anchor - Math.Min(anchor - _memory.ModuleBase, 0x80UL);
                if (!TryReadWindow(start, 0x580, out var code)) continue;
                for (int i = 0; i + 7 <= code.Length; i++)
                {
                    bool rex = (code[i] & 0xF0) == 0x40;
                    if (!rex || (code[i + 1] != 0x8D && code[i + 1] != 0x8B) || (code[i + 2] & 0xC7) != 0x05) continue;
                    AddRipTarget(start + (ulong)i, 3, 7);
                }
            }
            return result.OrderBy(rva => rva).ToArray();
        }
    }

    internal sealed class TypeRecord
    {
        public int Id, Parent, Declaring, Element, Impl;
        public uint Flags, Size, Fqn, RawCrc;
        public int GenericOffset, ObjectType;
        public ulong RuntimeType, ManagedVt;
        public string RawName = "", Namespace = "", BaseName = "", FullName = "", AssemblyIdentity = "";
        public string[] Hierarchy = Array.Empty<string>();
        public int ArrayRank, NumNativeVtable;
        public int GenericDefinition;
        public int[] GenericArguments = Array.Empty<int>();
        public string[] GenericParameterNames = Array.Empty<string>();
    }

    internal readonly struct MethodRecord
    {
        public readonly int Owner, Impl, ParamOffset, EncodedOffset, InvokeId, NumParams, ReturnType;
        public readonly ulong DirectFunction;
        public MethodRecord(int owner, int impl, int paramOffset, int encodedOffset, ulong directFunction = 0, int invokeId = 0, int numParams = 0, int returnType = 0)
        {
            Owner = owner; Impl = impl; ParamOffset = paramOffset; EncodedOffset = encodedOffset;
            DirectFunction = directFunction; InvokeId = invokeId; NumParams = numParams; ReturnType = returnType;
        }
    }

    internal readonly struct ParameterRecord
    {
        public readonly string Name;
        public readonly int Type, Modifier;
        public readonly ushort Flags;
        public ParameterRecord(string name, int type, ushort flags, int modifier)
        { Name = name; Type = type; Flags = flags; Modifier = modifier; }
    }

    internal readonly struct FieldRecord
    {
        public readonly int Owner, Impl, Type, InitHigh, DirectOffset;
        public FieldRecord(int owner, int impl, int type, int initHigh, int directOffset = 0)
        { Owner = owner; Impl = impl; Type = type; InitHigh = initHigh; DirectOffset = directOffset; }
    }

    internal readonly struct PropertyRecord
    {
        public readonly int Impl, Getter, Setter;
        public PropertyRecord(int impl, int getter, int setter) { Impl = impl; Getter = getter; Setter = setter; }
    }

    internal sealed class CoreDatabase
    {
        public readonly ProcessMemory Memory;
        public readonly TdbLayout Layout;
        public readonly ulong TypesAddress;
        public readonly uint Version, NumMethods, NumFields, NumProperties, NumParams;
        public int TypeStride { get; }
        public ulong EncodedBase { get; private set; }
        public readonly TypeRecord[] Types;
        public readonly Dictionary<int, List<int>> MethodsByOwner = new Dictionary<int, List<int>>();
        public readonly Dictionary<int, List<int>> FieldsByOwner = new Dictionary<int, List<int>>();
        public readonly Dictionary<int, List<int>> PropertiesByOwner = new Dictionary<int, List<int>>();
        public readonly Dictionary<string, List<int>> TypeIdsByName = new Dictionary<string, List<int>>(StringComparer.Ordinal);
        public readonly Dictionary<uint, int> FqnToType = new Dictionary<uint, int>();

        private readonly byte[] _methods, _methodImpls, _fields, _fieldImpls, _properties, _propertyImpls, _params, _initData, _stringPool, _bytePool;
        private readonly int _numMethodImpl, _numFieldImpl, _numPropertyImpl, _numInitData;
        private readonly uint _stringMask, _byteMask;
        private readonly Dictionary<int, string> _strings = new Dictionary<int, string>();
        private readonly int[] _fieldPtrOffsets;

        private static uint U32(byte[] b, int o) => PeImage.U32(b, o);
        private static int I32(byte[] b, int o) => PeImage.I32(b, o);
        private static ushort U16(byte[] b, int o) => PeImage.U16(b, o);
        private static short I16(byte[] b, int o) => PeImage.I16(b, o);
        private static ulong U64(byte[] b, int o) => PeImage.U64(b, o);
        private static int Bits(ulong value, int shift, ulong mask) => (int)((value >> shift) & mask);

        public CoreDatabase(ProcessMemory memory, ulong tdbAddress)
        {
            Memory = memory;
            var h = memory.Read(tdbAddress, 0xF0);
            if (U32(h, 0) != 0x00424454)
                throw new InvalidDataException($"Expected a TDB header at 0x{tdbAddress:x}.");
            Version = U32(h, 4);
            Layout = TdbLayout.For(Version);
            uint numTypes = U32(h, Layout.NumTypes);
            NumMethods = U32(h, Layout.NumMethods);
            NumFields = U32(h, Layout.NumFields);
            uint numTypeImpl = Layout.SplitMetadata ? U32(h, Layout.NumTypeImpl) : 0;
            uint numFieldImpl = Layout.SplitMetadata ? U32(h, Layout.NumFieldImpl) : NumFields;
            uint numMethodImpl = Layout.SplitMetadata ? U32(h, Layout.NumMethodImpl) : NumMethods;
            uint numPropertyImpl = Layout.SplitMetadata ? U32(h, Layout.NumPropertyImpl) : 0;
            NumProperties = U32(h, Layout.NumProperties);
            NumParams = Layout.SplitMetadata ? U32(h, Layout.NumParams) : 0;
            int numInitData = I32(h, Layout.NumInitData);
            uint numModules = U32(h, Layout.NumModules);
            uint stringPoolSize = U32(h, Layout.StringPoolSize);
            uint bytePoolSize = U32(h, Layout.BytePoolSize);
            if (numTypes == 0 || numTypes > 1000000 || NumMethods > 10000000 || NumFields > 10000000 || NumProperties > 10000000 || numModules > 4096)
                throw new InvalidDataException("TDB counts are outside sane limits.");

            uint PoolMask(uint size)
            {
                uint value = 1;
                while (value < size && value < 0x80000000U) value <<= 1;
                return value - 1;
            }
            _stringMask = PoolMask(stringPoolSize);
            _byteMask = PoolMask(bytePoolSize);

            ulong ResolveTable(int headerOffset)
            {
                if (headerOffset < 0) return 0;
                return U64(h, headerOffset);
            }

            ulong modules = ResolveTable(Layout.Modules);
            TypesAddress = ResolveTable(Layout.Types);
            ulong typesImplAddress = ResolveTable(Layout.TypeImpls);
            ulong methodsAddress = ResolveTable(Layout.Methods);
            ulong methodImplAddress = ResolveTable(Layout.MethodImpls);
            ulong fieldsAddress = ResolveTable(Layout.Fields);
            ulong fieldImplAddress = ResolveTable(Layout.FieldImpls);
            ulong propertiesAddress = ResolveTable(Layout.Properties);
            ulong propertyImplAddress = ResolveTable(Layout.PropertyImpls);
            ulong paramsAddress = ResolveTable(Layout.Params);
            ulong initDataAddress = ResolveTable(Layout.InitData);
            ulong stringPoolAddress = ResolveTable(Layout.StringPool);
            ulong bytePoolAddress = ResolveTable(Layout.BytePool);

            Console.WriteLine($"TDB {Version}: {numTypes:N0} types, {NumMethods:N0} methods, {NumFields:N0} fields, {NumProperties:N0} properties");
            Console.WriteLine("Loading core TDB tables...");
            var moduleBytes = memory.Read(modules, checked((int)numModules * Layout.ModuleStride));
            _stringPool = memory.Read(stringPoolAddress, checked((int)stringPoolSize));
            _bytePool = memory.Read(bytePoolAddress, checked((int)bytePoolSize));

            int typeStride = Layout.TypeStride;
            bool reorderedLegacy67 = false;
            if (Version == 67)
            {
                int sampleCount = Math.Min((int)numTypes, 512);
                var probe = memory.Read(TypesAddress, checked(sampleCount * 0x80));
                int Score(int stride)
                {
                    int score = 0;
                    for (int i = 0; i < sampleCount; i++)
                    {
                        int offset = i * stride;
                        if ((U64(probe, offset) & Layout.TypeMask) == (uint)i) score += 2;
                        uint name = U32(probe, offset + 0x18) & _stringMask;
                        if (name < _stringPool.Length) score++;
                    }
                    return score;
                }
                int packedScore = Score(0x78), reorderedScore = Score(0x80);
                if (Math.Max(packedScore, reorderedScore) < sampleCount)
                    throw new InvalidDataException("Could not derive the TDB 67 type-record layout.");
                reorderedLegacy67 = reorderedScore > packedScore;
                typeStride = reorderedLegacy67 ? 0x80 : 0x78;
                Console.WriteLine($"TDB 67 type-record stride: 0x{typeStride:x} (derived from the type table)");
            }
            TypeStride = typeStride;

            var typeBytes = memory.Read(TypesAddress, checked((int)numTypes * TypeStride));
            var typeImplBytes = Layout.SplitMetadata ? memory.Read(typesImplAddress, checked((int)numTypeImpl * 0x30)) : Array.Empty<byte>();
            _methods = memory.Read(methodsAddress, checked((int)NumMethods * Layout.MethodStride));
            _methodImpls = Layout.SplitMetadata ? memory.Read(methodImplAddress, checked((int)numMethodImpl * 0x0C)) : Array.Empty<byte>();
            _fields = memory.Read(fieldsAddress, checked((int)NumFields * Layout.FieldStride));
            _fieldImpls = Layout.SplitMetadata ? memory.Read(fieldImplAddress, checked((int)numFieldImpl * 0x0C)) : Array.Empty<byte>();
            _properties = memory.Read(propertiesAddress, checked((int)NumProperties * Layout.PropertyStride));
            _propertyImpls = Layout.SplitMetadata ? memory.Read(propertyImplAddress, checked((int)numPropertyImpl * 0x08)) : Array.Empty<byte>();
            _params = Layout.SplitMetadata ? memory.Read(paramsAddress, checked((int)NumParams * 0x0C)) : Array.Empty<byte>();
            _initData = numInitData > 0 ? memory.Read(initDataAddress, checked(numInitData * 4)) : Array.Empty<byte>();
            _numMethodImpl = (int)numMethodImpl;
            _numFieldImpl = (int)numFieldImpl;
            _numPropertyImpl = (int)numPropertyImpl;
            _numInitData = numInitData;

            Types = new TypeRecord[numTypes];
            _fieldPtrOffsets = Enumerable.Repeat(int.MinValue, (int)numTypes).ToArray();
            for (int i = 0; i < Types.Length; i++)
            {
                int o = i * TypeStride;
                ulong w0 = U64(typeBytes, o);
                ulong w1 = U64(typeBytes, o + 8);
                TypeRecord t;
                if (Version >= 71)
                {
                    uint obj = U32(typeBytes, o + 0x2C);
                    ulong gen = U64(typeBytes, o + 0x30);
                    t = new TypeRecord
                    {
                        Id = i,
                        Parent = Bits(w0, 19, 0x7FFFF),
                        Declaring = Bits(w0, 38, 0x7FFFF),
                        Element = Bits(w1, 19, 0x7FFFF),
                        Impl = Bits(w1, 38, 0x3FFFF),
                        Flags = U32(typeBytes, o + 0x10),
                        Size = U32(typeBytes, o + 0x14),
                        Fqn = U32(typeBytes, o + 0x18),
                        RawCrc = U32(typeBytes, o + 0x1C),
                        ObjectType = (int)((obj >> 26) & 7),
                        ArrayRank = (int)((obj >> 29) & 7),
                        GenericOffset = Bits(gen, 26, 0x3FFFFFF),
                        RuntimeType = U64(typeBytes, o + 0x38),
                        ManagedVt = U64(typeBytes, o + 0x40)
                    };
                }
                else if (Version >= 69)
                {
                    t = new TypeRecord
                    {
                        Id = i,
                        Parent = Bits(w0, 18, 0x3FFFF),
                        Declaring = Bits(w0, 36, 0x3FFFF),
                        ObjectType = Bits(w0, 61, 7),
                        Element = Bits(w1, 18, 0x3FFFF),
                        Impl = Bits(w1, 36, 0x3FFFF),
                        Flags = U32(typeBytes, o + 0x10),
                        Size = U32(typeBytes, o + 0x14),
                        Fqn = U32(typeBytes, o + 0x18),
                        RawCrc = U32(typeBytes, o + 0x1C),
                        GenericOffset = I32(typeBytes, o + 0x3C),
                        RuntimeType = U64(typeBytes, o + 0x40),
                        ManagedVt = U64(typeBytes, o + 0x48)
                    };
                }
                else
                {
                    int unknownBits = 64 - Layout.TypeBits * 3;
                    int parentShift = Layout.TypeBits + unknownBits;
                    int declaringShift = parentShift + Layout.TypeBits;
                    t = new TypeRecord
                    {
                        Id = i,
                        Parent = Bits(w0, parentShift, Layout.TypeMask),
                        Declaring = Bits(w0, declaringShift, Layout.TypeMask),
                        Impl = -1,
                        ObjectType = typeBytes[o + 0x26],
                        Flags = U32(typeBytes, o + 0x20),
                        Size = U32(typeBytes, o + 0x30),
                        Fqn = U32(typeBytes, o + 0x08),
                        RawCrc = U32(typeBytes, o + 0x0C),
                        GenericOffset = I32(typeBytes, o + (reorderedLegacy67 ? 0x58 : 0x50)),
                        RuntimeType = U64(typeBytes, o + (reorderedLegacy67 ? 0x70 : 0x68)),
                        ManagedVt = U64(typeBytes, o + (reorderedLegacy67 ? 0x78 : 0x70)),
                        RawName = StringAt(I32(typeBytes, o + 0x18)),
                        Namespace = StringAt(I32(typeBytes, o + 0x1C))
                    };
                }
                if (t.Impl >= 0 && t.Impl < numTypeImpl)
                {
                    int io = t.Impl * 0x30;
                    if (Layout.PackedTypeImplNames)
                    {
                        ulong names = U64(typeImplBytes, io);
                        t.RawName = StringAt((int)(names & 0x0FFFFFFF));
                        t.Namespace = StringAt((int)((names >> 28) & 0x0FFFFFFF));
                    }
                    else
                    {
                        t.RawName = StringAt(I32(typeImplBytes, io));
                        t.Namespace = StringAt(I32(typeImplBytes, io + 4));
                    }
                    if (Version >= 71)
                    {
                        t.NumNativeVtable = I16(typeImplBytes, io + 0x1A);
                    }
                    else
                    {
                        t.ArrayRank = typeImplBytes[io + 0x11];
                        t.NumNativeVtable = U16(typeImplBytes, io + 0x1A);
                    }
                }
                Types[i] = t;
                if (t.Fqn != 0) FqnToType[t.Fqn] = i;
            }

            AssignModules(moduleBytes, (int)numModules);
            ParseGenericData();
            BuildNames();
            if (Version < 69)
            {
                RecoverLegacyDerivedNames();
                RecoverLegacyRuntimeNames();
            }
            BuildNameGroups();
            Console.WriteLine($"Resolved {TypeIdsByName.Count:N0} unique type names.");
            BuildMemberGroups();
            BuildPropertyGroups();

            if (Layout.EncodedMethods)
            {
                if (!TryDiscoverEncodedBase(out ulong discoveredBase))
                    throw new InvalidDataException("Could not derive the encoded method-function base from live game code.");
                EncodedBase = discoveredBase;
                Console.WriteLine($"Encoded function base: RVA 0x{EncodedBase - memory.ModuleBase:x} (derived from live code)");
            }
        }

        private void AssignModules(byte[] modules, int count)
        {
            for (int i = 0; i < count; i++)
            {
                int o = i * Layout.ModuleStride;
                string assembly = StringAt(I32(modules, o + 0x24));
                if (String.IsNullOrEmpty(assembly)) assembly = "Unknown";
                string identity = String.Format(
                    CultureInfo.InvariantCulture,
                    "{0}, Version={1}.{2}.{3}.{4}, Culture=neutral, PublicKeyToken=null",
                    assembly,
                    U16(modules, o + 0x18),
                    U16(modules, o + 0x1A),
                    U16(modules, o + 0x1C),
                    U16(modules, o + 0x1E));
                int typeCount = I32(modules, o + 0x38);
                int typeOffset = (int)((uint)I32(modules, o + 0x3C) & _byteMask);
                if (typeCount < 0 || typeCount > Types.Length || typeOffset < 0 || (long)typeOffset + (long)typeCount * 4 > _bytePool.Length) continue;
                for (int n = 0; n < typeCount; n++)
                {
                    int typeId = (int)(U32(_bytePool, typeOffset + n * 4) & Layout.TypeMask);
                    if (typeId > 0 && typeId < Types.Length) Types[typeId].AssemblyIdentity = identity;
                }
            }
        }

        private void ParseGenericData()
        {
            foreach (var t in Types)
            {
                t.GenericOffset = (int)((uint)t.GenericOffset & _byteMask);
                if (t.GenericOffset <= 0 || t.GenericOffset + 4 > _bytePool.Length) continue;
                uint head = U32(_bytePool, t.GenericOffset);
                int typeBits = Layout.TypeBits;
                t.GenericDefinition = (int)(head & Layout.TypeMask);
                int count = Version < 69
                    ? (int)((head >> typeBits) & (Version == 67 ? 0x3FFFU : 0xFFFFU))
                    : (int)((head >> typeBits) & 0x7F);
                int argumentStride = Version == 66 ? 2 : 4;
                if (count < 0 || count > 127 || t.GenericOffset + 4 + count * argumentStride > _bytePool.Length) continue;
                t.GenericArguments = new int[count];
                for (int i = 0; i < count; i++)
                {
                    int at = t.GenericOffset + 4 + i * argumentStride;
                    t.GenericArguments[i] = Version == 66 ? U16(_bytePool, at) : (int)(U32(_bytePool, at) & Layout.TypeMask);
                }
                if (t.GenericDefinition == t.Id && count > 0)
                {
                    t.GenericParameterNames = new string[count];
                    for (int i = 0; i < count; i++)
                    {
                        int at = t.GenericOffset + 0x0C + i * 8;
                        if (Version >= 69 && at >= 0 && at + 4 <= _bytePool.Length)
                            t.GenericParameterNames[i] = StringAt((int)(U32(_bytePool, at) & _stringMask));
                        if (String.IsNullOrEmpty(t.GenericParameterNames[i]))
                            t.GenericParameterNames[i] = count == 1 ? "T" : "T" + (i + 1).ToString(CultureInfo.InvariantCulture);
                    }
                }
            }
        }

        private string BuildBaseName(int id, HashSet<int> seen)
        {
            if (id < 0 || id >= Types.Length) return "";
            var t = Types[id];
            if (id == 0)
            {
                if (t.Hierarchy.Length == 0) t.Hierarchy = new[] { "" };
                return "";
            }
            if (t.BaseName.Length != 0) return t.BaseName;
            if (!seen.Add(id)) return t.RawName;
            var parts = new List<string>();
            if (t.Declaring > 0 && t.Declaring != id && t.Declaring < Types.Length)
            {
                var chain = new List<int>();
                int at = id;
                var local = new HashSet<int>();
                while (at >= 0 && at < Types.Length && local.Add(at))
                {
                    chain.Add(at);
                    int next = Types[at].Declaring;
                    if (next <= 0 || next == at || next >= Types.Length) break;
                    at = next;
                }
                chain.Reverse();
                int outer = chain[0];
                if (!String.IsNullOrEmpty(Types[outer].Namespace)) parts.Add(Types[outer].Namespace);
                foreach (int item in chain) parts.Add(Types[item].RawName);
            }
            else
            {
                if (!String.IsNullOrEmpty(t.Namespace)) parts.Add(t.Namespace);
                parts.Add(t.RawName);
            }
            t.Hierarchy = parts.ToArray();
            t.BaseName = String.Join(".", parts);
            seen.Remove(id);
            return t.BaseName;
        }

        private string BuildFullName(int id, byte[] state)
        {
            if (id < 0 || id >= Types.Length) return "";
            var t = Types[id];
            if (state[id] == 2) return t.FullName;
            if (state[id] == 1) return t.BaseName;
            state[id] = 1;
            string result = t.BaseName;
            if (t.ObjectType == 2 && String.IsNullOrEmpty(t.RawName) && t.Element > 0 && t.Element < Types.Length)
            {
                int rank = t.ArrayRank <= 0 ? 1 : t.ArrayRank;
                result = BuildReflectionName(t.Element, state) + "[" + new string(',', rank - 1) + "]";
            }
            else if (t.GenericArguments.Length > 0 && t.GenericDefinition == id)
            {
                string assembly = AssemblyIdentityFor(id);
                var args = t.GenericParameterNames.Select(x => x + ", " + assembly);
                result = t.BaseName + "[[" + String.Join("],[", args) + "]]";
            }
            else if (t.GenericArguments.Length > 0)
            {
                var args = t.GenericArguments.Select(x => x > 0 && x < Types.Length ? BuildFullName(x, state) : "");
                result += "<" + String.Join(",", args) + ">";
            }
            t.FullName = result;
            state[id] = 2;
            return result;
        }

        private string BuildReflectionName(int id, byte[] state)
        {
            if (id < 0 || id >= Types.Length) return "";
            var t = Types[id];
            if (t.ObjectType == 2 && String.IsNullOrEmpty(t.RawName) && t.Element > 0 && t.Element < Types.Length)
            {
                int rank = t.ArrayRank <= 0 ? 1 : t.ArrayRank;
                return BuildReflectionName(t.Element, state) + "[" + new string(',', rank - 1) + "]";
            }
            if (t.GenericArguments.Length > 0 && t.GenericDefinition != id)
            {
                string assembly = ", " + AssemblyIdentityFor(id);
                var args = t.GenericArguments.Select(x => BuildReflectionName(x, state) + assembly);
                return t.BaseName + "[[" + String.Join("],[", args) + "]]";
            }
            if (t.GenericArguments.Length > 0 && t.GenericDefinition == id)
            {
                string assembly = ", " + AssemblyIdentityFor(id);
                var args = t.GenericParameterNames.Select(x => x + assembly);
                return t.BaseName + "[[" + String.Join("],[", args) + "]]";
            }
            return BuildFullName(id, state);
        }

        private string AssemblyIdentityFor(int id)
        {
            var seen = new HashSet<int>();
            int at = id;
            while (at > 0 && at < Types.Length && seen.Add(at))
            {
                var t = Types[at];
                if (!String.IsNullOrEmpty(t.AssemblyIdentity)) return t.AssemblyIdentity;
                if (t.GenericDefinition > 0 && t.GenericDefinition != at) { at = t.GenericDefinition; continue; }
                if (t.Element > 0 && t.Element != at) { at = t.Element; continue; }
                break;
            }
            return "System.Private.CoreLib, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null";
        }

        private void BuildNames()
        {
            var seen = new HashSet<int>();
            for (int i = 0; i < Types.Length; i++) BuildBaseName(i, seen);
            var state = new byte[Types.Length];
            for (int i = 0; i < Types.Length; i++) BuildFullName(i, state);
        }

        internal static uint TypeNameHash(string value)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(value);
            uint hash = UInt32.MaxValue;
            const uint c1 = 0xCC9E2D51, c2 = 0x1B873593;
            unchecked
            {
                int blocks = bytes.Length / 4;
                for (int i = 0; i < blocks; i++)
                {
                    uint k = U32(bytes, i * 4) * c1;
                    k = (k << 15) | (k >> 17);
                    k *= c2;
                    hash ^= k;
                    hash = (hash << 13) | (hash >> 19);
                    hash = hash * 5 + 0xE6546B64;
                }

                uint tail = 0;
                int at = blocks * 4, left = bytes.Length - at;
                if (left >= 3) tail ^= (uint)bytes[at + 2] << 16;
                if (left >= 2) tail ^= (uint)bytes[at + 1] << 8;
                if (left >= 1)
                {
                    tail ^= bytes[at];
                    tail *= c1;
                    tail = (tail << 15) | (tail >> 17);
                    tail *= c2;
                    hash ^= tail;
                }

                hash ^= (uint)bytes.Length;
                hash ^= hash >> 16;
                hash *= 0x85EBCA6B;
                hash ^= hash >> 13;
                hash *= 0xC2B2AE35;
                hash ^= hash >> 16;
                return hash;
            }
        }

        private void RecoverLegacyDerivedNames()
        {
            int recovered = 0;
            for (int pass = 0; pass < 8; pass++)
            {
                int before = recovered;
                foreach (var source in Types.Where(type => !String.IsNullOrEmpty(type.FullName)).ToArray())
                {
                    void Resolve(string suffix, int rank)
                    {
                        if (!FqnToType.TryGetValue(TypeNameHash(source.FullName + suffix), out int id)) return;
                        var target = Types[id];
                        if (!String.IsNullOrEmpty(target.FullName)) return;
                        target.Element = source.Id;
                        target.ArrayRank = rank;
                        target.Namespace = source.Namespace;
                        target.RawName = source.RawName + suffix;
                        target.BaseName = source.BaseName + suffix;
                        target.FullName = source.FullName + suffix;
                        target.Hierarchy = source.Hierarchy.Length == 0
                            ? new[] { target.RawName }
                            : source.Hierarchy.Select((part, index) => index == source.Hierarchy.Length - 1 ? part + suffix : part).ToArray();
                        recovered++;
                    }

                    for (int rank = 1; rank <= 8; rank++) Resolve("[" + new string(',', rank - 1) + "]", rank);
                    Resolve("&", 0);
                    Resolve("*", 0);
                }
                if (recovered == before) break;
            }
            if (recovered > 0) Console.WriteLine($"Resolved {recovered:N0} legacy derived type names from FQN hashes.");
        }

        private void RecoverLegacyRuntimeNames()
        {
            int recovered = 0;
            foreach (var type in Types.Where(item => String.IsNullOrEmpty(item.FullName) && item.RuntimeType >= 0x10000))
            {
                if (!Memory.TryRead(type.RuntimeType + 0x20, 8, out var pointer)) continue;
                ulong address = U64(pointer, 0);
                if (address < 0x10000) continue;
                string name = Memory.ReadCString(address, 4096);
                if (String.IsNullOrEmpty(name) || TypeNameHash(name) != type.Fqn) continue;
                type.RawName = type.BaseName = type.FullName = name;
                type.Hierarchy = new[] { name };
                recovered++;
            }
            if (recovered > 0) Console.WriteLine($"Resolved {recovered:N0} legacy runtime-generated type names from live metadata.");
        }

        private void BuildNameGroups()
        {
            for (int i = 0; i < Types.Length; i++)
            {
                string name = Types[i].FullName;
                if (!TypeIdsByName.TryGetValue(name, out var ids)) TypeIdsByName[name] = ids = new List<int>(1);
                ids.Add(i);
            }
        }

        private void BuildMemberGroups()
        {
            for (int i = 0; i < NumMethods; i++)
            {
                int owner = GetMethod(i).Owner;
                if (owner < 0 || owner >= Types.Length) continue;
                if (!MethodsByOwner.TryGetValue(owner, out var list)) MethodsByOwner[owner] = list = new List<int>();
                list.Add(i);
            }
            for (int i = 0; i < NumFields; i++)
            {
                int owner = GetField(i).Owner;
                if (owner < 0 || owner >= Types.Length) continue;
                if (!FieldsByOwner.TryGetValue(owner, out var list)) FieldsByOwner[owner] = list = new List<int>();
                list.Add(i);
            }
        }

        private void BuildPropertyGroups()
        {
            for (int i = 0; i < NumProperties; i++)
            {
                var p = GetProperty(i);
                int owner = -1;
                if (p.Getter >= 0 && p.Getter < NumMethods) owner = GetMethod(p.Getter).Owner;
                if (p.Setter >= 0 && p.Setter < NumMethods) owner = GetMethod(p.Setter).Owner;
                if (owner < 0 || owner >= Types.Length) continue;
                if (!PropertiesByOwner.TryGetValue(owner, out var list)) PropertiesByOwner[owner] = list = new List<int>();
                list.Add(i);
            }
        }

        public string StringAt(int offset)
        {
            offset = (int)((uint)offset & _stringMask);
            if (offset < 0 || offset >= _stringPool.Length) return "";
            if (_strings.TryGetValue(offset, out var value)) return value;
            int end = offset;
            while (end < _stringPool.Length && _stringPool[end] != 0) end++;
            value = Encoding.UTF8.GetString(_stringPool, offset, end - offset);
            _strings[offset] = value;
            return value;
        }

        public MethodRecord GetMethod(int id)
        {
            if (id < 0 || id >= NumMethods) return new MethodRecord(0, 0, 0, 0);
            int o = id * Layout.MethodStride;
            if (Version < 69)
            {
                ulong legacy = U64(_methods, o);
                int invoke = Version == 67 ? Bits(legacy, 17, 0xFFFF) : U16(_methods, o + 0x0A);
                int count = Version == 67 ? Bits(legacy, 33, 0x3F) : Bits(legacy, 32, 0xFF);
                int returns = Version == 67 ? Bits(legacy, 47, 0x1FFFF) : Bits(legacy, 48, 0xFFFF);
                return new MethodRecord(
                    Bits(legacy, 0, Layout.TypeMask), id, I32(_methods, o + 0x14), 0,
                    U64(_methods, o + Layout.MethodFunctionOffset), invoke, count, returns);
            }
            if (Version >= 71)
            {
                uint a = U32(_methods, o), b = U32(_methods, o + 4);
                return new MethodRecord((int)(a & 0x7FFFF), (int)(b & 0x7FFFF), (int)((a >> 19) | ((b >> 19) << 13)), I32(_methods, o + 8));
            }
            ulong w = U64(_methods, o);
            return new MethodRecord(Bits(w, 0, 0x3FFFF), Bits(w, 18, 0xFFFFF), Bits(w, 38, 0x3FFFFFF), 0, U64(_methods, o + 8));
        }

        public string MethodName(int id)
        {
            var m = GetMethod(id);
            if (Version < 69) return id >= 0 && id < NumMethods ? StringAt(I32(_methods, id * Layout.MethodStride + 0x0C)) : "";
            if (m.Impl < 0 || m.Impl >= _numMethodImpl) return "";
            return StringAt(I32(_methodImpls, m.Impl * 0x0C + 8));
        }

        public void MethodImpl(int impl, out short vtable, out ushort flags, out ushort implFlags)
        {
            if (impl < 0 || impl >= _numMethodImpl) { vtable = -1; flags = implFlags = 0; return; }
            if (Version < 69)
            {
                int legacy = impl * Layout.MethodStride;
                vtable = I16(_methods, legacy + (Version == 67 ? 0x0A : 0x02));
                flags = U16(_methods, legacy + 0x10);
                implFlags = U16(_methods, legacy + 0x12);
                return;
            }
            int o = impl * 0x0C;
            vtable = I16(_methodImpls, o + 2);
            flags = U16(_methodImpls, o + 4);
            implFlags = U16(_methodImpls, o + 6);
        }

        public FieldRecord GetField(int id)
        {
            if (id < 0 || id >= NumFields) return new FieldRecord(0, 0, 0, 0);
            int o = id * Layout.FieldStride;
            ulong w = U64(_fields, o);
            if (Version < 69)
                return new FieldRecord(Bits(w, 0, Layout.TypeMask), id, Bits(w, Layout.TypeBits, Layout.TypeMask), 0, I32(_fields, o + 0x10));
            if (Version >= 71)
                return new FieldRecord(Bits(w, 0, 0x7FFFF), Bits(w, 19, 0x7FFFF), Bits(w, 38, 0x7FFFF), Bits(w, 57, 0x3F));
            int impl = Bits(w, 18, 0xFFFFF);
            int type = impl >= 0 && impl < _numFieldImpl ? (int)(U32(_fieldImpls, impl * 0x0C + 4) & 0x3FFFF) : 0;
            return new FieldRecord(Bits(w, 0, 0x3FFFF), impl, type, 0, Bits(w, 38, 0x3FFFFFF));
        }

        public void FieldImpl(FieldRecord f, out string name, out ushort flags, out int offset, out int initIndex)
        {
            if (f.Impl < 0 || f.Impl >= _numFieldImpl) { name = ""; flags = 0; offset = initIndex = 0; return; }
            if (Version < 69)
            {
                int legacy = f.Impl * Layout.FieldStride;
                name = StringAt(I32(_fields, legacy + 0x08));
                flags = U16(_fields, legacy + 0x0C);
                initIndex = U16(_fields, legacy + 0x0E);
                offset = I32(_fields, legacy + 0x10);
                return;
            }
            int o = f.Impl * 0x0C;
            ushort fw = U16(_fieldImpls, o + 2);
            uint ow = U32(_fieldImpls, o + 4), nw = U32(_fieldImpls, o + 8);
            if (Version >= 71)
            {
                flags = (ushort)(fw >> 1);
                offset = (int)(ow & 0x3FFFFFF);
                int lo = (int)(ow >> 26), mid = (int)(nw >> 28);
                initIndex = lo | (mid << 6) | (f.InitHigh << 10);
                name = StringAt((int)(nw & 0x0FFFFFFF));
            }
            else
            {
                flags = fw;
                offset = f.DirectOffset;
                initIndex = (int)(ow >> 18) | (int)((nw >> 30) << 14);
                name = StringAt((int)(nw & 0x3FFFFFFF));
            }
        }

        public PropertyRecord GetProperty(int id)
        {
            if (id < 0 || id >= NumProperties) return new PropertyRecord(0, 0, 0);
            int o = id * Layout.PropertyStride;
            if (Version < 69) return new PropertyRecord(id, I32(_properties, o + 0x08), I32(_properties, o + 0x0C));
            ulong w = U64(_properties, o);
            return new PropertyRecord(Bits(w, 0, 0xFFFFF), Bits(w, 20, 0x3FFFFF), Bits(w, 42, 0x3FFFFF));
        }

        public string PropertyName(int id)
        {
            var p = GetProperty(id);
            if (Version < 69) return id >= 0 && id < NumProperties ? StringAt(I32(_properties, id * Layout.PropertyStride + 4)) : "";
            if (p.Impl < 0 || p.Impl >= _numPropertyImpl) return "";
            return StringAt(I32(_propertyImpls, p.Impl * 8 + 4));
        }

        private ParameterRecord Parameter(int id)
        {
            if (id < 0 || id >= NumParams) return new ParameterRecord("", 0, 0, 0);
            int o = id * 0x0C;
            uint nw = U32(_params, o + 4), tw = U32(_params, o + 8);
            return new ParameterRecord(
                StringAt((int)(nw & 0x3FFFFFFF)),
                (int)(tw & Layout.TypeMask),
                (ushort)(tw >> Layout.TypeBits),
                (int)(nw >> 30));
        }

        public bool TryMethodParameters(MethodRecord method, out int invoke, out ParameterRecord returns, out ParameterRecord[] parameters)
        {
            invoke = 0;
            returns = new ParameterRecord("", 0, 0, 0);
            parameters = Array.Empty<ParameterRecord>();
            int offset = (int)((uint)method.ParamOffset & _byteMask);

            if (Version < 69)
            {
                invoke = method.InvokeId;
                returns = new ParameterRecord("", method.ReturnType, 0, 0);
                if (method.NumParams < 0 || method.NumParams > 1024 ||
                    (method.NumParams > 0 && (offset < 0 || offset + method.NumParams * 8 > _bytePool.Length))) return false;
                parameters = new ParameterRecord[method.NumParams];
                for (int i = 0; i < parameters.Length; i++)
                {
                    ulong word = U64(_bytePool, offset + i * 8);
                    int nameShift = Layout.TypeBits + 16;
                    parameters[i] = new ParameterRecord(
                        StringAt((int)((word >> nameShift) & 0x7FFFFFFF)),
                        (int)(word & Layout.TypeMask),
                        (ushort)((word >> Layout.TypeBits) & 0xFFFF),
                        0);
                }
                return true;
            }

            if (offset < 0 || offset + 8 > _bytePool.Length) return false;
            int count = U16(_bytePool, offset);
            invoke = U16(_bytePool, offset + 2);
            int returnParam = I32(_bytePool, offset + 4);
            if (count < 0 || count > 1024 || offset + 8 + count * 4 > _bytePool.Length) return false;
            parameters = new ParameterRecord[count];
            for (int i = 0; i < count; i++) parameters[i] = Parameter(I32(_bytePool, offset + 8 + i * 4));
            returns = Parameter(returnParam);
            return true;
        }

        public object? DefaultValue(int initIndex, int fieldType)
        {
            if (initIndex <= 0 || initIndex >= _numInitData || fieldType < 0 || fieldType >= Types.Length) return null;
            int dataOffset = I32(_initData, initIndex * 4);
            if (dataOffset == 0) return null;
            string typeName = Types[fieldType].FullName;
            var ft = Types[fieldType];
            if (ft.Parent > 0 && ft.Parent < Types.Length && Types[ft.Parent].FullName == "System.Enum")
            {
                uint parentSize = Types[ft.Parent].Size;
                switch (ft.Size >= parentSize ? ft.Size - parentSize : ft.Size)
                {
                    case 1: typeName = "System.Byte"; break;
                    case 2: typeName = "System.UInt16"; break;
                    case 4: typeName = "System.UInt32"; break;
                    case 8: typeName = "System.UInt64"; break;
                }
            }
            byte[] pool;
            int at;
            if (dataOffset < 0) { pool = _stringPool; at = -dataOffset; }
            else { pool = _bytePool; at = (int)((uint)dataOffset & _byteMask); }
            if (dataOffset < 0) at = (int)((uint)at & _stringMask);
            if (at < 0 || at >= pool.Length) return null;
            try
            {
                switch (typeName)
                {
                    case "System.Boolean": return pool[at] != 0;
                    case "System.Char": return (int)U16(pool, at);
                    case "System.Byte": return (int)pool[at];
                    case "System.SByte": return (int)unchecked((sbyte)pool[at]);
                    case "System.UInt16": return (int)U16(pool, at);
                    case "System.Int16": return (int)I16(pool, at);
                    case "System.UInt32": return U32(pool, at);
                    case "System.Int32": return I32(pool, at);
                    case "System.UInt64": return U64(pool, at);
                    case "System.Int64": return unchecked((long)U64(pool, at));
                    case "System.Single": return BitConverter.Int32BitsToSingle(I32(pool, at));
                    case "System.Double": return BitConverter.Int64BitsToDouble(unchecked((long)U64(pool, at)));
                    case "System.String":
                        int end = at; while (end < pool.Length && pool[end] != 0) end++;
                        return Encoding.UTF8.GetString(pool, at, end - at);
                    default: return "REFRAMEWORK_UNIMPLEMENTED_INIT_TYPE";
                }
            }
            catch { return null; }
        }

        public int FieldPtrOffset(int typeId)
        {
            if (typeId < 0 || typeId >= Types.Length) return 0;
            if (_fieldPtrOffsets[typeId] != int.MinValue) return _fieldPtrOffsets[typeId];
            var t = Types[typeId];
            ulong vt = t.ManagedVt;
            if (vt == 0 && (t.Flags & 128) != 0)
            {
                int p = t.Parent;
                var seen = new HashSet<int>();
                while (p > 0 && p < Types.Length && seen.Add(p))
                {
                    if (Types[p].ManagedVt != 0) { vt = Types[p].ManagedVt; break; }
                    p = Types[p].Parent;
                }
            }
            int result = 0;
            if (vt >= 8 && Memory.TryRead(vt - 8, 4, out var b))
            {
                int candidate = I32(b, 0);
                if (candidate > -0x10000 && candidate < 0x100000) result = candidate;
            }
            _fieldPtrOffsets[typeId] = result;
            return result;
        }

        private bool TryDiscoverEncodedBase(out ulong result)
        {
            result = 0;
            long moduleStart = (long)Memory.ModuleBase;
            long moduleEnd = (long)(Memory.ModuleBase + Memory.ImageSize);
            var candidates = new Dictionary<ulong, int>();
            int observations = 0;

            foreach (var type in Types)
            {
                if (type.ManagedVt < 0x10000 || type.NumNativeVtable < 0) continue;
                if (!MethodsByOwner.TryGetValue(type.Id, out var methods)) continue;
                foreach (int id in methods)
                {
                    var method = GetMethod(id);
                    if (method.EncodedOffset == 0) continue;
                    MethodImpl(method.Impl, out short vtableIndex, out _, out _);
                    if (vtableIndex < 0) continue;

                    ulong slot = type.ManagedVt + (ulong)(type.NumNativeVtable + vtableIndex) * 8;
                    if (!Memory.TryRead(slot, 8, out var pointer)) continue;
                    ulong function = U64(pointer, 0);
                    long candidate = unchecked((long)function - method.EncodedOffset);
                    if (candidate < moduleStart || candidate >= moduleEnd) continue;

                    ulong address = (ulong)candidate;
                    candidates.TryGetValue(address, out int count);
                    candidates[address] = count + 1;
                    observations++;
                }
            }

            if (observations < 128 || candidates.Count == 0) return false;
            var winner = candidates.OrderByDescending(pair => pair.Value).First();
            if (winner.Value * 100L < observations * 95L) return false;
            result = winner.Key;
            return true;
        }

        public ulong MethodFunction(MethodRecord m)
        {
            if (!Layout.EncodedMethods) return m.DirectFunction;
            if (m.EncodedOffset == 0) return 0;
            return unchecked((ulong)((long)EncodedBase + m.EncodedOffset));
        }
    }

    internal sealed class RszEntry
    {
        public string Type = "", Code = "", PotentialName = "";
        public int CodeId, Align, Depth;
        public uint Size, Offset;
        public bool Array, Static;
    }

    internal sealed class DeserializerEntry { public ulong Address; public string Name = ""; }
    internal sealed class ReflectionParam { public string Type = "", Name = ""; public uint TypeIndex; }
    internal sealed class ReflectionMethod
    {
        public ulong Function; public string Returns = ""; public uint TypeIndex;
        public List<ReflectionParam> Params = new List<ReflectionParam>();
    }
    internal sealed class ReflectionProperty { public ulong Getter; public string Type = ""; public int Order; }
    internal sealed class RuntimeData
    {
        public uint? Crc;
        public string NativeName = "";
        public List<RszEntry>? Rsz;
        public List<DeserializerEntry>? Deserializers;
        public SortedDictionary<string, ReflectionMethod>? ReflectionMethods;
        public SortedDictionary<string, ReflectionProperty>? ReflectionProperties;
    }

    internal sealed class NativeTypeRecord
    {
        public ulong Address, Fields, Super, Child, Chain;
        public uint Fqn, Crc, Size;
        public string Name = "";
    }

    internal sealed class TypeListCandidate
    {
        public ulong Address, Data;
        public int Size, Capacity, Score;
    }

    internal sealed class RuntimeLayout
    {
        public readonly string Name;
        public readonly int RecordSize, Size, Crc, Super, Child, Chain, Fields, ClassInfo, Rsz;

        private RuntimeLayout(
            string name, int recordSize, int size, int crc, int super, int child,
            int chain, int fields, int classInfo, int rsz)
        {
            Name = name; RecordSize = recordSize; Size = size; Crc = crc;
            Super = super; Child = child; Chain = chain; Fields = fields;
            ClassInfo = classInfo; Rsz = rsz;
        }

        private static readonly RuntimeLayout[] Candidates =
        {
            new RuntimeLayout("standard", 0x60, 0x2C, 0x30, 0x38, 0x40, 0x48, 0x50, 0x58, 0x60),
            new RuntimeLayout("standard-reordered", 0x60, 0x30, 0x2C, 0x38, 0x40, 0x48, 0x50, 0x58, 0x60),
            new RuntimeLayout("shifted", 0x68, 0x2C, 0x30, 0x40, 0x48, 0x50, 0x58, 0x60, 0x68),
            new RuntimeLayout("shifted-reordered", 0x68, 0x30, 0x2C, 0x40, 0x48, 0x50, 0x58, 0x60, 0x68)
        };

        private static bool Pointer(ulong value) => value >= 0x10000 && value < 0x0000800000000000UL;

        internal static bool ValidTypeName(string value)
        {
            if (String.IsNullOrEmpty(value) || value.Length > 4096 || Char.IsWhiteSpace(value[0]) || Char.IsWhiteSpace(value[value.Length - 1])) return false;
            foreach (char c in value) if (Char.IsControl(c) || c == '\uFFFD') return false;
            return true;
        }

        public static RuntimeLayout Detect(CoreDatabase db)
        {
            RuntimeLayout? best = null;
            double bestScore = Double.NegativeInfinity;
            double secondScore = Double.NegativeInfinity;
            int bestValid = 0;
            int step = Math.Max(1, db.Types.Length / 1024);

            foreach (var layout in Candidates)
            {
                long score = 0;
                int valid = 0;
                for (int id = 1; id < db.Types.Length && valid < 768; id += step)
                {
                    var type = db.Types[id];
                    if (!Pointer(type.RuntimeType) || !db.Memory.TryRead(type.RuntimeType, layout.RecordSize, out var raw)) continue;
                    ulong namePointer = PeImage.U64(raw, 0x20);
                    if (!Pointer(namePointer)) continue;
                    string name = db.Memory.ReadCString(namePointer, 4096);
                    if (!ValidTypeName(name) || PeImage.U32(raw, 8) != type.Fqn) continue;
                    valid++;
                    score += 20;
                    if (PeImage.U32(raw, layout.Size) == type.Size) score += 5;
                    if (PeImage.U32(raw, layout.Crc) == type.RawCrc) score += 3;

                    ulong super = PeImage.U64(raw, layout.Super);
                    if (type.Parent > 0 && type.Parent < db.Types.Length && Pointer(db.Types[type.Parent].RuntimeType))
                        score += super == db.Types[type.Parent].RuntimeType ? 14 : -10;
                    else
                        score += super == 0 ? 2 : -1;

                    ulong fields = PeImage.U64(raw, layout.Fields);
                    score += fields == 0 || Pointer(fields) ? 1 : -5;
                }

                if (valid < 32) continue;
                double normalized = (double)score / valid;
                if (normalized > bestScore)
                {
                    secondScore = bestScore;
                    best = layout;
                    bestScore = normalized;
                    bestValid = valid;
                }
                else if (normalized > secondScore) secondScore = normalized;
            }

            if (best == null)
                throw new InvalidDataException("Could not derive a supported live REType layout.");
            if (bestScore - secondScore < 0.5)
                throw new InvalidDataException("The live REType layout is ambiguous; refusing to guess.");
            Console.WriteLine($"Runtime REType layout: {best.Name} ({bestValid:N0} validated samples)");
            return best;
        }
    }

    internal sealed class Dumper
    {
        private readonly CoreDatabase _db;
        private readonly Options _options;
        private readonly RuntimeLayout? _runtime;
        private RuntimeLayout Runtime => _runtime ?? throw new InvalidOperationException("Runtime metadata was disabled by -CoreOnly.");
        private readonly Dictionary<ulong, byte[]> _runtimeTypeCache = new Dictionary<ulong, byte[]>();
        private readonly Dictionary<string, NativeTypeRecord> _nativeTypes = new Dictionary<string, NativeTypeRecord>(StringComparer.Ordinal);
        private static readonly string[] TypeCodes = new[]
        {
            "Undefined","Object","Action","Struct","NativeObject","Resource","UserData","Bool","C8","C16","S8","U8","S16","U16","S32","U32","S64","U64","F32","F64","String","MBString","Enum","Uint2","Uint3","Uint4","Int2","Int3","Int4","Float2","Float3","Float4","Float3x3","Float3x4","Float4x3","Float4x4","Half2","Half4","Mat3","Mat4","Vec2","Vec3","Vec4","VecU4","Quaternion","Guid","Color","DateTime","AABB","Capsule","TaperedCapsule","Cone","Line","LineSegment","OBB","Plane","PlaneXZ","Point","Range","RangeI","Ray","RayY","Segment","Size","Sphere","Triangle","Cylinder","Ellipsoid","Area","Torus","Rect","Rect3D","Frustum","KeyFrame","Uri","GameObjectRef","RuntimeType","Sfix","Sfix2","Sfix3","Sfix4","Position","F16","Decimal","End"
        };
        private static readonly int[] FunctionReturnTypeNameOffsets = { 0x28, 0x30 };

        public Dumper(CoreDatabase db, Options options)
        {
            _db = db;
            _options = options;
            if (!options.CoreOnly) _runtime = RuntimeLayout.Detect(db);
        }

        private static ulong Q(byte[] b, int o) => PeImage.U64(b, o);
        private static uint D(byte[] b, int o) => PeImage.U32(b, o);
        private static int SD(byte[] b, int o) => PeImage.I32(b, o);

        private bool Ptr(ulong p) => p >= 0x10000 && p < 0x0000800000000000UL;

        private bool TryReadRuntimeType(ulong address, out byte[] data)
        {
            if (_runtimeTypeCache.TryGetValue(address, out var cached)) { data = cached; return true; }
            if (!Ptr(address) || !_db.Memory.TryRead(address, Runtime.RecordSize, out var read)) { data = Array.Empty<byte>(); return false; }
            data = read;
            _runtimeTypeCache[address] = data;
            return true;
        }

        private int ValidateTypeList(ulong data, int capacity, HashSet<ulong> known)
        {
            const int Block = 131072;
            var matches = new HashSet<ulong>();
            int invalid = 0;
            for (int start = 0; start < capacity; start += Block)
            {
                int count = Math.Min(Block, capacity - start);
                if (!_db.Memory.TryRead(data + (ulong)start * 8, count * 8, out var pointers)) return -1;
                for (int i = 0; i < count; i++)
                {
                    ulong p = Q(pointers, i * 8);
                    if (p == 0) continue;
                    if (!Ptr(p) || (p & 7) != 0) { invalid++; continue; }
                    if (known.Contains(p)) matches.Add(p);
                }
            }
            if (matches.Count < Math.Min(known.Count, 1024)) return -1;
            return checked(matches.Count * 1000 - Math.Min(invalid, 999));
        }

        private TypeListCandidate? FindTypeList(HashSet<ulong> known)
        {
            var live = _db.Memory;
            TypeListCandidate? best = null;

            void Consider(ulong rva)
            {
                ulong address = live.ModuleBase + rva;
                if (!_db.Memory.TryRead(address, 0x10, out var header)) return;
                ulong data = Q(header, 0);
                int size = SD(header, 8), capacity = SD(header, 12);
                if (!Ptr(data) || (data & 7) != 0 || size < 100 || capacity < size || capacity > 2000000) return;
                int score = ValidateTypeList(data, capacity, known);
                if (score < 0 || (best != null && score <= best.Score)) return;
                best = new TypeListCandidate { Address = address, Data = data, Size = size, Capacity = capacity, Score = score };
            }

            foreach (ulong rva in _options.TypeListCandidateRvas.Distinct()) Consider(rva);
            return best;
        }

        private void EnqueueType(ulong address, Queue<ulong> queue, HashSet<ulong> queued)
        {
            if (Ptr(address) && (address & 7) == 0 && queued.Add(address)) queue.Enqueue(address);
        }

        private void DiscoverNativeTypes()
        {
            if (_options.CoreOnly) return;
            Console.WriteLine("Discovering the live native type registry...");
            var known = new HashSet<ulong>(_db.Types.Select(t => t.RuntimeType).Where(p => Ptr(p) && (p & 7) == 0));
            var queue = new Queue<ulong>();
            var queued = new HashSet<ulong>();
            foreach (ulong p in known) EnqueueType(p, queue, queued);

            var typeList = FindTypeList(known)
                ?? throw new InvalidDataException("Could not derive and validate the live native TypeList.");
            Console.WriteLine($"TypeList: RVA 0x{typeList.Address - _db.Memory.ModuleBase:x}, {typeList.Size:N0}/{typeList.Capacity:N0} slots");
            const int PointersPerBlock = 131072;
            for (int start = 0; start < typeList.Capacity; start += PointersPerBlock)
            {
                int count = Math.Min(PointersPerBlock, typeList.Capacity - start);
                var pointers = _db.Memory.Read(typeList.Data + (ulong)start * 8, count * 8);
                for (int i = 0; i < count; i++) EnqueueType(Q(pointers, i * 8), queue, queued);
            }

            var seen = new HashSet<ulong>();
            while (queue.Count != 0 && seen.Count < 2000000)
            {
                ulong address = queue.Dequeue();
                if (!seen.Add(address) || !TryReadRuntimeType(address, out var raw)) continue;
                ulong namePointer = Q(raw, 0x20);
                string name = Ptr(namePointer) ? _db.Memory.ReadCString(namePointer, 4096) : "";
                if (!RuntimeLayout.ValidTypeName(name)) continue;
                uint fqn = D(raw, 8);
                uint crc = D(raw, Runtime.Crc);
                bool validFqn = fqn == CoreDatabase.TypeNameHash(name) || (name == "TypeInfoNone" && fqn == 0x08F7E7AEU);
                if (!known.Contains(address) && !validFqn) continue;
                uint size = D(raw, Runtime.Size);
                if (size > 0x10000000U) continue;
                var record = new NativeTypeRecord
                {
                    Address = address,
                    Name = name,
                    Fqn = fqn,
                    Crc = crc,
                    Size = size,
                    Super = Q(raw, Runtime.Super),
                    Child = Q(raw, Runtime.Child),
                    Chain = Q(raw, Runtime.Chain),
                    Fields = Q(raw, Runtime.Fields)
                };
                if (!_nativeTypes.TryGetValue(name, out var existing) || (!Ptr(existing.Fields) && Ptr(record.Fields)))
                    _nativeTypes[name] = record;
                EnqueueType(record.Super, queue, queued);
                EnqueueType(record.Child, queue, queued);
                EnqueueType(record.Chain, queue, queued);
                if ((seen.Count % 25000) == 0) Console.Write($"\rNative types visited: {seen.Count:N0}");
            }
            Console.WriteLine($"\rResolved {_nativeTypes.Count:N0} unique native type names ({seen.Count:N0} objects visited).     ");
        }

        private int TypeIdFromPointer(ulong p)
        {
            if (p >= _db.TypesAddress && p < _db.TypesAddress + (ulong)_db.Types.Length * (ulong)_db.TypeStride)
                return (int)((p - _db.TypesAddress) / (ulong)_db.TypeStride);
            if (_db.Memory.TryRead(p, 8, out var b))
            {
                int id = (int)(Q(b, 0) & _db.Layout.TypeMask);
                if (id >= 0 && id < _db.Types.Length) return id;
            }
            return -1;
        }

        private string RuntimeTypeName(ulong reType)
        {
            if (!Ptr(reType) || !_db.Memory.TryRead(reType + 0x20, 8, out var p)) return "";
            return _db.Memory.ReadCString(Q(p, 0));
        }

        private string RuntimeChainName(ulong reType)
        {
            if (!_db.Memory.TryRead(reType + (ulong)Runtime.ClassInfo, 8, out var c)) return RuntimeTypeName(reType);
            ulong classInfo = Q(c, 0);
            if (Ptr(classInfo))
            {
                int id = TypeIdFromPointer(classInfo);
                if (id >= 0) return _db.Types[id].FullName;
            }
            return RuntimeTypeName(reType);
        }

        private List<DeserializerEntry>? DeserializerChain(ulong reType)
        {
            var reverse = new List<DeserializerEntry>();
            var seen = new HashSet<ulong>();
            ulong at = reType;
            while (Ptr(at) && seen.Add(at) && seen.Count <= 128)
            {
                if (_db.Memory.TryRead(at + (ulong)Runtime.Fields, 8, out var fp))
                {
                    ulong fields = Q(fp, 0);
                    if (Ptr(fields) && _db.Memory.TryRead(fields + 0x28, 8, out var dp))
                    {
                        ulong des = Q(dp, 0);
                        if (Ptr(des)) reverse.Add(new DeserializerEntry { Address = _db.Memory.Normalize(des), Name = RuntimeChainName(at) });
                    }
                }
                if (!_db.Memory.TryRead(at + (ulong)Runtime.Super, 8, out var sp)) break;
                at = Q(sp, 0);
            }
            if (reverse.Count == 0) return null;
            reverse.Reverse();
            return reverse;
        }

        private string PotentialRszName(int typeId, int depth, uint offset, bool isStatic)
        {
            int at = typeId;
            int adjustment = 0;
            for (int d = 0; d < depth; d++)
            {
                if (at <= 0 || at >= _db.Types.Length) break;
                int parent = _db.Types[at].Parent;
                if (parent <= 0 || parent >= _db.Types.Length) break;
                int a = _db.FieldPtrOffset(at), b = _db.FieldPtrOffset(parent);
                adjustment += a - b;
                at = parent;
            }
            var t = _db.Types[at];
            if (!_db.FieldsByOwner.TryGetValue(at, out var ownedFields)) return "";
            foreach (int id in ownedFields)
            {
                var f = _db.GetField(id);
                _db.FieldImpl(f, out string name, out ushort flags, out int fieldOffset, out _);
                if (((flags & 16) != 0) != isStatic) continue;
                if ((long)fieldOffset + adjustment == offset) return name;
            }
            return "";
        }

        private List<RszEntry>? Rsz(int typeId, ulong reType)
        {
            if (!_db.Memory.TryRead(reType + 0x0C, 4, out var flagsBytes)) return null;
            int flags = PeImage.I32(flagsBytes, 0);
            if ((flags & 0x20) == 0) return null;
            if (!_db.Memory.TryRead(reType + (ulong)Runtime.Rsz, 0x10, out var list)) return null;
            ulong data = Q(list, 0);
            int count = SD(list, 8);
            if (!Ptr(data) || count <= 0 || count > 16384) return null;
            if (!_db.Memory.TryRead(data, checked(count * 0x10), out var seqs)) return null;
            var result = new List<RszEntry>(count);
            for (int i = 0; i < count; i++)
            {
                int o = i * 0x10;
                uint bits = D(seqs, o), offset = D(seqs, o + 4);
                ulong native = Q(seqs, o + 8);
                int code = (int)(bits & 0xFF), size = (int)((bits >> 8) & 0xFF), align = (int)((bits >> 16) & 0xFF), depth = (int)((bits >> 24) & 0x3F);
                bool array = (bits & 0x40000000) != 0, stat = (bits & 0x80000000) != 0;
                int nativeId = TypeIdFromPointer(native);
                var e = new RszEntry
                {
                    Type = nativeId >= 0 ? _db.Types[nativeId].FullName : "",
                    Code = code >= 0 && code < TypeCodes.Length ? TypeCodes[code] : code.ToString(CultureInfo.InvariantCulture),
                    CodeId = code, Size = (uint)size, Align = align, Depth = depth, Array = array, Static = stat, Offset = offset
                };
                e.PotentialName = PotentialRszName(typeId, depth, offset, stat);
                result.Add(e);
            }
            return result.Count == 0 ? null : result;
        }

        private SortedDictionary<string, ReflectionMethod>? ReflectionMethods(ulong fields)
        {
            if (!_db.Memory.TryRead(fields + 0x10, 8, out var methodBytes) ||
                !_db.Memory.TryRead(fields + 0x18, 4, out var countBytes)) return null;
            ulong methods = Q(methodBytes, 0);
            int count = SD(countBytes, 0);
            if (!Ptr(methods) || count <= 0 || count > 4000) return null;
            var result = new SortedDictionary<string, ReflectionMethod>(StringComparer.Ordinal);
            if (!_db.Memory.TryRead(methods, count * 8, out var tops)) return null;
            for (int i = 0; i < count; i++)
            {
                try
                {
                    ulong top = Q(tops, i * 8);
                    if (!Ptr(top) || !_db.Memory.TryRead(top, 8, out var hp)) continue;
                    ulong holder = Q(hp, 0);
                    if (!Ptr(holder) || !_db.Memory.TryRead(holder, 8, out var dp)) continue;
                    ulong descriptor = Q(dp, 0);
                    int descriptorSize = FunctionReturnTypeNameOffsets.Max() + 8;
                    if (!Ptr(descriptor) || !_db.Memory.TryRead(descriptor, descriptorSize, out var d)) continue;
                    string name = _db.Memory.ReadCString(Q(d, 0));
                    ulong function = Q(d, 0x18);
                    if (String.IsNullOrEmpty(name) || !Ptr(function)) continue;
                    int numParams = SD(d, 0x14);
                    ulong paramsPtr = Q(d, 8);
                    uint typeIndex = D(d, 0x24);
                    string ret = typeIndex > 0 && typeIndex < _db.Types.Length ? _db.Types[typeIndex].FullName : "";
                    if (ret.Length == 0)
                    {
                        foreach (int offset in FunctionReturnTypeNameOffsets)
                        {
                            string candidate = _db.Memory.ReadCString(Q(d, offset), 4096);
                            if (RuntimeLayout.ValidTypeName(candidate)) { ret = candidate; break; }
                        }
                    }
                    var rm = new ReflectionMethod { Function = _db.Memory.Normalize(function), Returns = ret, TypeIndex = typeIndex };
                    if (Ptr(paramsPtr) && numParams > 0 && numParams <= 256 && _db.Memory.TryRead(paramsPtr, numParams * 0x20, out var ps))
                    {
                        for (int p = 0; p < numParams; p++)
                        {
                            int po = p * 0x20;
                            uint pi = D(ps, po + 0x14);
                            rm.Params.Add(new ReflectionParam
                            {
                                Name = _db.Memory.ReadCString(Q(ps, po + 8)),
                                TypeIndex = pi,
                                Type = pi > 0 && pi < _db.Types.Length ? _db.Types[pi].FullName : _db.Memory.ReadCString(Q(ps, po + 0x18))
                            });
                        }
                    }
                    result[name] = rm;
                }
                catch { }
            }
            return result.Count == 0 ? null : result;
        }

        private SortedDictionary<string, ReflectionProperty>? ReflectionProperties(ulong fields)
        {
            if (!_db.Memory.TryRead(fields + 0x20, 8, out var vp)) return null;
            ulong vars = Q(vp, 0);
            if (!Ptr(vars) || !_db.Memory.TryRead(vars + 8, 0x0C, out var v)) return null;
            ulong data = Q(v, 0);
            int count = SD(v, 8);
            if (!Ptr(data) || count <= 0 || count > 65536 || !_db.Memory.TryRead(data, count * 8, out var ptrs)) return null;
            var result = new SortedDictionary<string, ReflectionProperty>(StringComparer.Ordinal);
            int order = 0;
            for (int i = 0; i < count; i++)
            {
                ulong descriptor = Q(ptrs, i * 8);
                if (!Ptr(descriptor) || !_db.Memory.TryRead(descriptor, 0x48, out var d)) continue;
                string name = _db.Memory.ReadCString(Q(d, 0));
                if (String.IsNullOrEmpty(name)) continue;
                ulong getter = Q(d, 0x10);
                uint fqn = D(d, 0x1C);
                string type = fqn != 0 && _db.FqnToType.TryGetValue(fqn, out int typeId) ? _db.Types[typeId].FullName : _db.Memory.ReadCString(Q(d, 0x20));
                result[name] = new ReflectionProperty { Getter = _db.Memory.Normalize(getter), Type = type, Order = order++ };
            }
            return result.Count == 0 ? null : result;
        }

        private RuntimeData? ReadCoreRuntime(int typeId)
        {
            if (_options.CoreOnly) return null;
            var t = _db.Types[typeId];
            ulong reType = t.RuntimeType;
            if (!TryReadRuntimeType(reType, out var r)) return null;
            var data = new RuntimeData();
            data.Crc = D(r, Runtime.Crc);
            data.NativeName = _db.Memory.ReadCString(Q(r, 0x20));
            data.Rsz = Rsz(typeId, reType);
            if (data.Rsz == null) data.Deserializers = DeserializerChain(reType);
            return data;
        }

        private RuntimeData ReadNativeRuntime(NativeTypeRecord native, bool includeDeserializer)
        {
            var data = new RuntimeData();
            if (includeDeserializer) data.Deserializers = DeserializerChain(native.Address);
            if (!Ptr(native.Fields) || native.Name.IndexOfAny(new[] { '`', '<', '>' }) >= 0) return data;
            data.ReflectionMethods = ReflectionMethods(native.Fields);
            data.ReflectionProperties = ReflectionProperties(native.Fields);
            return data;
        }

        private static string Hex(ulong value) => value.ToString("x", CultureInfo.InvariantCulture);
        private static string Hex0(ulong value) => "0x" + Hex(value);

        private static string Flags(ulong value, (ulong, string)[] names)
        {
            var result = new List<string>();
            foreach (var item in names) if ((value & item.Item1) != 0) result.Add(item.Item2);
            return String.Join(" | ", result);
        }

        private static readonly (ulong, string)[] TypeFlagNames = {
            (1,"Public"),(2,"NestedPublic"),(4,"NestedFamily"),(8,"SequentialLayout"),(16,"ExplicitLayout"),(32,"Interface"),(128,"Abstract"),(256,"Sealed"),(1024,"SpecialName"),(2048,"RTSpecialName"),(4096,"Import"),(8192,"Serializable"),(16384,"WindowsRuntime"),(65536,"UnicodeClass"),(131072,"AutoClass"),(262144,"HasSecurity"),(1048576,"BeforeFieldInit"),(16777216,"LocalHeap"),(33554432,"Finalize"),(67108864,"NativeType"),(134217728,"ContainsGenericParameters"),(268435456,"NativeCtor"),(536870912,"Constracted"),(1073741824,"ManagedVTable") };
        private static readonly (ulong, string)[] MethodFlagNames = {
            (1,"Private"),(2,"FamANDAssem"),(4,"Family"),(8,"UnmanagedExp"),(16,"Static"),(32,"Final"),(64,"Virtual"),(128,"HideBySig"),(256,"NewSlot"),(1024,"Abstract"),(2048,"SpecialName"),(4096,"RTSpecialName"),(8192,"PinvokeImpl"),(16384,"NoILAsmKeyword"),(32768,"ReqsecObj") };
        private static readonly (ulong, string)[] MethodImplFlagNames = {
            (1,"Native"),(2,"OPTIL"),(4,"Unmanaged"),(8,"NoInlining"),(16,"ForwardRef"),(32,"Synchronized"),(64,"NoOptimization"),(128,"PreserveSig"),(256,"AggressiveInlining"),(512,"HasRetVal"),(1024,"ExposeMember"),(2048,"EmptyCtor"),(4096,"InternalCall"),(8192,"ContainsGenericParameters"),(16384,"HasThis"),(32768,"Break") };
        private static readonly (ulong, string)[] FieldFlagNames = {
            (1,"Private"),(2,"FamANDAssem"),(4,"Family"),(16,"Static"),(32,"InitOnly"),(64,"Literal"),(128,"NotSerialized"),(256,"HasFieldRVA"),(512,"SpecialName"),(1024,"RTSpecialName"),(2048,"Pointer"),(4096,"HasFieldMarshal"),(8192,"PInvokeImpl"),(16384,"ExposeMember"),(32768,"HasDefault") };
        private static readonly (ulong, string)[] ParamFlagNames = {
            (1,"IN_"),(2,"Out"),(16,"Optional"),(4096,"HasDefault"),(8192,"HasFieldMarshal"),(16384,"ByRef"),(32768,"Ptr") };
        private static readonly (ulong, string)[] ParamModifierNames = { (1,"Ptr"),(2,"ByRef") };

        private void WriteParam(Utf8JsonWriter w, ParameterRecord parameter)
        {
            w.WriteStartObject();
            string fs = Flags(parameter.Flags, ParamFlagNames); if (fs.Length != 0) w.WriteString("flags", fs);
            string ms = Flags((uint)parameter.Modifier, ParamModifierNames); if (ms.Length != 0) w.WriteString("modifier", ms);
            w.WriteString("name", parameter.Name);
            w.WriteString("type", parameter.Type >= 0 && parameter.Type < _db.Types.Length ? _db.Types[parameter.Type].FullName : "");
            w.WriteEndObject();
        }

        private void WriteMethods(Utf8JsonWriter w, List<int> typeIds)
        {
            var methods = new SortedDictionary<string, int>(StringComparer.Ordinal);
            foreach (int typeId in typeIds)
            {
                if (_db.MethodsByOwner.TryGetValue(typeId, out var ownedMethods))
                    foreach (int id in ownedMethods) methods[_db.MethodName(id) + id.ToString(CultureInfo.InvariantCulture)] = id;
            }
            if (methods.Count == 0) return;
            w.WritePropertyName("methods"); w.WriteStartObject();
            foreach (var pair in methods)
            {
                int id = pair.Value; var m = _db.GetMethod(id);
                _db.MethodImpl(m.Impl, out short vtable, out ushort flags, out ushort implFlags);
                w.WritePropertyName(pair.Key); w.WriteStartObject();
                string fs = Flags(flags, MethodFlagNames); if (fs.Length != 0) w.WriteString("flags", fs);
                w.WriteString("function", Hex(_db.Memory.Normalize(_db.MethodFunction(m))));
                w.WriteNumber("id", id);
                string ifs = Flags(implFlags, MethodImplFlagNames); if (ifs.Length != 0) w.WriteString("impl_flags", ifs);
                if (_db.TryMethodParameters(m, out int invoke, out ParameterRecord returns, out ParameterRecord[] parameters))
                {
                    w.WriteNumber("invoke_id", invoke);
                    if (parameters.Length > 0)
                    {
                        w.WritePropertyName("params"); w.WriteStartArray();
                        foreach (var parameter in parameters) WriteParam(w, parameter);
                        w.WriteEndArray();
                    }
                    w.WritePropertyName("returns"); WriteParam(w, returns);
                }
                else
                {
                    w.WriteNumber("invoke_id", 0);
                    w.WritePropertyName("returns"); w.WriteStartObject(); w.WriteString("name", ""); w.WriteString("type", ""); w.WriteEndObject();
                }
                if (vtable >= 0) w.WriteNumber("vtable_index", vtable);
                w.WriteEndObject();
            }
            w.WriteEndObject();
        }

        private void WriteFields(Utf8JsonWriter w, List<int> typeIds)
        {
            var fields = new SortedDictionary<string, int>(StringComparer.Ordinal);
            foreach (int typeId in typeIds)
            {
                if (_db.FieldsByOwner.TryGetValue(typeId, out var ownedFields)) foreach (int id in ownedFields)
                {
                    var f = _db.GetField(id); _db.FieldImpl(f, out string name, out _, out _, out _); fields[name] = id;
                }
            }
            if (fields.Count == 0) return;
            w.WritePropertyName("fields"); w.WriteStartObject();
            foreach (var pair in fields)
            {
                int id = pair.Value; var f = _db.GetField(id);
                _db.FieldImpl(f, out _, out ushort flags, out int offset, out int initIndex);
                int fieldBase = f.Owner >= 0 && f.Owner < _db.Types.Length ? _db.FieldPtrOffset(f.Owner) : 0;
                w.WritePropertyName(pair.Key); w.WriteStartObject();
                object? def = _db.DefaultValue(initIndex, f.Type);
                if (def != null)
                {
                    w.WritePropertyName("default");
                    if (def is bool bo) w.WriteBooleanValue(bo);
                    else if (def is string st) w.WriteStringValue(st);
                    else if (def is float fl) { if (Single.IsFinite(fl)) w.WriteNumberValue(fl); else w.WriteNullValue(); }
                    else if (def is double db) { if (Double.IsFinite(db)) w.WriteNumberValue(db); else w.WriteNullValue(); }
                    else if (def is int si) w.WriteNumberValue(si);
                    else if (def is uint ui) w.WriteNumberValue(ui);
                    else if (def is long sl) w.WriteNumberValue(sl);
                    else if (def is ulong ul) w.WriteNumberValue(ul);
                    else w.WriteStringValue(def.ToString());
                }
                string fs = Flags(flags, FieldFlagNames); if (fs.Length != 0) w.WriteString("flags", fs);
                w.WriteNumber("id", id);
                w.WriteNumber("init_data_index", initIndex);
                w.WriteString("offset_from_base", Hex0(unchecked((ulong)(long)(fieldBase + offset))));
                w.WriteString("offset_from_fieldptr", Hex0((uint)offset));
                w.WriteString("type", f.Type >= 0 && f.Type < _db.Types.Length ? _db.Types[f.Type].FullName : "");
                w.WriteEndObject();
            }
            w.WriteEndObject();
        }

        private void WriteProperties(Utf8JsonWriter w, List<int> typeIds)
        {
            var props = new SortedDictionary<string, int>(StringComparer.Ordinal);
            foreach (int typeId in typeIds)
                if (_db.PropertiesByOwner.TryGetValue(typeId, out var list)) foreach (int id in list) props[_db.PropertyName(id)] = id;
            if (props.Count == 0) return;
            w.WritePropertyName("properties"); w.WriteStartObject();
            foreach (var pair in props)
            {
                int id = pair.Value; var p = _db.GetProperty(id);
                w.WritePropertyName(pair.Key); w.WriteStartObject();
                w.WriteString("getter", p.Getter >= 0 && p.Getter < _db.NumMethods ? _db.MethodName(p.Getter) : "");
                w.WriteNumber("id", id);
                w.WriteString("setter", p.Setter >= 0 && p.Setter < _db.NumMethods ? _db.MethodName(p.Setter) : "");
                w.WriteEndObject();
            }
            w.WriteEndObject();
        }

        private void WriteRuntime(Utf8JsonWriter w, RuntimeData? runtime)
        {
            if (runtime == null) return;
            if (runtime.Deserializers != null)
            {
                w.WritePropertyName("deserializer_chain"); w.WriteStartArray();
                foreach (var d in runtime.Deserializers) { w.WriteStartObject(); w.WriteString("address", Hex0(d.Address)); w.WriteString("name", d.Name); w.WriteEndObject(); }
                w.WriteEndArray();
            }
            if (runtime.ReflectionMethods != null)
            {
                w.WritePropertyName("reflection_methods"); w.WriteStartObject();
                foreach (var p in runtime.ReflectionMethods)
                {
                    var m = p.Value; w.WritePropertyName(p.Key); w.WriteStartObject();
                    w.WriteString("function", Hex0(m.Function));
                    w.WritePropertyName("params");
                    if (m.Params.Count == 0) w.WriteNullValue();
                    else
                    {
                        w.WriteStartArray();
                        foreach (var a in m.Params) { w.WriteStartObject(); w.WriteString("name", a.Name); w.WriteString("type", a.Type); w.WriteNumber("typeindex", a.TypeIndex); w.WriteEndObject(); }
                        w.WriteEndArray();
                    }
                    w.WriteString("returns", m.Returns); w.WriteNumber("typeindex", m.TypeIndex); w.WriteEndObject();
                }
                w.WriteEndObject();
            }
            if (runtime.ReflectionProperties != null)
            {
                w.WritePropertyName("reflection_properties"); w.WriteStartObject();
                foreach (var p in runtime.ReflectionProperties)
                {
                    w.WritePropertyName(p.Key); w.WriteStartObject();
                    w.WriteString("getter", Hex0(p.Value.Getter)); w.WriteNumber("order", p.Value.Order); w.WriteString("type", p.Value.Type); w.WriteEndObject();
                }
                w.WriteEndObject();
            }
            if (runtime.Rsz != null)
            {
                w.WritePropertyName("RSZ"); w.WriteStartArray();
                foreach (var e in runtime.Rsz)
                {
                    w.WriteStartObject(); w.WriteNumber("align", e.Align); w.WriteBoolean("array", e.Array); w.WriteString("code", e.Code); w.WriteNumber("code_id", e.CodeId); w.WriteNumber("depth", e.Depth); w.WriteString("offset_from_fieldptr", Hex0(e.Offset));
                    if (e.PotentialName.Length != 0) w.WriteString("potential_name", e.PotentialName);
                    w.WriteString("size", Hex0(e.Size)); w.WriteBoolean("static", e.Static); w.WriteString("type", e.Type); w.WriteEndObject();
                }
                w.WriteEndArray();
            }
        }

        public void Run()
        {
            int idLimit = _options.MaxTypes > 0 ? Math.Min(_options.MaxTypes, _db.Types.Length) : _db.Types.Length;
            var names = new SortedDictionary<string, List<int>>(StringComparer.Ordinal);
            bool fullSelection = _options.TypeIds.Length == 0 && _options.MaxTypes == 0;
            IEnumerable<int> selected = _options.TypeIds.Length > 0
                ? _options.TypeIds.Where(i => i >= 0 && i < _db.Types.Length).Distinct()
                : Enumerable.Range(0, idLimit);
            foreach (int i in selected)
            {
                string name = _db.Types[i].FullName;
                if (!names.TryGetValue(name, out var ids)) names[name] = ids = new List<int>();
                ids.Add(i);
            }
            if (fullSelection) DiscoverNativeTypes();
            var outputNames = new SortedSet<string>(names.Keys, StringComparer.Ordinal);
            if (fullSelection) outputNames.UnionWith(_nativeTypes.Keys);
            if (fullSelection)
            {
                int nativeOnly = outputNames.Count - names.Count;
                Console.WriteLine($"Native registry added {nativeOnly:N0} non-TDB type names.");
                if (nativeOnly == 0) throw new InvalidDataException("The validated TypeList contained no non-TDB native registrations.");
            }
            string partial = _options.OutputPath + ".partial";
            using (var file = new FileStream(partial, FileMode.Create, FileAccess.Write, FileShare.Read, 1 << 20, FileOptions.SequentialScan))
            using (var writer = new Utf8JsonWriter(file, new JsonWriterOptions { Indented = !_options.Compact, SkipValidation = true, Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping }))
            {
                writer.WriteStartObject();
                int done = 0;
                foreach (string name in outputNames)
                {
                    names.TryGetValue(name, out var coreIds);
                    _nativeTypes.TryGetValue(name, out var native);
                    writer.WritePropertyName(name); writer.WriteStartObject();
                    RuntimeData? coreRuntime = null;
                    if (coreIds != null && coreIds.Count > 0)
                    {
                        int coreId = coreIds[coreIds.Count - 1];
                        var t = _db.Types[coreId];
                        coreRuntime = ReadCoreRuntime(coreId);
                        writer.WriteString("address", Hex(_db.Memory.Normalize(_db.TypesAddress + (ulong)coreId * (ulong)_db.TypeStride)));
                        writer.WriteString("crc", Hex(coreRuntime?.Crc ?? t.RawCrc));
                        if (t.Declaring > 0 && t.Declaring < _db.Types.Length) writer.WriteString("declaring_type", _db.Types[t.Declaring].FullName);
                        if (t.Element > 0 && t.Element < _db.Types.Length) writer.WriteString("element_type_name", _db.Types[t.Element].FullName);
                        WriteFields(writer, coreIds);
                        string tf = Flags(t.Flags, TypeFlagNames); if (tf.Length != 0) writer.WriteString("flags", tf);
                        writer.WriteString("fqn", Hex(t.Fqn));
                        if (t.GenericArguments.Length > 0)
                        {
                            writer.WritePropertyName("generic_arg_types"); writer.WriteStartArray();
                            foreach (int arg in t.GenericArguments)
                            {
                                writer.WriteStartObject(); writer.WriteString("type", arg > 0 && arg < _db.Types.Length ? _db.Types[arg].FullName : "unknown"); writer.WriteNumber("typeid", arg > 0 && arg < _db.Types.Length ? arg : 0); writer.WriteEndObject();
                            }
                            writer.WriteEndArray();
                        }
                        if (t.GenericDefinition > 0 && t.GenericDefinition < _db.Types.Length) writer.WriteString("generic_type_definition", _db.Types[t.GenericDefinition].FullName);
                        writer.WriteNumber("id", coreId);
                        writer.WriteBoolean("is_generic_type", t.GenericOffset > 0);
                        writer.WriteBoolean("is_generic_type_definition", t.GenericOffset > 0 && t.GenericDefinition == coreId);
                        WriteMethods(writer, coreIds);
                        writer.WritePropertyName("name_hierarchy"); writer.WriteStartArray(); foreach (string part in t.Hierarchy) writer.WriteStringValue(part); writer.WriteEndArray();
                        if (coreRuntime != null && coreRuntime.NativeName.Length != 0 && coreRuntime.NativeName != t.FullName) writer.WriteString("native_typename", coreRuntime.NativeName);
                        if (t.Parent > 0 && t.Parent < _db.Types.Length) writer.WriteString("parent", _db.Types[t.Parent].FullName);
                        WriteProperties(writer, coreIds);
                        WriteRuntime(writer, coreRuntime);
                        if (native != null)
                        {
                            bool needChain = coreRuntime == null || (coreRuntime.Rsz == null && coreRuntime.Deserializers == null);
                            WriteRuntime(writer, ReadNativeRuntime(native, needChain));
                        }
                        writer.WriteString("size", Hex(t.Size));
                    }
                    else if (native != null)
                    {
                        writer.WriteString("crc", Hex(native.Crc));
                        writer.WriteString("fqn", Hex(native.Fqn));
                        WriteRuntime(writer, ReadNativeRuntime(native, true));
                    }
                    writer.WriteEndObject();
                    done++;
                    if ((done % 1000) == 0) { writer.Flush(); Console.Write($"\rWriting types: {done:N0}/{outputNames.Count:N0}"); }
                }
                writer.WriteEndObject(); writer.Flush();
            }
            Console.WriteLine($"\rWriting types: {outputNames.Count:N0}/{outputNames.Count:N0}");
            File.Move(partial, _options.OutputPath, true);
            Console.WriteLine($"Dump complete: {_options.OutputPath}");
        }
    }

    public static class Entry
    {
        private static bool IsRunning(string executable)
        {
            var processes = Process.GetProcessesByName(Path.GetFileNameWithoutExtension(executable));
            try { return processes.Any(process => !process.HasExited); }
            finally { foreach (var process in processes) process.Dispose(); }
        }

        private static void ResolveGamePath(Options o)
        {
            if (!String.IsNullOrWhiteSpace(o.GamePath))
            {
                o.GamePath = Path.GetFullPath(o.GamePath);
                return;
            }

            var files = Directory.EnumerateFiles(o.SearchDirectory, "*.exe", SearchOption.TopDirectoryOnly)
                .Select(Path.GetFullPath).OrderBy(path => path, StringComparer.OrdinalIgnoreCase).ToList();
            if (o.ProcessId > 0)
            {
                try
                {
                    using var process = Process.GetProcessById(o.ProcessId);
                    string? runningPath = process.MainModule?.FileName;
                    var selected = files.SingleOrDefault(path => String.Equals(path, runningPath, StringComparison.OrdinalIgnoreCase));
                    if (selected != null) { o.GamePath = selected; Console.WriteLine($"Executable: {selected}"); return; }
                }
                catch { }
            }
            else
            {
                var running = files.Where(IsRunning).ToList();
                if (running.Count == 1) { o.GamePath = running[0]; Console.WriteLine($"Executable: {o.GamePath}"); return; }
                if (running.Count > 1)
                    throw new InvalidOperationException("Multiple executables in the script directory are running; specify -GamePath explicitly: " + String.Join(", ", running.Select(Path.GetFileName)));
            }

            var matches = new List<string>();
            foreach (string path in files)
            {
                try { using var pe = new PeImage(path); pe.FindTdbRva(); matches.Add(path); }
                catch { }
            }
            if (matches.Count == 0)
                throw new FileNotFoundException("No running executable or disk image with a supported RE Engine TDB was found. Start the game or specify -GamePath explicitly.");
            if (matches.Count > 1)
                throw new InvalidOperationException("Multiple RE Engine executables were found and none was uniquely running; specify -GamePath explicitly: " + String.Join(", ", matches.Select(Path.GetFileName)));
            o.GamePath = matches[0];
            Console.WriteLine($"Executable: {o.GamePath}");
        }

        private static Process WaitForGame(Options o)
        {
            string processName = Path.GetFileNameWithoutExtension(o.GamePath);
            var until = DateTime.UtcNow.AddSeconds(o.WaitSeconds);
            bool announced = false;
            while (true)
            {
                Process? p = null;
                if (o.ProcessId > 0)
                {
                    try { p = Process.GetProcessById(o.ProcessId); } catch { }
                }
                else p = Process.GetProcessesByName(processName).OrderBy(x => x.Id).FirstOrDefault();
                if (p != null && !p.HasExited) return p;
                if (DateTime.UtcNow >= until) throw new TimeoutException($"{Path.GetFileName(o.GamePath)} was not found. Start the game normally, wait for the title screen, and run the script again.");
                if (!announced) { Console.WriteLine($"Waiting for {Path.GetFileName(o.GamePath)}..."); announced = true; }
                Thread.Sleep(1000);
            }
        }

        private static void WaitForTdb(ProcessMemory memory, ulong address, TdbLayout layout, int seconds)
        {
            var until = DateTime.UtcNow.AddSeconds(seconds);
            bool announced = false;
            while (true)
            {
                try
                {
                    memory.ClearCache();
                    var h = memory.Read(address, 0xF0);
                    ulong types = PeImage.U64(h, layout.Types), methods = PeImage.U64(h, layout.Methods);
                    bool absolute = types >= memory.ModuleBase && types < memory.ModuleBase + memory.ImageSize && methods >= memory.ModuleBase && methods < memory.ModuleBase + memory.ImageSize;
                    if (PeImage.U32(h, 0) == 0x00424454 && PeImage.U32(h, 4) == layout.Version && absolute)
                    {
                        var sample = memory.Read(methods, 0x6000);
                        bool patched = false;
                        for (int i = layout.MethodFunctionOffset; i + 8 <= sample.Length; i += layout.MethodStride)
                        {
                            if (layout.EncodedMethods)
                            {
                                if (PeImage.I32(sample, i) != 0) { patched = true; break; }
                            }
                            else
                            {
                                ulong function = PeImage.U64(sample, i);
                                if (function >= memory.ModuleBase && function < memory.ModuleBase + memory.ImageSize) { patched = true; break; }
                            }
                        }
                        if (layout.Version < 69 && patched && PeImage.U32(h, 8) != 0) return;

                        var typeSample = memory.Read(types, layout.TypeStride * 256);
                        int runtimeOffset = layout.Version >= 71 ? 0x38 : 0x40;
                        int vtableOffset = layout.Version >= 71 ? 0x40 : 0x48;
                        int runtimeCount = 0, vtableCount = 0;
                        for (int i = 0; i < 256; i++)
                        {
                            int offset = i * layout.TypeStride;
                            ulong runtimeType = PeImage.U64(typeSample, offset + runtimeOffset);
                            ulong vtable = PeImage.U64(typeSample, offset + vtableOffset);
                            if (runtimeType >= 0x10000 && runtimeType < 0x0000800000000000UL) runtimeCount++;
                            if (vtable >= 0x10000 && vtable < 0x0000800000000000UL) vtableCount++;
                        }
                        if (patched && runtimeCount >= 8 && (!layout.EncodedMethods || vtableCount >= 4)) return;
                    }
                }
                catch { }
                if (DateTime.UtcNow >= until) throw new TimeoutException("The game's TDB did not finish initializing. Leave the game at the title screen and retry.");
                if (!announced) { Console.WriteLine("Game found; waiting for the runtime type database to initialize..."); announced = true; }
                Thread.Sleep(1000);
            }
        }

        private static void ValidateLiveExecutable(ProcessMemory memory, PeImage pe)
        {
            var dos = memory.Read(memory.ModuleBase, 0x40);
            int peOffset = PeImage.I32(dos, 0x3C);
            if (peOffset < 0x40 || peOffset > 0x1000) throw new InvalidDataException("The running process has an invalid PE header.");
            var headers = memory.Read(memory.ModuleBase + (ulong)peOffset, 0x60);
            uint timestamp = PeImage.U32(headers, 8);
            uint imageSize = PeImage.U32(headers, 0x18 + 0x38);
            if (timestamp != pe.TimeDateStamp || imageSize != pe.ImageSize)
                throw new InvalidDataException("The selected executable does not match the running process build.");
        }

        public static void Run(Options options)
        {
            ResolveGamePath(options);
            using var pe = new PeImage(options.GamePath);
            Console.WriteLine($"Executable build: PE timestamp 0x{pe.TimeDateStamp:x8}, image size 0x{pe.ImageSize:x}");
            ulong rva = 0;
            TdbLayout? layout = null;
            try
            {
                rva = pe.FindTdbRva();
                var staticHeader = pe.Read(pe.PreferredBase + rva, 8);
                layout = TdbLayout.For(PeImage.U32(staticHeader, 4));
                Console.WriteLine($"TDB {layout.Version} RVA: 0x{rva:x} (disk image)");
            }
            catch (InvalidDataException) { }

            using var process = WaitForGame(options);
            Console.WriteLine($"Attached read-only to PID {process.Id}.");
            using var live = new ProcessMemory(process, pe.PreferredBase, pe.ImageSize);
            ValidateLiveExecutable(live, pe);
            var scanner = new LiveImageScanner(live);
            if (layout == null)
            {
                Console.WriteLine("Scanning the running game image for its TDB...");
                rva = scanner.FindTdbRva();
                var liveHeader = live.Read(live.ModuleBase + rva, 8);
                layout = TdbLayout.For(PeImage.U32(liveHeader, 4));
                Console.WriteLine($"TDB {layout.Version} RVA: 0x{rva:x} (live image)");
            }
            ulong tdbAddress = live.ModuleBase + rva;
            WaitForTdb(live, tdbAddress, layout, options.WaitSeconds);

            if (!options.CoreOnly)
            {
                options.TypeListCandidateRvas = pe.FindTypeListRvas(layout.Version);
                string source = "disk";
                if (options.TypeListCandidateRvas.Length == 0)
                {
                    Console.WriteLine("Scanning the running game image for its TypeList references...");
                    options.TypeListCandidateRvas = scanner.FindTypeListRvas(layout.Version);
                    source = "live";
                }
                if (options.TypeListCandidateRvas.Length == 0)
                    throw new InvalidDataException("Could not derive any TypeList candidates from the disk or live game image.");
                Console.WriteLine($"TypeList anchor candidates: {options.TypeListCandidateRvas.Length} ({source} image)");
            }

            var liveDb = new CoreDatabase(live, tdbAddress);
            new Dumper(liveDb, options).Run();
        }
    }
}
'@

    Add-Type -TypeDefinition $csharp -Language CSharp
}

$options = [ReEngineStandaloneDump.Options]::new()
$options.GamePath = $GamePath
$options.SearchDirectory = $PSScriptRoot
$options.OutputPath = $OutputPath
$options.ProcessId = $ProcessId
$options.WaitSeconds = $WaitSeconds
$options.MaxTypes = $MaxTypes
$options.TypeIds = $TypeId
$options.CoreOnly = [bool]$CoreOnly
$options.Compact = [bool]$Compact

[ReEngineStandaloneDump.Entry]::Run($options)
