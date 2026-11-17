// See docs/internals/gui-rendering.md, Glyphs.
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include "hb.h"

namespace {

using i32 = int32_t;
using i64 = int64_t;

i32 mulfix(i32 a, i32 b) {
    int s = 1;
    i64 x = a, y = b;
    if (x < 0) { x = -x; s = -s; }
    if (y < 0) { y = -y; s = -s; }
    i32 r = (i32)((x * y + 0x8000) >> 16);
    return s > 0 ? r : -r;
}

i32 divfix(i32 a, i32 b) {
    int s = 1;
    i64 x = a, y = b;
    if (x < 0) { x = -x; s = -s; }
    if (y < 0) { y = -y; s = -s; }
    i32 r = y == 0 ? 0x7FFFFFFF : (i32)((((uint64_t)x << 16) + (uint64_t)(y >> 1)) / (uint64_t)y);
    return s > 0 ? r : -r;
}

i32 muldiv(i32 a, i32 b, i32 c) {
    int s = 1;
    i64 x = a, y = b, z = c;
    if (x < 0) { x = -x; s = -s; }
    if (y < 0) { y = -y; s = -s; }
    if (z < 0) { z = -z; s = -s; }
    i32 r = z == 0 ? 0x7FFFFFFF : (i32)((x * y + (z >> 1)) / z);
    return s > 0 ? r : -r;
}

struct Vec { i32 x = 0, y = 0; bool operator!=(const Vec& o) const { return x != o.x || y != o.y; } };

struct Index { std::vector<std::pair<const uint8_t*, size_t>> items; };

struct Reader {
    const uint8_t* data;
    size_t size;
    void need(size_t at, size_t n) const { if (at + n > size) throw std::runtime_error("CFF data ends early"); }
    unsigned u8(size_t at) const { need(at, 1); return data[at]; }
    unsigned u16(size_t at) const { need(at, 2); return (data[at] << 8) | data[at + 1]; }
    unsigned off(size_t at, unsigned n) const {
        need(at, n);
        unsigned v = 0;
        for (unsigned i = 0; i < n; ++i) v = (v << 8) | data[at + i];
        return v;
    }
    Index index(size_t at, size_t* end = nullptr) const {
        Index out;
        unsigned count = u16(at);
        if (count == 0) { if (end) *end = at + 2; return out; }
        unsigned n = u8(at + 2);
        size_t base = at + 3 + (size_t)(count + 1) * n - 1;
        for (unsigned i = 0; i < count; ++i) {
            unsigned a = off(at + 3 + (size_t)i * n, n), b = off(at + 3 + (size_t)(i + 1) * n, n);
            need(base + a, b - a);
            out.items.push_back({data + base + a, b - a});
        }
        if (end) *end = base + off(at + 3 + (size_t)count * n, n);
        return out;
    }
};

// DICT operands as doubles (cff_parse_num keeps integers; StdVW/StdHW are truncated like FreeType's parser does).
std::map<int, std::vector<double>> read_dict(const uint8_t* p, size_t n) {
    std::map<int, std::vector<double>> out;
    std::vector<double> ops;
    size_t at = 0;
    while (at < n) {
        unsigned b = p[at];
        if (b <= 21) {
            int key = b == 12 ? 1200 + p[at + 1] : (int)b;
            at += b == 12 ? 2 : 1;
            out[key] = ops;
            ops.clear();
        } else if (b == 28) {
            ops.push_back((int16_t)((p[at + 1] << 8) | p[at + 2])); at += 3;
        } else if (b == 29) {
            ops.push_back((i32)(((uint32_t)p[at + 1] << 24) | (p[at + 2] << 16) | (p[at + 3] << 8) | p[at + 4])); at += 5;
        } else if (b == 30) {
            std::string text;
            ++at;
            for (bool done = false; !done; ++at) {
                for (unsigned nib : {(unsigned)(p[at] >> 4), (unsigned)(p[at] & 15)}) {
                    if (nib == 15) { done = true; break; }
                    static const char* digits[] = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", ".", "E", "E-", "", "-"};
                    text += digits[nib];
                }
            }
            ops.push_back(std::strtod(text.c_str(), nullptr));
        } else if (b >= 32 && b <= 246) {
            ops.push_back((int)b - 139); at += 1;
        } else if (b <= 250) {
            ops.push_back(((int)b - 247) * 256 + p[at + 1] + 108); at += 2;
        } else if (b <= 254) {
            ops.push_back(-((int)b - 251) * 256 - p[at + 1] - 108); at += 2;
        } else {
            throw std::runtime_error("bad CFF DICT byte");
        }
    }
    return out;
}

struct SubFont {
    Index subrs;
    i32 std_hw = 0, std_vw = 0, default_width = 0, nominal_width = 0;
};

struct Cff {
    std::vector<uint8_t> bytes;
    Index charstrings, gsubrs;
    std::vector<SubFont> fds;
    std::vector<uint8_t> fdselect;

    explicit Cff(const uint8_t* p, size_t n) : bytes(p, p + n) {
        Reader r{bytes.data(), bytes.size()};
        size_t at = r.u8(2), end = 0;
        r.index(at, &end);
        Index tops = r.index(end, &end);
        r.index(end, &end);
        gsubrs = r.index(end, &end);
        if (tops.items.empty()) throw std::runtime_error("CFF without a Top DICT");
        auto top = read_dict(tops.items[0].first, tops.items[0].second);
        if (!top.count(17)) throw std::runtime_error("CFF without CharStrings");
        charstrings = r.index((size_t)top[17][0]);
        if (top.count(1236)) {
            Index fdarray = r.index((size_t)top[1236][0]);
            for (auto& item : fdarray.items) fds.push_back(sub_font(r, read_dict(item.first, item.second)));
            select(r, (size_t)top.at(1237).at(0));
        } else {
            fds.push_back(sub_font(r, top));
        }
    }

    SubFont sub_font(const Reader& r, const std::map<int, std::vector<double>>& dict) {
        SubFont out;
        auto it = dict.find(18);
        if (it == dict.end() || it->second.size() < 2) return out;
        size_t size = (size_t)it->second[0], at = (size_t)it->second[1];
        r.need(at, size);
        auto priv = read_dict(bytes.data() + at, size);
        if (priv.count(19)) out.subrs = r.index(at + (size_t)priv[19][0]);
        if (priv.count(10) && !priv[10].empty()) out.std_hw = (i32)std::floor(priv[10][0]);
        if (priv.count(11) && !priv[11].empty()) out.std_vw = (i32)std::floor(priv[11][0]);
        if (priv.count(20) && !priv[20].empty()) out.default_width = (i32)priv[20][0];
        if (priv.count(21) && !priv[21].empty()) out.nominal_width = (i32)priv[21][0];
        return out;
    }

    void select(const Reader& r, size_t at) {
        size_t count = charstrings.items.size();
        fdselect.assign(count, 0);
        unsigned format = r.u8(at);
        if (format == 0) {
            for (size_t g = 0; g < count; ++g) fdselect[g] = (uint8_t)r.u8(at + 1 + g);
        } else if (format == 3) {
            unsigned ranges = r.u16(at + 1);
            for (unsigned i = 0; i < ranges; ++i) {
                unsigned first = r.u16(at + 3 + i * 3), fd = r.u8(at + 5 + i * 3), next = r.u16(at + 6 + i * 3);
                for (unsigned g = first; g < next && g < count; ++g) fdselect[g] = (uint8_t)fd;
            }
        } else {
            throw std::runtime_error("unknown FDSelect format");
        }
    }

    const SubFont& fd(unsigned gid) const { return fds.at(fdselect.empty() ? 0 : fdselect.at(gid)); }
};

struct Builder {  // 16.16 device points >> 10
    std::vector<std::vector<Vec>> points;
    std::vector<std::vector<uint8_t>> tags;
    bool begun = false;

