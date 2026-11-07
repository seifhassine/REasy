// See docs/internals/jcns-evaluation.md. sinf, cosf, tanf, acosf, asinf and atan2f come from ucrtbase, which the
// engines import.
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <immintrin.h>

#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <deque>
#include <initializer_list>
#include <optional>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

float (*sin_f)(float), (*cos_f)(float), (*tan_f)(float), (*acos_f)(float), (*asin_f)(float);
float (*atan2_f)(float, float);

using V3 = std::array<float, 3>;
using Q4 = std::array<float, 4>;
using M3 = std::array<float, 9>;
using M4 = std::array<float, 16>;  // engine rows: the position in the last

constexpr float PI = 3.1415927410125732f, HALF_PI = 1.5707963705062866f, TWO_PI = 6.2831854820251465f;
constexpr float EPS = 1.1920928955078125e-07f, DEGREES = 57.2957763671875f, HUNDREDTH = 0.009999999776482582f;
constexpr float THIRD = 0.3333333432674408f, AIM_ARC_EPSILON = 9.999999747378752e-05f, NEAR_BACK = -0.9998999834060669f;
constexpr float INV_180 = 0.0055555556900799274f;
constexpr Q4 IDENTITY{0.f, 0.f, 0.f, 1.f};
constexpr V3 ZERO3{0.f, 0.f, 0.f}, X_AXIS{1.f, 0.f, 0.f}, Y_AXIS{0.f, 1.f, 0.f}, Z_AXIS{0.f, 0.f, 1.f};
constexpr M4 EYE{1.f, 0.f, 0.f, 0.f, 0.f, 1.f, 0.f, 0.f, 0.f, 0.f, 1.f, 0.f, 0.f, 0.f, 0.f, 1.f};

// The enum members the records' codes map to (by name, per game): index order is what the code below compares.
const std::vector<std::pair<const char*, std::vector<const char*>>> NAMES = {
    {"element", {"Trans", "Rot", "Scale", "Deform", "RotRPY", "RotPYR", "ExpMap", "Material", "MaterialF4",
                 "MaterialPosF4", "MaterialRotF4", "ComponentProperty", "UserValue", "Rot2", "RotRPY2", "RotPYR2",
                 "ExpMap2"}},
    {"mode", {"Mode_Sum", "Mode_Average", "Mode_Mul", "Mode_Min", "Mode_Max"}},
    {"input", {"Trans", "Rot", "Scale", "RotRPY", "RotPYR", "ExpMap"}},
    {"curve", {"MinMax", "Function", "Scaling"}},
    {"output_flag", {"BasePose", "World", "ClampMin", "ClampMax", "OutJoint", "OutRot"}},
    {"driver_flag", {"BasePose", "MidPoint", "World"}},
    {"cone_flag", {"BasePose"}},
    {"kind", {"Outputs", "RotExpression", "Multi", "Aim", "Material", "JointExprGraph"}},
    {"world_up", {"SceneUp", "ObjectUp", "ObjectRotationUp", "Vector", "None", "None_MayaLike"}},
    {"property", {"TransRot", "Trans", "Rot"}},
    {"interpolation", {"Linear", "Slow", "Fast", "Smooth"}},
    {"order", {"XYZ", "YZX", "ZXY", "ZYX", "YXZ", "XZY"}},
    {"axis", {"X", "Y", "Z", "W"}},
    {"infinity", {"Const", "Linear", "Oscilate", "Cycle", "CycleRelative"}},
    {"tangent", {"Step", "StepNext"}},
    {"apply", {"Trans", "Euler", "Scale", "Rot"}},
};
enum Element { TRANS, ROT, SCALE, DEFORM, ROT_RPY, ROT_PYR, EXP_MAP, MATERIAL, MATERIAL_F4, MATERIAL_POS_F4,
               MATERIAL_ROT_F4, COMPONENT_PROPERTY, USER_VALUE, ROT2, ROT_RPY2, ROT_PYR2, EXP_MAP2 };
enum Mode { SUM, AVERAGE, MUL, MIN, MAX };
enum Input { IN_TRANS, IN_ROT, IN_SCALE, IN_ROT_RPY, IN_ROT_PYR, IN_EXP_MAP };
enum Curve { MIN_MAX, FUNCTION, SCALING };
enum OutputFlag { O_BASE = 1, O_WORLD = 2, O_CLAMP_MIN = 4, O_CLAMP_MAX = 8, O_JOINT = 16, O_ROT = 32 };
enum DriverFlag { D_BASE = 1, D_MID = 2, D_WORLD = 4 };
enum Kind { OUTPUTS, ROT_EXPRESSION, MULTI, AIM, MATERIAL_KIND, GRAPH };
enum WorldUp { SCENE_UP, OBJECT_UP, OBJECT_ROTATION_UP, VECTOR_UP, NONE_UP, NONE_MAYA };
enum Property { TRANS_ROT, TRANS_ONLY, ROT_ONLY };
enum Infinity { HOLD, EXTEND, OSCILLATE, CYCLE, CYCLE_RELATIVE };
enum Tangent { STEP, STEP_NEXT };
enum Apply { APPLY_TRANS, APPLY_EULER, APPLY_SCALE, APPLY_ROT };

Q4 neg(const Q4& q) { return {-q[0], -q[1], -q[2], -q[3]}; }
V3 scaled(const V3& v, float s) { return {v[0] * s, v[1] * s, v[2] * s}; }
float dot(const V3& a, const V3& b) { return (a[0] * b[0] + a[1] * b[1]) + a[2] * b[2]; }
V3 cross(const V3& a, const V3& b) { return {a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]}; }
V3 xyz(const Q4& q) { return {q[0], q[1], q[2]}; }
bool nonzero(const Q4& q) { return q[0] != 0.f || q[1] != 0.f || q[2] != 0.f || q[3] != 0.f; }
// x where a <= b (a < b), else y, as an SSE mask: MSVC turns such selects into minss / maxss with the operands swapped,
// which flips signed zeros.
float blend(__m128 mask, float x, float y) {
    return _mm_cvtss_f32(_mm_or_ps(_mm_and_ps(mask, _mm_set_ss(x)), _mm_andnot_ps(mask, _mm_set_ss(y))));
}
float if_le(float a, float b, float x, float y) { return blend(_mm_cmple_ss(_mm_set_ss(a), _mm_set_ss(b)), x, y); }
float if_lt(float a, float b, float x, float y) { return blend(_mm_cmplt_ss(_mm_set_ss(a), _mm_set_ss(b)), x, y); }

Q4 mul(const Q4& a, const Q4& b) {
    return {((b[0] * a[3] + a[0] * b[3]) + b[1] * a[2]) - b[2] * a[1],
            ((b[1] * a[3] + a[1] * b[3]) + b[2] * a[0]) - a[2] * b[0],
            ((b[2] * a[3] + a[2] * b[3]) + a[1] * b[0]) - b[1] * a[0],
            ((a[3] * b[3] - b[0] * a[0]) - a[1] * b[1]) - b[2] * a[2]};
}
Q4 conjugate(const Q4& q) { return {-q[0], -q[1], -q[2], q[3]}; }
Q4 inverse(const Q4& q) {
    const float norm = ((q[0] * q[0] + q[1] * q[1]) + q[2] * q[2]) + q[3] * q[3];
    if (!(norm > 0.f))
        return IDENTITY;
    const float s = 1.f / norm;
    return {-q[0] * s, -q[1] * s, -q[2] * s, q[3] * s};
}
// The rotation of a vector holding half the angle.
Q4 exp_rotation(const V3& v) {
    const float angle = std::sqrt((v[0] * v[0] + v[1] * v[1]) + v[2] * v[2]), sine = sin_f(angle);
    const V3 axis = std::fabs(sine) >= 1e-5f ? scaled(v, sine / angle) : v;
    return {axis[0], axis[1], axis[2], cos_f(angle)};
}
V3 rotate(const V3& v, const Q4& q) {
    const float tx = (v[2] * q[1] - v[1] * q[2]) * 2.f, ty = (v[0] * q[2] - v[2] * q[0]) * 2.f;
    const float tz = (v[1] * q[0] - v[0] * q[1]) * 2.f;
    return {(tz * q[1] - ty * q[2]) + (v[0] + tx * q[3]), (tx * q[2] - tz * q[0]) + (v[1] + ty * q[3]),
            (ty * q[0] - tx * q[1]) + (v[2] + tz * q[3])};
}
// A rotation output's blend toward target in the newer builds: the shorter way, renormalized.
Q4 nlerp(const Q4& current, Q4 target, float blend) {
    if (0.f > ((target[0] * current[0] + target[1] * current[1]) + target[2] * current[2]) + target[3] * current[3])
        target = neg(target);
    Q4 mixed;
    for (int i = 0; i < 4; ++i) mixed[i] = current[i] + blend * (target[i] - current[i]);
    const float s = 1.f / std::sqrt(((mixed[0] * mixed[0] + mixed[1] * mixed[1]) + mixed[2] * mixed[2]) + mixed[3] * mixed[3]);
    return {mixed[0] * s, mixed[1] * s, mixed[2] * s, mixed[3] * s};
}
Q4 rotation_arc(const V3& a, const V3& b, float epsilon) {
    const float d = dot(b, a);
    if (!(NEAR_BACK > d)) {
        const float root = std::sqrt((d + 1.f) * 2.f), s = 1.f / root;
        return {(b[2] * a[1] - a[2] * b[1]) * s, (a[2] * b[0] - b[2] * a[0]) * s, (a[0] * b[1] - b[0] * a[1]) * s,
                root * 0.5f};
    }
    const V3& axis = !(epsilon > std::fabs(a[0])) ? (epsilon > std::fabs(a[1]) ? Y_AXIS : Z_AXIS) : X_AXIS;
    V3 across = cross(a, axis);
    const float length = std::sqrt(dot(across, across));
    if (!(length < EPS))
        across = scaled(across, 1.f / length);
    const float sine = sin_f(HALF_PI);
    return {across[0] * sine, across[1] * sine, across[2] * sine, cos_f(HALF_PI)};
}
float wrapped(float roll) { return roll > PI ? roll - TWO_PI : -PI > roll ? roll + TWO_PI : roll; }
// Roll-bend conversions: the rotation arc forms, and the closed forms of the newer builds.
V3 to_roll_bend(const Q4& q) {
    const V3 direction = rotate(X_AXIS, q);
    const float side = dot(direction, X_AXIS) + 1.f;
    const Q4 twist = mul(q, inverse(rotation_arc(X_AXIS, direction, 1e-4f)));
    return {wrapped(atan2_f(twist[0], twist[3]) * 2.f), atan2_f(dot(direction, Z_AXIS), side) * -2.f,
            atan2_f(dot(direction, Y_AXIS), side) * 2.f};
}
Q4 from_roll_bend(const V3& v) {
    const float half = v[0] * 0.5f, a = tan_f(v[1] * -0.5f), b = tan_f(v[2] * 0.5f);
    const float scale = 2.f / ((b * b + a * a) + 1.f), pa = a * scale, pb = b * scale, c = scale - 1.f;
    V3 direction;
    for (int i = 0; i < 3; ++i)
        direction[i] = pa * Z_AXIS[i] + (pb * Y_AXIS[i] + c * X_AXIS[i]);
    return mul({sin_f(half) * X_AXIS[0], 0.f, 0.f, cos_f(half)}, rotation_arc(X_AXIS, direction, 1e-4f));
}
V3 to_roll_bend_closed(const Q4& q) {
    const float x = q[0], y = q[1], z = q[2], w = q[3], y2 = y + y, z2 = z + z;
    const float dx = 1.f - (y * y2 + z * z2), dy = z2 * w + y2 * x, dz = z2 * x - y2 * w;
    Q4 arc;
    if (NEAR_BACK > dx) {
        arc = {1.f, 0.f, 0.f, 0.f};
        if (0.9999998807907104f > std::fabs(w) && z * z + (x * x + y * y) > EPS) {
            const float sine = sin_f(acos_f(w));
            arc = !(std::fabs(sine) > 1e-10f) ? Q4{x, y, z, 0.f} : Q4{1.f / sine * x, 1.f / sine * y, 1.f / sine * z, 0.f};
        }
    } else {
        const float root = std::sqrt((dx + 1.f) + (dx + 1.f)), s = 1.f / root;
        arc = {0.f, s * -dz, dy * s, root * 0.5f};
    }
    const Q4 twist = mul(q, inverse(arc));
    const float roll = atan2_f(twist[0], twist[3]), side = dx + 1.f;
    return {wrapped(roll + roll), atan2_f(dz, side) * -2.f, atan2_f(dy, side) * 2.f};
}
Q4 from_roll_bend_closed(const V3& v, bool expanded) {
    const float half = 0.5f * v[0], sine = sin_f(half), cosine = cos_f(half);
    const float a = tan_f(v[1] * -0.5f), b = tan_f(0.5f * v[2]);
    const float scale = 2.f / ((a * a + b * b) + 1.f), side = scale + -1.f;
    float y = 0.f, z = 1.f, w = 0.f;
    if (!(NEAR_BACK > side)) {
        const float root = std::sqrt((side + 1.f) + (side + 1.f)), inv = 1.f / root;
        y = (scale * -a) * inv, z = (b * scale) * inv, w = root * 0.5f;
    }
    if (expanded)
        return {sine * w, y * cosine + sine * z, z * cosine - sine * y, cosine * w};
    return mul({sine, 0.f, 0.f, cosine}, {0.f, y, z, w});
}

