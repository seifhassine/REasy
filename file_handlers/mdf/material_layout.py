from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MaterialDescriptorLayout:
    parameter_size: int
    textures: tuple[str, ...] = ()
    gpu_buffers: tuple[str, ...] = ()
    shader_lod_redirects: tuple[tuple[tuple[int, ...], tuple[int, ...]], ...] = ()
    baked_texture_array_size: int = 0

    def __post_init__(self):
        if self.parameter_size < 0 or self.parameter_size % 4 or self.baked_texture_array_size < 0:
            raise ValueError("Invalid material descriptor allocation.")
        for names in (self.textures, self.gpu_buffers):
            # Names are case-sensitive (WotS has separate NoiseMap/noisemap slots).
            if any(not n for n in names) or len(set(names)) != len(names):
                raise ValueError("Material descriptor names are empty or ambiguous.")
        for textures, buffers in self.shader_lod_redirects:
            for indices, names in ((textures, self.textures), (buffers, self.gpu_buffers)):
                if len(indices) != len(set(indices)) or any(i < 0 or i >= len(names) for i in indices):
                    raise ValueError("Material shader-LOD redirect is invalid.")

    @classmethod
    def from_material(cls, material):
        return cls(
            int(material.header.params_size),
            tuple(str(t.tex_type) for t in material.textures),
            tuple(str(name.name) for name, _data in material.gpu_buffers),
            tuple((tuple(textures), tuple(buffers)) for textures, buffers in material.shader_lod_redirects),
            int(material.header.BakeTextureArraySize),
        )

    @classmethod
    def from_dict(cls, value):
        return cls(
            int(value["parameter_size"]), tuple(value.get("textures", ())),
            tuple(value.get("gpu_buffers", ())),
            tuple((tuple(t), tuple(b)) for t, b in value.get("shader_lod_redirects", ())),
            int(value.get("baked_texture_array_size", 0)),
        )

    def to_dict(self):
        return {"parameter_size": self.parameter_size, "textures": list(self.textures),
                "gpu_buffers": list(self.gpu_buffers),
                "shader_lod_redirects": [[list(t), list(b)] for t, b in self.shader_lod_redirects],
                "baked_texture_array_size": self.baked_texture_array_size}

    def word_offsets(self, shader_lod: int | None = None) -> dict[tuple[str, str], int]:
        if self.shader_lod_redirects:
            if shader_lod is None or not 0 <= shader_lod < len(self.shader_lod_redirects):
                raise ValueError("The material's active shader LOD must be resolved.")
            textures, buffers = self.shader_lod_redirects[shader_lod]
            cursor = (self.parameter_size + 15) & ~15
        else:
            if shader_lod is not None:
                raise ValueError("A non-LOD material cannot select a shader LOD.")
            textures, buffers = range(len(self.textures)), range(len(self.gpu_buffers))
            cursor = self.parameter_size
        result = {}
        for kind, indices, names in (("texture", textures, self.textures), ("gpu_buffer", buffers, self.gpu_buffers)):
            for index in indices:
                result[kind, names[index]] = cursor
                cursor += 4
        if self.baked_texture_array_size:
            result["baked_texture", ""] = cursor
        return result

    def allocation_size(self, shader_lod: int | None = None) -> int:
        words = self.word_offsets(shader_lod)
        end = max((offset + 4 for offset in words.values()), default=self.parameter_size)
        return (end + 15) & ~15