    void close() {
        if (points.empty()) return;
        auto& p = points.back();
        auto& t = tags.back();
        if (p.size() > 1 && !(p.front() != p.back()) && t.back() == 1) { p.pop_back(); t.pop_back(); }
        if (p.size() == 1) { points.pop_back(); tags.pop_back(); }
    }
    void move() { close(); begun = false; }
    void start(Vec pt0) {
        if (begun) return;
        begun = true;
        points.push_back({{pt0.x >> 10, pt0.y >> 10}});
        tags.push_back({1});
    }
    void add(Vec p, uint8_t tag) { points.back().push_back({p.x >> 10, p.y >> 10}); tags.back().push_back(tag); }
    void line(Vec pt0, Vec pt1) { start(pt0); add(pt1, 1); }
    void cube(Vec pt0, Vec p1, Vec p2, Vec p3) { start(pt0); add(p1, 2); add(p2, 2); add(p3, 1); }
};

struct GlyphPath {
    Builder& b;
    bool darken, reverse;
    i32 xo, yo, miter, snap = 0x199A, scale = 0x400;
    i32 momentum = 0;
    Vec cs, start, ds, start0, start1;
    bool move_pending = true, open = false, closing = false, queued = false, hint_new = false;
    bool prev_line = true;
    Vec prev[4];

    GlyphPath(Builder& builder, bool dk, i32 x, i32 y, bool rev) : b(builder), darken(dk), reverse(rev), xo(x), yo(y) {
        i32 m = std::abs(y) < std::abs(x) ? x : y;
        miter = 2 * std::abs(m);
    }

    Vec hint(Vec p) const { return {mulfix(scale, p.x) + mulfix(0, p.y), mulfix(p.y, scale)}; }

    Vec offset(Vec a, Vec c) {
        i32 dx = (i32)((uint32_t)c.x - (uint32_t)a.x), dy = (i32)((uint32_t)c.y - (uint32_t)a.y);
        i32 rx = reverse ? -dx : dx, ry = reverse ? -dy : dy;
        if (!darken) return {0, 0};
        momentum += (dy >> 16) * (a.x >> 16) - (dx >> 16) * (a.y >> 16);
        if (rx >= 0) {
            if (ry >= 0) {
                if (2 * ry < rx) return {0, 0};
                if (2 * rx < ry) return {xo, yo};
                return {mulfix(0xB333, xo), mulfix(0x4CCD, yo)};
            }
            if (-2 * ry < rx) return {0, 0};
            if (2 * rx < -ry) return {-xo, yo};
            return {mulfix(-45874, xo), mulfix(0x4CCD, yo)};
        }
        if (ry >= 0) {
            if (-rx > 2 * ry) return {0, 2 * yo};
            if (-2 * rx < ry) return {xo, yo};
            return {mulfix(0xB333, xo), mulfix(0x1B333, yo)};
        }
        if (-rx > -2 * ry) return {0, 2 * yo};
        if (-2 * rx < -ry) return {-xo, xo};
        return {mulfix(-45874, xo), mulfix(0x1B333, yo)};
    }

    bool intersect(Vec u1, Vec u2, Vec v1, Vec v2, Vec& out) const {
        auto sc = [](i32 v) { return (v + 0x10) >> 5; };
        Vec u{sc(u2.x - u1.x), sc(u2.y - u1.y)}, v{sc(v2.x - v1.x), sc(v2.y - v1.y)}, w{sc(v1.x - u1.x), sc(v1.y - u1.y)};
        i32 den = mulfix(u.x, v.y) - mulfix(u.y, v.x);
        if (den == 0) return false;
        i32 s = divfix(mulfix(w.x, v.y) - mulfix(w.y, v.x), den);
        out = {u1.x + mulfix(s, u2.x - u1.x), u1.y + mulfix(s, u2.y - u1.y)};
        if (u1.x == u2.x && std::abs(out.x - u1.x) < snap) out.x = u1.x;
        if (u1.y == u2.y && std::abs(out.y - u1.y) < snap) out.y = u1.y;
        if (v1.x == v2.x && std::abs(out.x - v1.x) < snap) out.x = v1.x;
        if (v1.y == v2.y && std::abs(out.y - v1.y) < snap) out.y = v1.y;
        return std::abs(out.x - (u2.x + v1.x) / 2) <= miter && std::abs(out.y - (u2.y + v1.y) / 2) <= miter;
    }

    Vec push_prev(Vec next0, Vec next1, bool close) {
        int i0 = prev_line ? 0 : 2, i1 = prev_line ? 1 : 3;
        Vec inter;
        bool use = false;
        if (prev[i1] != next0) {
            use = intersect(prev[i0], prev[i1], next0, next1, inter);
            if (use) prev[i1] = inter;
        }
        Vec pt0 = ds;
        if (prev_line) {
            Vec pt1 = hint(prev[1]);
            if (pt0 != pt1) { b.line(pt0, pt1); ds = pt1; }
        } else {
            Vec p1 = hint(prev[1]), p2 = hint(prev[2]), p3 = hint(prev[3]);
            b.cube(pt0, p1, p2, p3);
            ds = p3;
        }
        if (!use || close) {
            Vec pt1 = hint(next0);
            if (pt1 != ds) { b.line(ds, pt1); ds = pt1; }
        }
        return use ? inter : next0;
    }

    void push_move(Vec s) { ds = hint(s); b.move(); start0 = s; }

    void move_to(Vec p) { close_open(); cs = start = p; move_pending = true; hint_new = false; }

    void line_to(Vec p) {
        bool new_map = hint_new && !closing;
        if (!(cs != p) && !new_map) return;
        Vec o = offset(cs, p);
        Vec p0{cs.x + o.x, cs.y + o.y}, p1{p.x + o.x, p.y + o.y};
        if (move_pending) { push_move(p0); move_pending = false; open = true; start1 = p1; }
        if (queued) p0 = push_prev(p0, p1, false);
        queued = true; prev_line = true;
        prev[0] = p0; prev[1] = p1;
        if (new_map) hint_new = false;
        cs = p;
    }

    void curve_to(Vec a, Vec c, Vec d) {
        Vec o1 = offset(cs, a), o3 = offset(c, d);
        momentum += ((c.y - a.y) >> 16) * (a.x >> 16) - ((c.x - a.x) >> 16) * (a.y >> 16);
        Vec p0{cs.x + o1.x, cs.y + o1.y}, p1{a.x + o1.x, a.y + o1.y}, p2{c.x + o3.x, c.y + o3.y}, p3{d.x + o3.x, d.y + o3.y};
        if (move_pending) { push_move(p0); move_pending = false; open = true; start1 = p1; }
        if (queued) p0 = push_prev(p0, p1, false);
        queued = true; prev_line = false;
        prev[0] = p0; prev[1] = p1; prev[2] = p2; prev[3] = p3;
        hint_new = false;
        cs = d;
    }

    void close_open() {
        if (!open) return;
        closing = true;
        line_to(start);
        if (queued) start0 = push_prev(start0, start1, true);
        move_pending = true; open = false; closing = false; queued = false;
    }
};

struct Type2 {
    const Cff& cff;
    const SubFont& fd;
    GlyphPath& gp;
    std::vector<i32> s;
    Vec cur;
    int stems = 0, depth = 0;
    bool width_done = false;
    i32 advance = fd.default_width * 65536;

    void width(bool odd_extra) {
        if (!width_done && (s.size() % 2 == 1) == odd_extra) {
            advance = fd.nominal_width * 65536 + s.front();
            s.erase(s.begin());
        }
        width_done = true;
    }
    void move(i32 dx, i32 dy) { cur = {cur.x + dx, cur.y + dy}; gp.move_to(cur); }
    void line(i32 dx, i32 dy) { cur = {cur.x + dx, cur.y + dy}; gp.line_to(cur); }
    void curve(i32 a, i32 b, i32 c, i32 d, i32 e, i32 f) {
        Vec p1{cur.x + a, cur.y + b}, p2{p1.x + c, p1.y + d}, p3{p2.x + e, p2.y + f};
        gp.curve_to(p1, p2, p3);
        cur = p3;
    }
    static int bias(const Index& subrs) {
        size_t n = subrs.items.size();
        return n < 1240 ? 107 : n < 33900 ? 1131 : 32768;
    }