// A 3 x 3 rotation (rows) of q: the diagonal 1 - (a + b), or (1 - a) - b with ``subtract``.
M3 quaternion_matrix(const Q4& q, bool subtract = false) {
    const float x = q[0], y = q[1], z = q[2], w = q[3], x2 = x * 2.f, y2 = y * 2.f, z2 = z * 2.f;
    const float xx = x * x2, yy = y * y2, zz = z * z2;
    const float d0 = subtract ? (1.f - yy) - zz : 1.f - (zz + yy), d1 = subtract ? (1.f - xx) - zz : 1.f - (zz + xx);
    const float d2 = subtract ? (1.f - xx) - yy : 1.f - (yy + xx);
    return {d0, x * y2 + w * z2, x * z2 - w * y2, x * y2 - w * z2, d1, y * z2 + w * x2, x * z2 + w * y2,
            y * z2 - w * x2, d2};
}
Q4 matrix_rotation(const M3& m) {
    const float trace = (m[4] + m[0]) + m[8];
    if (trace > 0.f) {
        const float root = std::sqrt(trace + 1.f), f = 0.5f / root;
        return {(m[5] - m[7]) * f, (m[6] - m[2]) * f, (m[1] - m[3]) * f, root * 0.5f};
    }
    int i = m[4] > m[0] ? 1 : 0;
    i = m[8] > m[i * 4] ? 2 : i;
    const int j = (i + 1) % 3, k = (j + 1) % 3;
    const float root = std::sqrt(((m[i * 4] - m[j * 4]) - m[k * 4]) + 1.f), f = 0.5f / root;
    Q4 q{};
    q[i] = root * 0.5f;
    q[3] = (m[j * 3 + k] - m[k * 3 + j]) * f;
    q[j] = (m[i * 3 + j] + m[j * 3 + i]) * f;
    q[k] = (m[i * 3 + k] + m[k * 3 + i]) * f;
    return q;
}
M3 upper(const M4& m) { return {m[0], m[1], m[2], m[4], m[5], m[6], m[8], m[9], m[10]}; }
Q4 normalize4(const Q4& v) {
    const float length = std::sqrt(((v[1] * v[1] + v[0] * v[0]) + v[2] * v[2]) + v[3] * v[3]);
    if (EPS > length)
        return v;
    const float s = 1.f / length;
    return {v[0] * s, v[1] * s, v[2] * s, v[3] * s};
}
Q4 rotate_quaternion(const M4& m) {
    Q4 n[3];
    for (int r = 0; r < 3; ++r)
        n[r] = normalize4({m[r * 4], m[r * 4 + 1], m[r * 4 + 2], m[r * 4 + 3]});
    const float trace = (n[1][1] + n[0][0]) + n[2][2];
    if (trace > 0.f) {
        const float s = 0.5f / std::sqrt(trace + 1.f);
        return {(n[1][2] - n[2][1]) * s, (n[2][0] - n[0][2]) * s, (n[0][1] - n[1][0]) * s, 0.25f / s};
    }
    if (n[0][0] > n[1][1] && n[0][0] > n[2][2]) {
        const float s = std::sqrt(((n[0][0] + 1.f) - n[1][1]) - n[2][2]) * 2.f;
        return {s * 0.25f, (n[0][1] + n[1][0]) / s, (n[2][0] + n[0][2]) / s, (n[1][2] - n[2][1]) / s};
    }
    if (n[1][1] > n[2][2]) {
        const float s = std::sqrt(((n[1][1] + 1.f) - n[0][0]) - n[2][2]) * 2.f;
        return {(n[0][1] + n[1][0]) / s, s * 0.25f, (n[1][2] + n[2][1]) / s, (n[2][0] - n[0][2]) / s};
    }
    const float s = std::sqrt(((n[2][2] + 1.f) - n[0][0]) - n[1][1]) * 2.f;
    return {(n[2][0] + n[0][2]) / s, (n[1][2] + n[2][1]) / s, s * 0.25f, (n[0][1] - n[1][0]) / s};
}
using L = std::array<float, 4>;
L operator*(const L& a, const L& b) { return {a[0] * b[0], a[1] * b[1], a[2] * b[2], a[3] * b[3]}; }
L operator+(const L& a, const L& b) { return {a[0] + b[0], a[1] + b[1], a[2] + b[2], a[3] + b[3]}; }
L operator-(const L& a, const L& b) { return {a[0] - b[0], a[1] - b[1], a[2] - b[2], a[3] - b[3]}; }
L pairs(const L& v) { return {v[1], v[0], v[3], v[2]}; }
L halves(const L& v) { return {v[2], v[3], v[0], v[1]}; }
// Cramer's rule in four lanes, the SSE way; identity at determinant 0.
M4 matrix_inverse(const M4& r) {
    const L a{r[0], r[4], r[8], r[12]}, b{r[9], r[13], r[1], r[5]}, c{r[10], r[14], r[2], r[6]}, d{r[11], r[15], r[3], r[7]};
    const L cb = pairs(halves(c) * b), dc = pairs(d * halves(c)), bd = pairs(halves(b) * d);
    const L minor0 = ((((halves(dc) * b - dc * b) + cb * d) - halves(cb) * d) + bd * c) - halves(bd) * c;
    const L minor1 = halves(halves(dc) * a - dc * a), minor2 = halves(halves(bd) * a - bd * a);
    const L minor3 = halves(halves(cb) * a - cb * a);
    L det = minor0 * a;
    det = det + halves(det);
    det = det + pairs(det);
    if (det[0] == 0.f)
        return EYE;
    const float s = 1.f / det[0];
    const L ba = pairs(b * a), da = pairs(d * a), ca = pairs(c * a);
    const L rows[4] = {minor0, (((minor1 - da * c) + halves(da) * c) + ca * d) - halves(ca) * d,
                       ((halves(ba) * d - (ba * d + minor2)) + da * b) - halves(da) * b,
                       (((ba * c - minor3) - halves(ba) * c) - ca * b) + halves(ca) * b};
    M4 out;
    for (int i = 0; i < 16; ++i)
        out[i] = rows[i / 4][i % 4] * s;
    return out;
}
// The engines' affine inverse of a row matrix; identity below a determinant of FLT_EPSILON.
M4 affine_inverse(const M4& m) {
    auto at = [&](int r, int c) { return m[r * 4 + c]; };
    const float c00 = at(1, 1) * at(2, 2) - at(1, 2) * at(2, 1), c01 = at(2, 0) * at(1, 2) - at(1, 0) * at(2, 2);
    const float c02 = at(1, 0) * at(2, 1) - at(2, 0) * at(1, 1);
    const float determinant = at(0, 0) * c00 + at(0, 1) * c01 + at(0, 2) * c02;
    M4 out = EYE;
    if (!(EPS < std::fabs(determinant)))
        return out;
    const float s = 1.f / determinant;
    const float rows[9] = {s * c00, (at(0, 2) * at(2, 1) - at(0, 1) * at(2, 2)) * s, (at(0, 1) * at(1, 2) - at(0, 2) * at(1, 1)) * s,
                           s * c01, (at(0, 0) * at(2, 2) - at(0, 2) * at(2, 0)) * s, (at(0, 2) * at(1, 0) - at(0, 0) * at(1, 2)) * s,
                           s * c02, (at(0, 1) * at(2, 0) - at(0, 0) * at(2, 1)) * s, (at(0, 0) * at(1, 1) - at(0, 1) * at(1, 0)) * s};
    for (int r = 0; r < 3; ++r)
        for (int c = 0; c < 3; ++c)
            out[r * 4 + c] = rows[r * 3 + c];
    for (int c = 0; c < 3; ++c)
        out[12 + c] = -(at(3, 1) * out[4 + c] + at(3, 0) * out[c] + at(3, 2) * out[8 + c]);
    return out;
}
M4 product(const M4& a, const M4& b, bool paired) {
    M4 out;
    for (int i = 0; i < 4; ++i)
        for (int j = 0; j < 4; ++j) {
            const float p0 = a[i * 4] * b[j], p1 = a[i * 4 + 1] * b[4 + j], p2 = a[i * 4 + 2] * b[8 + j];
            const float p3 = a[i * 4 + 3] * b[12 + j];
            out[i * 4 + j] = paired ? (p0 + p1) + (p2 + p3) : ((p0 + p1) + p2) + p3;
        }
    return out;
}
M3 product3(const M3& a, const M3& b) {
    M3 out;
    for (int i = 0; i < 3; ++i)
        for (int j = 0; j < 3; ++j)
            out[i * 3 + j] = (a[i * 3] * b[j] + a[i * 3 + 1] * b[3 + j]) + a[i * 3 + 2] * b[6 + j];
    return out;
}
V3 transformed(const V3& p, const M4& m) {
    V3 out;
    for (int c = 0; c < 3; ++c)
        out[c] = ((p[1] * m[4 + c] + p[0] * m[c]) + p[2] * m[8 + c]) + m[12 + c];
    return out;
}
V3 row_lengths(const M4& m) {
    V3 out;
    for (int r = 0; r < 3; ++r) {
        const float* v = &m[r * 4];
        out[r] = std::sqrt(((v[0] * v[0] + v[1] * v[1]) + v[2] * v[2]) + v[3] * v[3]);
    }
    return out;
}
Q4 descriptor_rotation(const M4& m) {
    const V3 scale = row_lengths(m);
    M3 rows;
    for (int r = 0; r < 3; ++r) {
        const float s = 1.f / scale[r];
        for (int c = 0; c < 3; ++c)
            rows[r * 3 + c] = m[r * 4 + c] * s;
    }
    return matrix_rotation(rows);
}
V3 normalized(const V3& v) {
    const float length = std::sqrt(dot(v, v));
    return length < EPS ? v : scaled(v, 1.f / length);
}
V3 normalized3(const V3& v) {
    const float length = std::sqrt((v[0] * v[0] + v[1] * v[1]) + v[2] * v[2]);
    return EPS <= length ? scaled(v, 1.f / length) : v;
}

// matrix -> Euler angles and back, by RotOrder (XYZ, YZX, ZXY, ZYX, YXZ, XZY).
V3 matrix_euler(int order, const M3& m) {
    auto a2 = atan2_f;
    switch (order) {
    case 0: {
        const float s = m[2];
        if (!(1.f > s)) return {-a2(m[3], m[4]), -HALF_PI, 0.f};
        if (!(s > -1.f)) return {a2(m[3], m[4]), HALF_PI, 0.f};
        return {-a2(-m[5], m[8]), -asin_f(s), -a2(-m[1], m[0])};
    }
    case 1: {
        const float s = m[3];
        if (!(1.f > s)) return {0.f, -a2(m[7], m[8]), -HALF_PI};
        if (!(s > -1.f)) return {0.f, a2(m[7], m[8]), HALF_PI};
        return {-a2(-m[5], m[4]), -a2(-m[6], m[0]), -asin_f(s)};
    }
    case 2: {
        const float s = m[7];
        if (!(1.f > s)) return {-HALF_PI, 0.f, -a2(m[2], m[0])};
        if (!(s > -1.f)) return {HALF_PI, 0.f, a2(m[2], m[0])};
        return {-asin_f(s), -a2(-m[6], m[8]), -a2(-m[1], m[4])};
    }
    case 3: {
        const float s = m[6];
        if (!(1.f > s)) return {0.f, HALF_PI, -a2(-m[1], -m[2])};
        if (!(s > -1.f)) return {0.f, -HALF_PI, a2(m[1], m[2])};
        return {-a2(m[7], m[8]), -asin_f(-s), -a2(m[3], m[0])};
    }
    case 4: {
        const float s = m[5];
        if (!(1.f > s)) return {HALF_PI, -a2(-m[1], m[0]), 0.f};
        if (!(s > -1.f)) return {-HALF_PI, -a2(m[1], m[0]), 0.f};
        return {-asin_f(-s), -a2(m[2], m[8]), -a2(m[3], m[4])};
    }
    case 5: {
        const float s = m[1];
        if (!(1.f > s)) return {-a2(-m[6], m[8]), 0.f, HALF_PI};
        if (!(s > -1.f)) return {-a2(m[6], m[8]), 0.f, -HALF_PI};
        return {-a2(m[7], m[4]), -a2(m[2], m[0]), -asin_f(-s)};
    }
    }
    return ZERO3;
}
M3 euler_matrix(int order, const V3& e) {
    const float sx = sin_f(e[0]), sy = sin_f(e[1]), sz = sin_f(e[2]), cx = cos_f(e[0]), cy = cos_f(e[1]), cz = cos_f(e[2]);
    switch (order) {
    case 0: return {cy * cz, sz * cy, -sy, (sy * cz) * sx - sz * cx, (sy * sz) * sx + cx * cz, sx * cy,
                    (sy * cz) * cx + sx * sz, (sy * sz) * cx - sx * cz, cx * cy};
    case 1: return {cy * cz, (cx * cy) * sz + sx * sy, (sz * cy) * sx - sy * cx, -sz, cx * cz, sx * cz, sy * cz,
                    (sy * cx) * sz - sx * cy, (sx * sy) * sz + cx * cy};
    case 2: return {(sx * sy) * sz + cy * cz, sz * cx, (sx * sz) * cy - sy * cz, (sx * cz) * sy - sz * cy, cx * cz,
                    (sx * cy) * cz + sy * sz, sy * cx, -sx, cx * cy};
    case 3: return {cy * cz, (sy * cz) * sx + sz * cx, sx * sz - (cx * cz) * sy, -sz * cy, cx * cz - (sx * sy) * sz,
                    (sy * cx) * sz + sx * cz, sy, -sx * cy, cx * cy};
    case 4: return {cy * cz - (sx * sy) * sz, (sx * sy) * cz + sz * cy, -cx * sy, -cx * sz, cx * cz, sx,
                    (sx * cy) * sz + sy * cz, sy * sz - (sx * cy) * cz, cx * cy};
    case 5: return {cy * cz, sz, -cz * sy, sx * sy - (sz * cx) * cy, cx * cz, (sy * cx) * sz + sx * cy,
                    (sx * sz) * cy + sy * cx, -sx * cz, cx * cy - (sx * sy) * sz};
    }
    return {1.f, 0.f, 0.f, 0.f, 1.f, 0.f, 0.f, 0.f, 1.f};
}
Q4 euler_quaternion(int order, const V3& e) {
    const float hx = e[0] * 0.5f, hy = e[1] * 0.5f, hz = e[2] * 0.5f;
    const float sx = sin_f(hx), sy = sin_f(hy), sz = sin_f(hz), cx = cos_f(hx), cy = cos_f(hy), cz = cos_f(hz);
    switch (order) {
    case 0: return {(cy * sx) * cz - (sy * cx) * sz, (sy * cx) * cz + (cy * sx) * sz, (cy * cx) * sz - (sy * sx) * cz,
                    (sy * sx) * sz + (cy * cx) * cz};
    case 1: return {(cz * cy) * sx - (sz * sy) * cx, (cz * sy) * cx - (sz * cy) * sx, (sz * cy) * cx + (cz * sy) * sx,
                    (sz * sy) * sx + (cz * cy) * cx};
    case 2: return {(cz * sx) * cy + (sz * cx) * sy, (cz * cx) * sy - (sz * sx) * cy, (sz * cx) * cy - (cz * sx) * sy,
                    (sz * sx) * sy + (cz * cx) * cy};
    case 3: return {(cz * cy) * sx + (sz * sy) * cx, (cz * sy) * cx - (sz * cy) * sx, (sz * cy) * cx + (cz * sy) * sx,
                    (cz * cy) * cx - (sz * sy) * sx};
    case 4: return {(cy * sx) * cz - (sy * cx) * sz, (sy * cx) * cz + (cy * sx) * sz, (cy * cx) * sz + (sy * sx) * cz,
                    (cy * cx) * cz - (sy * sx) * sz};
    case 5: return {(cz * sx) * cy + (sz * cx) * sy, (cz * cx) * sy + (sz * sx) * cy, (sz * cx) * cy - (cz * sx) * sy,
                    (cz * cx) * cy - (sz * sx) * sy};
    }
    return IDENTITY;
}

