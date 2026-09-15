from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

import numpy as np
from OpenGL.GL import (
    GL_MAX_TEXTURE_IMAGE_UNITS,
    GL_TEXTURE0,
    GL_TEXTURE_2D,
    glActiveTexture,
    glBindTexture,
    glDeleteProgram,
    glGetIntegerv,
    glGetUniformLocation,
    glUniform1f,
    glUniform1fv,
    glUniform1i,
    glUniform4f,
    glUseProgram,
)
from OpenGL.GL.shaders import compileProgram, compileShader
from OpenGL.GL import GL_FRAGMENT_SHADER, GL_VERTEX_SHADER

from file_handlers.mesh.material_effects import (
    DMC5_FULL_WRINKLE_EFFECT,
    DMC5_PEOPLE_WRINKLE_EFFECT,
    DMC5_TEETH_OCCLUSION_EFFECT,
    RigLogicMaterialEffect,
    material_texture_key,
)


_CHANNELS = "rgba"
_FULL_MASKS = (
    (0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0),
    (0, 1, 0), (1, 1, 0), (2, 1, 0), (3, 1, 0),
    (0, 2, 0), (1, 2, 0), (2, 2, 0), (3, 2, 2),
    (0, 3, 0), (1, 3, 2), (2, 3, 2), (3, 3, 2),
    (0, 0, 1), (1, 0, 1), (2, 0, 1), (3, 0, 1),
    (0, 1, 1), (1, 1, 1), (2, 1, 1), (3, 1, 1),
    (0, 2, 1), (1, 2, 1), (2, 2, 1), (3, 2, 3),
    (0, 3, 1), (1, 3, 3), (2, 3, 3), (3, 3, 3),
    (0, 0, 2), (1, 0, 2), (2, 0, 2), (3, 0, 2),
    (0, 1, 2), (1, 1, 2), (2, 1, 2), (3, 1, 2),
    (0, 2, 2),
)


@dataclass(slots=True)
class _Program:
    handle: int
    samplers: tuple[int, ...]
    weight_names: tuple[str, ...]
    weights: int
    tint: int
    ambient: int
    diffuse: int
    lit: int


class RigLogicMaterialRenderer:
    """Render established RigLogic material outputs without emulating MMTR."""

    def __init__(self) -> None:
        self._programs: dict[tuple[str, str], _Program] = {}
        self._bound_texture_count = 0

    def bind(
        self,
        effect: RigLogicMaterialEffect,
        material_key: str,
        texture_ids: dict[str, int],
        parameters: dict[str, float],
        *,
        tint: tuple[float, float, float, float],
        ambient: float,
        diffuse: float,
        lit: bool,
        vertex_shader: tuple[str, str] | None = None,
    ) -> int | None:
        texture_handles = tuple(
            texture_ids.get(material_texture_key(material_key, texture_type), 0)
            for texture_type in effect.texture_types
        )
        if not all(texture_handles):
            return None

        program = self._program(effect, vertex_shader)
        glUseProgram(program.handle)
        self._bound_texture_count = len(texture_handles)
        try:
            for unit, (location, texture) in enumerate(
                zip(program.samplers, texture_handles, strict=True)
            ):
                glActiveTexture(GL_TEXTURE0 + unit)
                glBindTexture(GL_TEXTURE_2D, texture)
                glUniform1i(location, unit)
            weights = np.fromiter(
                (
                    float(parameters.get(name, 0.0))
                    for name in program.weight_names
                ),
                dtype=np.float32,
                count=effect.weight_count,
            )
            glUniform1fv(program.weights, effect.weight_count, weights)
            glUniform4f(program.tint, *tint)
            glUniform1f(program.ambient, float(ambient))
            glUniform1f(program.diffuse, float(diffuse))
            glUniform1i(program.lit, int(lit))
        except Exception:
            self.unbind()
            raise
        return program.handle

    def unbind(self) -> None:
        for unit in range(self._bound_texture_count):
            glActiveTexture(GL_TEXTURE0 + unit)
            glBindTexture(GL_TEXTURE_2D, 0)
        glActiveTexture(GL_TEXTURE0)
        glUseProgram(0)
        self._bound_texture_count = 0

    def dispose_gl(self) -> None:
        for program in self._programs.values():
            with suppress(Exception):
                glDeleteProgram(program.handle)
        self._programs.clear()
        self._bound_texture_count = 0

    def _program(
        self,
        effect: RigLogicMaterialEffect,
        vertex_shader: tuple[str, str] | None,
    ) -> _Program:
        variant, vertex_source = vertex_shader or ("fixed", _VERTEX_SHADER)
        key = effect.name, variant
        cached = self._programs.get(key)
        if cached is not None:
            return cached
        texture_limit = int(
            np.asarray(glGetIntegerv(GL_MAX_TEXTURE_IMAGE_UNITS)).reshape(-1)[0]
        )
        if texture_limit < len(effect.texture_types):
            raise RuntimeError(
                f"{effect.name} needs {len(effect.texture_types)} texture "
                f"units; OpenGL exposes {texture_limit}"
            )
        handle = compileProgram(
            compileShader(vertex_source, GL_VERTEX_SHADER),
            compileShader(_fragment_shader(effect), GL_FRAGMENT_SHADER),
        )
        program = _Program(
            handle,
            tuple(
                glGetUniformLocation(handle, f"u_texture{index}")
                for index in range(len(effect.texture_types))
            ),
            effect.weight_names,
            glGetUniformLocation(handle, "u_weights[0]"),
            glGetUniformLocation(handle, "u_tint"),
            glGetUniformLocation(handle, "u_ambient"),
            glGetUniformLocation(handle, "u_diffuse"),
            glGetUniformLocation(handle, "u_lit"),
        )
        if any(location < 0 for location in (
            *program.samplers,
            program.weights,
            program.tint,
            program.ambient,
            program.diffuse,
            program.lit,
        )):
            glDeleteProgram(handle)
            raise RuntimeError(
                f"{effect.name} preview shader omitted a required uniform"
            )
        self._programs[key] = program
        return program