    bool run(const uint8_t* code, size_t n) {  // false at endchar
        if (++depth > 10) throw std::runtime_error("charstring subroutines nest too deep");
        size_t at = 0;
        const i32 F = 65536;
        while (at < n) {
            unsigned op = code[at];
            if (op >= 32 || op == 28) {
                if (op == 28) { s.push_back((i32)(int16_t)((code[at + 1] << 8) | code[at + 2]) * F); at += 3; }
                else if (op <= 246) { s.push_back(((i32)op - 139) * F); at += 1; }
                else if (op <= 250) { s.push_back((((i32)op - 247) * 256 + code[at + 1] + 108) * F); at += 2; }
                else if (op <= 254) { s.push_back((-((i32)op - 251) * 256 - code[at + 1] - 108) * F); at += 2; }
                else { s.push_back((i32)(((uint32_t)code[at + 1] << 24) | (code[at + 2] << 16) | (code[at + 3] << 8) | code[at + 4])); at += 5; }
                continue;
            }
            ++at;
            size_t k = s.size();
            switch (op) {
            case 1: case 3: case 18: case 23:
                width(true); stems += (int)s.size() / 2; s.clear(); break;
            case 19: case 20:
                width(true); stems += (int)s.size() / 2; s.clear();
                at += (size_t)(stems + 7) / 8;
                if (op == 19) gp.hint_new = true;
                break;
            case 21: width(true); k = s.size(); move(s[k - 2], s[k - 1]); s.clear(); break;
            case 22: width(false); k = s.size(); move(s[k - 1], 0); s.clear(); break;
            case 4: width(false); k = s.size(); move(0, s[k - 1]); s.clear(); break;
            case 5: for (size_t i = 0; i + 1 < k; i += 2) line(s[i], s[i + 1]); s.clear(); break;
            case 6: case 7: {
                bool h = op == 6;
                for (size_t i = 0; i < k; ++i, h = !h) h ? line(s[i], 0) : line(0, s[i]);
                s.clear(); break;
            }
            case 8: for (size_t i = 0; i + 6 <= k; i += 6) curve(s[i], s[i + 1], s[i + 2], s[i + 3], s[i + 4], s[i + 5]); s.clear(); break;
            case 24:
                for (size_t i = 0; i + 6 <= k - 2; i += 6) curve(s[i], s[i + 1], s[i + 2], s[i + 3], s[i + 4], s[i + 5]);
                line(s[k - 2], s[k - 1]); s.clear(); break;
            case 25: {
                size_t i = 0;
                for (; i + 2 <= k - 6; i += 2) line(s[i], s[i + 1]);
                curve(s[i], s[i + 1], s[i + 2], s[i + 3], s[i + 4], s[i + 5]); s.clear(); break;
            }
            case 27: case 26: {
                size_t i = k % 2;
                i32 first = i ? s[0] : 0;
                for (; i + 4 <= k; i += 4, first = 0)
                    op == 27 ? curve(s[i], first, s[i + 1], s[i + 2], s[i + 3], 0)
                             : curve(first, s[i], s[i + 1], s[i + 2], 0, s[i + 3]);
                s.clear(); break;
            }
            case 30: case 31: {
                bool h = op == 31;
                for (size_t i = 0; i + 4 <= k; i += 4, h = !h) {
                    i32 last = k - i == 5 ? s[i + 4] : 0;
                    h ? curve(s[i], 0, s[i + 1], s[i + 2], last, s[i + 3]) : curve(0, s[i], s[i + 1], s[i + 2], s[i + 3], last);
                }
                s.clear(); break;
            }
            case 10: case 29: {
                const Index& subrs = op == 10 ? fd.subrs : cff.gsubrs;
                i32 index = (s.back() >> 16) + bias(subrs);
                s.pop_back();
                if (index < 0 || (size_t)index >= subrs.items.size()) throw std::runtime_error("charstring calls a missing subroutine");
                if (!run(subrs.items[index].first, subrs.items[index].second)) { --depth; return false; }
                break;
            }
            case 11: --depth; return true;
            case 14:
                width(true);
                if (s.size() >= 4) throw std::runtime_error("charstring uses seac");
                s.clear();
                --depth;
                return false;
            case 12: {
                unsigned esc = code[at++];
                if (esc == 35 && k >= 12) { curve(s[0], s[1], s[2], s[3], s[4], s[5]); curve(s[6], s[7], s[8], s[9], s[10], s[11]); }
                else if (esc == 34 && k >= 7) {
                    curve(s[0], 0, s[1], s[2], s[3], 0); curve(s[4], 0, s[5], -s[2], s[6], 0);
                } else if (esc == 36 && k >= 9) {
                    curve(s[0], s[1], s[2], s[3], s[4], 0); curve(s[5], 0, s[6], s[7], s[8], -(s[1] + s[3] + s[7]));
                } else if (esc == 37 && k >= 11) {
                    i32 dx = s[0] + s[2] + s[4] + s[6] + s[8], dy = s[1] + s[3] + s[5] + s[7] + s[9];
                    curve(s[0], s[1], s[2], s[3], s[4], s[5]);
                    if (std::abs(dx) > std::abs(dy)) curve(s[6], s[7], s[8], s[9], s[10], -dy);
                    else curve(s[6], s[7], s[8], s[9], -dx, s[10]);
                } else {
                    throw std::runtime_error("unsupported charstring escape operator " + std::to_string(esc));
                }
                s.clear(); break;
            }
            default:
                throw std::runtime_error("unsupported charstring operator " + std::to_string(op));
            }
        }
        --depth;
        return true;
    }
};

// character space, 16.16
i32 darkening(i32 em_ratio, i32 ppem, i32 stem, const int* p) {
    if (em_ratio < 0x28F) return 0;
    i32 per1000 = mulfix(stem, em_ratio), scaled;
    if (em_ratio > 0x10000 && per1000 <= stem) { per1000 = 0; scaled = p[6] << 16; }
    else {
        scaled = mulfix(per1000, ppem);
        if (ppem > 0x10000 && scaled <= per1000) scaled = p[6] << 16;
    }
    auto seg = [&](int xa, int ya, int xb, int yb) {
        return muldiv(per1000 - divfix(xa << 16, ppem), yb - ya, xb - xa) + divfix(ya << 16, ppem);
    };
    i32 out;
    if (scaled < (p[0] << 16)) out = divfix(p[1] << 16, ppem);
    else if (scaled < (p[2] << 16) && p[2] != p[0]) out = seg(p[0], p[1], p[2], p[3]);
    else if (scaled < (p[4] << 16) && p[4] != p[2]) out = seg(p[2], p[3], p[4], p[5]);
    else if (scaled < (p[6] << 16) && p[6] != p[4]) out = seg(p[4], p[5], p[6], p[7]);
    else out = divfix(p[7] << 16, ppem);
    return divfix(out, em_ratio * 2);
}

// FreeType 2.5.3's FT_Vector_Length (CORDIC).
i32 vector_length(i32 x, i32 y) {
    if (x == 0) return std::abs(y);
    if (y == 0) return std::abs(x);
    uint32_t z = (uint32_t)std::abs(x) | (uint32_t)std::abs(y);
    int shift = 0;
    while (z >>= 1) ++shift;
    if (shift <= 29) { shift = 29 - shift; x = (i32)((uint32_t)x << shift); y = (i32)((uint32_t)y << shift); }
    else { shift -= 29; x >>= shift; y >>= shift; shift = -shift; }
    if (y > x) {
        if (y > -x) { i32 t = y; y = -x; x = t; }
        else { x = -x; y = -y; }
    } else if (y < -x) { i32 t = -y; y = x; x = t; }
    for (i32 i = 1, b = 1; i < 23; b <<= 1, i++) {
        i32 t = y > 0 ? x + ((y + b) >> i) : x - ((y + b) >> i);
        y = y > 0 ? y - ((x + b) >> i) : y + ((x + b) >> i);
        x = t;
    }
    i64 v = ((i64)std::abs(x) * 0xDBD95B16LL + 0x100000000LL) >> 32;
    x = x < 0 ? -(i32)v : (i32)v;
    return shift > 0 ? (x + (1 << (shift - 1))) >> shift : (i32)((uint32_t)x << -shift);
}

// FT_Outline_Get_Orientation: 0 TrueType, 1 PostScript, 2 none.
int orientation(const std::vector<std::vector<Vec>>& contours) {
    bool any = false;
    i32 x0 = 0, y0 = 0, x1 = 0, y1 = 0;
    for (auto& c : contours) for (auto& v : c) {
        if (!any) { x0 = x1 = v.x; y0 = y1 = v.y; any = true; }
        x0 = std::min(x0, v.x); x1 = std::max(x1, v.x); y0 = std::min(y0, v.y); y1 = std::max(y1, v.y);
    }
    if (!any) return 0;
    if (x0 == x1 || y0 == y1) return 2;
    auto msb = [](uint32_t z) { int s = 0; while (z >>= 1) ++s; return s; };
    int xs = std::max(msb((uint32_t)std::abs(x1) | (uint32_t)std::abs(x0)) - 14, 0);
    int ys = std::max(msb((uint32_t)(y1 - y0)) - 14, 0);
    uint32_t area = 0;
    for (auto& c : contours) {
        Vec prev = c.back();
        for (auto& v : c) {
            area += (uint32_t)((v.y - prev.y) >> ys) * (uint32_t)((v.x + prev.x) >> xs);
            prev = v;
        }
    }
    return (i32)area > 0 ? 1 : (i32)area < 0 ? 0 : 2;
}

// FT_Outline_EmboldenXY as SF6's FreeType has it: each point moved by its in and out unit vectors.
void embolden(std::vector<std::vector<Vec>>& contours, i32 xstrength, i32 ystrength) {
    xstrength /= 2;
    ystrength /= 2;
    int orient = orientation(contours);
    if ((xstrength == 0 && ystrength == 0) || orient == 2) return;
    for (auto& c : contours) {
        Vec first = c.front(), cur = first;
        i32 inx = cur.x - c.back().x, iny = cur.y - c.back().y, l_in = vector_length(inx, iny);
        if (l_in) { inx = divfix(inx, l_in); iny = divfix(iny, l_in); }
        for (size_t n = 0; n < c.size(); ++n) {
            Vec next = n + 1 < c.size() ? c[n + 1] : first;
            i32 outx = next.x - cur.x, outy = next.y - cur.y, l_out = vector_length(outx, outy);
            if (l_out) { outx = divfix(outx, l_out); outy = divfix(outy, l_out); }
            i32 d = mulfix(inx, outx) + mulfix(iny, outy), sx = 0, sy = 0;
            if (d > -0xF000) {
                d += 0x10000;
                sx = orient == 0 ? -(iny + outy) : iny + outy;
                sy = orient == 0 ? inx + outx : -(inx + outx);
                i32 q = orient == 0 ? mulfix(outy, inx) - mulfix(outx, iny) : mulfix(outx, iny) - mulfix(outy, inx);
                i32 l = std::min(l_in, l_out);
                sx = mulfix(xstrength, q) <= mulfix(l, d) ? muldiv(sx, xstrength, d) : muldiv(sx, l, q);
                sy = mulfix(ystrength, q) <= mulfix(l, d) ? muldiv(sy, ystrength, d) : muldiv(sy, l, q);
            }
            c[n] = {cur.x + xstrength + sx, cur.y + ystrength + sy};
            inx = outx; iny = outy; l_in = l_out; cur = next;
        }
    }
}

constexpr int PIXEL_BITS = 8, ONE_PIXEL = 1 << PIXEL_BITS;

i32 cdiv(i32 a, i32 b) { return a / b; }

struct Gray {
    int min_ex, max_ex, min_ey, max_ey;
    std::vector<std::map<int, std::pair<i32, i32>>> rows;  // per row: x -> (cover, area)
    int ex = 0, ey = 0;
    i32 area = 0, cover = 0;
    bool invalid = true;
    i32 x = 0, y = 0, last_ey = 0;

