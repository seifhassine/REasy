#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define STB_VORBIS_NO_STDIO
#define STB_VORBIS_NO_PUSHDATA_API
#define STB_VORBIS_NO_INTEGER_CONVERSION
#include "third_party/stb_vorbis/stb_vorbis.c"
#include "third_party/opus/include/opus_multistream.h"

typedef struct { uint8_t *p; size_t n, cap; } Buf;
typedef struct { const uint8_t *p; size_t n, pos; int bad; } BitReader;
typedef struct { uint8_t *p; size_t n, pos; int bad; } BitWriter;
typedef struct {
    const uint8_t *fmt, *data, *seek, *books;
    size_t fmt_n, data_n, seek_n, books_n;
    int tag, channels, rate, align, bits, extra, config_type;
    uint32_t mask;
    Buf pcm;
    const char *error;
} Wem;

static uint16_t u16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }
static uint32_t u32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }
static int16_t clamp16(int v) { return (int16_t)(v < -32768 ? -32768 : v > 32767 ? 32767 : v); }

static int fail(Wem *w, const char *message) {
    w->error = message;
    return 0;
}

static uint8_t *reserve(Buf *b, size_t bytes) {
    if (b->n + bytes > b->cap) {
        size_t cap = b->cap * 2 > b->n + bytes ? b->cap * 2 : b->n + bytes + 4096;
        uint8_t *p = realloc(b->p, cap);
        if (!p) return NULL;
        b->p = p;
        b->cap = cap;
    }
    b->n += bytes;
    return b->p + b->n - bytes;
}

static int parse(Wem *w, const uint8_t *p, size_t n) {
    if (n < 12 || memcmp(p, "RIFF", 4) || memcmp(p + 8, "WAVE", 4)) return fail(w, "Not a RIFF WEM.");
    for (size_t pos = 12; pos + 8 <= n;) {
        size_t size = u32(p + pos + 4), left = n - pos - 8;
        const uint8_t *body = p + pos + 8;
        if (!memcmp(p + pos, "data", 4)) {
            w->data = body;
            w->data_n = size < left ? size : left;
            break;
        }
        if (size > left) return fail(w, "WEM chunk runs past the end of the file.");
        if (!memcmp(p + pos, "fmt ", 4)) { w->fmt = body; w->fmt_n = size; }
        if (!memcmp(p + pos, "seek", 4)) { w->seek = body; w->seek_n = size; }
        pos += 8 + size;
    }
    if (!w->data || w->fmt_n < 16) return fail(w, "WEM has no fmt or data chunk.");
    w->tag = u16(w->fmt);
    w->channels = u16(w->fmt + 2);
    w->rate = (int)u32(w->fmt + 4);
    w->align = u16(w->fmt + 12);
    w->bits = u16(w->fmt + 14);
    w->extra = w->fmt_n >= 18 ? u16(w->fmt + 16) : 0;
    if (w->fmt_n >= 24 && w->extra >= 6) {
        uint32_t layout = u32(w->fmt + 20);
        w->mask = layout;
        if ((int)(layout & 0xFF) == w->channels) {
            w->config_type = (layout >> 8) & 0xF;
            w->mask = layout >> 12;
        }
    }
    if (w->channels < 1 || w->channels > 16) return fail(w, "Unsupported WEM channel count.");
    if (w->rate < 1) return fail(w, "Unsupported WEM sample rate.");
    return 1;
}

static int decode_pcm(Wem *w) {
    size_t bytes = w->data_n - w->data_n % (2 * (size_t)w->channels);
    uint8_t *out;
    if (w->bits != 16) return fail(w, "Only 16-bit Wwise PCM is supported.");
    if (!(out = reserve(&w->pcm, bytes))) return fail(w, "Out of memory.");
    memcpy(out, w->data, bytes);
    return 1;
}

static const int16_t IMA_STEP[89] = {
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107,
    118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894,
    6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767};
static const int8_t IMA_INDEX[16] = {-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8};