_VERTEX_SHADER = """
#version 120

varying vec2 v_uv;
varying vec3 v_eye_position;
varying vec3 v_eye_normal;
varying vec4 v_color;

void main() {
    vec4 eye = gl_ModelViewMatrix * gl_Vertex;
    gl_Position = gl_ProjectionMatrix * eye;
    v_uv = gl_MultiTexCoord0.xy;
    v_eye_position = eye.xyz;
    v_eye_normal = normalize(gl_NormalMatrix * gl_Normal);
    v_color = gl_Color;
}
"""


def _fragment_shader(effect: RigLogicMaterialEffect) -> str:
    if effect is DMC5_FULL_WRINKLE_EFFECT:
        body = _full_wrinkle_body()
    elif effect is DMC5_PEOPLE_WRINKLE_EFFECT:
        body = _people_wrinkle_body()
    elif effect is DMC5_TEETH_OCCLUSION_EFFECT:
        body = _teeth_occlusion_body()
    else:
        raise ValueError(f"unsupported wrinkle effect {effect.name}")
    samplers = "\n".join(
        f"uniform sampler2D u_texture{index};"
        for index in range(len(effect.texture_types))
    )
    return f"""
#version 120

{samplers}
uniform float u_weights[{effect.weight_count}];
uniform vec4 u_tint;
uniform float u_ambient;
uniform float u_diffuse;
uniform bool u_lit;

varying vec2 v_uv;
varying vec3 v_eye_position;
varying vec3 v_eye_normal;
varying vec4 v_color;

vec3 mappedNormal(vec3 tangentNormal) {{
    vec3 normal = normalize(v_eye_normal);
    vec3 positionX = dFdx(v_eye_position);
    vec3 positionY = dFdy(v_eye_position);
    vec2 uvX = dFdx(v_uv);
    vec2 uvY = dFdy(v_uv);
    vec3 positionYPerp = cross(positionY, normal);
    vec3 positionXPerp = cross(normal, positionX);
    vec3 tangent = positionYPerp * uvX.x + positionXPerp * uvY.x;
    vec3 bitangent = positionYPerp * uvX.y + positionXPerp * uvY.y;
    float scale = max(dot(tangent, tangent), dot(bitangent, bitangent));
    if (scale < 1e-12) {{
        return normal;
    }}
    float inverseScale = inversesqrt(scale);
    return normalize(
        tangent * (tangentNormal.x * inverseScale)
        + bitangent * (tangentNormal.y * inverseScale)
        + normal * tangentNormal.z
    );
}}

void main() {{
{body}
    vec3 normal = mappedNormal(normalize(tangentNormal));
    float light = u_lit
        ? u_ambient * surfaceOcclusion + u_diffuse * max(
            dot(normal, normalize(vec3(0.5, 1.0, 1.0))),
            0.0
        )
        : 1.0;
    gl_FragColor = vec4(
        albedo * u_tint.rgb * v_color.rgb * light,
        baseSample.a * u_tint.a * v_color.a
    );
}}
"""