    Gray(int w, int h) : min_ex(0), max_ex(w), min_ey(0), max_ey(h), rows(h) {}

    void record() {
        if (!invalid && (area | cover)) {
            auto& c = rows[ey][std::min(ex, max_ex)];
            c.first += cover;
            c.second += area;
        }
    }
    void set_cell(int nx, int ny) {
        if (nx > max_ex) nx = max_ex;
        if (nx < min_ex) nx = min_ex - 1;
        if (nx != ex || ny != ey) { record(); area = cover = 0; ex = nx; ey = ny; }
        invalid = ny >= max_ey || ny < min_ey || nx >= max_ex;
    }
    void start_cell(int nx, int ny) {
        if (nx > max_ex) nx = max_ex;
        if (nx < min_ex) nx = min_ex - 1;
        area = cover = 0; ex = nx; ey = ny;
        invalid = ny >= max_ey || ny < min_ey || nx >= max_ex;
    }
    void move_to(i32 px, i32 py) {
        record();
        px *= 4; py *= 4;
        start_cell(px >> PIXEL_BITS, py >> PIXEL_BITS);
        x = px; y = py; last_ey = (py >> PIXEL_BITS) << PIXEL_BITS;
    }

    void scanline(int sey, i32 x1, i32 y1, i32 x2, i32 y2) {
        i32 dx = x2 - x1;
        int ex1 = x1 >> PIXEL_BITS, ex2 = x2 >> PIXEL_BITS;
        i32 fx1 = x1 - (ex1 << PIXEL_BITS), fx2 = x2 - (ex2 << PIXEL_BITS);
        if (y1 == y2) { set_cell(ex2, sey); return; }
        if (ex1 == ex2) { i32 delta = y2 - y1; area += (fx1 + fx2) * delta; cover += delta; return; }
        i32 p = (ONE_PIXEL - fx1) * (y2 - y1), first = ONE_PIXEL;
        int incr = 1;
        if (dx < 0) { p = fx1 * (y2 - y1); first = 0; incr = -1; dx = -dx; }
        i32 delta = cdiv(p, dx), mod = p % dx;
        if (mod < 0) { delta--; mod += dx; }
        area += (fx1 + first) * delta;
        cover += delta;
        ex1 += incr;
        set_cell(ex1, sey);
        y1 += delta;
        if (ex1 != ex2) {
            p = ONE_PIXEL * (y2 - y1 + delta);
            i32 lift = cdiv(p, dx), rem = p % dx;
            if (rem < 0) { lift--; rem += dx; }
            mod -= dx;
            while (ex1 != ex2) {
                delta = lift;
                mod += rem;
                if (mod >= 0) { mod -= dx; delta++; }
                area += ONE_PIXEL * delta;
                cover += delta;
                y1 += delta;
                ex1 += incr;
                set_cell(ex1, sey);
            }
        }
        delta = y2 - y1;
        area += (fx2 + ONE_PIXEL - first) * delta;
        cover += delta;
    }

    void line(i32 to_x, i32 to_y) {
        int ey1 = last_ey >> PIXEL_BITS, ey2 = to_y >> PIXEL_BITS;
        i32 fy1 = y - last_ey, fy2 = to_y - (ey2 << PIXEL_BITS);
        i32 dx = to_x - x, dy = to_y - y;
        if (std::min(ey1, ey2) >= max_ey || std::max(ey1, ey2) < min_ey) {
        } else if (ey1 == ey2) {
            scanline(ey1, x, fy1, to_x, fy2);
        } else if (dx == 0) {
            int ex0 = x >> PIXEL_BITS;
            i32 two_fx = (x - (ex0 << PIXEL_BITS)) << 1, first = ONE_PIXEL;
            int incr = 1;
            if (dy < 0) { first = 0; incr = -1; }
            i32 delta = first - fy1;
            area += two_fx * delta; cover += delta;
            ey1 += incr;
            set_cell(ex0, ey1);
            delta = first + first - ONE_PIXEL;
            i32 step = two_fx * delta;
            while (ey1 != ey2) { area += step; cover += delta; ey1 += incr; set_cell(ex0, ey1); }
            delta = fy2 - ONE_PIXEL + first;
            area += two_fx * delta; cover += delta;
        } else {
            i32 p = (ONE_PIXEL - fy1) * dx, first = ONE_PIXEL;
            int incr = 1;
            if (dy < 0) { p = fy1 * dx; first = 0; incr = -1; dy = -dy; }
            i32 delta = cdiv(p, dy), mod = p % dy;
            if (mod < 0) { delta--; mod += dy; }
            i32 cx = x + delta;
            scanline(ey1, x, fy1, cx, first);
            ey1 += incr;
            set_cell(cx >> PIXEL_BITS, ey1);
            if (ey1 != ey2) {
                p = ONE_PIXEL * dx;
                i32 lift = cdiv(p, dy), rem = p % dy;
                if (rem < 0) { lift--; rem += dy; }
                mod -= dy;
                while (ey1 != ey2) {
                    delta = lift;
                    mod += rem;
                    if (mod >= 0) { mod -= dy; delta++; }
                    i32 x2 = cx + delta;
                    scanline(ey1, cx, ONE_PIXEL - first, x2, first);
                    cx = x2;
                    ey1 += incr;
                    set_cell(cx >> PIXEL_BITS, ey1);
                }
            }
            scanline(ey1, cx, ONE_PIXEL - first, to_x, fy2);
        }
        x = to_x; y = to_y; last_ey = ey2 << PIXEL_BITS;
    }