// A value's place between low and high eased by interpolation, between start and end: clamped and eased by
// polynomials, or (bounds) the ends compared first and eased in factored form.
float remap(float x, float low, float high, float start, float end, int interpolation, bool bounds) {
    if (bounds) {
        const bool above = high < low ? x < low : low < x, below = high < low ? high < x : x < high;
        if (!above)
            return start;
        if (!below)
            return end;
        float t = (x - low) / (high - low);
        if (interpolation == 1)
            t = t * t * t;
        else if (interpolation == 2)
            t = 1.f - t, t = 1.f - t * t * t;
        else if (interpolation == 3)
            t = t * t * ((3.f - t) - t);
        return (end - start) * t + start;
    }
    float t = (x - low) / (high - low);
    t = 0.f > t ? 0.f : t < 1.f ? t : 1.f;
    const float square = t * t;
    const float eased = interpolation == 1 ? (t * square + t * 0.f) + square * 0.f
                        : interpolation == 2 ? (t * square + t * 3.f) - square * 3.f
                        : interpolation == 3 ? (t * 0.f - (t * square) * 2.f) + square * 3.f : t;
    return (end - start) * eased + start;
}
float hermite(float p0, float m0, float p1, float m1, float s) {
    const float square = s * s;
    return (((((p0 * 2.f + m0) - p1 * 2.f) + m1) * (square * s) + (((m0 * -2.f - p0 * 3.f) + p1 * 3.f) - m1) * square)
            + m0 * s) + p0;
}
enum Roots { STABLE, NAIVE, NO_ROOTS };
float rate(float a0, float a1, float out_x, float in_x, float x, int roots) {
    const float whole = ((a0 * 2.f - a1 * 2.f) + out_x) + in_x;
    float square = ((a1 * 3.f - a0 * 3.f) - out_x * 2.f) - in_x, d = a0 - x, s = (x - a0) / (a1 - a0);
    if (whole == 0.f && roots == NO_ROOTS)
        return s;
    if (whole == 0.f) {
        if (square == 0.f)
            return out_x == 0.f ? d : -d / out_x;
        const float root = std::sqrt(out_x * out_x - (square * 4.f) * d), twice = square * 2.f;
        float first, second;
        if (roots == NAIVE)
            first = (root - out_x) / twice, second = (-out_x - root) / twice;
        else if (out_x > 0.f) {
            const float q = -out_x - root;
            first = (d * 2.f) / q, second = q / twice;
        } else {
            const float q = root - out_x;
            first = q / twice, second = (d * 2.f) / q;
        }
        for (float candidate : {first, second})
            if (candidate >= 0.f && 1.f >= candidate)
                return candidate;
        return s;
    }
    square = square / whole;
    const float linear = out_x / whole, twice = square * 2.f;
    d = d / whole;
    if ((s * 3.f) * s + twice * s + linear != 0.f)
        for (int i = 0; i < 100; ++i) {
            const float old = s;
            s = ((((s * 2.f) * s) * s + (s * square) * s) - d) / (((s * 3.f) * s + twice * s) + linear);
            if (1e-5f >= std::fabs(s - old))
                break;
        }
    if (s >= 0.f && 1.f >= s)
        return s;
    const float shifted = s + square;
    const float discriminant = shifted * shifted - (((s * square) + linear) + s * s) * 4.f;
    for (float candidate : {(discriminant - shifted) * 0.5f, (-shifted - discriminant) * 0.5f})
        if (candidate >= 0.f && 1.f >= candidate)
            return candidate;
    return s;
}
struct Key { float frame, value, ix, iy, ox, oy; int tangent; };
float solve_curve(const Key* keys, int count, int pre, int post, float x, bool positive_odd, int roots) {
    if (count == 0)
        return 0.f;
    const Key &first = keys[0], &last = keys[count - 1];
    const float span = last.frame - first.frame;
    if (span == 0.f)
        return 0.f;
    float base = 0.f;
    const float t = (x - first.frame) / span;
    const Key* beyond = 0.f > t ? &first : t > 1.f ? &last : nullptr;
    if (beyond) {
        const int mode = beyond == &first ? pre : post;
        if (mode == HOLD)
            return beyond->value;
        if (mode == EXTEND) {
            const float sx = beyond == &first ? first.ix : last.ox, sy = beyond == &first ? first.iy : last.oy;
            return sx == 0.f ? beyond->value : (sy / sx) * (x - beyond->frame) + beyond->value;
        }
        const long long cycles = (long long)_mm_cvtt_ss2si(_mm_set_ss(t)) - (0.f > t ? 1 : 0);
        x = x - span * (float)cycles;
        if (mode == OSCILLATE && std::llabs(cycles) % 2 == 1 && (!positive_odd || cycles > 0))
            x = (first.frame + last.frame) - x;
        else if (mode == CYCLE_RELATIVE)
            base = (last.value - first.value) * (float)cycles;
    }
    int low = 0, high = count - 1, index = -1;
    while (low < high) {
        const int middle = (high + low) / 2;
        if (keys[middle].frame > x)
            high = middle - 1;
        else if (x > keys[middle].frame)
            low = middle + 1;
        else {
            index = middle;
            break;
        }
    }
    if (index < 0)
        index = x >= keys[low].frame ? low : low - 1;
    const Key& key = keys[index < 0 ? count + index : index];
    if (key.frame == x)
        return base + key.value;
    const Key& following = keys[std::min(index + 1, count - 1)];
    if (key.tangent == STEP)
        return base + key.value;
    if (key.tangent == STEP_NEXT)
        return base + following.value;
    const float s = key.frame >= x ? 0.f : !(x < following.frame) ? 1.f
                    : rate(key.frame, following.frame, key.ox, following.ix, x, roots);
    return base + hermite(key.value, key.oy, following.value, following.iy, s);
}

// A target's (weight, slot) pairs largest weight first, as every build's weight-list update leaves them: a Shell
// sort (gaps 3h + 1) to 32 pairs, which swaps equal weights, MSVC's std::sort past that.
struct Weighted { float weight; int slot; };
struct EngineSort {
    std::vector<Weighted>& a;
    bool gt(int i, int j) const { return a[i].weight > a[j].weight; }
    void swap(int i, int j) { std::swap(a[i], a[j]); }
    void med3(int first, int mid, int last) {
        if (gt(mid, first)) swap(mid, first);
        if (gt(last, mid)) {
            swap(last, mid);
            if (gt(mid, first)) swap(mid, first);
        }
    }
    std::pair<int, int> partition(int first, int last) {
        const int mid = first + ((last - first) >> 1), count = last - 1 - first;
        if (count > 40) {
            const int step = (count + 1) >> 3, two = step << 1;
            med3(first, first + step, first + two);
            med3(mid - step, mid, mid + step);
            med3(last - 1 - two, last - 1 - step, last - 1);
            med3(first + step, mid, last - 1 - step);
        } else
            med3(first, mid, last - 1);
        int pfirst = mid, plast = mid + 1;
        while (first < pfirst && !gt(pfirst - 1, pfirst) && !gt(pfirst, pfirst - 1)) --pfirst;
        while (plast < last && !gt(plast, pfirst) && !gt(pfirst, plast)) ++plast;
        int gfirst = plast, glast = pfirst;
        for (;;) {
            for (; gfirst < last; ++gfirst) {
                if (gt(pfirst, gfirst)) continue;
                if (gt(gfirst, pfirst)) break;
                if (plast != gfirst) swap(plast, gfirst);
                ++plast;
            }
            for (; first < glast; --glast) {
                if (gt(glast - 1, pfirst)) continue;
                if (gt(pfirst, glast - 1)) break;
                if (--pfirst != glast - 1) swap(pfirst, glast - 1);
            }
            if (glast == first && gfirst == last) return {pfirst, plast};
            if (glast == first) {
                if (plast != gfirst) swap(pfirst, plast);
                ++plast;
                swap(pfirst++, gfirst++);
            } else if (gfirst == last) {
                if (--glast != --pfirst) swap(glast, pfirst);
                swap(pfirst, --plast);
            } else
                swap(gfirst++, --glast);
        }
    }
    void insertion(int first, int last) {
        for (int mid = first + 1; mid < last; ++mid) {
            const Weighted value = a[mid];
            if (value.weight > a[first].weight) {
                for (int i = mid; i > first; --i) a[i] = a[i - 1];
                a[first] = value;
                continue;
            }
            int hole = mid;
            for (; value.weight > a[hole - 1].weight; --hole) a[hole] = a[hole - 1];
            a[hole] = value;
        }
    }
    void pop_hole(int first, int hole, int bottom, Weighted value) {
        const int top = hole, last_parent = (bottom - 1) >> 1;
        int index = hole;
        while (index < last_parent) {
            index = 2 * index + 2;
            if (a[first + index].weight > a[first + index - 1].weight) --index;
            a[first + hole] = a[first + index];
            hole = index;
        }
        if (index == last_parent && bottom % 2 == 0) {
            a[first + hole] = a[first + bottom - 1];
            hole = bottom - 1;
        }
        for (int parent = (hole - 1) >> 1; top < hole && a[first + parent].weight > value.weight; parent = (hole - 1) >> 1) {
            a[first + hole] = a[first + parent];
            hole = parent;
        }
        a[first + hole] = value;
    }
    void sort(int first, int last, int ideal) {
        for (;;) {
            if (last - first <= 32) return insertion(first, last);
            if (ideal <= 0) {
                const int bottom = last - first;
                for (int hole = (bottom >> 1) - 1; hole >= 0; --hole) pop_hole(first, hole, bottom, a[first + hole]);
                while (last - first >= 2) {
                    const Weighted value = a[--last];
                    a[last] = a[first];
                    pop_hole(first, 0, last - first, value);
                }
                return;
            }
            const auto [pfirst, plast] = partition(first, last);
            ideal = (ideal >> 1) + (ideal >> 2);
            if (pfirst - first < last - plast) {
                sort(first, pfirst, ideal);
                first = plast;
            } else {
                sort(plast, last, ideal);
                last = pfirst;
            }
        }
    }
    void run() {
        const int n = (int)a.size();
        if (n > 32)
            return sort(0, n, n);
        int gap = 1;
        while (gap < n) gap = gap * 3 + 1;
        for (; gap; gap /= 3)
            for (int i = gap; i < n; ++i)
                for (int j = i; j >= gap && !gt(j - gap, j); j -= gap) swap(j - gap, j);
    }
};

struct Rules {
    bool sums_paired[4];  // world, base, multi, aim
    bool chain_from_root, rotation_from_root, skip_missing_cone_parent, curves_by_keyframes, positive_odd;
    int roots;
    bool legacy_always, frame_inverse, pyr_inverse;
    int output_values;  // 0 always, 1 with_target, 2 none
    std::vector<int> order;  // empty: the file's order
    bool mismatched_untouched, blend_set, total_above_epsilon, subtract, sse, aim_none_parent, rotate_up;
    int euler_from[3], roll_bend_axes[3], roll_bend;  // 0 arc, 1 closed, 2 closed_expanded
    bool rot_matrix, rot2_matrix, exp_shortest, remap_bounds, radians_by_flag, outputs_by_flag, aim_premultiplied;
    bool zero_blend_skip, legacy_general_inverse, multi_fields_ignored, materials;
    int joint_map_from;
};
enum Site { WORLD, BASE, MULTI_SITE, AIM_SITE };

struct Rig;
struct Joint {
    Rig* rig = nullptr;
    int number = -1;
    explicit operator bool() const { return rig != nullptr; }
};
struct Rig {
    const Rules* rules = nullptr;
    Py_ssize_t count = 0;
    const uint32_t* hashes = nullptr;
    const int32_t *parents = nullptr, *symmetry = nullptr, *follows = nullptr;
    float *positions = nullptr, *rotations = nullptr, *scales = nullptr;
    const float *base_positions = nullptr, *base_rotations = nullptr, *base_scales = nullptr;
    Q4 owner_rotation{};
    M4 owner_matrix{};
    Rig* host = nullptr;
    std::unordered_map<uint32_t, int> index;

    V3 pos(int n) const { return {positions[n * 3], positions[n * 3 + 1], positions[n * 3 + 2]}; }
    Q4 rot(int n) const { return {rotations[n * 4], rotations[n * 4 + 1], rotations[n * 4 + 2], rotations[n * 4 + 3]}; }
    V3 scale(int n) const { return {scales[n * 3], scales[n * 3 + 1], scales[n * 3 + 2]}; }
    V3 base_pos(int n) const { return {base_positions[n * 3], base_positions[n * 3 + 1], base_positions[n * 3 + 2]}; }
    Q4 base_rot(int n) const {
        return {base_rotations[n * 4], base_rotations[n * 4 + 1], base_rotations[n * 4 + 2], base_rotations[n * 4 + 3]};
    }
    V3 base_scale(int n) const { return {base_scales[n * 3], base_scales[n * 3 + 1], base_scales[n * 3 + 2]}; }
    void set_pos(int n, const V3& v) { std::memcpy(positions + n * 3, v.data(), 12); }
    void set_rot(int n, const Q4& q) { std::memcpy(rotations + n * 4, q.data(), 16); }
    void set_scale(int n, const V3& v) { std::memcpy(scales + n * 3, v.data(), 12); }
    bool followed(int n) const { return host && follows[n] >= 0; }
    int own_number(uint32_t hash) const {
        const auto found = index.find(hash);
        return found == index.end() ? -1 : found->second;
    }
    M4 multiply(Site site, const M4& a, const M4& b) const { return product(a, b, rules->sums_paired[site]); }
    Joint locate(uint32_t hash) {
        if (const int n = own_number(hash); n >= 0) return {this, n};
        if (const int n = host ? host->own_number(hash) : -1; n >= 0) return {host, n};
        return {};
    }
    Joint follow(int n) { return followed(n) ? Joint{host, follows[n]} : Joint{this, n}; }
    Joint find(uint32_t hash) {
        const Joint found = locate(hash);
        return found ? found.rig->follow(found.number) : found;
    }
    Joint own(uint32_t hash) {
        const int n = own_number(hash);
        return n < 0 ? Joint{} : follow(n);
    }
    Q4 rotation(int n) {
        if (followed(n)) return host->rotation(follows[n]);
        int parent = parents[n];
        if (rules->rotation_from_root) return mul(rot(n), parent >= 0 ? rotation(parent) : owner_rotation);
        Q4 r = rot(n);
        for (; parent >= 0; parent = parents[parent]) {
            if (followed(parent)) return mul(r, host->rotation(follows[parent]));
            r = mul(r, rot(parent));
        }
        return mul(r, owner_rotation);
    }
    M4 trs(const V3& p, const Q4& q, const V3& s) const {
        const M3 r = quaternion_matrix(q, rules->subtract);
        return {r[0] * s[0], r[1] * s[0], r[2] * s[0], 0.f, r[3] * s[1], r[4] * s[1], r[5] * s[1], 0.f,
                r[6] * s[2], r[7] * s[2], r[8] * s[2], 0.f, p[0], p[1], p[2], 1.f};
    }
    M4 local_matrix(int n) const { return trs(pos(n), rot(n), scale(n)); }
    M4 world_matrix(int n) {
        if (followed(n)) return host->world_matrix(follows[n]);
        M4 m = local_matrix(n);
        int parent = parents[n];
        if (rules->chain_from_root) return multiply(WORLD, m, parent >= 0 ? world_matrix(parent) : owner_matrix);
        for (; parent >= 0; parent = parents[parent]) {
            if (followed(parent)) return multiply(WORLD, m, host->world_matrix(follows[parent]));
            m = multiply(WORLD, m, local_matrix(parent));
        }
        return multiply(WORLD, m, owner_matrix);
    }
    M4 parent_matrix(int n) { return parents[n] >= 0 ? world_matrix(parents[n]) : owner_matrix; }
    V3 position(int n) {
        if (followed(n)) return host->position(follows[n]);
        return transformed(pos(n), parent_matrix(n));
    }
    M4 base_world_matrix(int n) const {
        const M4 m = trs(base_pos(n), base_rot(n), base_scale(n));
        return parents[n] >= 0 ? multiply(BASE, m, base_world_matrix(parents[n])) : m;
    }
    void set_position(int n, const V3& world, const M4* parent = nullptr) {
        set_pos(n, transformed(world, affine_inverse(parent ? *parent : parent_matrix(n))));
    }
    void set_rotation(int n, const Q4& world, const Q4* parent = nullptr) {
        const Q4 p = parent ? *parent : parents[n] >= 0 ? rotation(parents[n]) : owner_rotation;
        const Q4 local = mul(world, conjugate(p));
        const float s = 1.f / std::sqrt(((local[1] * local[1] + local[0] * local[0]) + local[2] * local[2]) + local[3] * local[3]);
        set_rot(n, {local[0] * s, local[1] * s, local[2] * s, local[3] * s});
    }
};