static int decode_ima(Wem *w) {
    size_t ch = (size_t)w->channels, blocks = w->data_n / (36 * ch);
    int16_t *out;
    if (w->bits != 4 || w->align != 36 * w->channels) return fail(w, "Unsupported Wwise ADPCM block layout.");
    if (!(out = (int16_t *)reserve(&w->pcm, blocks * 64 * ch * 2))) return fail(w, "Out of memory.");
    for (size_t b = 0; b < blocks; b++)
        for (size_t c = 0; c < ch; c++) {
            const uint8_t *block = w->data + (b * ch + c) * 36;
            int16_t *dst = out + b * 64 * ch + c;
            int hist = (int16_t)u16(block), index = (int8_t)block[2];
            index = index < 0 ? 0 : index > 88 ? 88 : index;
            dst[0] = (int16_t)hist;
            for (int i = 0; i < 63; i++) {
                int code = (block[4 + i / 2] >> (i & 1 ? 4 : 0)) & 0xF;
                int delta = (((code & 7) * 2 + 1) * IMA_STEP[index]) >> 3;
                hist = clamp16(hist + (code & 8 ? -delta : delta));
                index += IMA_INDEX[code];
                index = index < 0 ? 0 : index > 88 ? 88 : index;
                dst[(i + 1) * ch] = (int16_t)hist;
            }
        }
    return 1;
}

static const int16_t PT_STEP[12][16] = {
    {-14, -10, -7, -5, -3, -2, -1, 0, 0, 1, 2, 3, 5, 7, 10, 14},
    {-28, -20, -14, -10, -7, -5, -3, -1, 1, 3, 5, 7, 10, 14, 20, 28},
    {-56, -40, -28, -20, -14, -10, -6, -2, 2, 6, 10, 14, 20, 28, 40, 56},
    {-112, -80, -56, -40, -28, -20, -12, -4, 4, 12, 20, 28, 40, 56, 80, 112},
    {-224, -160, -112, -80, -56, -40, -24, -8, 8, 24, 40, 56, 80, 112, 160, 224},
    {-448, -320, -224, -160, -112, -80, -48, -16, 16, 48, 80, 112, 160, 224, 320, 448},
    {-896, -640, -448, -320, -224, -160, -96, -32, 32, 96, 160, 224, 320, 448, 640, 896},
    {-1792, -1280, -896, -640, -448, -320, -192, -64, 64, 192, 320, 448, 640, 896, 1280, 1792},
    {-3584, -2560, -1792, -1280, -896, -640, -384, -128, 128, 384, 640, 896, 1280, 1792, 2560, 3584},
    {-7168, -5120, -3584, -2560, -1792, -1280, -768, -256, 256, 768, 1280, 1792, 2560, 3584, 5120, 7168},
    {-14336, -10240, -7168, -5120, -3584, -2560, -1536, -512, 512, 1536, 2560, 3584, 5120, 7168, 10240, 14336},
    {-28672, -20480, -14336, -10240, -7168, -5120, -3072, -1024, 1024, 3072, 5120, 7168, 10240, 14336, 20480, 28672}};
static const uint8_t PT_INDEX[12][8] = {
    {0, 0, 0, 0, 1, 1, 2, 2}, {0, 1, 1, 1, 2, 2, 3, 3}, {1, 2, 2, 2, 3, 3, 4, 4}, {2, 3, 3, 3, 4, 4, 5, 5},
    {3, 4, 4, 4, 5, 5, 6, 6}, {4, 5, 5, 5, 6, 6, 7, 7}, {5, 6, 6, 6, 7, 7, 8, 8}, {6, 7, 7, 7, 8, 8, 9, 9},
    {7, 8, 8, 8, 9, 9, 10, 10}, {8, 9, 9, 9, 10, 10, 11, 11}, {9, 10, 10, 10, 11, 11, 11, 11},
    {10, 11, 11, 11, 11, 11, 11, 11}};

static int decode_ptadpcm(Wem *w) {
    size_t ch = (size_t)w->channels, frame = (size_t)w->align / ch, per = 2 + (frame - 5) * 2;
    size_t frames = frame ? w->data_n / (frame * ch) : 0;
    int16_t *out;
    if (w->bits != 4 || (frame != 0x24 && frame != 0x104) || w->align % w->channels)
        return fail(w, "Unsupported Wwise Platinum ADPCM block layout.");
    if (!(out = (int16_t *)reserve(&w->pcm, frames * per * ch * 2))) return fail(w, "Out of memory.");
    for (size_t f = 0; f < frames; f++)
        for (size_t c = 0; c < ch; c++) {
            const uint8_t *block = w->data + (f * ch + c) * frame;
            int16_t *dst = out + f * per * ch + c;
            int hist2 = (int16_t)u16(block), hist1 = (int16_t)u16(block + 2), index = block[4];
            dst[0] = (int16_t)hist2;
            dst[ch] = (int16_t)hist1;
            for (size_t i = 0; i < per - 2; i++) {
                int nibble = (block[5 + i / 2] >> (i & 1 ? 4 : 0)) & 0xF;
                int sample = clamp16((index > 11 ? 0 : PT_STEP[index][nibble]) + 2 * hist1 - hist2);
                index = index > 11 ? 0 : PT_INDEX[index][nibble < 8 ? 7 - nibble : nibble - 8];
                dst[(i + 2) * ch] = (int16_t)sample;
                hist2 = hist1;
                hist1 = sample;
            }
        }
    return 1;
}