    static void split(Vec* b) {
        for (int k = 0; k < 2; ++k) {
            auto at = [&](int i) -> i32& { return k ? b[i].y : b[i].x; };
            i32 p0 = at(0), p1 = at(1), p2 = at(2), p3 = at(3);
            at(6) = p3;
            i32 a = (p0 + p1) / 2, bb = (p3 + p2) / 2, c = (p1 + p2) / 2;
            at(1) = a; at(5) = bb;
            a = (a + c) / 2; bb = (bb + c) / 2;
            at(2) = a; at(4) = bb;
            at(3) = (a + bb) / 2;
        }
    }

    void cubic(Vec c1, Vec c2, Vec to) {
        Vec stack[32 * 3 + 1];
        Vec* arc = stack;
        arc[0] = {to.x * 4, to.y * 4}; arc[1] = {c2.x * 4, c2.y * 4}; arc[2] = {c1.x * 4, c1.y * 4}; arc[3] = {x, y};
        i32 lo = std::min({arc[0].y, arc[1].y, arc[2].y, arc[3].y}), hi = std::max({arc[0].y, arc[1].y, arc[2].y, arc[3].y});
        if ((lo >> PIXEL_BITS) >= max_ey || (hi >> PIXEL_BITS) < min_ey) { line(arc[0].x, arc[0].y); return; }
        for (;;) {
            i32 dx = (i32)((uint32_t)arc[3].x - (uint32_t)arc[0].x), dy = (i32)((uint32_t)arc[3].y - (uint32_t)arc[0].y);
            i32 ax = std::abs(dx), ay = std::abs(dy);
            i32 L = (236 * std::max(ax, ay) + 97 * std::min(ax, ay)) >> 8;
            bool cut = L > 32767;
            if (!cut) {
                i32 limit = L * (ONE_PIXEL / 6);
                i32 dx1 = arc[1].x - arc[0].x, dy1 = arc[1].y - arc[0].y, dx2 = arc[2].x - arc[0].x, dy2 = arc[2].y - arc[0].y;
                i32 s1 = (i32)((uint32_t)dx1 * (uint32_t)dy - (uint32_t)dy1 * (uint32_t)dx);
                i32 s2 = (i32)((uint32_t)dx2 * (uint32_t)dy - (uint32_t)dy2 * (uint32_t)dx);
                i32 d1 = (i32)((uint32_t)dy1 * (uint32_t)(dy1 - dy) + (uint32_t)dx1 * (uint32_t)(dx1 - dx));
                i32 d2 = (i32)((uint32_t)dy2 * (uint32_t)(dy2 - dy) + (uint32_t)dx2 * (uint32_t)(dx2 - dx));
                cut = std::abs(s1) > limit || std::abs(s2) > limit || d1 > 0 || d2 > 0;
            }
            if (cut && arc + 3 < stack + 32 * 3 - 3) { split(arc); arc += 3; continue; }
            line(arc[0].x, arc[0].y);
            if (arc == stack) return;
            arc -= 3;
        }
    }

    void conic(Vec control, Vec to) {
        Vec stack[32 * 3 + 1];
        int levels[32], top = 0;
        Vec* arc = stack;
        arc[0] = {to.x * 4, to.y * 4}; arc[1] = {control.x * 4, control.y * 4}; arc[2] = {x, y};
        i32 dx = std::max(std::abs(arc[2].x + arc[0].x - 2 * arc[1].x), std::abs(arc[2].y + arc[0].y - 2 * arc[1].y));
        i32 lo = std::min({arc[0].y, arc[1].y, arc[2].y}), hi = std::max({arc[0].y, arc[1].y, arc[2].y});
        if (dx < ONE_PIXEL / 4 || (lo >> PIXEL_BITS) >= max_ey || (hi >> PIXEL_BITS) < min_ey)
            return line(arc[0].x, arc[0].y);
        levels[0] = 0;
        do { dx >>= 2; ++levels[0]; } while (dx > ONE_PIXEL / 4);
        while (top >= 0) {
            if (levels[top] > 0) {
                for (int k = 0; k < 2; ++k) {
                    auto at = [&](int i) -> i32& { return k ? arc[i].y : arc[i].x; };
                    at(4) = at(2);
                    i32 b = at(1), a = at(3) = (at(2) + b) / 2;
                    b = at(1) = (at(0) + b) / 2;
                    at(2) = (a + b) / 2;
                }
                arc += 2;
                ++top;
                levels[top] = levels[top - 1] = levels[top - 1] - 1;
                continue;
            }
            line(arc[0].x, arc[0].y);
            --top;
            arc -= 2;
        }
    }

    // FT_Outline_Decompose: tags 1 on the curve, 0 a conic control, 2 a cubic control.
    void decompose(const std::vector<std::vector<Vec>>& pts, const std::vector<std::vector<uint8_t>>& tags) {
        for (size_t c = 0; c < pts.size(); ++c) {
            const auto& p = pts[c];
            const auto& t = tags[c];
            if (p.empty()) continue;
            Vec start = p[0];
            int i = 0, limit = (int)p.size() - 1;
            bool closed = false;
            if (t[0] == 0) {
                if (t[limit] == 1) start = p[limit--];
                else start = {(start.x + p[limit].x) / 2, (start.y + p[limit].y) / 2};
                i = -1;
            }
            move_to(start.x, start.y);
            while (i < limit && !closed) {
                ++i;
                if (t[i] == 1) line(p[i].x * 4, p[i].y * 4);
                else if (t[i] == 0) {
                    Vec control = p[i];
                    for (;;) {
                        if (i >= limit) { conic(control, start); closed = true; break; }
                        Vec to = p[++i];
                        if (t[i] == 1) { conic(control, to); break; }
                        conic(control, {(control.x + to.x) / 2, (control.y + to.y) / 2});
                        control = to;
                    }
                } else {
                    i += 2;
                    closed = i > limit;
                    cubic(p[i - 2], p[i - 1], closed ? start : p[i]);
                }
            }
            if (!closed) line(start.x * 4, start.y * 4);
        }
        record();
    }

    void sweep(uint8_t* out, int width, int height) const {
        auto hline = [&](int hx, int hy, i32 coverage, int count) {
            coverage >>= PIXEL_BITS * 2 + 1 - 8;
            if (coverage < 0) coverage = -coverage;
            if (coverage >= 256) coverage = 255;
            if (!coverage) return;
            uint8_t* row = out + (size_t)(height - 1 - hy) * width;
            for (int i = std::max(hx, 0); i < std::min(hx + count, width); ++i) row[i] = (uint8_t)coverage;
        };
        for (int ry = min_ey; ry < max_ey; ++ry) {
            int cx = min_ex;
            i32 cov = 0;
            for (const auto& [ex0, ca] : rows[ry]) {
                if (cov != 0 && ex0 > cx) hline(cx, ry, cov * (ONE_PIXEL * 2), ex0 - cx);
                cov += ca.first;
                i32 a = cov * (ONE_PIXEL * 2) - ca.second;
                if (a != 0 && ex0 >= min_ex) hline(ex0, ry, a, 1);
                cx = ex0 + 1;
            }
            if (cov != 0) hline(cx, ry, cov * (ONE_PIXEL * 2), max_ex - cx);
        }
    }
};

struct FontObject {
    PyObject_HEAD
    Cff* cff;
    int units;
};

void font_dealloc(PyObject* self) {
    delete ((FontObject*)self)->cff;
    PyTypeObject* type = Py_TYPE(self);
    PyObject_Free(self);
    Py_DECREF(type);
}

int font_init(PyObject* self, PyObject* args, PyObject*) {
    Py_buffer view;
    int units;
    if (!PyArg_ParseTuple(args, "y*i", &view, &units)) return -1;
    ((FontObject*)self)->units = units ? units : 1000;
    try {
        ((FontObject*)self)->cff = new Cff((const uint8_t*)view.buf, (size_t)view.len);
    } catch (const std::exception& error) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, error.what());
        return -1;
    }
    PyBuffer_Release(&view);
    return 0;
}