// Joint references: by name hash (v22 and older) or by index into the file's name hashes.
struct Ref { bool by_index = false; long long value = 0; bool none() const { return !by_index && value == 0; } };
// A cone input: ``axis`` and ``up`` the file's ConeAxis codes, which the engine does arithmetic on.
struct Cone {
    Ref joint, parent, aim;
    int parent_index;
    long long axis, up;
    bool vector, up_is_vector, base;
    float half, scale;
    Q4 offset;
    V3 cone_vec, target_vec, up_vec;
};
struct Driver {
    Ref joint;
    int cone, flags, input, axis, order, interpolation, curve, pre, post, first_key, keys;
    bool keyframes;
    float in_min, in_mid, in_max, out_min, out_mid, out_max;
    Q4 offset;
};
// ``named``: the record has material and variable name strings (hashed as named_*), which a MaterialParam needs.
struct Output {
    Ref joint;
    uint32_t joint_hash, variable_hash;
    int element, mode, axis, order, lod, flags, pack, first_cone_driver, cone_drivers, first_driver, drivers;
    float min_value, max_value;
    Q4 offset;
    bool named;
    uint32_t named_material, named_variable;
};
struct MaterialConstraint { Ref joint; uint32_t material, variable; int mode, order; bool world; };
struct Expression {
    Ref target, source;
    int lod;
    Q4 in_base_quat, out_base_quat;
    float x, y, z;
    bool in_base, out_base, inverse_, mirroring;
};
struct Weight { Ref parent; float weight; };
struct Multi { Ref joint; int lod, property; bool maintain; std::vector<Weight> weights; };
struct Target { Ref joint; float weight; };
struct Aim {
    Ref joint, up_object;
    int lod, up_type, offset_order;
    V3 aim_vector, up_vector, world_up_vector, rotate_offset;
    std::vector<Target> targets;
};
struct Group { int top; std::vector<uint32_t> hashes; };
struct Offset { int parent; V3 position; Q4 rotation; float weight; };
// A mesh's materials: each material variable by (material, variable) name hash, its type (its component count: 1
// float, 4 float4), current value and lock, and the via.render.MaterialParam objects set on the mesh, in the order
// they were made.
struct Variable { int type; Q4 value; bool locked; };
struct Param { uint32_t material, variable; Q4 vector; float scalar; };
struct Materials {
    std::unordered_map<uint64_t, Variable> variables;
    std::vector<Param> params;
    static uint64_t key(uint32_t material, uint32_t variable) { return (uint64_t)material << 32 | variable; }
    Variable* find(uint32_t material, uint32_t variable) {
        const auto found = variables.find(key(material, variable));
        return found == variables.end() ? nullptr : &found->second;
    }
};

struct Results {
    bool has_channels = false, weights_set = false, outputs_ran = false;
    std::vector<uint32_t> channels;
    std::vector<float> weights;
    std::vector<float> outputs;
    std::vector<char> written;
    Materials* materials = nullptr;
};

struct Program {
    Rules rules{};
    std::vector<uint32_t> names;
    bool legacy = false, legacy_init = false, table = false, cone_states = false, legacy_material = false;
    std::vector<Cone> cones;
    std::vector<Output> outputs;
    std::vector<Driver> cone_drivers, drivers;
    std::vector<Key> keys;
    std::vector<Expression> expressions;
    std::vector<Multi> multis;
    std::vector<std::pair<int, int>> multi_parents;  // name index, lod
    std::vector<uint32_t> map_hashes;
    std::vector<Aim> aims;
    std::vector<MaterialConstraint> material_constraints;
    std::vector<Group> groups;
    std::vector<int> order;
    // State the layer keeps across updates: the name cache (rig 0 own, 1 host; joint), cone values, multi offsets.
    std::vector<std::pair<int, int>> cache;
    std::vector<float> cone_values;
    // Each joint and cone driver's last input and output (NaN until its output runs), for the editor.
    std::vector<float> driver_points, cone_driver_points;
    // Each aim's world up and each cone's axis and the direction it measures in the last run (NaN until then).
    std::vector<float> aim_ups, cone_axes;
    bool initialized = false;
    std::vector<std::vector<Offset>> offsets;
    std::vector<char> dropped;
    std::vector<int> slots, parent_slots;
    std::vector<std::pair<int, int>> map_nodes;  // joint, map parent slot
    std::vector<std::pair<M4, Q4>> table_entries;
    std::vector<char> table_valid;

    Rig* own = nullptr;
    Results* results = nullptr;

    Joint cached(int which, int n) { return which == 0 ? Joint{own, n} : Joint{own->host, n}; }
    Joint located(long long i, bool host) {
        if (i < 0 || i >= (long long)names.size()) return {};
        if (cache[i].first >= 0) return cached(cache[i].first, cache[i].second);
        Joint found;
        if (host)
            found = own->locate(names[i]);
        else if (const int n = own->own_number(names[i]); n >= 0)
            found = {own, n};
        if (found) cache[i] = {found.rig == own ? 0 : 1, found.number};
        return found;
    }
    Joint named(long long i) {
        const Joint found = located(i, true);
        return found ? found.rig->follow(found.number) : found;
    }
    Joint joint(const Ref& r) { return r.by_index ? named(r.value) : own->find((uint32_t)r.value); }
    Joint output_joint(const Output& o) {
        if (o.joint.by_index) return located(o.joint.value, false);
        const int n = own->own_number((uint32_t)o.joint.value);
        return n < 0 ? Joint{} : Joint{own, n};
    }

    // Rotation expressions.
    void rot_expressions(int lod) {
        for (const Expression& e : expressions) {
            if (lod > e.lod) continue;
            const Joint target = e.target.by_index ? named(e.target.value) : own->own((uint32_t)e.target.value);
            const Joint source = !target ? Joint{} : e.source.by_index ? named(e.source.value) : own->own((uint32_t)e.source.value);
            if (!target || !source) continue;
            rot_expression(target, source, e);
            if (e.mirroring) {
                const int t = target.rig->symmetry[target.number], s = source.rig->symmetry[source.number];
                if (t >= 0 && s >= 0) rot_expression({target.rig, t}, {source.rig, s}, e);
            }
        }
    }
    static void rot_expression(Joint target, Joint source, const Expression& e) {
        Q4 rotation = mul(source.rig->rot(source.number), conjugate(e.in_base_quat));
        if (e.in_base) rotation = mul(rotation, conjugate(source.rig->base_rot(source.number)));
        const float weight = ((e.x + e.y) + e.z) * THIRD;
        if (rotation[3] < 0.f) rotation = neg(rotation);
        const float x = rotation[0] * e.x, y = rotation[1] * e.y, z = rotation[2] * e.z;
        const float w = rotation[3] * weight + (1.f - weight);
        const float s = 1.f / std::sqrt(((y * y + x * x) + z * z) + w * w);
        rotation = {s * x, s * y, s * z, s * w};
        if (e.inverse_) rotation = conjugate(rotation);
        rotation = mul(rotation, e.out_base_quat);
        if (e.out_base) rotation = mul(rotation, target.rig->base_rot(target.number));
        target.rig->set_rot(target.number, rotation);
    }

    // Aim constraints.
    void aim_constraints(int lod) {
        aim_ups.assign(aims.size() * 3, NAN);
        for (const Aim& a : aims) {
            const Joint joint_ = lod <= a.lod ? joint(a.joint) : Joint{};
            if (!joint_) continue;
            Rig& rig = *joint_.rig;
            const int n = joint_.number;
            const V3 origin = rig.position(n);
            V3 target = ZERO3;
            int found = 0;
            for (const Target& t : a.targets) {
                const Joint other = joint(t.joint);
                if (!other) continue;
                ++found;
                const V3 point = other.rig->position(other.number);
                for (int c = 0; c < 3; ++c) target[c] = target[c] + t.weight * point[c];
            }
            if (!found) continue;
            const V3 direction = normalized3({target[0] - origin[0], target[1] - origin[1], target[2] - origin[2]});
            if ((a.up_type == NONE_UP || a.up_type == NONE_MAYA) && rules.aim_none_parent) {
                parent_aim(rig, n, a, direction, a.up_type == NONE_MAYA);
                continue;
            }
            if (a.up_type == NONE_UP) {
                const M4 m = rig.world_matrix(n);
                V3 turned;
                for (int c = 0; c < 3; ++c)
                    turned[c] = (a.aim_vector[1] * m[4 + c] + a.aim_vector[0] * m[c]) + a.aim_vector[2] * m[8 + c];
                rig.set_rotation(n, mul(rig.rotation(n), rotation_arc(turned, direction, AIM_ARC_EPSILON)));
                continue;
            }
            const V3 up = world_up_vector(a, origin), side = normalized3(cross(up, direction));
            std::memcpy(&aim_ups[3 * (&a - aims.data())], up.data(), sizeof up);
            const V3 frame_up = normalized3(cross(direction, side));
            const V3 aim_side = normalized3(cross(a.up_vector, a.aim_vector));
            const V3 aim_up = normalized3(cross(a.aim_vector, aim_side));
            const M3 frame{direction[0], direction[1], direction[2], frame_up[0], frame_up[1], frame_up[2], side[0], side[1], side[2]};
            const M3 local{a.aim_vector[0], aim_up[0], aim_side[0], a.aim_vector[1], aim_up[1], aim_side[1],
                           a.aim_vector[2], aim_up[2], aim_side[2]};
            const M3 offset = euler_matrix(a.offset_order, a.rotate_offset);
            M3 rotation;
            if (rules.aim_premultiplied)
                rotation = product3(product3(offset, local), frame);
            else
                rotation = upper(rig.multiply(AIM_SITE, widen(offset), rig.multiply(AIM_SITE, widen(local), widen(frame))));
            rig.set_rotation(n, matrix_rotation(rotation));
        }
    }
    static M4 widen(const M3& m) { return {m[0], m[1], m[2], 0.f, m[3], m[4], m[5], 0.f, m[6], m[7], m[8], 0.f, 0.f, 0.f, 0.f, 1.f}; }
    void parent_aim(Rig& rig, int n, const Aim& a, const V3& direction, bool maya_like) {
        const int parent = rig.parents[n];
        const Q4 parent_rotation = parent >= 0 ? rig.rotation(parent) : IDENTITY;
        const Q4 frame = maya_like ? parent_rotation : mul(rig.rot(n), parent_rotation);
        const Q4 rotation = mul(frame, rotation_arc(rotate(a.aim_vector, frame), direction, AIM_ARC_EPSILON));
        const float s = 1.f / std::sqrt(((rotation[0] * rotation[0] + rotation[1] * rotation[1]) + rotation[2] * rotation[2])
                                        + rotation[3] * rotation[3]);
        rig.set_rot(n, mul({rotation[0] * s, rotation[1] * s, rotation[2] * s, rotation[3] * s}, conjugate(parent_rotation)));
    }
    V3 world_up_vector(const Aim& a, const V3& origin) {
        if (a.up_type == VECTOR_UP) return a.world_up_vector;
        if (a.up_type == OBJECT_UP || a.up_type == OBJECT_ROTATION_UP) {
            const Joint other = joint(a.up_object);
            if (a.up_type == OBJECT_ROTATION_UP) {
                if (!other) return a.world_up_vector;
                const Q4 r = other.rig->rotation(other.number);
                if (rules.rotate_up) return rotate(a.world_up_vector, r);
                const M3 m = quaternion_matrix(r);
                const V3& v = a.world_up_vector;
                return {(v[0] * m[0] + v[1] * m[3]) + v[2] * m[6], (v[0] * m[1] + v[1] * m[4]) + v[2] * m[7],
                        (v[0] * m[2] + v[1] * m[5]) + v[2] * m[8]};
            }
            if (!other) return xyz(normalize4({-origin[0], -origin[1], -origin[2], 0.f}));
            const V3 point = other.rig->position(other.number);
            return normalized3({point[0] - origin[0], point[1] - origin[1], point[2] - origin[2]});
        }
        return Y_AXIS;
    }

    // Multi constraints.
    Joint multi_joint(long long i, bool legacy_init_) {
        const Joint found = located(i, legacy_init_);
        return found && legacy_init_ ? found.rig->follow(found.number) : found;
    }
    void initialize_multi() {
        initialized = true;
        if (!table) {
            for (const Multi& m : multis) {
                std::vector<Offset> entries;
                const Joint j = joint(m.joint);
                float total = 0.f;
                if (j) {
                    const M4 child = j.rig->base_world_matrix(j.number);
                    for (const Weight& w : m.weights) {
                        const Joint parent = joint(w.parent);
                        if (!parent) {
                            entries.push_back({-1, ZERO3, {0.f, 0.f, 0.f, 0.f}, w.weight});
                            continue;
                        }
                        const M4 p = own->multiply(MULTI_SITE, child, matrix_inverse(parent.rig->base_world_matrix(parent.number)));
                        entries.push_back({0, {p[12], p[13], p[14]}, rotate_quaternion(p), w.weight});
                        total = total + w.weight;
                    }
                }
                dropped.push_back(!j || (rules.total_above_epsilon && total <= EPS));
                offsets.push_back(entries);
                totals.push_back(total);
            }
            return;
        }
        joint_map();
        std::vector<M4> inverses;
        for (const auto& [index, lod] : multi_parents) {
            const Joint j = multi_joint(index, legacy_init);
            const M4 base = j ? j.rig->base_world_matrix(j.number) : EYE;
            inverses.push_back(!j ? EYE : legacy_init && rules.legacy_general_inverse ? matrix_inverse(base) : affine_inverse(base));
            parent_slots.push_back(j && index >= 0 && index < (long long)names.size() ? slot_of(names[index]) : -1);
            table_entries.push_back({EYE, IDENTITY});
            table_valid.push_back(0);
        }
        for (const Multi& m : multis) {
            std::vector<Offset> entries;
            const Joint j = multi_joint(m.joint.value, legacy_init);
            int slot = -1;
            if (j) {
                const M4 child = j.rig->base_world_matrix(j.number);
                float total = 0.f;
                for (const Weight& w : m.weights) {
                    const int index = (int)w.parent.value;
                    if (!(0 <= index && index < (int)inverses.size())) continue;
                    const M4 p = own->multiply(MULTI_SITE, child, inverses[index]);
                    entries.push_back({index, {p[12], p[13], p[14]}, rotate_quaternion(p), w.weight});
                    total = total + w.weight;
                }
                if (EPS < total) {
                    for (Offset& e : entries) e.weight = 1.f / total * e.weight;
                    const int parent = j.rig->parents[j.number];
                    slot = parent >= 0 ? slot_of(j.rig->hashes[parent]) : -1;
                }
            }
            offsets.push_back(entries);
            dropped.push_back(!j);
            slots.push_back(slot);
        }
    }
    std::vector<float> totals;
    std::unordered_map<uint32_t, int> map_slots;
    int slot_of(uint32_t hash) const {
        const auto found = map_slots.find(hash);
        return found == map_slots.end() || found->second < rules.joint_map_from ? -1 : found->second;
    }
    // The joint map multi_joint_hashes make (36 and later): each named joint of this transform after its ancestors
    // not yet in it; none when a name is missing.
    void joint_map() {
        if (legacy_init) return;
        std::vector<int> numbers;
        std::unordered_map<uint32_t, int> found;
        for (const uint32_t hash : map_hashes) {
            const int n = own->own_number(hash);
            if (n < 0) return;
            if (found.count(own->hashes[n])) continue;
            std::vector<int> chain{n};
            while (own->parents[chain.back()] >= 0 && !found.count(own->hashes[own->parents[chain.back()]]))
                chain.push_back(own->parents[chain.back()]);
            for (auto it = chain.rbegin(); it != chain.rend(); ++it) {
                found[own->hashes[*it]] = (int)numbers.size();
                numbers.push_back(*it);
            }
        }
        for (const int n : numbers) {
            const int parent = own->parents[n];
            map_nodes.push_back({n, parent >= 0 && found.count(own->hashes[parent]) ? found[own->hashes[parent]] : -1});
        }
        map_slots = std::move(found);
    }
    std::vector<std::pair<M4, Q4>> map_poses() const {
        std::vector<std::pair<M4, Q4>> poses;
        for (const auto& [n, parent] : map_nodes) {
            if (parent < 0) {
                const M4 world = own->world_matrix(n);
                poses.push_back({world, descriptor_rotation(world)});
            } else {
                const M4 local = own->local_matrix(n);
                poses.push_back({own->multiply(WORLD, local, poses[parent].first), mul(own->rot(n), poses[parent].second)});
            }
        }
        return poses;
    }
    void multi_constraints(int lod) {
        if (!initialized) initialize_multi();
        if (!table) {
            for (size_t m = 0; m < multis.size(); ++m) {
                const Joint j = dropped[m] ? Joint{} : joint(multis[m].joint);
                if (!j) continue;
                V3 position = ZERO3;
                Q4 rotation{0.f, 0.f, 0.f, 0.f};
                for (size_t w = 0; w < multis[m].weights.size(); ++w) {
                    const Offset& e = offsets[m][w];
                    const Joint parent = joint(multis[m].weights[w].parent);
                    if (!parent) continue;
                    const M4 pm = parent.rig->world_matrix(parent.number);
                    const float share = multis[m].weights[w].weight / totals[m];
                    for (int c = 0; c < 3; ++c)
                        position[c] = position[c] + (((e.position[1] * pm[4 + c] + e.position[0] * pm[c]) + e.position[2] * pm[8 + c]) + pm[12 + c]) * share;
                    rotation = summed(rotation, mul(e.rotation, parent.rig->rotation(parent.number)), share);
                }
                j.rig->set_position(j.number, position);
                set_normalized(j, rotation, nullptr);
            }
            return;
        }
        const auto poses = map_poses();
        for (size_t p = 0; p < multi_parents.size(); ++p) {
            if (lod > multi_parents[p].second) continue;
            if (parent_slots[p] >= 0) {
                table_entries[p] = poses[parent_slots[p]];
                table_valid[p] = 1;
                continue;
            }
            const Joint j = multi_joint(multi_parents[p].first, legacy_init);
            if (j) table_entries[p] = {j.rig->world_matrix(j.number), j.rig->rotation(j.number)};
            table_valid[p] = j ? 1 : 0;
        }
        for (size_t m = 0; m < multis.size(); ++m) {
            const Multi& record = multis[m];
            const Joint j = lod <= record.lod ? multi_joint(record.joint.value, legacy_init) : Joint{};
            if (!j) continue;
            const bool fixed = legacy_init || rules.multi_fields_ignored;
            const bool moving = fixed || record.property == TRANS_ROT || record.property == TRANS_ONLY;
            const bool turning = fixed || record.property == TRANS_ROT || record.property == ROT_ONLY;
            const bool keep = fixed || record.maintain;
            V3 position = ZERO3;
            Q4 rotation{0.f, 0.f, 0.f, 0.f};
            for (const Offset& e : offsets[m]) {
                const M4& pm = table_entries[e.parent].first;
                if (!(0.f < e.weight) || (legacy_init && !table_valid[e.parent])) continue;
                if (moving) {
                    const V3 point = keep ? V3{((e.position[1] * pm[4] + e.position[0] * pm[0]) + e.position[2] * pm[8]) + pm[12],
                                               ((e.position[1] * pm[5] + e.position[0] * pm[1]) + e.position[2] * pm[9]) + pm[13],
                                               ((e.position[1] * pm[6] + e.position[0] * pm[2]) + e.position[2] * pm[10]) + pm[14]}
                                          : V3{pm[12], pm[13], pm[14]};
                    for (int c = 0; c < 3; ++c) position[c] = position[c] + e.weight * point[c];
                }
                if (turning)
                    rotation = summed(rotation, keep ? mul(e.rotation, table_entries[e.parent].second) : table_entries[e.parent].second, e.weight);
            }
            const bool mapped = slots[m] >= 0;
            if (moving) j.rig->set_position(j.number, position, mapped ? &poses[slots[m]].first : nullptr);
            if (turning) set_normalized(j, rotation, mapped ? &poses[slots[m]].second : nullptr);
        }
    }
    // Each rotation turned onto the sum's side (negative dot: x -1) before it is added with its share.
    static Q4 summed(const Q4& sum, Q4 turned, float share) {
        if (nonzero(sum) && 0.f > ((turned[1] * sum[1] + turned[0] * sum[0]) + turned[2] * sum[2]) + turned[3] * sum[3])
            turned = {turned[0] * -1.f, turned[1] * -1.f, turned[2] * -1.f, turned[3] * -1.f};
        return {sum[0] + turned[0] * share, sum[1] + turned[1] * share, sum[2] + turned[2] * share, sum[3] + turned[3] * share};
    }
    static void set_normalized(Joint j, const Q4& r, const Q4* parent) {
        if (!nonzero(r)) return;
        const float s = 1.f / std::sqrt(((r[1] * r[1] + r[0] * r[0]) + r[2] * r[2]) + r[3] * r[3]);
        j.rig->set_rotation(j.number, {r[0] * s, r[1] * s, r[2] * s, r[3] * s}, parent);
    }