static uint32_t get(BitReader *r, int bits) {
    uint32_t value = 0;
    for (int done = 0; done < bits;) {
        size_t byte = r->pos >> 3;
        int off = (int)(r->pos & 7), take = 8 - off < bits - done ? 8 - off : bits - done;
        if (byte >= r->n) { r->bad = 1; return 0; }
        value |= (uint32_t)((r->p[byte] >> off) & ((1 << take) - 1)) << done;
        done += take;
        r->pos += take;
    }
    return value;
}

static void put(BitWriter *w, int bits, uint32_t value) {
    while (bits) {
        size_t byte = w->pos >> 3;
        int off = (int)(w->pos & 7), take = 8 - off < bits ? 8 - off : bits;
        if (byte >= w->n) { w->bad = 1; return; }
        w->p[byte] |= (uint8_t)((value & ((1u << take) - 1)) << off);
        value >>= take;
        bits -= take;
        w->pos += take;
    }
}

static uint32_t copy(BitReader *r, BitWriter *w, int from, int to) {
    uint32_t value = get(r, from);
    put(w, to, value);
    return value;
}

static int bit_width(uint32_t v) {
    int bits = 0;
    for (; v; v >>= 1) bits++;
    return bits;
}

static uint32_t quantvals(uint32_t entries, uint32_t dimensions) {
    int bits = bit_width(entries);
    uint32_t vals = entries >> ((bits - 1) * (dimensions - 1) / dimensions);
    for (;;) {
        uint64_t acc = 1, acc1 = 1;
        for (uint32_t i = 0; i < dimensions; i++) { acc *= vals; acc1 *= vals + 1; }
        if (acc <= entries && acc1 > entries) return vals;
        vals += acc > entries ? -1 : 1;
    }
}

static int rebuild_codebook(BitReader *r, BitWriter *w) {
    uint32_t dimensions, entries;
    put(w, 24, 0x564342);
    dimensions = copy(r, w, 4, 16);
    entries = copy(r, w, 14, 24);
    if (copy(r, w, 1, 1)) {
        copy(r, w, 5, 5);
        for (uint32_t current = 0; current < entries && !r->bad;) {
            int bits = bit_width(entries - current);
            current += copy(r, w, bits, bits);
        }
    } else {
        int length_bits = (int)get(r, 3);
        uint32_t sparse = copy(r, w, 1, 1);
        if (length_bits < 1 || length_bits > 5) return 0;
        for (uint32_t i = 0; i < entries && !r->bad; i++)
            if (!sparse || copy(r, w, 1, 1)) copy(r, w, length_bits, 5);
    }
    if (copy(r, w, 1, 4)) {
        int value_bits;
        uint32_t count;
        copy(r, w, 32, 32);
        copy(r, w, 32, 32);
        value_bits = (int)copy(r, w, 4, 4) + 1;
        copy(r, w, 1, 1);
        if (!dimensions || !entries) return 0;
        count = quantvals(entries, dimensions);
        for (uint32_t i = 0; i < count && !r->bad; i++) copy(r, w, value_bits, value_bits);
    }
    return !r->bad && !w->bad;
}