// A scaled outline (26.6) to (width, rows, left, top, bytes, box, advance, strength): box its control box (xMin, yMin,
// xMax, yMax); a shear moves each point's x by (int)(shear * y) first, and a sheared outline without points has xMin
// INT_MAX; a strength then emboldens the outline as FT_GlyphSlot_Embolden does (yMax raised by it).
PyObject* finish(std::vector<std::vector<Vec>>& points, const std::vector<std::vector<uint8_t>>& tags, float shear,
                 i32 strength, i32 advance) {
    if (shear != 0.0f)
        for (auto& contour : points) for (auto& v : contour) v.x += (i32)(shear * (float)v.y);
    i32 box[4] = {0, 0, 0, 0}, x0 = 0, y0 = 0, x1 = 0, y1 = 0;
    for (int pass = 0; pass < 2; ++pass) {
        bool any = false;
        for (auto& contour : points)
            for (auto& v : contour) {
                if (!any) { x0 = x1 = v.x; y0 = y1 = v.y; any = true; }
                x0 = std::min(x0, v.x); x1 = std::max(x1, v.x); y0 = std::min(y0, v.y); y1 = std::max(y1, v.y);
            }
        if (pass == 0) {
            box[0] = shear != 0.0f && !any ? 0x7fffffff : x0, box[1] = y0, box[2] = x1, box[3] = y1 + strength;
            if (!strength) break;
            embolden(points, strength, strength);
        }
    }
    x0 &= ~63; y0 &= ~63; x1 = (x1 + 63) & ~63; y1 = (y1 + 63) & ~63;
    int width = (x1 - x0) >> 6, rows = (y1 - y0) >> 6;
    std::vector<uint8_t> bitmap((size_t)width * rows, 0);
    if (width > 0 && rows > 0) {
        for (auto& contour : points) for (auto& v : contour) v = {v.x - x0, v.y - y0};
        Gray gray(width, rows);
        gray.decompose(points, tags);
        gray.sweep(bitmap.data(), width, rows);
    }
    return Py_BuildValue("(iiiiy#(iiii)ii)", width, rows, x0 >> 6, y1 >> 6,
                         bitmap.empty() ? "" : (const char*)bitmap.data(), (Py_ssize_t)bitmap.size(), box[0], box[1],
                         box[2], box[3], advance, strength);
}

// render(gid, x_scale, y_scale, y_ppem, darkening params or None, shear=0, bold=False) -> finish's tuple for the glyph
// at the size's scales (16.16) and y pixels per em; advance 26.6, without the strength.
PyObject* font_render(PyObject* self, PyObject* args) {
    unsigned gid;
    int x_scale, y_scale, y_ppem, bold = 0;
    PyObject* params;
    float shear = 0.0f;
    if (!PyArg_ParseTuple(args, "IiiiO|fp", &gid, &x_scale, &y_scale, &y_ppem, &params, &shear, &bold)) return nullptr;
    Cff* cff = ((FontObject*)self)->cff;
    int upem = ((FontObject*)self)->units;
    if (gid >= cff->charstrings.items.size()) { PyErr_SetString(PyExc_IndexError, "glyph index out of range"); return nullptr; }
    int p[8] = {0};
    bool darkened = params != Py_None;
    if (darkened && (!PyArg_ParseTuple(params, "iiiiiiii", &p[0], &p[1], &p[2], &p[3], &p[4], &p[5], &p[6], &p[7]))) return nullptr;
    try {
        const SubFont& fd = cff->fd(gid);
        i32 xo = 0, yo = 0;
        if (darkened) {
            i32 ppem = std::max(y_ppem << 16, 0x40000), em_ratio = (i32)(0x3E80000LL / upem);
            i32 vw = fd.std_vw << 16;
            if (vw < 1) vw = divfix(0x4B0000, em_ratio);
            xo = darkening(em_ratio, ppem, vw, p);
            i32 hw = divfix((fd.std_hw << 16) >= 1 && vw > fd.std_hw * 0x20000 ? 0x4B0000 : 0x6E0000, em_ratio);
            yo = darkening(em_ratio, ppem, hw, p);
        }
        bool offset = xo != 0 || yo != 0;
        Builder builder;
        i32 momentum, advance;
        {
            GlyphPath gp(builder, offset, xo, yo, false);
            Type2 t2{*cff, fd, gp};
            t2.run(cff->charstrings.items[gid].first, cff->charstrings.items[gid].second);
            gp.close_open();
            builder.close();
            momentum = gp.momentum;
            advance = (int16_t)(((uint32_t)t2.advance + 0x8000u) >> 16);
        }
        if (offset && momentum < 0) {
            builder = Builder();
            GlyphPath gp(builder, offset, xo, yo, true);
            Type2 t2{*cff, fd, gp};
            t2.run(cff->charstrings.items[gid].first, cff->charstrings.items[gid].second);
            gp.close_open();
            builder.close();
        }
        for (auto& contour : builder.points)
            for (auto& v : contour) v = {mulfix(v.x, x_scale), mulfix(v.y, y_scale)};
        return finish(builder.points, builder.tags, shear, bold ? mulfix(upem, y_scale) / 24 : 0,
                      mulfix(advance, x_scale));
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    }
}

PyObject* font_glyphs(PyObject* self, PyObject*) {
    return PyLong_FromSize_t(((FontObject*)self)->cff->charstrings.items.size());
}

// Each glyph's advance in font units as FreeType's FT_Get_Advance reads it unscaled (INT32_MIN where its charstring
// fails, which FT_Get_Advance reports as an error).
PyObject* font_widths(PyObject* self, PyObject*) {
    Cff* cff = ((FontObject*)self)->cff;
    std::vector<i32> out(cff->charstrings.items.size());
    for (size_t gid = 0; gid < out.size(); ++gid) {
        try {
            Builder builder;
            GlyphPath gp(builder, false, 0, 0, false);
            Type2 t2{*cff, cff->fd((unsigned)gid), gp};
            t2.run(cff->charstrings.items[gid].first, cff->charstrings.items[gid].second);
            out[gid] = (int16_t)(((uint32_t)t2.advance + 0x8000u) >> 16);
        } catch (const std::exception&) {
            out[gid] = INT32_MIN;
        }
    }
    return PyBytes_FromStringAndSize((const char*)out.data(), (Py_ssize_t)(out.size() * sizeof(i32)));
}

PyMethodDef font_methods[] = {
    {"render", font_render, METH_VARARGS,
     "render(gid, x_scale, y_scale, y_ppem, darkening, shear=0, bold=False) -> (width, rows, left, top, bytes, box, "
     "advance, strength)"},
    {"glyph_count", font_glyphs, METH_NOARGS, "glyph_count() -> int"},
    {"widths", font_widths, METH_NOARGS, "widths() -> bytes: int32 advance per glyph in font units"},
    {nullptr, nullptr, 0, nullptr},
};

PyType_Slot font_slots[] = {
    {Py_tp_dealloc, (void*)font_dealloc},
    {Py_tp_init, (void*)font_init},
    {Py_tp_methods, (void*)font_methods},
    {Py_tp_new, (void*)PyType_GenericNew},
    {0, nullptr},
};

PyType_Spec font_spec = {"fastglyph.CffFont", sizeof(FontObject), 0, Py_TPFLAGS_DEFAULT, font_slots};