    // Cone inputs.
    std::optional<float> cone_value(const Cone& c, float* axes) {
        const Joint joint_ = joint(c.joint);
        if (!joint_ || !(c.half > 0.f)) return std::nullopt;
        Rig& rig = *joint_.rig;
        Joint parent = joint(c.parent);
        const Joint actual = rig.parents[joint_.number] >= 0 ? Joint{&rig, rig.parents[joint_.number]} : Joint{};
        bool is_actual;
        if (!parent) {
            if (!actual || rules.skip_missing_cone_parent) return std::nullopt;
            parent = actual, is_actual = true;
            if (0 <= c.parent_index && c.parent_index < (int)names.size())
                cache[c.parent_index] = {actual.rig == own ? 0 : 1, actual.number};
        } else
            is_actual = actual && parent.rig == actual.rig && parent.number == actual.number;
        const Q4 parent_rotation = parent.rig->rotation(parent.number);
        const Q4 reference = c.base ? mul(rig.base_rot(joint_.number), parent_rotation) : parent_rotation;
        const Q4 joint_rotation = is_actual ? mul(rig.rot(joint_.number), parent_rotation) : rig.rotation(joint_.number);
        float value;
        if (cone_states) {
            const Joint aim = joint(c.aim);
            value = aim ? state_value_from_aim(c, aim.rig->position(aim.number), rig.position(joint_.number), reference,
                                               axes)
                        : state_value(c, joint_rotation, reference, axes);
        } else {
            V3 cone_axis, joint_axis;
            if (c.vector) {
                cone_axis = rotate(xyz(c.offset), reference);
                joint_axis = rotate(c.target_vec, joint_rotation);
            } else {
                const M3 cm = quaternion_matrix(mul(c.offset, reference)), jm = quaternion_matrix(rig.rotation(joint_.number));
                const int row = (int)(c.axis % 3 * 3);
                cone_axis = {cm[row], cm[row + 1], cm[row + 2]}, joint_axis = {jm[row], jm[row + 1], jm[row + 2]};
                if (c.axis >= 3) cone_axis = {-cone_axis[0], -cone_axis[1], -cone_axis[2]}, joint_axis = {-joint_axis[0], -joint_axis[1], -joint_axis[2]};
                joint_axis = normalized(joint_axis), cone_axis = normalized(cone_axis);
            }
            note_axes(axes, cone_axis, joint_axis);
            const float d = dot(cone_axis, joint_axis);
            value = 1.f;
            if (-1.f < d && d < 1.f) {
                const float angle = acos_f(d);
                value = c.half <= angle ? 0.f : 1.f - angle / c.half;
            }
        }
        return std::isnan(value) ? 1.f : value;
    }
    // via::motion::ConeInput (35 and later): direction 1-3 the X-Z axes, 5-7 the negated ones.
    static int direction_of(long long axis) { return (int)((axis + 2 - (axis < 3)) & 0xFF); }
    static bool axis_row(const Q4& q, int direction, V3& out) {
        if (!(direction == 1 || direction == 2 || direction == 3 || direction == 5 || direction == 6 || direction == 7))
            return false;
        const int start = (direction & 3) * 3 - 3;
        const M3 m = quaternion_matrix(q);
        out = {m[start], m[start + 1], m[start + 2]};
        if (direction > 4) out = {-out[0], -out[1], -out[2]};
        return true;
    }
    static bool offset_set(const Cone& c) { return c.vector && nonzero(c.offset); }
    static Q4 cone_offset(const Cone& c) { return !c.vector || offset_set(c) ? c.offset : IDENTITY; }
    static V3 cone_up(const Cone& c, const Q4& turned, const Q4& frame) {
        if (c.up_is_vector) return rotate(c.up_vec, frame);
        V3 row;
        return axis_row(turned, direction_of(c.up), row) ? row : ZERO3;
    }
    static V3 scale_deform(const V3& v, const V3& axis, const V3& up, float scale) {
        V3 side = cross(up, axis);
        float length = std::sqrt(dot(side, side));
        if (EPS > length) return v;
        side = scaled(side, 1.f / length);
        V3 across = cross(axis, side);
        length = std::sqrt(dot(across, across));
        if (EPS > length) return v;
        across = scaled(across, 1.f / length);
        const Q4 frame = matrix_rotation({side[0], side[1], side[2], across[0], across[1], across[2], axis[0], axis[1], axis[2]});
        const V3 local = rotate(v, conjugate(frame));
        return rotate({local[0], (1.f / scale) * local[1], local[2]}, frame);
    }
    static void note_axes(float* axes, const V3& axis, const V3& measured) {
        std::memcpy(axes, axis.data(), sizeof axis), std::memcpy(axes + 3, measured.data(), sizeof measured);
    }
    static float angle_value(const Cone& c, const V3& axis, const V3& joint_axis, float* axes) {
        note_axes(axes, axis, joint_axis);
        const float d = dot(axis, joint_axis);
        if (!(-1.f < d && d < 1.f)) return 1.f;
        const float angle = acos_f(d);
        return c.half > angle ? 1.f - angle / c.half : 0.f;
    }
    static float state_value(const Cone& c, const Q4& joint_rotation, const Q4& reference, float* axes) {
        if (0.f >= c.half) return 0.f;
        const Q4 turned = mul(cone_offset(c), reference), frame = offset_set(c) ? turned : reference;
        V3 axis, joint_axis;
        if (c.vector)
            axis = rotate(c.cone_vec, frame), joint_axis = rotate(c.target_vec, joint_rotation);
        else {
            if (!axis_row(turned, direction_of(c.axis), axis)) return 0.f;
            axis_row(joint_rotation, direction_of(c.axis), joint_axis);
        }
        axis = normalized(axis);
        if (c.scale > EPS) joint_axis = scale_deform(joint_axis, axis, cone_up(c, turned, frame), c.scale);
        return angle_value(c, axis, normalized(joint_axis), axes);
    }
    static float state_value_from_aim(const Cone& c, const V3& target, const V3& origin, const Q4& reference,
                                      float* axes) {
        if (0.f >= c.half) return 0.f;
        V3 direction{target[0] - origin[0], target[1] - origin[1], target[2] - origin[2]};
        const float length = std::sqrt(dot(direction, direction));
        if (EPS > length) return 0.f;
        direction = scaled(direction, 1.f / length);
        const Q4 turned = mul(cone_offset(c), reference);
        V3 axis = ZERO3;
        if (c.vector)
            axis = rotate(c.cone_vec, offset_set(c) ? turned : reference);
        else
            axis_row(turned, direction_of(c.axis), axis);
        axis = normalized(axis);
        if (c.scale > EPS) direction = normalized(scale_deform(direction, axis, cone_up(c, turned, reference), c.scale));
        return angle_value(c, axis, direction, axes);
    }