def _full_wrinkle_body() -> str:
    samples = "\n".join(
        f"    vec4 mask{x}{y} = texture2D("
        f"u_texture8, v_uv * 0.25 + vec2({x}.0, {y}.0) * 0.25);"
        for y in range(4)
        for x in range(4)
    )
    terms = tuple(
        f"u_weights[{index}] * mask{x}{y}.{_CHANNELS[channel]}"
        for index, (x, y, channel) in enumerate(_FULL_MASKS)
    )
    group1 = " + ".join(terms[:19])
    group2 = " + ".join(terms[19:29])
    group3 = " + ".join(terms[29:])
    normal_group3 = f"({group3}) - 2.0 * {terms[38]}"
    return f"""    vec4 baseSample = texture2D(u_texture0, v_uv);
    vec3 diffuse1 = texture2D(u_texture1, v_uv).rgb;
    vec3 diffuse2 = texture2D(u_texture2, v_uv).rgb;
    vec3 diffuse3 = texture2D(u_texture3, v_uv).rgb;
    vec3 normal0 = texture2D(u_texture4, v_uv).rgb * 2.0 - 1.0;
    vec3 normal1 = texture2D(u_texture5, v_uv).rgb * 2.0 - 1.0;
    vec3 normal2 = texture2D(u_texture6, v_uv).rgb * 2.0 - 1.0;
    vec3 normal3 = texture2D(u_texture7, v_uv).rgb * 2.0 - 1.0;
{samples}
    float group1 = {group1};
    float group2 = {group2};
    float group3 = {group3};
    vec3 albedo = baseSample.rgb
        + (diffuse1 - baseSample.rgb) * group1
        + (diffuse2 - baseSample.rgb) * group2
        + (diffuse3 - baseSample.rgb) * group3;
    vec3 tangentNormal = normal0
        + (normal1 - normal0) * group1
        + (normal2 - normal0) * group2
        + (normal3 - normal0) * ({normal_group3});
    float surfaceOcclusion = 1.0;"""


def _people_wrinkle_body() -> str:
    return """    vec4 baseSample = texture2D(u_texture0, v_uv);
    vec3 normal0 = texture2D(u_texture1, v_uv).rgb * 2.0 - 1.0;
    vec3 normal1 = texture2D(u_texture2, v_uv).rgb * 2.0 - 1.0;
    vec3 normal2 = texture2D(u_texture3, v_uv).rgb * 2.0 - 1.0;
    vec2 atlasUv = v_uv * 0.33333;
    vec4 mask00 = texture2D(u_texture4, atlasUv);
    vec4 mask10 = texture2D(u_texture4, atlasUv + vec2(0.3333, 0.0));
    vec4 mask20 = texture2D(u_texture4, atlasUv + vec2(0.6666, 0.0));
    vec4 mask01 = texture2D(u_texture4, atlasUv + vec2(0.0, 0.3333));
    vec4 mask11 = texture2D(u_texture4, atlasUv + vec2(0.3333, 0.3333));
    vec4 mask21 = texture2D(u_texture4, atlasUv + vec2(0.6666, 0.3333));
    vec3 delta1 = normal1 - normal0;
    vec3 delta2 = normal2 - normal0;
    vec3 tangentNormal = normal0 + delta1 * (
        u_weights[0] * mask00.r
        + u_weights[1] * mask10.r
        + u_weights[2] * mask20.r
        + u_weights[3] * mask01.r
        + u_weights[4] * mask11.r
        + u_weights[5] * mask21.r
        + u_weights[6] * mask00.g
        + u_weights[7] * mask10.g
        + u_weights[8] * mask20.g
        + u_weights[9] * mask01.g
        + u_weights[11] * mask21.g
        + u_weights[12] * mask00.a
        + u_weights[13] * mask10.b
    ) + u_weights[10] * mask11.g * (normal1 - vec3(mask01.a))
      + delta2 * (
        u_weights[14] * mask20.a
        + u_weights[15] * mask01.a
        + u_weights[16] * mask11.b
        + u_weights[17] * mask21.a
        + u_weights[18] * mask00.a
        + u_weights[19] * mask10.a
        + u_weights[20] * mask20.a
        + u_weights[21] * mask01.a
        + u_weights[22] * mask11.a
        + u_weights[23] * mask21.a
    );
    vec3 albedo = baseSample.rgb;
    float surfaceOcclusion = 1.0;"""


def _teeth_occlusion_body() -> str:
    return """    vec4 baseSample = texture2D(u_texture0, v_uv);
    vec3 tangentNormal = texture2D(u_texture1, v_uv).rgb * 2.0 - 1.0;
    vec4 baseAtos = texture2D(u_texture2, v_uv);
    vec4 blendAtos = texture2D(u_texture3, v_uv);
    vec3 albedo = baseSample.rgb;
    float surfaceOcclusion = clamp(
        baseAtos.a + (baseAtos.a - blendAtos.b) * u_weights[0],
        0.0,
        1.0
    );"""