// HarfBuzz 1.0.4 over a face as the games' hb-ft font functions see it through FreeType 2.5.3 (unhinted): the cmap
// FreeType selected, advances FT_MulDiv(width, x_scale, 64) rounded from 16.16 to 26.6, origins 0. A callback hb-ft
// answers from FreeType that the port lacks records its name and the shape is refused.
struct Shaping {
    PyObject* data = nullptr;
    hb_font_t* font = nullptr;
    std::vector<uint32_t> codes, glyphs;
    std::vector<i32> widths;
    i32 x_scale = 0;
    bool variants = false;
    const char* unported = nullptr;
    ~Shaping() {
        if (font) hb_font_destroy(font);
        Py_XDECREF(data);
    }
};

struct ShaperObject {
    PyObject_HEAD
    Shaping* shaping;
};

hb_bool_t shaper_glyph(hb_font_t*, void* data, hb_codepoint_t code, hb_codepoint_t selector, hb_codepoint_t* glyph,
                       void*) {
    auto* self = (Shaping*)data;
    *glyph = 0;
    if (selector) {
        if (self->variants && !self->unported) self->unported = "a variation selector's glyph";
        return false;
    }
    auto at = std::lower_bound(self->codes.begin(), self->codes.end(), code);
    if (at != self->codes.end() && *at == code) *glyph = self->glyphs[at - self->codes.begin()];
    if (*glyph >= self->widths.size()) *glyph = 0;
    return *glyph != 0;
}

hb_position_t shaper_advance(hb_font_t*, void* data, hb_codepoint_t glyph, void*) {
    auto* self = (Shaping*)data;
    if (glyph >= self->widths.size() || self->widths[glyph] == INT32_MIN) return 0;
    return (muldiv(self->widths[glyph], self->x_scale, 64) + 0x200) >> 10;
}

hb_bool_t shaper_origin(hb_font_t*, void*, hb_codepoint_t, hb_position_t*, hb_position_t*, void*) { return true; }

template <const char* Name, typename R, typename... A>
R shaper_unported(hb_font_t*, void* data, A..., void*) {
    auto* self = (Shaping*)data;
    if (!self->unported) self->unported = Name;
    return R();
}

const char vertical[] = "a vertical advance or origin", kerning[] = "a kerning pair outside GPOS",
           extents[] = "a glyph's extents", contour[] = "a contour point", names[] = "a glyph name";

hb_font_funcs_t* shaper_funcs() {
    static hb_font_funcs_t* funcs = [] {
        hb_font_funcs_t* f = hb_font_funcs_create();
        hb_font_funcs_set_glyph_func(f, shaper_glyph, nullptr, nullptr);
        hb_font_funcs_set_glyph_h_advance_func(f, shaper_advance, nullptr, nullptr);
        hb_font_funcs_set_glyph_h_origin_func(f, shaper_origin, nullptr, nullptr);
        hb_font_funcs_set_glyph_v_advance_func(f, shaper_unported<vertical, hb_position_t, hb_codepoint_t>, nullptr,
                                               nullptr);
        hb_font_funcs_set_glyph_v_origin_func(
            f, shaper_unported<vertical, hb_bool_t, hb_codepoint_t, hb_position_t*, hb_position_t*>, nullptr, nullptr);
        hb_font_funcs_set_glyph_h_kerning_func(
            f, shaper_unported<kerning, hb_position_t, hb_codepoint_t, hb_codepoint_t>, nullptr, nullptr);
        hb_font_funcs_set_glyph_v_kerning_func(
            f, shaper_unported<kerning, hb_position_t, hb_codepoint_t, hb_codepoint_t>, nullptr, nullptr);
        hb_font_funcs_set_glyph_extents_func(
            f, shaper_unported<extents, hb_bool_t, hb_codepoint_t, hb_glyph_extents_t*>, nullptr, nullptr);
        hb_font_funcs_set_glyph_contour_point_func(
            f, shaper_unported<contour, hb_bool_t, hb_codepoint_t, unsigned, hb_position_t*, hb_position_t*>, nullptr,
            nullptr);
        hb_font_funcs_set_glyph_name_func(f, shaper_unported<names, hb_bool_t, hb_codepoint_t, char*, unsigned>,
                                          nullptr, nullptr);
        hb_font_funcs_set_glyph_from_name_func(
            f, shaper_unported<names, hb_bool_t, const char*, int, hb_codepoint_t*>, nullptr, nullptr);
        hb_font_funcs_make_immutable(f);
        return f;
    }();
    return funcs;
}

void shaper_dealloc(PyObject* self) {
    delete ((ShaperObject*)self)->shaping;
    PyTypeObject* type = Py_TYPE(self);
    PyObject_Free(self);
    Py_DECREF(type);
}

// Shaper(sfnt, cmap, widths, variants): cmap the (code point, glyph) uint32 pairs FreeType's Unicode charmap gives,
// sorted by code point; widths int32 per glyph (CffFont.widths); variants whether the font has a cmap format 14.
int shaper_init(PyObject* self, PyObject* args, PyObject*) {
    PyObject* data;
    Py_buffer cmap, widths;
    int variants;
    if (!PyArg_ParseTuple(args, "Sy*y*p", &data, &cmap, &widths, &variants)) return -1;
    auto* s = new Shaping();
    delete ((ShaperObject*)self)->shaping;
    ((ShaperObject*)self)->shaping = s;
    s->widths.assign((const i32*)widths.buf, (const i32*)widths.buf + widths.len / 4);
    const uint32_t* pairs = (const uint32_t*)cmap.buf;
    for (Py_ssize_t at = 0; at + 1 < cmap.len / 4; at += 2) {
        s->codes.push_back(pairs[at]);
        s->glyphs.push_back(pairs[at + 1]);
    }
    PyBuffer_Release(&cmap);
    PyBuffer_Release(&widths);
    Py_INCREF(data);
    s->data = data;
    s->variants = variants != 0;
    hb_blob_t* blob = hb_blob_create(PyBytes_AS_STRING(data), (unsigned)PyBytes_GET_SIZE(data), HB_MEMORY_MODE_READONLY,
                                     nullptr, nullptr);
    hb_face_t* face = hb_face_create(blob, 0);
    s->font = hb_font_create(face);
    hb_face_destroy(face);
    hb_blob_destroy(blob);
    hb_font_set_funcs(s->font, shaper_funcs(), s, nullptr);
    return 0;
}

// shape(text, script, rtl, x_scale, y_scale, size_scale) -> (infos, positions): text uint32 code points shaped as the
// games' hb_buffer_add_utf32(text, -1, 0, -1) reads them (up to the first 0), the font's scales the 26.6 size,
// size_scale FreeType's 16.16 x scale for the advances; the buffer's 20-byte glyph infos and positions.
PyObject* shaper_shape(PyObject* self, PyObject* args) {
    Shaping* s = ((ShaperObject*)self)->shaping;
    Py_buffer text;
    unsigned script;
    int rtl, x, y, scale;
    if (!s) { PyErr_SetString(PyExc_ValueError, "the shaper has no font"); return nullptr; }
    if (!PyArg_ParseTuple(args, "y*Ipiii", &text, &script, &rtl, &x, &y, &scale)) return nullptr;
    std::vector<uint32_t> codes((const uint32_t*)text.buf, (const uint32_t*)text.buf + text.len / 4);
    PyBuffer_Release(&text);
    codes.push_back(0);
    s->x_scale = scale;
    s->unported = nullptr;
    hb_font_set_scale(s->font, x, y);
    hb_buffer_t* buffer = hb_buffer_create();
    hb_buffer_set_script(buffer, (hb_script_t)script);
    hb_buffer_set_direction(buffer, rtl ? HB_DIRECTION_RTL : HB_DIRECTION_LTR);
    hb_buffer_add_utf32(buffer, codes.data(), -1, 0, -1);
    hb_shape(s->font, buffer, nullptr, 0);
    unsigned count = hb_buffer_get_length(buffer);
    PyObject* out = s->unported ? nullptr : Py_BuildValue(
        "(y#y#)", (const char*)hb_buffer_get_glyph_infos(buffer, nullptr), (Py_ssize_t)count * 20,
        (const char*)hb_buffer_get_glyph_positions(buffer, nullptr), (Py_ssize_t)count * 20);
    hb_buffer_destroy(buffer);
    if (s->unported) PyErr_Format(PyExc_ValueError, "HarfBuzz asked the font for %s, which is not ported", s->unported);
    return out;
}