    // Outputs.
    void run_outputs(int lod, float blend_value) {
        if (rules.zero_blend_skip && blend_value <= 0.f) return;
        const float* blend = rules.blend_set ? nullptr : &blend_value;
        cone_axes.assign(cones.size() * 6, NAN);
        for (size_t c = 0; c < cones.size(); ++c) {
            const auto value = cone_value(cones[c], &cone_axes[6 * c]);
            cone_values[c] = value ? *value : 0.f;
        }
        Results& r = *results;
        r.outputs_ran = true;
        r.outputs.assign(outputs.size(), 0.f);
        r.written.assign(outputs.size(), 0);
        driver_points.assign(drivers.size() * 2, NAN);
        cone_driver_points.assign(cone_drivers.size() * 2, NAN);
        for (size_t number = 0; number < outputs.size();) {
            const Output& first = outputs[number];
            if (first.lod < lod || ((first.flags & O_JOINT) && !named(first.joint.value))) {
                ++number;
                continue;
            }
            const size_t count = std::min<size_t>(first.pack, outputs.size() - number);
            float pack[4] = {0.f, 0.f, 0.f, 0.f};
            bool any = false;
            for (size_t slot = 0; slot < count; ++slot) {
                const Output& o = outputs[number + slot];
                const auto value = output_value(o);
                if (!value) continue;
                pack[slot] = *value;
                if (stores(o)) r.outputs[number + slot] = *value, r.written[number + slot] = 1;
                any = true;
            }
            if (any) apply(number, count, pack, blend);
            number += count;
        }
        if (r.weights_set && !r.weights.empty()) normalize_groups();
    }
    bool stores(const Output& o) const {
        if (rules.output_values == 0) return true;
        if (rules.output_values == 2) return false;
        const bool transform = o.element == TRANS || o.element == ROT || o.element == SCALE || o.element == ROT_RPY
                               || o.element == ROT_PYR || o.element == EXP_MAP;
        return !transform || own->own_number(o.joint_hash) >= 0;
    }
    void normalize_groups() {
        std::vector<float>& weights = results->weights;
        const std::vector<uint32_t>& channels = results->channels;
        for (const Group& g : groups) {
            std::vector<Weighted> items;
            for (const uint32_t hash : g.hashes)
                for (size_t i = 0; i < std::min(channels.size(), weights.size()); ++i)
                    if (channels[i] == hash) {
                        items.push_back({weights[i], (int)i});
                        break;
                    }
            EngineSort{items}.run();
            const int top = std::min(g.top, (int)items.size());
            if (top >= 1) {
                float total = 0.f;
                for (int i = 0; i < top; ++i) total = total + items[i].weight;
                if (!(0.f <= total)) {
                    std::fill(weights.begin(), weights.end(), 0.f);
                    continue;
                }
                if (1.f <= total)
                    for (int i = 0; i < top; ++i) items[i].weight = items[i].weight / total;
            }
            for (size_t i = std::max(top, 0); i < items.size(); ++i) items[i].weight = 0.f;
            for (const Weighted& w : items) weights[w.slot] = w.weight;
        }
    }
    float combine(int mode, float total, float value, int count) const {
        if (mode == SUM || mode == AVERAGE) return total + value;
        if (mode == MUL) return total * value;
        if (mode == MIN) return count == 0 ? value : rules.sse ? if_lt(total, value, total, value) : if_le(value, total, value, total);
        if (mode == MAX) return count == 0 ? value : rules.sse ? if_lt(total, value, value, total) : if_le(value, total, total, value);
        return total;
    }
    std::optional<float> output_value(const Output& o) {
        float total = o.mode == MUL ? 1.f : 0.f;
        int count = 0;
        for (int i = 0; i < o.cone_drivers; ++i) {
            const Driver& d = cone_drivers[o.first_cone_driver + i];
            if (d.cone == 0xFF) continue;
            total = combine(o.mode, total, cone_driver_value(d), count), ++count;
        }
        for (int i = 0; i < o.drivers; ++i)
            total = combine(o.mode, total, driver_value(drivers[o.first_driver + i]), count), ++count;
        if (count < 1) return std::nullopt;
        if (o.mode == SUM || o.mode == AVERAGE) {
            if ((o.flags & O_BASE) && o.element == SCALE) total = total - (float)count;
            if (o.mode == AVERAGE) total = total / (float)count;
        }
        if (o.flags & O_CLAMP_MIN)
            total = rules.sse ? if_lt(o.min_value, total, total, o.min_value) : if_le(total, o.min_value, o.min_value, total);
        if (o.flags & O_CLAMP_MAX)
            total = rules.sse ? if_lt(o.max_value, total, o.max_value, total) : if_le(total, o.max_value, total, o.max_value);
        const bool angle = o.element == ROT || o.element == ROT_RPY || o.element == ROT_PYR || o.element == EXP_MAP
                           || o.element == ROT2 || o.element == ROT_RPY2 || o.element == ROT_PYR2 || o.element == EXP_MAP2;
        if (rules.radians_by_flag ? (o.flags & O_ROT) != 0 : angle) return total * 0.0055555556900799274f * PI;
        if (o.element == TRANS) return total * HUNDREDTH;
        if (o.element == SCALE) return total + ((o.flags & O_BASE) ? 1.f : 0.f);
        return total;
    }
    float function_curve(const Driver& d, float x) const {
        return d.keys ? solve_curve(&keys[d.first_key], d.keys, d.pre, d.post, x, rules.positive_odd, rules.roots) : 0.f;
    }
    float cone_curve(const Driver& d, float x) const {
        if (d.curve == MIN_MAX) return remap(x, 0.f, 1.f, d.out_min, d.out_max, d.interpolation, rules.remap_bounds);
        return d.curve == FUNCTION ? function_curve(d, x) : 0.f;
    }
    float cone_driver_value(const Driver& d) {
        const float x = d.cone < (int)cone_values.size() ? cone_values[d.cone] : 0.f;
        return noted(cone_driver_points, &d - cone_drivers.data(), x, cone_curve(d, x));
    }
    float joint_curve(const Driver& d, float x) const {
        if (rules.curves_by_keyframes) return d.keyframes ? function_curve(d, x) : min_max(x, d);
        if (d.curve == MIN_MAX) return min_max(x, d);
        if (d.curve == FUNCTION) return function_curve(d, x);
        if (d.curve == SCALING) return x * d.in_min;
        return 0.f;
    }
    float driver_value(const Driver& d) {
        V3 source = ZERO3;
        if (const Joint j = d.joint.none() ? Joint{} : joint(d.joint)) source = driver_input(j, d);
        const float x = 0 <= d.axis && d.axis < 3 ? source[d.axis] : 0.f;
        return noted(driver_points, &d - drivers.data(), x, joint_curve(d, x));
    }
    static float noted(std::vector<float>& points, ptrdiff_t at, float x, float y) {
        if (2 * at + 1 < (ptrdiff_t)points.size()) points[2 * at] = x, points[2 * at + 1] = y;
        return y;
    }
    V3 euler_degrees(const Q4& q, int order) const {
        return scaled(0 <= order && order < 6 ? matrix_euler(order, quaternion_matrix(q, rules.subtract)) : ZERO3, DEGREES);
    }
    V3 roll_bend_degrees(const Q4& q, const Q4& offset, bool pyr) const {
        const V3 values = decompose_rotate(q, offset, pyr);
        return {values[rules.roll_bend_axes[0]] * DEGREES, values[rules.roll_bend_axes[1]] * DEGREES,
                values[rules.roll_bend_axes[2]] * DEGREES};
    }
    V3 driver_input(Joint j, const Driver& d) {
        Rig& rig = *j.rig;
        const int n = j.number;
        const bool base = d.flags & D_BASE;
        if (d.flags & D_WORLD) {
            switch (d.input) {
            case IN_TRANS: return scaled(rig.position(n), 100.f);
            case IN_ROT: return euler_degrees(rig.rotation(n), d.order);
            case IN_SCALE: return row_lengths(rig.world_matrix(n));
            case IN_ROT_RPY: case IN_ROT_PYR: return roll_bend_degrees(rig.rotation(n), d.offset, d.input == IN_ROT_PYR);
            case IN_EXP_MAP: return scaled(decompose_exp_map(rig.rotation(n), d.offset), DEGREES);
            }
            return ZERO3;
        }
        switch (d.input) {
        case IN_TRANS: {
            const V3 p = rig.pos(n), b = base ? rig.base_pos(n) : ZERO3;
            return base ? V3{(p[0] - b[0]) * 100.f, (p[1] - b[1]) * 100.f, (p[2] - b[2]) * 100.f} : scaled(p, 100.f);
        }
        case IN_ROT: return euler_degrees(base ? mul(rig.rot(n), conjugate(rig.base_rot(n))) : rig.rot(n), d.order);
        case IN_SCALE: {
            const V3 s = rig.scale(n);
            return base ? V3{s[0] - 1.f, s[1] - 1.f, s[2] - 1.f} : s;
        }
        case IN_ROT_RPY: case IN_ROT_PYR:
            return roll_bend_degrees(local_rotation(rig, n, base), d.offset, d.input == IN_ROT_PYR);
        case IN_EXP_MAP: return scaled(decompose_exp_map(local_rotation(rig, n, base), d.offset), DEGREES);
        }
        return ZERO3;
    }
    static Q4 local_rotation(const Rig& rig, int n, bool base) { return base ? mul(rig.rot(n), inverse(rig.base_rot(n))) : rig.rot(n); }
    float min_max(float x, const Driver& d) const {
        float low = d.in_min, middle = d.in_mid, high = d.in_max, start, end;
        if (!(d.flags & D_MID))
            return low != high ? remap(x, low, high, d.out_min, d.out_max, d.interpolation, rules.remap_bounds) : 0.f;
        if (!(middle < low) && !(high < middle))
            start = d.out_min, end = d.out_max;
        else if (!(low < middle) && !(middle < high))
            std::swap(low, high), start = d.out_max, end = d.out_min;
        else
            return 0.f;
        if (!(low <= x)) return start;
        if (x < middle) return remap(x, low, middle, start, d.out_mid, d.interpolation, rules.remap_bounds);
        if (x < high) return remap(x, middle, high, d.out_mid, end, d.interpolation, rules.remap_bounds);
        return end;
    }
    Q4 framed(const Q4& rotation, const Q4& offset) const {
        return mul(mul(offset, rotation), rules.frame_inverse ? inverse(offset) : conjugate(offset));
    }
    V3 to_rb(const Q4& q) const { return rules.roll_bend == 0 ? to_roll_bend(q) : to_roll_bend_closed(q); }
    Q4 from_rb(const V3& v) const {
        return rules.roll_bend == 0 ? from_roll_bend(v) : from_roll_bend_closed(v, rules.roll_bend == 2);
    }
    // expression::DecomposeRotate: RotPYR negates the angles of the conjugate (or the inverse).
    V3 decompose_rotate(const Q4& rotation, const Q4& offset, bool pyr) const {
        const Q4 turned = framed(rotation, offset);
        if (!pyr) return to_rb(turned);
        return scaled(to_rb(rules.pyr_inverse ? inverse(turned) : conjugate(turned)), -1.f);
    }
    V3 decompose_exp_map(const Q4& rotation, const Q4& offset) const {
        Q4 turned = framed(rotation, offset);
        if (rules.exp_shortest && 0.f > turned[3]) turned = neg(turned);
        V3 axis = xyz(turned);
        if (1.f > std::fabs(turned[3])) {
            const float angle = acos_f(turned[3]), sine = sin_f(angle);
            if (std::fabs(sine) >= 1e-5f) axis = scaled(axis, angle / sine);
        }
        return scaled(axis, 2.f);
    }
    Q4 compose_rotate(const V3& values, const Q4& offset, bool pyr, bool legacy_) const {
        if (legacy_) {
            Q4 rotation = from_rb(pyr ? scaled(values, -1.f) : values);
            if (pyr) rotation = inverse(rotation);
            return mul(mul(inverse(offset), rotation), offset);
        }
        Q4 rotation;
        if (pyr) {
            const Q4 turned = from_rb(scaled(values, -1.f));
            rotation = {-turned[0], -turned[1], -turned[2], turned[3]};
        } else
            rotation = from_rb(values);
        return mul(mul(conjugate(offset), rotation), offset);
    }
    static Q4 compose_exp_map(const V3& values, const Q4& offset) {
        return mul(mul(inverse(offset), exp_rotation(scaled(values, 0.5f))), offset);
    }
    Q4 euler_rotation(bool matrix_form, int order, const V3& angles) const {
        if (matrix_form) return matrix_rotation(euler_matrix(order, angles));
        return 0 <= order && order < 6 ? euler_quaternion(order, angles) : IDENTITY;
    }
    void apply(size_t number, size_t count, const float* values, const float* blend) {
        const Output& first = outputs[number];
        const int element = first.element;
        const bool jointed = element == TRANS || element == ROT || element == SCALE || element == ROT_RPY || element == ROT_PYR
                             || element == EXP_MAP || element == ROT2 || element == ROT_RPY2 || element == ROT_PYR2
                             || element == EXP_MAP2;
        if (rules.outputs_by_flag && jointed != ((first.flags & O_JOINT) != 0)) return;
        auto blended = [&](float current, float target) { return blend ? current + (target - current) * *blend : target; };
        Results& r = *results;
        if (element == DEFORM) {
            if (!r.has_channels) return;
            if (!r.weights_set) {
                if (r.weights.size() != r.channels.size() && rules.mismatched_untouched) return;
                r.weights_set = true;
                if (r.weights.size() != r.channels.size()) r.weights.assign(r.channels.size(), 0.f);
            }
            for (size_t i = 0; i < r.channels.size(); ++i)
                if (r.channels[i] == first.joint_hash) {
                    r.weights[i] = blend ? values[0] * *blend : values[0];
                    break;
                }
            return;
        }
        if (element == MATERIAL || element == MATERIAL_F4 || element == MATERIAL_POS_F4 || element == MATERIAL_ROT_F4) {
            if (rules.materials && r.materials && (first.joint_hash || first.variable_hash)) set_param(number, count, values);
            return;
        }
        const Joint j = jointed ? output_joint(first) : Joint{};
        if (!j) return;
        Rig& rig = *j.rig;
        const int n = j.number;
        const bool base = first.flags & O_BASE;
        if (element == ROT2 || element == ROT_RPY2 || element == ROT_PYR2 || element == EXP_MAP2) {
            V3 angles = ZERO3;
            for (size_t slot = 0; slot < count; ++slot)
                if (0 <= outputs[number + slot].axis && outputs[number + slot].axis < 3) angles[outputs[number + slot].axis] = values[slot];
            Q4 rotation = element == ROT2 ? euler_rotation(rules.rot2_matrix, first.order, angles)
                          : element == EXP_MAP2 ? compose_exp_map(angles, first.offset)
                          : compose_rotate(angles, first.offset, element == ROT_PYR2, false);
            if (base) rotation = mul(rotation, rig.base_rot(n));
            if (blend && *blend < 1.f) rotation = nlerp(rig.rot(n), rotation, *blend);
            rig.set_rot(n, rotation);
            return;
        }
        if (element == TRANS) {
            V3 position = rig.pos(n);
            const V3 offset = base ? rig.base_pos(n) : ZERO3;
            for (size_t slot = 0; slot < count; ++slot) {
                const int axis = outputs[number + slot].axis;
                if (0 <= axis && axis < 3) position[axis] = blended(position[axis], values[slot] + offset[axis]);
            }
            rig.set_pos(n, position);
        } else if (element == SCALE) {
            V3 scale = rig.scale(n);
            for (size_t slot = 0; slot < count; ++slot) {
                const int axis = outputs[number + slot].axis;
                if (0 <= axis && axis < 3) scale[axis] = blended(scale[axis], values[slot]);
            }
            rig.set_scale(n, scale);
        } else if (element == ROT) {
            const int order = first.order;
            V3 angles = 0 <= order && order < 6 ? matrix_euler(order, quaternion_matrix(local_rotation(rig, n, base), rules.subtract)) : ZERO3;
            for (size_t slot = 0; slot < count; ++slot) {
                const int axis = outputs[number + slot].axis;
                if (0 <= axis && axis < 3) angles[axis] = blended(angles[rules.euler_from[axis]], values[slot]);
            }
            const Q4 rotation = euler_rotation(rules.rot_matrix, order, angles);
            rig.set_rot(n, base ? mul(rotation, rig.base_rot(n)) : rotation);
        } else {
            const Q4 local = local_rotation(rig, n, base);
            const bool exp_map = element == EXP_MAP, pyr = element == ROT_PYR;
            V3 angles = exp_map ? decompose_exp_map(local, first.offset) : decompose_rotate(local, first.offset, pyr);
            for (size_t slot = 0; slot < count; ++slot) {
                const int axis = outputs[number + slot].axis;
                if (!(0 <= axis && axis < 3)) continue;
                const int at = exp_map ? axis : rules.roll_bend_axes[axis];
                angles[at] = blended(angles[at], values[slot]);
            }
            Q4 rotation = exp_map ? compose_exp_map(angles, first.offset) : compose_rotate(angles, first.offset, pyr, legacy);
            if (legacy)
                rotation = mul(rotation, base ? rig.base_rot(n) : rig.rot(n));
            else if (base)
                rotation = mul(rotation, rig.base_rot(n));
            rig.set_rot(n, rotation);
        }
    }

    // A material output: the mesh's last MaterialParam for the record's material and variable, else a new one (when
    // the record names both) seeded from the variable's value unless legacy_material_param; Material sets its float,
    // the others lanes of its float4 by the outputs' axes (MaterialPosF4 in hundredths, MaterialRotF4 Euler degrees
    // of the param's quaternion).
    void set_param(size_t number, size_t count, const float* values) {
        const Output& first = outputs[number];
        Materials& m = *results->materials;
        Param* param = nullptr;
        for (Param& p : m.params)
            if (p.material == first.joint_hash && p.variable == first.variable_hash) param = &p;
        if (!param) {
            if (!first.named) return;
            Param made{first.named_material, first.named_variable, {}, 0.f};
            const Variable* seed = legacy_material ? nullptr : m.find(first.joint_hash, first.variable_hash);
            if (seed && seed->type == 4) made.vector = seed->value;
            else if (seed && seed->type == 1) made.scalar = seed->value[0];
            param = &m.params.emplace_back(made);
        }
        if (first.element == MATERIAL) {
            param->scalar = values[0];
            return;
        }
        Q4 vector = param->vector;
        if (first.element == MATERIAL_ROT_F4) {
            V3 angles = 0 <= first.order && first.order < 6 ? matrix_euler(first.order, quaternion_matrix(vector, rules.subtract)) : ZERO3;
            for (size_t slot = 0; slot < count; ++slot)
                if (const int axis = outputs[number + slot].axis; 0 <= axis && axis < 3) angles[axis] = INV_180 * values[slot] * PI;
            vector = euler_quaternion(first.order, angles);
        } else
            for (size_t slot = 0; slot < count; ++slot)
                if (const int axis = outputs[number + slot].axis; 0 <= axis && axis < 4)
                    vector[axis] = first.element == MATERIAL_POS_F4 ? HUNDREDTH * values[slot] : values[slot];
        param->vector = vector;
    }
    // Material constraints: a joint's local or world position, Euler degrees, scale (world: the world matrix's row
    // lengths) or rotation into an unlocked float4 material variable.
    void constrain_materials() {
        if (!rules.materials || !results->materials) return;
        for (const MaterialConstraint& c : material_constraints) {
            const Joint j = c.joint.by_index ? located(c.joint.value, true) : own->locate((uint32_t)c.joint.value);
            Variable* variable = j ? results->materials->find(c.material, c.variable) : nullptr;
            if (!variable) continue;
            Rig& rig = *j.rig;
            const int n = j.number;
            Q4 value{};
            if (c.mode == APPLY_TRANS) {
                const V3 t = c.world ? rig.position(n) : rig.pos(n);
                value = {t[0], t[1], t[2], 0.f};
            } else if (c.mode == APPLY_EULER) {
                const Q4 q = c.world ? rig.rotation(n) : rig.rot(n);
                const V3 e = 0 <= c.order && c.order < 6 ? matrix_euler(c.order, quaternion_matrix(q, rules.subtract)) : ZERO3;
                value = {e[0] * DEGREES, e[1] * DEGREES, e[2] * DEGREES, 0.f};
            } else if (c.mode == APPLY_SCALE && c.world) {
                const M4 w = rig.world_matrix(n);
                auto length = [&](int row) { return std::sqrt(((w[row] * w[row] + w[row + 1] * w[row + 1]) + w[row + 2] * w[row + 2]) + w[row + 3] * w[row + 3]); };
                value = {length(0), length(4), length(8), 0.f};
            } else if (c.mode == APPLY_SCALE) {
                const V3 s = rig.scale(n);
                value = {s[0], s[1], s[2], 0.f};
            } else if (c.mode == APPLY_ROT)
                value = c.world ? rig.rotation(n) : rig.rot(n);
            if (variable->type == 4 && !variable->locked) variable->value = value;
        }
    }
    void run(int lod, float blend, const std::vector<int>* kinds) {
        cone_values.resize(cones.size(), 0.f);
        for (const int kind : kinds ? *kinds : rules.order.empty() ? order : rules.order) {
            if (kind == OUTPUTS) run_outputs(lod, blend);
            else if (kind == ROT_EXPRESSION) rot_expressions(lod);
            else if (kind == MULTI) multi_constraints(lod);
            else if (kind == AIM) aim_constraints(lod);
            else if (kind == MATERIAL_KIND) constrain_materials();
        }
    }
};

