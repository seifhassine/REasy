#!/usr/bin/env python3

import argparse, json, os, struct, pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86_const import *

# --- small constants / helpers -------------------------------------------------

EXEC = 0x20000000           # thabet mba3d?
MAXS = 24                   # max accepted  string (extension name) length
s32  = lambda b,i: struct.unpack_from("<i", b, i)[0]

def map_pe(path):
    pe = pefile.PE(path, fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    secs = []
    for s in pe.sections:
        n  = s.Name.rstrip(b"\0").decode("latin-1","ignore").lower()
        va = base + s.VirtualAddress
        sz = max(s.SizeOfRawData, s.Misc_VirtualSize)
        execy = (s.Characteristics & EXEC) != 0 or n in (".text",".code",".xcode",".didata",".srdata",".data2")
        secs.append({"name": n, "va": va, "end": va + sz, "exec": execy, "pe_section": s})
    # Create a wrapper with section cache for memory-efficient reading
    reader = PEReader(pe, base, secs)
    return base, reader, secs

class PEReader:
    """Memory-efficient PE reader with section caching."""
    def __init__(self, pe, base, secs):
        self.pe = pe
        self.base = base
        self.secs = secs
        self.cache = {}  # Cache sections as they're read
    
    def get_section_for_va(self, va):
        """Find which section contains this VA."""
        for s in self.secs:
            if s["va"] <= va < s["end"]:
                return s
        return None
    
    def get_section_data(self, sec):
        """Get cached section data or read it."""
        key = sec["va"]
        if key not in self.cache:
            pe_sec = sec["pe_section"]
            # Read raw data and pad to virtual size if needed
            data = pe_sec.get_data()
            vsize = sec["end"] - sec["va"]
            if len(data) < vsize:
                # Pad with zeros to match virtual size
                data = data + b"\x00" * (vsize - len(data))
            self.cache[key] = data
        return self.cache[key]

def rd(reader, base, va, n):
    """Read n bytes at virtual address va from PE file (memory-efficient with caching)."""
    sec = reader.get_section_for_va(va)
    if not sec:
        return None
    
    sec_data = reader.get_section_data(sec)
    offset = va - sec["va"]
    
    if offset < 0 or offset >= len(sec_data):
        return None
    
    # Allow partial reads at section boundaries
    end = min(offset + n, len(sec_data))
    result = sec_data[offset:end]
    
    # Return None if we got less than requested (strict mode for most operations)
    # But allow partial reads for larger requests (like string scanning)
    if len(result) < n and n <= 10:
        return None
    
    return result

def u32(pe, base, va): b = rd(pe, base, va, 4); return None if b is None else struct.unpack_from("<I", b)[0]
def u64(pe, base, va): b = rd(pe, base, va, 8); return None if b is None else struct.unpack_from("<Q", b)[0]

def in_data(secs, va):
    for s in secs:
        if s["va"] <= va < s["end"]:
            return not s["exec"]
    return False

# --- UTF-16 anchors (extension names) ------------------------------------------------------------

def utf16_at(reader, base, secs, va, maxlen=1024):
    """Return the UTF-16 string at VA if it looks valid; else None."""
    if va & 1: return None
    
    # Read up to maxlen UTF-16 chars (2 bytes each)
    data = rd(reader, base, va, 2 * maxlen)
    if not data or len(data) < 2: return None
    
    # Find null terminator
    out = bytearray()
    for i in range(0, len(data) - 1, 2):
        lo, hi = data[i], data[i+1]
        if lo == 0 and hi == 0: break
        out += data[i:i+2]
    
    if not out: return None
    
    try: s = out.decode("utf-16le")
    except: return None
    
    if len(s) > MAXS: return None
    
    # mostly printable ASCII?
    if sum(1 for ch in s if 32 <= ord(ch) < 127 or ord(ch) in (9,10,13)) / max(1, len(s)) < 0.80: return None
    
    # has a terminator nearby?
    check_data = rd(reader, base, va, 1024)
    if not check_data or check_data.find(b"\x00\x00") < 0: return None
    
    return s

def find_anchors(img, base, secs, text):
    """Find UTF-16 byte hits for 'text' in data sections and keep only valid ones."""
    needle = text.encode("utf-16le", "ignore"); hits = set()
    for s in secs:
        b = rd(img, base, s["va"], s["end"] - s["va"]) or b""
        i = 0
        while True:
            j = b.find(needle, i)
            if j < 0: break
            va = s["va"] + j
            if utf16_at(img, base, secs, va): hits.add(va)
            i = j + 2
    return hits

# --- call indexing (byte patterns only) ---------------------------------------

def index_calls(img, base, secs):
    """near[target] / ind[slot] -> list of (section, offset to call byte)."""
    near, ind = {}, {}
    for s in secs:
        if not s["exec"]: continue
        b = rd(img, base, s["va"], s["end"] - s["va"]) or b""; L = len(b)
        # E8 rel32
        i = 0
        while True:
            i = b.find(b"\xE8", i)
            if i < 0 or i + 5 > L: break
            tgt = (s["va"] + i + 5 + s32(b, i+1)) & 0xFFFFFFFFFFFFFFFF
            near.setdefault(tgt, []).append((s, i)); i += 1
        # FF 15 disp32
        i = 0
        while True:
            i = b.find(b"\xFF\x15", i)
            if i < 0 or i + 6 > L: break
            slot = (s["va"] + i + 6 + s32(b, i+2)) & 0xFFFFFFFFFFFFFFFF
            ind.setdefault(slot, []).append((s, i)); i += 1
        # REX + FF 15 disp32
        for rx in range(0x40, 0x50):
            pat = bytes([rx, 0xFF, 0x15]); j = 0
            while True:
                j = b.find(pat, j)
                if j < 0 or j + 7 > L: break
                slot = (s["va"] + j + 7 + s32(b, j+3)) & 0xFFFFFFFFFFFFFFFF
                ind.setdefault(slot, []).append((s, j)); j += 1
    return near, ind

# --- next CALL after RCX <- anchor --------------------------------------------

def _resolve_reg(img, base, s, pos, reg, back=64):
    """Small look-back to resolve 'call r?' targets (imm64 or [RIP+disp])."""
    b = rd(img, base, s["va"], s["end"] - s["va"]) or b""; L = len(b)
    t, floor = pos - 1, max(0, pos - back)
    while t >= floor:
        if t + 10 <= L and 0x48 <= b[t] <= 0x4F and 0xB8 <= b[t+1] <= 0xBF:
            r = (b[t+1]-0xB8) | ((b[t] & 1) << 3)
            if r == reg: return struct.unpack_from("<Q", b, t+2)[0]
        if t + 5 <= L and 0xB8 <= b[t] <= 0xBF and (b[t]-0xB8) == reg:
            return struct.unpack_from("<I", b, t+1)[0]
        if t + 7 <= L and b[t] == 0x8B:
            m = b[t+1]; mod=(m>>6)&3; r=(m>>3)&7; rm=m&7
            if mod == 0 and rm == 5 and r == (reg & 7):
                ip = s["va"] + t + 6; slot = (ip + s32(b, t+2)) & 0xFFFFFFFFFFFFFFFF
                v = u64(img, base, slot)
                if v is not None and (reg >> 3) == 0: return int(v)
        if t + 8 <= L and 0x40 <= b[t] <= 0x4F and b[t+1] == 0x8B:
            rex=b[t]; m=b[t+2]; mod=(m>>6)&3; r=((m>>3)&7)|(((rex>>2)&1)<<3); rm=m&7
            if mod == 0 and rm == 5 and r == reg:
                ip = s["va"] + t + 7; slot = (ip + s32(b, t+3)) & 0xFFFFFFFFFFFFFFFF
                v = u64(img, base, slot)
                if v is not None: return int(v)
        t -= 1
    return None

def _next_call(img, base, s, off):
    """Return the very next call after the RCX load (E8 / FF15 / REX+FF15 / call r?)."""
    b = rd(img, base, s["va"], s["end"] - s["va"]) or b""; L = len(b); j = off
    while j < L:
        if j + 5 <= L and b[j] == 0xE8:
            return ("near", (s["va"] + j + 5 + s32(b, j+1)) & 0xFFFFFFFFFFFFFFFF)
        if j + 6 <= L and b[j] == 0xFF and b[j+1] == 0x15:
            return ("ind", (s["va"] + j + 6 + s32(b, j+2)) & 0xFFFFFFFFFFFFFFFF)
        if j + 7 <= L and 0x40 <= b[j] <= 0x4F and b[j+1] == 0xFF and b[j+2] == 0x15:
            return ("ind", (s["va"] + j + 7 + s32(b, j+3)) & 0xFFFFFFFFFFFFFFFF)
        if j + 2 <= L and b[j] == 0xFF:            # call r?
            m = b[j+1]
            if (m >> 6) == 3 and ((m >> 3) & 7) == 2:
                reg = (m & 7)
                if j > 0 and 0x40 <= b[j-1] <= 0x4F and (b[j-1] & 1): reg |= 8
                v = _resolve_reg(img, base, s, j, reg)
                if v is not None: return ("near", v)
        j += 1
    return None

def discover_callee(img, base, secs, anchors):
    """Find RCX<-anchor or RDX<-anchor (LEA/MOV), then take the next CALL as the callee."""
    callees = []
    for s in secs:
        if not s["exec"]: continue
        b = rd(img, base, s["va"], s["end"] - s["va"]) or b""; L = len(b)

        # ---- RCX patterns ----
        # LEA RCX, [RIP+disp] -> 48 8D 0D disp32
        i = 0
        while True:
            i = b.find(b"\x48\x8D\x0D", i)
            if i < 0 or i + 7 > L: break
            ptr = (s["va"] + i + 7 + s32(b, i+3)) & 0xFFFFFFFFFFFFFFFF
            if ptr in anchors:
                r = _next_call(img, base, s, i + 7)
                if r and r not in callees: callees.append(r)
            i += 1
        # MOV RCX, imm64 -> 48 B9 imm64
        i = 0
        while True:
            i = b.find(b"\x48\xB9", i)
            if i < 0 or i + 10 > L: break
            if struct.unpack_from("<Q", b, i+2)[0] in anchors:
                r = _next_call(img, base, s, i + 10)
                if r and r not in callees: callees.append(r)
            i += 1
        # MOV RCX, [RIP+disp] -> 48 8B 0D disp32
        i = 0
        while True:
            i = b.find(b"\x48\x8B\x0D", i)
            if i < 0 or i + 7 > L: break
            slot = (s["va"] + i + 7 + s32(b, i+3)) & 0xFFFFFFFFFFFFFFFF
            v = u64(img, base, slot)
            if v in anchors:
                r = _next_call(img, base, s, i + 7)
                if r and r not in callees: callees.append(r)
            i += 1

        # ---- RDX patterns (older builds use RDX for the string) ----
        i = 0  # LEA RDX, [RIP+disp] -> 48 8D 15 disp32
        while True:
            i = b.find(b"\x48\x8D\x15", i)
            if i < 0 or i + 7 > L: break
            ptr = (s["va"] + i + 7 + s32(b, i+3)) & 0xFFFFFFFFFFFFFFFF
            if ptr in anchors:
                r = _next_call(img, base, s, i + 7)
                if r and r not in callees: callees.append(r)
            i += 1

        i = 0  # MOV RDX, imm64 -> 48 BA imm64
        while True:
            i = b.find(b"\x48\xBA", i)
            if i < 0 or i + 10 > L: break
            if struct.unpack_from("<Q", b, i+2)[0] in anchors:
                r = _next_call(img, base, s, i + 10)
                if r and r not in callees: callees.append(r)
            i += 1

        i = 0  # MOV RDX, [RIP+disp] -> 48 8B 15 disp32
        while True:
            i = b.find(b"\x48\x8B\x15", i)
            if i < 0 or i + 7 > L: break
            slot = (s["va"] + i + 7 + s32(b, i+3)) & 0xFFFFFFFFFFFFFFFF
            v = u64(img, base, slot)
            if v in anchors:
                r = _next_call(img, base, s, i + 7)
                if r and r not in callees: callees.append(r)
            i += 1

    return callees

def resolve_thunk(img, base, cal):
    """Follow simple JMP thunks (rel32 or [RIP+disp]) to the final near target."""
    k, t = cal
    if k != "near": return cal, [cal]
    b5 = rd(img, base, t, 5) or b""
    if len(b5) == 5 and b5[0] == 0xE9:
        t2 = (t + 5 + s32(b5, 1)) & 0xFFFFFFFFFFFFFFFF
        return ("near", t2), [cal, ("near", t2)]
    b6 = rd(img, base, t, 6) or b""
    if len(b6) == 6 and b6[0] == 0xFF and b6[1] == 0x25:
        slot = (t + 6 + s32(b6, 2)) & 0xFFFFFFFFFFFFFFFF
        t2 = u64(img, base, slot)
        if t2: return ("near", int(t2)), [cal, ("ind", slot), ("near", int(t2))]
    return cal, [cal]

def validate_callee(img, base, cal, allow_4_args=False):
    k, t = cal
    if k != "near": return False
    b = rd(img, base, t, 60)
    if not b or len(b) < 20: return False
    
    if not allow_4_args:
        has_conditional = False
        for i in range(min(40, len(b) - 1)):
            if 0x70 <= b[i] <= 0x7F:
                has_conditional = True
                break
            if i + 1 < len(b) and b[i] == 0x0F and 0x80 <= b[i+1] <= 0x8F:
                has_conditional = True
                break
        if not has_conditional:
            return False
    
    prologue_end = min(30, len(b) - 2)
    for i in range(prologue_end):
        if i + 1 < len(b) and b[i] == 0x8B:
            modrm = b[i+1]
            if ((modrm >> 6) & 3) == 3 and (modrm & 7) == 2:
                return True
            if (modrm & 0xF8) == 0xD0:
                return True
        if i + 2 < len(b) and b[i] == 0x44 and b[i+1] == 0x8B:
            return True
        if i + 2 < len(b) and b[i] == 0x45 and b[i+1] in (0x8B, 0x89):
            return True
        if i + 1 < len(b) and b[i] == 0x41 and b[i+1] == 0xB8:
            return True
    
    if allow_4_args:
        for i in range(prologue_end):
            if i + 2 < len(b) and b[i] in (0x44, 0x4C) and b[i+1] in (0x88, 0x89, 0x8B):
                if ((b[i+2] >> 3) & 7) == 1:
                    return True
            if i + 2 < len(b) and b[i] == 0x45 and b[i+1] in (0x88, 0x89, 0x8B, 0x84, 0x85):
                modrm = b[i+2]
                if ((modrm >> 3) & 7) == 1 or (modrm & 7) == 1:
                    return True
            if i + 1 < len(b) and b[i] == 0x41 and b[i+1] == 0xB9:
                return True
        return False
    
    return False

# --- tiny block emulator & some tail fallbacks -------------------------------------

def cap():
    m = Cs(CS_ARCH_X86, CS_MODE_64); m.detail = True; m.skipdata = True
    return m

def rname(m, r):
    n = m.reg_name(r) or ""
    if n.endswith("d"): n = n[:-1]          # r8d -> r8
    if n.startswith("e") and len(n) == 3: n = "r"+n[1:]  # eax -> rax
    return n

def apply_width(v, w):                      # mask value to operand width
    return v & (0xFFFFFFFFFFFFFFFF if w == 8 else 0xFFFFFFFF if w == 4 else 0xFFFF if w == 2 else 0xFF)

def is_hard_barrier(b, i):
    """Stop points for the backward block search (ret/int/jmp)."""
    op = b[i]
    if op in (0xC3,0xCB,0xC2,0xCA,0xCC,0xCD,0xE9,0xEB): return True
    if op == 0xFF and i+1 < len(b) and (((b[i+1]>>3)&7) == 4): return True  # jmp r/m
    return False

def block_start(img, base, s, call_off, SLACK=32):
    """ start a little after the previous barrier (tiny slack keeps last writes)."""
    b = rd(img, base, s["va"], s["end"] - s["va"]) or b""; i = call_off - 1
    while i >= 0:
        if is_hard_barrier(b, i):
            op = b[i]
            if op in (0xC2,0xCA): st = s["va"] + i + 3
            elif op in (0xCD,0xEB): st = s["va"] + i + 2
            elif op == 0xE9: st = s["va"] + i + 5
            else: st = s["va"] + i + 1
            return max(s["va"], st - SLACK)
        i -= 1
    return s["va"]

def mem_addr(insn, mem, st, m):
    """Compute [rip+disp] or [reg(+index)*scale+disp] when base/index regs are known."""
    pc = insn.address + insn.size
    if mem.base == X86_REG_RIP: base = pc
    elif mem.base != 0:
        bv = st.get(rname(m, mem.base))
        if not isinstance(bv, int): return None
        base = bv
    else:
        base = 0
    if mem.index != 0:
        iv = st.get(rname(m, mem.index))
        if not isinstance(iv, int): return None
        base += iv * (mem.scale or 1)
    return (base + mem.disp) & 0xFFFFFFFFFFFFFFFF

def read_width(img, base, addr, w):
    if w == 8: return u64(img, base, addr)
    if w == 4: return u32(img, base, addr)
    if w == 2:
        b = rd(img, base, addr, 2); return None if not b else struct.unpack_from("<H", b)[0]
    b = rd(img, base, addr, 1); return None if not b else b[0]

def decode_with_resync(m, img, base, sec_start, start_va, end_va):
    """Try <= 15-byte realignments; keep the decode that reaches the call boundary best."""
    best_ins = None; best_cov = None
    for d in range(16):
        alt  = max(sec_start, start_va - d)
        data = rd(img, base, alt, end_va - alt) or b""
        ins  = list(m.disasm(data, alt))
        if not ins: continue
        last = ins[-1].address + ins[-1].size
        if last > end_va:
            k = 0
            while k < len(ins) and ins[k].address + ins[k].size <= end_va: k += 1
            ins = ins[:k]; last = end_va if k and ins else last
        cov = last - alt
        if best_cov is None or cov > best_cov:
            best_cov, best_ins = cov, ins
    return best_ins or []

def tail_scan_edx(img, base, s, call_off, span=32):
    """Quick scan of the last bytes for: mov edx,imm32 / xor edx,edx."""
    b = rd(img, base, s["va"], s["end"] - s["va"]) or b""; lo = max(0, call_off - span); hi = call_off
    i = hi - 1
    while i >= lo:
        if i + 5 <= hi and b[i] == 0xBA: return struct.unpack_from("<I", b, i+1)[0]
        if i + 2 <= hi and ((b[i]==0x31 and b[i+1]==0xD2) or (b[i]==0x33 and b[i+1]==0xD2)): return 0
        i -= 1
    return None

def tail_scan_rcx(img, base, s, call_off, span=48):
    """Quick scan of the last bytes for RCX forms i've seen: lea/mov imm/mov [rip+disp]."""
    b = rd(img, base, s["va"], s["end"] - s["va"]) or b""; lo = max(0, call_off - span); hi = call_off
    i = hi - 1
    while i >= lo:
        if i + 7 <= hi and b[i:i+3] == b"\x48\x8D\x0D":  # lea rcx,[rip+disp]
            return (s["va"] + i + 7 + s32(b, i+3)) & 0xFFFFFFFFFFFFFFFF
        if i + 10 <= hi and b[i:i+2] == b"\x48\xB9":     # mov rcx,imm64
            return int(struct.unpack_from("<Q", b, i+2)[0])
        if i + 7 <= hi and b[i:i+3] == b"\x48\x8B\x0D":  # mov rcx,[rip+disp]
            slot = (s["va"] + i + 7 + s32(b, i+3)) & 0xFFFFFFFFFFFFFFFF
            v = u64(img, base, slot)
            if v is not None: return int(v)
        i -= 1
    return None

def tail_scan_r8d(img, base, s, call_off, span=32):
    """Scan just-before-call for r8d imm/zero patterns."""
    b = rd(img, base, s["va"], s["end"] - s["va"]) or b""
    lo = max(0, call_off - span); hi = call_off; i = hi - 1
    while i >= lo:
        # mov r8d, imm32 -> 41 B8 imm32
        if i + 6 <= hi and b[i] == 0x41 and b[i+1] == 0xB8:
            return struct.unpack_from("<I", b, i+2)[0]
        # xor r8d, r8d -> 45 33 C0  (or 45 31 C0 depending?)
        if i + 3 <= hi and b[i] == 0x45 and b[i+1] in (0x31, 0x33) and b[i+2] == 0xC0:
            return 0
        i -= 1
    return None

def tail_scan_rdx(img, base, s, call_off, span=48):
    """Scan just-before-call for rdx set from utf16 string (lea/mov forms)."""
    b = rd(img, base, s["va"], s["end"] - s["va"]) or b""
    lo = max(0, call_off - span); hi = call_off; i = hi - 1
    while i >= lo:
        # lea rdx, [rip+disp] -> 48 8D 15 disp32
        if i + 7 <= hi and b[i:i+3] == b"\x48\x8D\x15":
            return (s["va"] + i + 7 + s32(b, i+3)) & 0xFFFFFFFFFFFFFFFF
        # mov rdx, imm64 -> 48 BA imm64
        if i + 10 <= hi and b[i:i+2] == b"\x48\xBA":
            return int(struct.unpack_from("<Q", b, i+2)[0])
        # mov rdx, [rip+disp] -> 48 8B 15 disp32
        if i + 7 <= hi and b[i:i+3] == b"\x48\x8B\x15":
            slot = (s["va"] + i + 7 + s32(b, i+3)) & 0xFFFFFFFFFFFFFFFF
            v = u64(img, base, slot)
            if v is not None: return int(v)
        i -= 1
    return None

def eval_block(img, base, secs, s, start_va, call_off, m):
    """Emulate the short block [start_va, call) to recover RCX/EDX."""
    end_va = s["va"] + call_off
    insns  = decode_with_resync(m, img, base, s["va"], start_va, end_va)
    st, stack = {}, {}  # reg ->int and simple stack slots keyed by ('rbp'|'rsp', disp)

    for insn in insns:
        if insn.address + insn.size > end_va: break
        try: ops = insn.operands
        except Exception: continue

        # store: mov [rbp|rsp+disp], src
        if insn.id == X86_INS_MOV and len(ops) == 2 and ops[0].type == X86_OP_MEM:
            mem, src = ops[0].mem, ops[1]
            if mem.base in (X86_REG_RBP, X86_REG_RSP):
                key = ("rbp" if mem.base == X86_REG_RBP else "rsp", mem.disp)
                v = int(src.imm) if src.type == X86_OP_IMM else st.get(rname(m, src.reg))
                if v is not None: stack[key] = v

        # MOV r, imm/reg/mem
        if insn.id == X86_INS_MOV and len(ops) == 2 and ops[0].type == X86_OP_REG:
            dst = rname(m, ops[0].reg); src = ops[1]
            if src.type == X86_OP_IMM:
                st[dst] = apply_width(int(src.imm), ops[0].size)
            elif src.type == X86_OP_REG:
                v = st.get(rname(m, src.reg))
                if v is not None: st[dst] = apply_width(v, ops[0].size)
            elif src.type == X86_OP_MEM:
                v = None
                if src.mem.base in (X86_REG_RBP, X86_REG_RSP):
                    key = ("rbp" if src.mem.base == X86_REG_RBP else "rsp", src.mem.disp); v = stack.get(key)
                if v is None:
                    addr = mem_addr(insn, src.mem, st, m)
                    if addr is not None: v = read_width(img, base, addr, ops[1].size)
                if v is not None: st[dst] = apply_width(int(v), ops[0].size)

        # LEA r, [rip+disp] or [reg+disp] (no index)
        elif insn.id == X86_INS_LEA and len(ops) == 2 and ops[0].type == X86_OP_REG and ops[1].type == X86_OP_MEM:
            dst = rname(m, ops[0].reg); mem = ops[1].mem
            if mem.base == X86_REG_RIP and mem.index == 0:
                st[dst] = (insn.address + insn.size + mem.disp) & 0xFFFFFFFFFFFFFFFF
            elif mem.index == 0 and mem.base != 0:
                base_v = st.get(rname(m, mem.base))
                if isinstance(base_v, int): st[dst] = (base_v + mem.disp) & 0xFFFFFFFFFFFFFFFF

        # MOVZX/MOVSX r, [mem]
        elif insn.id in (X86_INS_MOVZX, X86_INS_MOVSX) and len(ops) == 2 and ops[0].type == X86_OP_REG and ops[1].type == X86_OP_MEM:
            dst = rname(m, ops[0].reg); mem = ops[1].mem; width = ops[1].size
            v = None
            if mem.base in (X86_REG_RBP, X86_REG_RSP):
                key = ("rbp" if mem.base == X86_REG_RBP else "rsp", mem.disp); v = stack.get(key)
            if v is None:
                addr = mem_addr(insn, mem, st, m)
                if addr is not None: v = read_width(img, base, addr, width)
            if v is not None and insn.id == X86_INS_MOVSX:
                if width == 1 and (v & 0x80):   v |= (-1 ^ 0xFF)
                if width == 2 and (v & 0x8000): v |= (-1 ^ 0xFFFF)
            if v is not None: st[dst] = apply_width(int(v), ops[0].size)

        # zeroing / simple ALU (only when both sides are known)
        elif insn.id in (X86_INS_XOR, X86_INS_SUB) and len(ops) == 2 and ops[0].type == X86_OP_REG and ops[1].type == X86_OP_REG and ops[0].reg == ops[1].reg:
            st[rname(m, ops[0].reg)] = 0
        elif insn.id == X86_INS_AND and len(ops) == 2 and ops[0].type == X86_OP_REG and ops[1].type == X86_OP_IMM and int(ops[1].imm) == 0:
            st[rname(m, ops[0].reg)] = 0
        elif insn.id in (X86_INS_ADD,X86_INS_SUB,X86_INS_AND,X86_INS_OR,X86_INS_XOR) and len(ops) == 2 and ops[0].type == X86_OP_REG:
            dst = rname(m, ops[0].reg); cur = st.get(dst)
            if cur is None: continue
            srcv = int(ops[1].imm) if ops[1].type == X86_OP_IMM else st.get(rname(m, ops[1].reg)) if ops[1].type == X86_OP_REG else None
            if srcv is None: continue
            nv = cur+srcv if insn.id==X86_INS_ADD else cur-srcv if insn.id==X86_INS_SUB else (cur & srcv if insn.id==X86_INS_AND else cur | srcv if insn.id==X86_INS_OR else cur ^ srcv)
            st[dst] = apply_width(nv, ops[0].size)
        elif insn.id in (X86_INS_SHL,X86_INS_SHR,X86_INS_SAR) and len(ops) == 2 and ops[0].type == X86_OP_REG and ops[1].type == X86_OP_IMM:
            dst = rname(m, ops[0].reg); cur = st.get(dst); sh = int(ops[1].imm) & 0x3F
            if cur is None: continue
            if insn.id == X86_INS_SHL: nv = cur << sh
            elif insn.id == X86_INS_SHR: nv = (cur & 0xFFFFFFFFFFFFFFFF) >> sh
            else:
                nv = ((cur|(~0xFFFFFFFFFFFFFFFF))>>sh)&0xFFFFFFFFFFFFFFFF if (cur & (1<<63)) else (cur>>sh)
            st[dst] = apply_width(nv, ops[0].size)
        elif insn.id in (X86_INS_INC, X86_INS_DEC) and len(ops) == 1 and ops[0].type == X86_OP_REG:
            dst = rname(m, ops[0].reg); cur = st.get(dst)
            if cur is not None: st[dst] = apply_width(cur + (1 if insn.id == X86_INS_INC else -1), ops[0].size)

    rcx = st.get("rcx")
    edx = st.get("rdx") if "rdx" in st else st.get("edx")
    if (not isinstance(rcx, int) or not utf16_at(img, base, secs, rcx)):
        rcx = None
    if rcx is None or edx is None:
        # tail assists for A
        if edx is None:
            t = tail_scan_edx(img, base, s, call_off, span=32)
            if t is not None: edx = int(t)
        if rcx is None:
            r = tail_scan_rcx(img, base, s, call_off, span=48)
            if r is not None and utf16_at(img, base, secs, int(r)):
                rcx = int(r)

    if rcx is not None and isinstance(edx, int):
        return rcx, edx

    # B) fallback: RDX + R8D  (seen in newer builds)
    rdx = st.get("rdx") if isinstance(st.get("rdx"), int) else None
    r8  = st.get("r8") if isinstance(st.get("r8"), int) else None
    if (rdx is None or not utf16_at(img, base, secs, rdx)) or (r8 is None):
        # tail assists for B
        if r8 is None:
            t8 = tail_scan_r8d(img, base, s, call_off, span=32)
            if t8 is not None: r8 = int(t8)
        if rdx is None or not utf16_at(img, base, secs, rdx):
            rdxt = tail_scan_rdx(img, base, s, call_off, span=48)
            if rdxt is not None and utf16_at(img, base, secs, int(rdxt)):
                rdx = int(rdxt)

    if rdx is not None and isinstance(r8, int):
        # normalize to the same return contract: (string_ptr, number)
        return rdx, r8

    # nothing confident
    return None, None

# --- CLI ----------------------------------------------------------------------

def extract_extensions(pe_path, exts):
    if not exts:
        return {"error": "no -ext provided"}

    base, img, secs = map_pe(pe_path)
    near_map, ind_map = index_calls(img, base, secs)

    aliases = set()
    for ext in exts:
        anchors = find_anchors(img, base, secs, ext)
        if not anchors: continue
        callees = discover_callee(img, base, secs, anchors)
        if not callees: continue
        for cal in callees:
            cal, chain = resolve_thunk(img, base, cal)
            if not validate_callee(img, base, cal, allow_4_args=False):
                continue
            aliases.update(chain)
    
    if not aliases:
        for ext in exts:
            anchors = find_anchors(img, base, secs, ext)
            if not anchors: continue
            callees = discover_callee(img, base, secs, anchors)
            if not callees: continue
            for cal in callees:
                cal, chain = resolve_thunk(img, base, cal)
                if not validate_callee(img, base, cal, allow_4_args=True):
                    continue
                aliases.update(chain)
    
    if not aliases:
        return {"error": "no callees resolved from provided -ext strings"}

    sites = {}
    for kind, tgt in aliases:
        for s, off in (near_map.get(tgt, []) if kind == "near" else ind_map.get(tgt, [])):
            sites[(s["va"], off)] = s

    m = cap(); out = {}
    for (_, off), s in sorted(sites.items()):
        start = block_start(img, base, s, off)
        rcx, edx = eval_block(img, base, secs, s, start, off, m)
        if rcx is None or edx is None: continue
        txt = utf16_at(img, base, secs, rcx)
        if not txt: continue
        if txt in out: continue
        out[txt] = edx

    return out


def main():
    p = argparse.ArgumentParser(description="Extract {UTF-16 string:number} via known -ext anchors")
    p.add_argument("pe")
    p.add_argument("-ext", action="append", default=[], help="known extension string (repeatable)")
    p.add_argument("-F","--ext-file", help="file with one extension per line (# comments ok)")
    p.add_argument("-o","--output", help="output JSON path")
    a = p.parse_args()

    exts = list(a.ext)
    if a.ext_file:
        for ln in open(a.ext_file, "r", encoding="utf-8"):
            t = ln.strip()
            if t and not t.startswith("#"):
                exts.append(t)

    out = extract_extensions(a.pe, exts)
    if "error" in out:
        print(json.dumps(out, ensure_ascii=False))
        return

    js = json.dumps(out, indent="\t", ensure_ascii=False)
    out_path = a.output or f"{os.path.splitext(os.path.basename(a.pe))[0]}_extensions.json"
    open(out_path, "w", encoding="utf-8").write(js)
    print(js)

if __name__ == "__main__":
    main()