static int rebuild_setup(Wem *wem, BitReader *r, BitWriter *w, uint8_t *blockflag, int *mode_bits) {
    size_t table = wem->books_n >= 8 ? u32(wem->books + wem->books_n - 4) : 0;
    size_t library = wem->books_n >= 8 && table < wem->books_n ? (wem->books_n - table) / 4 - 1 : 0;
    uint32_t count;
    memcpy(w->p, "\5vorbis", 7);
    w->pos = 56;
    count = copy(r, w, 8, 8) + 1;
    for (uint32_t i = 0; i < count; i++) {
        uint32_t id = get(r, 10);
        BitReader book = {0};
        if (r->bad || id >= library) return 0;
        book.p = wem->books + u32(wem->books + table + id * 4);
        book.n = u32(wem->books + table + id * 4 + 4) - u32(wem->books + table + id * 4);
        if (book.p + book.n > wem->books + table || !rebuild_codebook(&book, w)) return 0;
    }
    put(w, 6, 0);
    put(w, 16, 0);
    count = copy(r, w, 6, 6) + 1;
    for (uint32_t i = 0; i < count && !r->bad; i++) {
        uint32_t classes[32], dimensions[16] = {0}, partitions, top = 0;
        int range;
        put(w, 16, 1);
        partitions = copy(r, w, 5, 5);
        for (uint32_t j = 0; j < partitions; j++) {
            classes[j] = copy(r, w, 4, 4);
            if (classes[j] > top) top = classes[j];
        }
        for (uint32_t j = 0; j <= top; j++) {
            uint32_t subclasses;
            dimensions[j] = copy(r, w, 3, 3) + 1;
            subclasses = copy(r, w, 2, 2);
            if (subclasses) copy(r, w, 8, 8);
            for (uint32_t k = 0; k < 1u << subclasses; k++) copy(r, w, 8, 8);
        }
        copy(r, w, 2, 2);
        range = (int)copy(r, w, 4, 4);
        for (uint32_t j = 0; j < partitions; j++)
            for (uint32_t k = 0; k < dimensions[classes[j]]; k++) copy(r, w, range, range);
    }
    count = copy(r, w, 6, 6) + 1;
    for (uint32_t i = 0; i < count && !r->bad; i++) {
        uint32_t cascade[64], classes;
        if (copy(r, w, 2, 16) > 2) return 0;
        copy(r, w, 24, 24);
        copy(r, w, 24, 24);
        copy(r, w, 24, 24);
        classes = copy(r, w, 6, 6) + 1;
        copy(r, w, 8, 8);
        for (uint32_t j = 0; j < classes; j++) {
            uint32_t low = copy(r, w, 3, 3);
            cascade[j] = (copy(r, w, 1, 1) ? copy(r, w, 5, 5) : 0) * 8 + low;
        }
        for (uint32_t j = 0; j < classes; j++)
            for (int k = 0; k < 8; k++)
                if (cascade[j] & (1u << k)) copy(r, w, 8, 8);
    }
    count = copy(r, w, 6, 6) + 1;
    for (uint32_t i = 0; i < count && !r->bad; i++) {
        uint32_t submaps;
        put(w, 16, 0);
        submaps = copy(r, w, 1, 1) ? copy(r, w, 4, 4) + 1 : 1;
        if (copy(r, w, 1, 1)) {
            uint32_t steps = copy(r, w, 8, 8) + 1;
            int bits = bit_width((uint32_t)wem->channels - 1);
            for (uint32_t j = 0; j < steps; j++) { copy(r, w, bits, bits); copy(r, w, bits, bits); }
        }
        if (copy(r, w, 2, 2)) return 0;
        if (submaps > 1)
            for (int j = 0; j < wem->channels; j++) copy(r, w, 4, 4);
        for (uint32_t j = 0; j < submaps; j++) copy(r, w, 24, 24);
    }
    count = copy(r, w, 6, 6) + 1;
    *mode_bits = bit_width(count - 1);
    for (uint32_t i = 0; i < count; i++) {
        blockflag[i] = (uint8_t)copy(r, w, 1, 1);
        put(w, 32, 0);
        copy(r, w, 8, 8);
    }
    put(w, 1, 1);
    return !r->bad && !w->bad;
}

static int ogg_packet(Buf *ogg, const uint8_t *packet, size_t size, uint32_t *sequence) {
    size_t segments = size / 255 + 1, done = 0;
    for (int continued = 0; segments; continued = 1) {
        size_t take = segments > 255 ? 255 : segments, body = take == segments ? size - done : take * 255;
        uint8_t *page = reserve(ogg, 27 + take + body);
        if (!page) return 0;
        memset(page, 0, 27);
        memcpy(page, "OggS", 4);
        page[5] = (uint8_t)(continued | (*sequence ? 0 : 2));
        page[14] = 1;
        memcpy(page + 18, sequence, 4);
        page[26] = (uint8_t)take;
        memset(page + 27, 255, take);
        if (take == segments) page[27 + take - 1] = (uint8_t)(size % 255);
        memcpy(page + 27 + take, packet + done, body);
        ++*sequence;
        segments -= take;
        done += body;
    }
    return 1;
}