// Python glue.
struct Fast {
    PyObject* list = nullptr;
    explicit Fast(PyObject* o) : list(o ? PySequence_Fast(o, "expected a sequence") : nullptr) {}
    ~Fast() { Py_XDECREF(list); }
    Py_ssize_t size() const { return list ? PySequence_Fast_GET_SIZE(list) : 0; }
    PyObject* operator[](Py_ssize_t i) const { return PySequence_Fast_GET_ITEM(list, i); }
};
struct Reader {
    Fast items;
    Py_ssize_t at = 0;
    explicit Reader(PyObject* o) : items(o) {}
    PyObject* next() { return at < items.size() ? items[at++] : Py_None; }
    long long integer() { return PyLong_AsLongLong(next()); }
    float number() { return (float)PyFloat_AsDouble(next()); }
    bool flag() { return PyObject_IsTrue(next()) == 1; }
    Ref ref() {
        Reader pair(next());
        Ref r;
        r.by_index = pair.flag();
        r.value = pair.integer();
        return r;
    }
    V3 vec3() {
        Reader v(next());
        return {v.number(), v.number(), v.number()};
    }
    Q4 quat() {
        Reader v(next());
        return {v.number(), v.number(), v.number(), v.number()};
    }
};

int choice(PyObject* rules, const char* key, std::initializer_list<const char*> values) {
    PyObject* value = PyDict_GetItemString(rules, key);
    if (!value || !PyUnicode_Check(value)) return 0;
    const char* text = PyUnicode_AsUTF8(value);
    int i = 0;
    for (const char* v : values) {
        if (std::strcmp(text, v) == 0) return i;
        ++i;
    }
    PyErr_Format(PyExc_ValueError, "rule %s: unknown value %s", key, text);
    return 0;
}
void triple(PyObject* rules, const char* key, int* out) {
    Reader values(PyDict_GetItemString(rules, key));
    for (int i = 0; i < 3; ++i) out[i] = values.items.size() > i ? (int)values.integer() : i;
}

int name_index(const char* category, PyObject* name) {
    for (const auto& [key, members] : NAMES)
        if (std::strcmp(key, category) == 0)
            for (size_t i = 0; i < members.size(); ++i)
                if (PyUnicode_CompareWithASCIIString(name, members[i]) == 0) return (int)i;
    return -1;
}

Rules parse_rules(PyObject* rules) {
    Rules r{};
    PyObject* sums = PyDict_GetItemString(rules, "matrix_sums");
    const char* sites[4] = {"world", "base", "multi", "aim"};
    for (int i = 0; i < 4; ++i) r.sums_paired[i] = sums && choice(sums, sites[i], {"sequential", "pairs"}) == 1;
    r.chain_from_root = choice(rules, "world_chain", {"from_leaf", "from_root"}) == 1;
    r.rotation_from_root = choice(rules, "rotation_chain", {"from_leaf", "from_root"}) == 1;
    r.skip_missing_cone_parent = choice(rules, "missing_cone_parent", {"actual_parent", "skip"}) == 1;
    r.curves_by_keyframes = choice(rules, "function_curves", {"by_curve_type", "by_keyframes"}) == 1;
    r.positive_odd = choice(rules, "oscillation", {"odd", "positive_odd"}) == 1;
    r.roots = choice(rules, "curve_roots", {"stable", "naive", "none"});
    r.legacy_always = choice(rules, "legacy_compose", {"by_flag", "always"}) == 1;
    r.frame_inverse = choice(rules, "decompose_frame", {"conjugate", "inverse"}) == 1;
    r.pyr_inverse = choice(rules, "pitch_yaw_roll", {"negated_conjugate", "negated_inverse"}) == 1;
    r.output_values = choice(rules, "output_values", {"always", "with_target", "none"});
    if (PyObject* order = PyDict_GetItemString(rules, "order"); order && !PyUnicode_Check(order)) {
        Fast names(order);
        for (Py_ssize_t i = 0; i < names.size(); ++i) r.order.push_back(name_index("kind", names[i]));
    }
    r.mismatched_untouched = choice(rules, "mismatched_weights", {"zeroed", "untouched"}) == 1;
    triple(rules, "euler_blend_from", r.euler_from);
    r.blend_set = choice(rules, "output_blend", {"lerp", "set"}) == 1;
    triple(rules, "roll_bend_axes", r.roll_bend_axes);
    r.total_above_epsilon = choice(rules, "multi_weight_total", {"any", "above_epsilon"}) == 1;
    r.subtract = choice(rules, "matrix_diagonal", {"sum", "subtract"}) == 1;
    r.roll_bend = choice(rules, "roll_bend", {"arc", "closed", "closed_expanded"});
    r.sse = choice(rules, "min_max", {"compare", "sse"}) == 1;
    PyObject* compose = PyDict_GetItemString(rules, "euler_compose");
    r.rot_matrix = !compose || choice(compose, "Rot", {"matrix", "half_angles"}) == 0;
    r.rot2_matrix = !compose || choice(compose, "Rot2", {"matrix", "half_angles"}) == 0;
    r.aim_none_parent = choice(rules, "aim_none", {"world", "parent"}) == 1;
    r.rotate_up = choice(rules, "object_rotation_up", {"matrix", "rotate"}) == 1;
    r.exp_shortest = choice(rules, "exp_map_angle", {"full", "shortest"}) == 1;
    r.remap_bounds = choice(rules, "remap", {"clamp", "bounds"}) == 1;
    r.radians_by_flag = choice(rules, "output_radians", {"type", "out_rot"}) == 1;
    r.outputs_by_flag = choice(rules, "output_target", {"always", "out_joint"}) == 1;
    r.aim_premultiplied = choice(rules, "aim_frame", {"per_update", "premultiplied"}) == 1;
    r.zero_blend_skip = choice(rules, "zero_blend", {"evaluate", "skip"}) == 1;
    r.legacy_general_inverse = choice(rules, "legacy_multi_inverse", {"affine", "general"}) == 1;
    r.multi_fields_ignored = choice(rules, "multi_fields", {"read", "ignored"}) == 1;
    r.materials = choice(rules, "materials", {"none", "params"}) == 1;
    PyObject* from = PyDict_GetItemString(rules, "joint_map_from");
    r.joint_map_from = from ? (int)PyLong_AsLong(from) : 0;
    return r;
}

Driver parse_driver(PyObject* item, std::vector<Key>& keys, bool cone) {
    Reader d(item);
    Driver r{};
    if (cone) {
        r.cone = (int)d.integer();
        r.out_min = d.number(), r.out_max = d.number();
        r.interpolation = (int)d.integer(), r.curve = (int)d.integer();
    } else {
        r.joint = d.ref();
        r.flags = (int)d.integer(), r.input = (int)d.integer(), r.axis = (int)d.integer(), r.order = (int)d.integer();
        r.interpolation = (int)d.integer(), r.curve = (int)d.integer();
        r.in_min = d.number(), r.in_mid = d.number(), r.in_max = d.number();
        r.out_min = d.number(), r.out_mid = d.number(), r.out_max = d.number();
        r.offset = d.quat();
    }
    r.pre = (int)d.integer(), r.post = (int)d.integer();
    PyObject* list = d.next();
    r.keyframes = list != Py_None;
    Fast frames(list == Py_None ? nullptr : list);
    r.first_key = (int)keys.size(), r.keys = (int)frames.size();
    for (Py_ssize_t i = 0; i < frames.size(); ++i) {
        Reader k(frames[i]);
        keys.push_back({k.number(), k.number(), k.number(), k.number(), k.number(), k.number(), (int)k.integer()});
    }
    return r;
}

struct ProgramObject {
    PyObject_HEAD
    Program* program;
};

void program_dealloc(PyObject* self) {
    delete reinterpret_cast<ProgramObject*>(self)->program;
    Py_TYPE(self)->tp_free(self);
}

uint32_t hash_of(PyObject* value) { return (uint32_t)PyLong_AsUnsignedLongMask(value); }

int program_init(PyObject* self, PyObject* args, PyObject*) {
    PyObject *rules, *root, *names, *cones, *outputs, *expressions, *multis, *parents, *hashes, *aims, *materials, *groups, *order;
    if (!PyArg_ParseTuple(args, "O!OOOOOOOOOOOO", &PyDict_Type, &rules, &root, &names, &cones, &outputs, &expressions,
                          &multis, &parents, &hashes, &aims, &materials, &groups, &order))
        return -1;
    auto& slot = reinterpret_cast<ProgramObject*>(self)->program;
    delete slot;
    auto* p = slot = new Program();
    p->rules = parse_rules(rules);
    Reader flags(root);
    p->legacy = p->rules.legacy_always || flags.flag();
    p->legacy_init = flags.flag(), p->table = flags.flag(), p->cone_states = flags.flag(), p->legacy_material = flags.flag();
    Fast name_list(names);
    for (Py_ssize_t i = 0; i < name_list.size(); ++i) p->names.push_back(hash_of(name_list[i]));
    p->cache.assign(p->names.size(), {-1, -1});
    Fast cone_list(cones);
    for (Py_ssize_t i = 0; i < cone_list.size(); ++i) {
        Reader c(cone_list[i]);
        Cone cone;
        cone.joint = c.ref(), cone.parent = c.ref(), cone.aim = c.ref();
        cone.parent_index = (int)c.integer(), cone.axis = c.integer(), cone.up = c.integer();
        cone.vector = c.flag(), cone.up_is_vector = c.flag(), cone.base = c.flag(), cone.half = c.number(), cone.scale = c.number();
        cone.offset = c.quat(), cone.cone_vec = c.vec3(), cone.target_vec = c.vec3(), cone.up_vec = c.vec3();
        p->cones.push_back(cone);
    }
    Fast output_list(outputs);
    for (Py_ssize_t i = 0; i < output_list.size(); ++i) {
        Reader o(output_list[i]);
        Output out{};
        out.joint = o.ref();
        out.joint_hash = (uint32_t)o.integer(), out.variable_hash = (uint32_t)o.integer();
        out.element = (int)o.integer(), out.mode = (int)o.integer(), out.axis = (int)o.integer(), out.order = (int)o.integer();
        out.lod = (int)o.integer(), out.flags = (int)o.integer(), out.pack = (int)o.integer();
        out.min_value = o.number(), out.max_value = o.number(), out.offset = o.quat();
        Fast cone_drivers(o.next()), drivers(o.next());
        out.first_cone_driver = (int)p->cone_drivers.size(), out.cone_drivers = (int)cone_drivers.size();
        for (Py_ssize_t k = 0; k < cone_drivers.size(); ++k) p->cone_drivers.push_back(parse_driver(cone_drivers[k], p->keys, true));
        out.first_driver = (int)p->drivers.size(), out.drivers = (int)drivers.size();
        for (Py_ssize_t k = 0; k < drivers.size(); ++k) p->drivers.push_back(parse_driver(drivers[k], p->keys, false));
        if (PyObject* named = o.next(); (out.named = named != Py_None)) {
            Reader pair(named);
            out.named_material = (uint32_t)pair.integer(), out.named_variable = (uint32_t)pair.integer();
        }
        p->outputs.push_back(out);
    }
    Fast expression_list(expressions);
    for (Py_ssize_t i = 0; i < expression_list.size(); ++i) {
        Reader e(expression_list[i]);
        Expression x{};
        x.target = e.ref(), x.source = e.ref(), x.lod = (int)e.integer();
        x.in_base_quat = e.quat(), x.out_base_quat = e.quat();
        x.x = e.number(), x.y = e.number(), x.z = e.number();
        x.in_base = e.flag(), x.out_base = e.flag(), x.inverse_ = e.flag(), x.mirroring = e.flag();
        p->expressions.push_back(x);
    }
    Fast multi_list(multis);
    for (Py_ssize_t i = 0; i < multi_list.size(); ++i) {
        Reader m(multi_list[i]);
        Multi multi{};
        multi.joint = m.ref(), multi.lod = (int)m.integer(), multi.property = (int)m.integer(), multi.maintain = m.flag();
        Fast weights(m.next());
        for (Py_ssize_t k = 0; k < weights.size(); ++k) {
            Reader w(weights[k]);
            const Ref ref = w.ref();
            multi.weights.push_back({ref, w.number()});
        }
        p->multis.push_back(multi);
    }
    Fast parent_list(parents);
    for (Py_ssize_t i = 0; i < parent_list.size(); ++i) {
        Reader m(parent_list[i]);
        const int index = (int)m.integer();
        p->multi_parents.push_back({index, (int)m.integer()});
    }
    Fast hash_list(hashes);
    for (Py_ssize_t i = 0; i < hash_list.size(); ++i) p->map_hashes.push_back(hash_of(hash_list[i]));
    Fast aim_list(aims);
    for (Py_ssize_t i = 0; i < aim_list.size(); ++i) {
        Reader a(aim_list[i]);
        Aim aim{};
        aim.joint = a.ref(), aim.up_object = a.ref();
        aim.lod = (int)a.integer(), aim.up_type = (int)a.integer(), aim.offset_order = (int)a.integer();
        aim.aim_vector = a.vec3(), aim.up_vector = a.vec3(), aim.world_up_vector = a.vec3(), aim.rotate_offset = a.vec3();
        Fast targets(a.next());
        for (Py_ssize_t k = 0; k < targets.size(); ++k) {
            Reader t(targets[k]);
            const Ref ref = t.ref();
            aim.targets.push_back({ref, t.number()});
        }
        p->aims.push_back(aim);
    }
    Fast material_list(materials);
    for (Py_ssize_t i = 0; i < material_list.size(); ++i) {
        Reader c(material_list[i]);
        MaterialConstraint constraint{};
        constraint.joint = c.ref();
        constraint.material = (uint32_t)c.integer(), constraint.variable = (uint32_t)c.integer();
        constraint.mode = (int)c.integer(), constraint.order = (int)c.integer(), constraint.world = c.flag();
        p->material_constraints.push_back(constraint);
    }
    Fast group_list(groups);
    for (Py_ssize_t i = 0; i < group_list.size(); ++i) {
        Reader g(group_list[i]);
        Group group{(int)g.integer(), {}};
        Fast group_hashes(g.next());
        for (Py_ssize_t k = 0; k < group_hashes.size(); ++k) group.hashes.push_back(hash_of(group_hashes[k]));
        p->groups.push_back(group);
    }
    Fast order_list(order);
    for (Py_ssize_t i = 0; i < order_list.size(); ++i) p->order.push_back((int)PyLong_AsLong(order_list[i]));
    return PyErr_Occurred() ? -1 : 0;
}

