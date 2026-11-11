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

    void decompose(const std::vector<std::vector<Vec>>& pts, const std::vector<std::vector<uint8_t>>& tags) {
        for (size_t c = 0; c < pts.size(); ++c) {
            const auto& p = pts[c];
            const auto& t = tags[c];
            if (p.empty()) continue;
            move_to(p[0].x, p[0].y);
            size_t i = 1, n = p.size();
            while (i < n) {
                if (t[i] == 1) { line(p[i].x * 4, p[i].y * 4); ++i; }
                else {
                    Vec to = i + 2 < n ? p[i + 2] : p[0];
                    cubic(p[i], p[i + 1], to);
                    i += 3;
                }
            }
            line(p[0].x * 4, p[0].y * 4);
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

// render(gid, x_ppem, y_ppem, darkening params or None) -> (width, rows, left, top, bytes, box, advance): box the
// scaled outline's control box (xMin, yMin, xMax, yMax) in 26.6, advance in font units.
PyObject* font_render(PyObject* self, PyObject* args) {
    unsigned gid;
    int x_ppem, y_ppem;
    PyObject* params;
    if (!PyArg_ParseTuple(args, "IiiO", &gid, &x_ppem, &y_ppem, &params)) return nullptr;
    Cff* cff = ((FontObject*)self)->cff;
    int upem = ((FontObject*)self)->units;
    i32 x_scale = divfix(x_ppem << 6, upem), y_scale = divfix(y_ppem << 6, upem);
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
        bool any = false;
        i32 x0 = 0, y0 = 0, x1 = 0, y1 = 0;
        for (auto& contour : builder.points)
            for (auto& v : contour) {
                v = {mulfix(v.x, x_scale), mulfix(v.y, y_scale)};
                if (!any) { x0 = x1 = v.x; y0 = y1 = v.y; any = true; }
                x0 = std::min(x0, v.x); x1 = std::max(x1, v.x); y0 = std::min(y0, v.y); y1 = std::max(y1, v.y);
            }
        i32 box[4] = {x0, y0, x1, y1};
        x0 &= ~63; y0 &= ~63; x1 = (x1 + 63) & ~63; y1 = (y1 + 63) & ~63;
        int width = (x1 - x0) >> 6, rows = (y1 - y0) >> 6;
        std::vector<uint8_t> bitmap((size_t)width * rows, 0);
        if (width > 0 && rows > 0) {
            for (auto& contour : builder.points) for (auto& v : contour) v = {v.x - x0, v.y - y0};
            Gray gray(width, rows);
            gray.decompose(builder.points, builder.tags);
            gray.sweep(bitmap.data(), width, rows);
        }
        return Py_BuildValue("(iiiiy#(iiii)i)", width, rows, x0 >> 6, y1 >> 6, bitmap.empty() ? "" : (const char*)bitmap.data(),
                             (Py_ssize_t)bitmap.size(), box[0], box[1], box[2], box[3], advance);
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    }
}

PyObject* font_glyphs(PyObject* self, PyObject*) {
    return PyLong_FromSize_t(((FontObject*)self)->cff->charstrings.items.size());
}

PyMethodDef font_methods[] = {
    {"render", font_render, METH_VARARGS,
     "render(gid, x_ppem, y_ppem, darkening) -> (width, rows, left, top, bytes, box, advance)"},
    {"glyph_count", font_glyphs, METH_NOARGS, "glyph_count() -> int"},
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

PyModuleDef module_def = {PyModuleDef_HEAD_INIT, "fastglyph", "FreeType 2.6 CFF glyph bitmaps (DMC5).", -1, nullptr};

}  // namespace

PyMODINIT_FUNC PyInit_fastglyph(void) {
    PyObject* module = PyModule_Create(&module_def);
    if (!module) return nullptr;
    PyObject* type = PyType_FromSpec(&font_spec);
    if (!type || PyModule_AddObject(module, "CffFont", type) < 0) {
        Py_XDECREF(type);
        Py_DECREF(module);
        return nullptr;
    }
    return module;
}