static int vorbis_stream(Wem *w, Buf *ogg) {
    uint8_t id[30] = "\1vorbis", comment[16] = "\3vorbis", blockflag[64] = {0}, *scratch;
    uint32_t sequence = 0, seek = u32(w->fmt + 0x28), audio = u32(w->fmt + 0x2C);
    int mode_bits = 0, modified = w->fmt[0x40] != w->fmt[0x41], previous = 0, ok = 0;
    size_t setup_size, scratch_n = 0x20000;
    BitReader r = {0};
    BitWriter out = {0};
    if (seek + 2 > w->data_n || audio > w->data_n) return fail(w, "Wwise Vorbis setup is outside the data chunk.");
    setup_size = u16(w->data + seek);
    if (seek + 2 + setup_size > w->data_n) return fail(w, "Wwise Vorbis setup is cut off.");
    if (!(scratch = calloc(1, scratch_n))) return fail(w, "Out of memory.");
    id[11] = (uint8_t)w->channels;
    memcpy(id + 12, w->fmt + 4, 4);
    id[28] = (uint8_t)(w->fmt[0x41] << 4 | w->fmt[0x40]);
    id[29] = 1;
    comment[15] = 1;
    r.p = w->data + seek + 2;
    r.n = setup_size;
    out.p = scratch;
    out.n = scratch_n;
    if (!rebuild_setup(w, &r, &out, blockflag, &mode_bits)) {
        fail(w, "Wwise Vorbis setup could not be rebuilt.");
        goto done;
    }
    if (!ogg_packet(ogg, id, 30, &sequence) || !ogg_packet(ogg, comment, 16, &sequence)
        || !ogg_packet(ogg, scratch, (out.pos + 7) / 8, &sequence)) {
        fail(w, "Out of memory.");
        goto done;
    }
    for (size_t pos = audio; pos + 2 <= w->data_n;) {
        size_t size = u16(w->data + pos);
        const uint8_t *packet = w->data + pos + 2;
        if (!size || pos + 2 + size > w->data_n) break;
        pos += 2 + size;
        if (modified) {
            uint32_t mode, rest;
            memset(scratch, 0, size + 2);
            r.p = packet;
            r.n = size;
            r.pos = 0;
            out.pos = 0;
            put(&out, 1, 0);
            mode = copy(&r, &out, mode_bits, mode_bits);
            rest = get(&r, 8 - mode_bits);
            if (blockflag[mode]) {
                put(&out, 1, (uint32_t)previous);
                put(&out, 1, pos + 2 < w->data_n ? blockflag[w->data[pos + 2] & ((1 << mode_bits) - 1)] : 0);
            }
            previous = blockflag[mode];
            put(&out, 8 - mode_bits, rest);
            for (size_t i = 1; i < size; i++) put(&out, 8, packet[i]);
            packet = scratch;
            size = (out.pos + 7) / 8;
        }
        if (!ogg_packet(ogg, packet, size, &sequence)) {
            fail(w, "Out of memory.");
            goto done;
        }
    }
    ok = 1;
done:
    free(scratch);
    return ok;
}

static int decode_vorbis(Wem *w) {
    Buf ogg = {0};
    stb_vorbis *decoder = NULL;
    size_t total = 0, wanted;
    int error = 0, ok = 0;
    if (w->fmt_n < 0x42 || w->extra != 0x30) return fail(w, "Unsupported Wwise Vorbis header version.");
    wanted = u32(w->fmt + 0x18);
    if (!vorbis_stream(w, &ogg)) goto done;
    if (!(decoder = stb_vorbis_open_memory(ogg.p, (int)ogg.n, &error, NULL))) {
        fail(w, "The Vorbis decoder rejected this WEM's setup.");
        goto done;
    }
    while (total < wanted && !decoder->eof) {
        float **planes;
        int channels, frames = stb_vorbis_get_frame_float(decoder, &channels, &planes);
        int16_t *out;
        if ((size_t)frames > wanted - total) frames = (int)(wanted - total);
        if (!(out = (int16_t *)reserve(&w->pcm, (size_t)frames * w->channels * 2))) {
            fail(w, "Out of memory.");
            goto done;
        }
        for (int c = 0; c < w->channels; c++)
            for (int i = 0; i < frames; i++) out[i * w->channels + c] = clamp16((int)lrintf(planes[c][i] * 32767.0f));
        total += frames;
    }
    ok = 1;
done:
    if (decoder) stb_vorbis_close(decoder);
    free(ogg.p);
    return ok;
}