struct Buffers {
    std::vector<Py_buffer> views;
    ~Buffers() {
        for (Py_buffer& view : views) PyBuffer_Release(&view);
    }
    void* get(PyObject* owner, const char* name, Py_ssize_t items, Py_ssize_t item_size, bool writable) {
        PyObject* value = PyObject_GetAttrString(owner, name);
        if (!value) return nullptr;
        if (value == Py_None) {
            Py_DECREF(value);
            return nullptr;
        }
        Py_buffer view;
        const int got = PyObject_GetBuffer(value, &view, PyBUF_C_CONTIGUOUS | PyBUF_FORMAT | (writable ? PyBUF_WRITABLE : 0));
        Py_DECREF(value);
        if (got < 0) return nullptr;
        views.push_back(view);
        if (view.len != items * item_size || view.itemsize != item_size) {
            PyErr_Format(PyExc_ValueError, "rig.%s: expected %zd items of %zd bytes", name, items, item_size);
            return nullptr;
        }
        return view.buf;
    }
};
bool read_rig(PyObject* object, Rig& rig, const Rules& rules, Buffers& buffers, std::deque<Rig>& hosts) {
    PyObject* hashes = PyObject_GetAttrString(object, "hashes");
    if (!hashes) return false;
    rig.count = PyObject_Length(hashes);
    Py_DECREF(hashes);
    const Py_ssize_t n = rig.count;
    rig.rules = &rules;
    rig.hashes = (const uint32_t*)buffers.get(object, "hashes", n, 4, false);
    rig.parents = (const int32_t*)buffers.get(object, "parents", n, 4, false);
    rig.symmetry = (const int32_t*)buffers.get(object, "symmetry", n, 4, false);
    rig.follows = (const int32_t*)buffers.get(object, "follows", n, 4, false);
    rig.positions = (float*)buffers.get(object, "positions", n * 3, 4, true);
    rig.rotations = (float*)buffers.get(object, "rotations", n * 4, 4, true);
    rig.scales = (float*)buffers.get(object, "scales", n * 3, 4, true);
    rig.base_positions = (const float*)buffers.get(object, "base_positions", n * 3, 4, false);
    rig.base_rotations = (const float*)buffers.get(object, "base_rotations", n * 4, 4, false);
    rig.base_scales = (const float*)buffers.get(object, "base_scales", n * 3, 4, false);
    const float* owner_rotation = (const float*)buffers.get(object, "owner_rotation", 4, 4, false);
    const float* owner_matrix = (const float*)buffers.get(object, "owner_matrix", 16, 4, false);
    if (PyErr_Occurred() || !rig.hashes || !rig.parents || !rig.symmetry || !rig.follows || !rig.positions || !rig.rotations
        || !rig.scales || !rig.base_positions || !rig.base_rotations || !rig.base_scales || !owner_rotation || !owner_matrix) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "rig arrays missing");
        return false;
    }
    std::memcpy(rig.owner_rotation.data(), owner_rotation, 16);
    std::memcpy(rig.owner_matrix.data(), owner_matrix, 64);
    for (Py_ssize_t i = n - 1; i >= 0; --i) rig.index[rig.hashes[i]] = (int)i;
    PyObject* host = PyObject_GetAttrString(object, "host");
    if (!host) return false;
    const bool hosted = host != Py_None;
    if (hosted) {
        Rig& held = hosts.emplace_back();
        if (!read_rig(host, held, rules, buffers, hosts)) {
            Py_DECREF(host);
            return false;
        }
        rig.host = &held;
    }
    Py_DECREF(host);
    return true;
}

struct MaterialsObject {
    PyObject_HEAD
    Materials* materials;
};
PyObject* materials_type = nullptr;

void materials_dealloc(PyObject* self) {
    delete reinterpret_cast<MaterialsObject*>(self)->materials;
    Py_TYPE(self)->tp_free(self);
}

int materials_init(PyObject* self, PyObject* args, PyObject*) {
    PyObject *variables, *params = nullptr;
    if (!PyArg_ParseTuple(args, "O|O", &variables, &params)) return -1;
    auto& slot = reinterpret_cast<MaterialsObject*>(self)->materials;
    delete slot;
    Materials* m = slot = new Materials();
    Fast variable_list(variables), param_list(params);
    for (Py_ssize_t i = 0; i < variable_list.size(); ++i) {
        Reader v(variable_list[i]);
        const uint32_t material = (uint32_t)v.integer(), variable = (uint32_t)v.integer();
        const int type = (int)v.integer();
        const Q4 value = v.quat();
        m->variables.try_emplace(Materials::key(material, variable), Variable{type, value, v.flag()});
    }
    for (Py_ssize_t i = 0; i < param_list.size(); ++i) {
        Reader p(param_list[i]);
        Param param{};
        param.material = (uint32_t)p.integer(), param.variable = (uint32_t)p.integer();
        param.vector = p.quat(), param.scalar = p.number();
        m->params.push_back(param);
    }
    return PyErr_Occurred() ? -1 : 0;
}

PyObject* quad(const Q4& q) { return Py_BuildValue("(dddd)", (double)q[0], (double)q[1], (double)q[2], (double)q[3]); }

PyObject* materials_variables(PyObject* self, void*) {
    PyObject* dict = PyDict_New();
    for (const auto& [key, variable] : reinterpret_cast<MaterialsObject*>(self)->materials->variables) {
        PyObject* k = Py_BuildValue("(kk)", (unsigned long)(key >> 32), (unsigned long)(uint32_t)key);
        PyObject* v = Py_BuildValue("(iN)", variable.type, quad(variable.value));
        PyDict_SetItem(dict, k, v);
        Py_DECREF(k);
        Py_DECREF(v);
    }
    return dict;
}

PyObject* materials_params(PyObject* self, void*) {
    const std::vector<Param>& params = reinterpret_cast<MaterialsObject*>(self)->materials->params;
    PyObject* list = PyList_New((Py_ssize_t)params.size());
    for (size_t i = 0; i < params.size(); ++i)
        PyList_SET_ITEM(list, (Py_ssize_t)i, Py_BuildValue("(kkNd)", (unsigned long)params[i].material,
                                                           (unsigned long)params[i].variable, quad(params[i].vector),
                                                           (double)params[i].scalar));
    return list;
}

PyGetSetDef materials_getset[] = {
    {"variables", materials_variables, nullptr, "{(material, variable): (type, (x, y, z, w))}", nullptr},
    {"params", materials_params, nullptr, "[(material, variable, (x, y, z, w), float)] in the order they were made", nullptr},
    {nullptr, nullptr, nullptr, nullptr, nullptr}};
PyType_Slot materials_slots[] = {{Py_tp_dealloc, (void*)materials_dealloc},
                                 {Py_tp_init, (void*)materials_init},
                                 {Py_tp_new, (void*)PyType_GenericNew},
                                 {Py_tp_getset, materials_getset},
                                 {Py_tp_doc, (void*)"A mesh's material variables and MaterialParams, which runs change."},
                                 {0, nullptr}};
PyType_Spec materials_spec = {"fastjcns.Materials", sizeof(MaterialsObject), 0, Py_TPFLAGS_DEFAULT, materials_slots};

PyObject* program_run(PyObject* self, PyObject* args) {
    PyObject *rig_object, *channels, *weights, *kind_names, *materials;
    int lod, weights_set;
    float blend;
    if (!PyArg_ParseTuple(args, "OifOOpOO", &rig_object, &lod, &blend, &channels, &weights, &weights_set, &kind_names,
                          &materials))
        return nullptr;
    if (materials != Py_None && !PyObject_TypeCheck(materials, (PyTypeObject*)materials_type)) {
        PyErr_SetString(PyExc_TypeError, "materials: expected fastjcns.Materials or None");
        return nullptr;
    }
    std::vector<int> kinds;
    Fast kind_list(kind_names == Py_None ? nullptr : kind_names);
    for (Py_ssize_t i = 0; i < kind_list.size(); ++i) kinds.push_back(name_index("kind", kind_list[i]));
    Program* program = reinterpret_cast<ProgramObject*>(self)->program;
    if (!program) {
        PyErr_SetString(PyExc_RuntimeError, "Program not initialized");
        return nullptr;
    }
    Program& p = *program;
    Buffers buffers;
    std::deque<Rig> hosts;
    Rig rig;
    if (!read_rig(rig_object, rig, p.rules, buffers, hosts)) return nullptr;
    Results results;
    results.has_channels = channels != Py_None;
    results.weights_set = weights_set;
    Fast channel_list(results.has_channels ? channels : nullptr), weight_list(weights == Py_None ? nullptr : weights);
    for (Py_ssize_t i = 0; i < channel_list.size(); ++i) results.channels.push_back(hash_of(channel_list[i]));
    for (Py_ssize_t i = 0; i < weight_list.size(); ++i) results.weights.push_back((float)PyFloat_AsDouble(weight_list[i]));
    if (PyErr_Occurred()) return nullptr;
    results.materials = materials == Py_None ? nullptr : reinterpret_cast<MaterialsObject*>(materials)->materials;
    p.own = &rig, p.results = &results;
    p.run(lod, blend, kind_names == Py_None ? nullptr : &kinds);
    p.own = nullptr, p.results = nullptr;
    PyObject* outputs = results.outputs_ran ? PyList_New((Py_ssize_t)results.written.size()) : Py_NewRef(Py_None);
    for (size_t i = 0; i < results.written.size(); ++i) {
        PyObject* value = results.written[i] ? PyFloat_FromDouble(results.outputs[i]) : Py_NewRef(Py_None);
        PyList_SET_ITEM(outputs, (Py_ssize_t)i, value);
    }
    PyObject* new_weights = PyList_New((Py_ssize_t)results.weights.size());
    for (size_t i = 0; i < results.weights.size(); ++i) PyList_SET_ITEM(new_weights, (Py_ssize_t)i, PyFloat_FromDouble(results.weights[i]));
    return Py_BuildValue("(NNO)", outputs, new_weights, results.weights_set ? Py_True : Py_False);
}

PyObject* program_cone_values(PyObject* self, void*) {
    const Program& p = *reinterpret_cast<ProgramObject*>(self)->program;
    PyObject* list = PyList_New((Py_ssize_t)p.cones.size());
    for (size_t i = 0; i < p.cones.size(); ++i) PyList_SET_ITEM(list, (Py_ssize_t)i, PyFloat_FromDouble(i < p.cone_values.size() ? p.cone_values[i] : 0.0));
    return list;
}

PyObject* program_sample(PyObject* self, PyObject* args) {
    int cone, index;
    PyObject* inputs;
    if (!PyArg_ParseTuple(args, "piO", &cone, &index, &inputs)) return nullptr;
    const Program* program = reinterpret_cast<ProgramObject*>(self)->program;
    if (!program) {
        PyErr_SetString(PyExc_RuntimeError, "Program not initialized");
        return nullptr;
    }
    const Program& p = *program;
    const std::vector<Driver>& drivers = cone ? p.cone_drivers : p.drivers;
    if (index < 0 || index >= (int)drivers.size()) {
        PyErr_SetString(PyExc_IndexError, "no such driver");
        return nullptr;
    }
    Fast list(inputs);
    PyObject* outputs = PyList_New(list.size());
    for (Py_ssize_t i = 0; i < list.size(); ++i) {
        const float x = (float)PyFloat_AsDouble(list[i]);
        const Driver& driver = drivers[index];
        PyList_SET_ITEM(outputs, i, PyFloat_FromDouble(cone ? p.cone_curve(driver, x) : p.joint_curve(driver, x)));
    }
    if (PyErr_Occurred()) {
        Py_DECREF(outputs);
        return nullptr;
    }
    return outputs;
}

PyObject* points_list(const std::vector<float>& points) {
    PyObject* list = PyList_New((Py_ssize_t)(points.size() / 2));
    for (size_t i = 0; i < points.size() / 2; ++i)
        PyList_SET_ITEM(list, (Py_ssize_t)i, std::isnan(points[2 * i]) ? Py_NewRef(Py_None)
                        : Py_BuildValue("(dd)", (double)points[2 * i], (double)points[2 * i + 1]));
    return list;
}

PyObject* program_driver_values(PyObject* self, void*) {
    const Program& p = *reinterpret_cast<ProgramObject*>(self)->program;
    return Py_BuildValue("(NN)", points_list(p.driver_points), points_list(p.cone_driver_points));
}

PyObject* vectors_list(const std::vector<float>& values, size_t width) {
    PyObject* list = PyList_New((Py_ssize_t)(values.size() / width));
    for (size_t i = 0; i < values.size() / width; ++i) {
        PyObject* item = std::isnan(values[width * i]) ? Py_NewRef(Py_None) : PyTuple_New((Py_ssize_t)width);
        for (size_t k = 0; item != Py_None && k < width; ++k)
            PyTuple_SET_ITEM(item, (Py_ssize_t)k, PyFloat_FromDouble((double)values[width * i + k]));
        PyList_SET_ITEM(list, (Py_ssize_t)i, item);
    }
    return list;
}

PyObject* program_guides(PyObject* self, void*) {
    const Program& p = *reinterpret_cast<ProgramObject*>(self)->program;
    return Py_BuildValue("(NN)", vectors_list(p.aim_ups, 3), vectors_list(p.cone_axes, 6));
}

PyMethodDef program_methods[] = {
    {"run", program_run, METH_VARARGS,
     "run(rig, lod, blend, channels, weights, weights_set, kinds, materials) -> (outputs, weights, weights_set)"},
    {"sample", program_sample, METH_VARARGS,
     "sample(cone, driver, inputs) -> the driver's curve at each input (drivers numbered over the outputs in order)"},
    {nullptr, nullptr, 0, nullptr}};
PyGetSetDef program_getset[] = {{"cone_values", program_cone_values, nullptr, "Each cone input's last value", nullptr},
                                {"driver_values", program_driver_values, nullptr,
                                 "([(input, output) | None per joint driver], [... per cone driver]) of the last run",
                                 nullptr},
                                {"guides", program_guides, nullptr,
                                 "([world up | None per aim], [cone axis + measured direction | None per cone]) of the "
                                 "last run", nullptr},
                                {nullptr, nullptr, nullptr, nullptr, nullptr}};
PyType_Slot program_slots[] = {{Py_tp_dealloc, (void*)program_dealloc},
                               {Py_tp_init, (void*)program_init},
                               {Py_tp_new, (void*)PyType_GenericNew},
                               {Py_tp_methods, program_methods},
                               {Py_tp_getset, program_getset},
                               {Py_tp_doc, (void*)"One .jcns layer's records, rules and state, evaluated on a rig."},
                               {0, nullptr}};
PyType_Spec program_spec = {"fastjcns.Program", sizeof(ProgramObject), 0, Py_TPFLAGS_DEFAULT, program_slots};

int module_exec(PyObject* module) {
    const HMODULE ucrt = GetModuleHandleW(L"ucrtbase.dll") ? GetModuleHandleW(L"ucrtbase.dll") : LoadLibraryW(L"ucrtbase.dll");
    auto unary = [&](const char* name) { return reinterpret_cast<float (*)(float)>(GetProcAddress(ucrt, name)); };
    sin_f = unary("sinf"), cos_f = unary("cosf"), tan_f = unary("tanf"), acos_f = unary("acosf"), asin_f = unary("asinf");
    atan2_f = reinterpret_cast<float (*)(float, float)>(GetProcAddress(ucrt, "atan2f"));
    if (!sin_f || !cos_f || !tan_f || !acos_f || !asin_f || !atan2_f) {
        PyErr_SetString(PyExc_ImportError, "ucrtbase float functions not found");
        return -1;
    }
    PyObject* type = PyType_FromSpec(&program_spec);
    if (!type || PyModule_AddObject(module, "Program", type) < 0) return -1;
    materials_type = PyType_FromSpec(&materials_spec);
    if (!materials_type || PyModule_AddObjectRef(module, "Materials", materials_type) < 0) return -1;
    PyObject* names = PyDict_New();
    for (const auto& [category, members] : NAMES) {
        PyObject* tuple = PyTuple_New((Py_ssize_t)members.size());
        for (size_t i = 0; i < members.size(); ++i) PyTuple_SET_ITEM(tuple, (Py_ssize_t)i, PyUnicode_FromString(members[i]));
        PyDict_SetItemString(names, category, tuple);
        Py_DECREF(tuple);
    }
    return PyModule_AddObject(module, "NAMES", names);
}
PyModuleDef_Slot module_slots[] = {{Py_mod_exec, (void*)module_exec}, {0, nullptr}};
PyModuleDef module_def = {PyModuleDef_HEAD_INIT, "fastjcns", "via.motion joint constraint evaluation", 0, nullptr, module_slots};

}  // namespace

PyMODINIT_FUNC PyInit_fastjcns() { return PyModuleDef_Init(&module_def); }