PyMethodDef shaper_methods[] = {
    {"shape", shaper_shape, METH_VARARGS,
     "shape(text, script, rtl, x_scale, y_scale, size_scale) -> (infos, positions): 20-byte records each"},
    {nullptr, nullptr, 0, nullptr},
};

PyType_Slot shaper_slots[] = {
    {Py_tp_dealloc, (void*)shaper_dealloc},
    {Py_tp_init, (void*)shaper_init},
    {Py_tp_methods, (void*)shaper_methods},
    {Py_tp_new, (void*)PyType_GenericNew},
    {0, nullptr},
};

PyType_Spec shaper_spec = {"fastglyph.Shaper", sizeof(ShaperObject), 0, Py_TPFLAGS_DEFAULT, shaper_slots};

struct DfCell {
    int32_t x, y;
    uint32_t d2;
    uint8_t inside;
};

void df_take(DfCell& cell, int32_t x, int32_t y, bool inside_rule) {
    uint32_t d2 = (uint32_t)y * (uint32_t)y + (uint32_t)x * (uint32_t)x;
    if (inside_rule ? (cell.d2 == 0 || d2 < cell.d2) : d2 < cell.d2) cell = {x, y, d2, (uint8_t)(x < 1 && y <= 0)};
}

void df_vertical(DfCell& cell, const DfCell& from) {
    if (!cell.inside)
        df_take(cell, from.inside ? 0 : from.x, from.inside ? 0x100 : from.y + 0x100, false);
    else
        df_take(cell, from.x < 0x100 ? from.x - 0x100 : 0, from.y < 0x100 ? from.y - 0x200 : -0x100, true);
}

void df_horizontal(DfCell& cell, const DfCell& from) {
    if (!cell.inside)
        df_take(cell, from.inside ? 0x100 : from.x + 0x100, from.inside ? 0 : from.y, false);
    else
        df_take(cell, from.x < 0x100 ? from.x - 0x200 : -0x100, from.y < 0x100 ? from.y - 0x100 : 0, true);
}

void df_row(std::vector<DfCell>& grid, int columns, int row, int from_row) {
    DfCell* cells = &grid[(size_t)row * columns];
    for (int c = 0; c < columns; ++c) df_vertical(cells[c], grid[(size_t)from_row * columns + c]);
    for (int c = 0; c + 1 < columns; ++c) df_horizontal(cells[c + 1], cells[c]);
    for (int c = columns - 2; c >= 0; --c) df_horizontal(cells[c], cells[c + 1]);
}

PyObject* distance_field(PyObject*, PyObject* args) {
    Py_buffer source;
    int width, height, cell_columns, cell_rows, spread;
    if (!PyArg_ParseTuple(args, "y*iiiii", &source, &width, &height, &cell_columns, &cell_rows, &spread))
        return nullptr;
    const uint8_t* pixels = (const uint8_t*)source.buf;
    int columns = cell_columns - 2, rows = cell_rows - 2;
    float left = (float)((uint32_t)(columns - width) >> 1), top = (float)((uint32_t)(rows - height) >> 1);
    std::vector<DfCell> grid((size_t)std::max(columns, 0) * std::max(rows, 0));
    for (int r = 0, line = 0; r < rows; ++r) {
        for (int c = 0; c < columns; ++c) {
            int32_t value = spread << 8;
            if (left <= (float)c && (float)c < (float)width + left && top <= (float)r
                && (float)r < top + (float)height) {
                uint8_t texel = pixels[(size_t)line * width + (c - (int)left)];
                if (texel) value = (int32_t)((1.0f - (float)texel / 255.0f) * 256.0f);
            }
            grid[(size_t)r * columns + c] = {value, value, (uint32_t)(value * value * 2), (uint8_t)(value < 1)};
        }
        if (top <= (float)r && (float)r < top + (float)height) ++line;
    }
    for (int r = 1; r < rows; ++r) df_row(grid, columns, r, r - 1);
    for (int r = rows - 2; r >= 0; --r) df_row(grid, columns, r, r + 1);
    std::string out((size_t)cell_columns * cell_rows, '\0');
    float scale = 2.0f / (float)spread;
    for (int r = 0; r < rows; ++r) {
        for (int c = 0; c < columns; ++c) {
            const DfCell& cell = grid[(size_t)r * columns + c];
            float value = (sqrtf((float)cell.d2) * 0.00390625f - 0.5f) * scale;
            if (cell.inside) value = value * -1.0f;
            value = 128.0f - value * 127.0f;
            value = value >= 0.0f ? (value >= 255.0f ? 255.0f : value) : 0.0f;
            out[(size_t)(r + 1) * cell_columns + c + 1] = (char)(int)value;
        }
    }
    PyBuffer_Release(&source);
    return PyBytes_FromStringAndSize(out.data(), (Py_ssize_t)out.size());
}

PyObject* outline(PyObject*, PyObject* args) {
    Py_buffer points, tags, ends;
    float shear;
    int strength, advance;
    if (!PyArg_ParseTuple(args, "y*y*y*fii", &points, &tags, &ends, &shear, &strength, &advance)) return nullptr;
    const i32* p = (const i32*)points.buf;
    const uint8_t* t = (const uint8_t*)tags.buf;
    const uint16_t* e = (const uint16_t*)ends.buf;
    Py_ssize_t count = ends.len / 2, at = 0;
    bool fits = points.len == 8 * tags.len && (!count || e[count - 1] < tags.len);
    for (Py_ssize_t c = 1; fits && c < count; ++c) fits = e[c - 1] < e[c];
    std::vector<std::vector<Vec>> contours(fits ? count : 0);
    std::vector<std::vector<uint8_t>> kinds(contours.size());
    for (size_t c = 0; c < contours.size(); ++c)
        for (; at <= e[c]; ++at) { contours[c].push_back({p[2 * at], p[2 * at + 1]}); kinds[c].push_back(t[at]); }
    PyBuffer_Release(&points);
    PyBuffer_Release(&tags);
    PyBuffer_Release(&ends);
    if (!fits) { PyErr_SetString(PyExc_ValueError, "the contour ends do not fit the points"); return nullptr; }
    return finish(contours, kinds, shear, strength, advance);
}

PyObject* length(PyObject*, PyObject* args) {
    int x, y;
    if (!PyArg_ParseTuple(args, "ii", &x, &y)) return nullptr;
    return PyLong_FromLong(vector_length(x, y));
}

PyMethodDef module_methods[] = {
    {"distance_field", distance_field, METH_VARARGS,
     "distance_field(coverage, width, height, columns, rows, spread) -> bytes: SF6's distance-field cell"},
    {"outline", outline, METH_VARARGS,
     "outline(points, tags, ends, shear, strength, advance) -> CffFont.render's tuple for a scaled outline: points "
     "int32 x, y pairs (26.6), a tag per point (1 on the curve, 0 a conic control, 2 a cubic control), ends each "
     "contour's last point (uint16)"},
    {"vector_length", length, METH_VARARGS, "vector_length(x, y) -> int: FT_Vector_Length"},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module_def = {PyModuleDef_HEAD_INIT, "fastglyph",
                          "FreeType 2.5.3 glyph bitmaps and HarfBuzz 1.0.4 shaping (DMC5, SF6).", -1, module_methods};

}  // namespace

PyMODINIT_FUNC PyInit_fastglyph(void) {
    PyObject* module = PyModule_Create(&module_def);
    if (!module) return nullptr;
    for (auto [spec, name] : {std::pair{&font_spec, "CffFont"}, std::pair{&shaper_spec, "Shaper"}}) {
        PyObject* type = PyType_FromSpec(spec);
        if (!type || PyModule_AddObject(module, name, type) < 0) {
            Py_XDECREF(type);
            Py_DECREF(module);
            return nullptr;
        }
    }
    return module;
}