static int decode_opus(Wem *w) {
    static const uint8_t ORDER[8][8] = {
        {0}, {0, 1}, {0, 1, 2}, {0, 1, 2, 3}, {0, 1, 4, 2, 3}, {0, 1, 4, 5, 2, 3}, {0, 1, 6, 5, 4, 2, 3},
        {0, 1, 6, 7, 4, 5, 2, 3}};
    uint8_t order[16];
    int ch = w->channels, coupled, mapping, error = 0, skip;
    size_t count, wanted, total = 0, pos = 0;
    int16_t *frame;
    OpusMSDecoder *decoder;
    if (w->fmt_n < 0x24 || !w->seek || w->fmt[0x22] != 1) return fail(w, "Unsupported WEM Opus header version.");
    wanted = u32(w->fmt + 0x18);
    count = u32(w->fmt + 0x1C);
    skip = u16(w->fmt + 0x20);
    mapping = w->fmt[0x23];
    if (count > w->seek_n / 2) count = w->seek_n / 2;
    for (int i = 0; i < ch; i++) order[i] = (uint8_t)i;
    if (w->config_type == 1 && mapping == 1 && ch <= 8) {
        coupled = w->mask == 0x63F ? 3 : w->mask == 0x60F || w->mask == 0x607 || w->mask == 0x603 ? 2
                  : w->mask == 0x7 || w->mask == 0x3 ? 1 : 0;
        memcpy(order, ORDER[ch - 1], (size_t)ch);
    } else if (mapping == 255)
        coupled = 0;
    else if (ch <= 2)
        coupled = ch - 1;
    else
        return fail(w, "Unsupported WEM Opus channel mapping.");
    decoder = opus_multistream_decoder_create(48000, ch, ch - coupled, coupled, order, &error);
    if (!decoder) return fail(w, "The Opus decoder rejected this WEM's channel layout.");
    if (!(frame = malloc(5760 * (size_t)ch * 2))) {
        opus_multistream_decoder_destroy(decoder);
        return fail(w, "Out of memory.");
    }
    w->rate = 48000;
    for (size_t i = 0; i < count && total < wanted; i++) {
        size_t size = u16(w->seek + i * 2);
        int frames, start;
        int16_t *out;
        if (pos + size > w->data_n) break;
        frames = opus_multistream_decode(decoder, w->data + pos, (opus_int32)size, frame, 5760, 0);
        pos += size;
        if (frames < 0) {
            free(frame);
            opus_multistream_decoder_destroy(decoder);
            return fail(w, "The Opus decoder could not decode a packet.");
        }
        start = skip < frames ? skip : frames;
        skip -= start;
        frames -= start;
        if ((size_t)frames > wanted - total) frames = (int)(wanted - total);
        if (!(out = (int16_t *)reserve(&w->pcm, (size_t)frames * ch * 2))) {
            free(frame);
            opus_multistream_decoder_destroy(decoder);
            return fail(w, "Out of memory.");
        }
        memcpy(out, frame + start * ch, (size_t)frames * ch * 2);
        total += frames;
    }
    free(frame);
    opus_multistream_decoder_destroy(decoder);
    return 1;
}

static PyObject *decode(PyObject *self, PyObject *args) {
    Py_buffer wem, books;
    Wem w = {0};
    PyObject *result = NULL;
    int ok;
    if (!PyArg_ParseTuple(args, "y*y*", &wem, &books)) return NULL;
    w.books = books.buf;
    w.books_n = (size_t)books.len;
    Py_BEGIN_ALLOW_THREADS
    ok = parse(&w, wem.buf, (size_t)wem.len);
    if (ok) {
        if (w.tag == 0x0001 || w.tag == 0xFFFE) ok = decode_pcm(&w);
        else if (w.tag == 0x0002) ok = decode_ima(&w);
        else if (w.tag == 0x8311) ok = decode_ptadpcm(&w);
        else if (w.tag == 0xFFFF) ok = decode_vorbis(&w);
        else if (w.tag == 0x3041) ok = decode_opus(&w);
        else ok = fail(&w, "Unsupported WEM codec.");
    }
    Py_END_ALLOW_THREADS
    if (ok)
        result = Py_BuildValue("y#ii", (const char *)w.pcm.p, (Py_ssize_t)w.pcm.n, w.channels, w.rate);
    else
        PyErr_SetString(PyExc_ValueError, w.error);
    free(w.pcm.p);
    PyBuffer_Release(&wem);
    PyBuffer_Release(&books);
    return result;
}

static PyMethodDef methods[] = {{"decode", decode, METH_VARARGS, NULL}, {NULL, NULL, 0, NULL}};
static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "fastwem", NULL, -1, methods};

PyMODINIT_FUNC PyInit_fastwem(void) { return PyModule_Create(&module); }
