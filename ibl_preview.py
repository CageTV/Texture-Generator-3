"""
ibl_preview.py
GPU Image-Based-Lighting PBR preview for TG3.

Same public API as gpu_preview.GPUPBRRenderer / material_preview.SoftwarePBRRenderer
(load_texture / clear / render(W,H,shape,p) / .az / .el), but shades against a
baked procedural-sky environment instead of a single point light:

    sky (cubemap, procedural sun+sky+ground)
      -> diffuse irradiance convolution   (32x32 cubemap)
      -> GGX specular prefilter           (5 roughness-banded cubemaps)
      -> split-sum BRDF LUT               (128x128 2D, roughness-independent)

Unlike GPUPBRRenderer this class keeps ONE persistent GL context alive across
render() calls (instead of creating/destroying a context every frame), because
the IBL bake above is too expensive to redo every frame. That means an
IBLPBRRenderer instance must only ever be constructed and used from a single,
consistent OS thread — see material_preview.py's worker-thread wiring.

There's no HLSL/DXIL source for this available (only compiled D3D12 .cso
bytecode was provided), so this is a from-scratch GLSL implementation of the
same well-established techniques (Karis 2013 split-sum IBL / LearnOpenGL IBL
tutorial), not a port of that bytecode. Clearcoat and fuzz/sheen are simplified
extra lobes on top of the base Cook-Torrance layer, not energy-conserving
layered BSDFs — good enough for a preview tool, not a offline renderer.
"""

import math
import numpy as np
from PIL import Image
import moderngl

from gpu_preview import (
    _gen_plane, _gen_cube, _gen_sphere, _gen_cylinder,
    _rot_x, _rot_y, _perspective, _translate,
    VERT_SRC as _BASE_VERT_SRC,
)

# OpenGL cubemap face order: +X,-X,+Y,-Y,+Z,-Z. Each face's (dir, right, up)
# basis lets a fullscreen-triangle fragment shader reconstruct a world
# direction from NDC coords for that face.
_FACES = [
    (np.array([ 1, 0, 0], 'f4'), np.array([0, 0, -1], 'f4'), np.array([0, -1, 0], 'f4')),
    (np.array([-1, 0, 0], 'f4'), np.array([0, 0,  1], 'f4'), np.array([0, -1, 0], 'f4')),
    (np.array([ 0, 1, 0], 'f4'), np.array([1, 0,  0], 'f4'), np.array([0,  0, 1], 'f4')),
    (np.array([ 0,-1, 0], 'f4'), np.array([1, 0,  0], 'f4'), np.array([0,  0,-1], 'f4')),
    (np.array([ 0, 0, 1], 'f4'), np.array([1, 0,  0], 'f4'), np.array([0, -1, 0], 'f4')),
    (np.array([ 0, 0,-1], 'f4'), np.array([-1,0,  0], 'f4'), np.array([0, -1, 0], 'f4')),
]

ROUGHNESS_BANDS  = [0.0, 0.25, 0.5, 0.75, 1.0]
PREFILTER_SIZES  = [128, 64, 32, 16, 8]
ENV_SIZE         = 128
IRRADIANCE_SIZE  = 32
BRDF_LUT_SIZE    = 128

# ── Shared fullscreen-triangle vertex shader (no VBO needed) ────────────────
FULLSCREEN_VERT = """
#version 330
out vec2 v_ndc;
void main() {
    vec2 verts[3] = vec2[3](vec2(-1.0,-1.0), vec2(3.0,-1.0), vec2(-1.0,3.0));
    vec2 p = verts[gl_VertexID];
    v_ndc = p;
    gl_Position = vec4(p, 0.0, 1.0);
}
"""

# ── Procedural sky (sun + sky gradient + ground) ─────────────────────────────
SKY_FRAG = """
#version 330
in vec2 v_ndc;
out vec4 fragColor;
uniform vec3 dir0; uniform vec3 rightV; uniform vec3 upV;
uniform vec3 sunDir; uniform float sunIntensity;
uniform vec3 skyColor; uniform vec3 horizonColor; uniform vec3 groundColor;
void main() {
    vec3 d = normalize(dir0 + v_ndc.x*rightV + v_ndc.y*upV);
    float h = d.y;
    vec3 col;
    if (h >= 0.0) col = mix(horizonColor, skyColor, smoothstep(0.0, 0.6, h));
    else          col = mix(horizonColor, groundColor, smoothstep(0.0, -0.5, h));
    float sun = pow(max(dot(d, normalize(sunDir)), 0.0), 300.0);
    col += sun * sunIntensity * vec3(1.0, 0.94, 0.85);
    fragColor = vec4(col, 1.0);
}
"""

# ── Diffuse irradiance convolution (cosine-weighted hemisphere sum) ─────────
IRRADIANCE_FRAG = """
#version 330
in vec2 v_ndc;
out vec4 fragColor;
uniform samplerCube envMap;
uniform vec3 dir0; uniform vec3 rightV; uniform vec3 upV;
const float PI = 3.14159265359;
void main() {
    vec3 N = normalize(dir0 + v_ndc.x*rightV + v_ndc.y*upV);
    vec3 up = abs(N.y) < 0.999 ? vec3(0.0,1.0,0.0) : vec3(1.0,0.0,0.0);
    vec3 right = normalize(cross(up, N));
    up = normalize(cross(N, right));
    vec3 irradiance = vec3(0.0);
    float nrSamples = 0.0;
    float dPhi = 2.0*PI/24.0;
    float dTheta = 0.5*PI/6.0;
    for (float phi = 0.0; phi < 2.0*PI - 0.001; phi += dPhi) {
        for (float theta = 0.0; theta < 0.5*PI - 0.001; theta += dTheta) {
            vec3 tangentSample = vec3(sin(theta)*cos(phi), sin(theta)*sin(phi), cos(theta));
            vec3 sampleVec = tangentSample.x*right + tangentSample.y*up + tangentSample.z*N;
            irradiance += texture(envMap, sampleVec).rgb * cos(theta) * sin(theta);
            nrSamples += 1.0;
        }
    }
    irradiance = PI * irradiance / max(nrSamples, 1.0);
    fragColor = vec4(irradiance, 1.0);
}
"""

# ── Shared GGX importance-sampling helpers (prefilter + BRDF LUT) ───────────
_GGX_HELPERS = """
float RadicalInverse_VdC(uint bits) {
    bits = (bits << 16u) | (bits >> 16u);
    bits = ((bits & 0x55555555u) << 1u) | ((bits & 0xAAAAAAAAu) >> 1u);
    bits = ((bits & 0x33333333u) << 2u) | ((bits & 0xCCCCCCCCu) >> 2u);
    bits = ((bits & 0x0F0F0F0Fu) << 4u) | ((bits & 0xF0F0F0F0u) >> 4u);
    bits = ((bits & 0x00FF00FFu) << 8u) | ((bits & 0xFF00FF00u) >> 8u);
    return float(bits) * 2.3283064365386963e-10;
}
vec2 Hammersley(uint i, uint N) {
    return vec2(float(i)/float(N), RadicalInverse_VdC(i));
}
vec3 ImportanceSampleGGX(vec2 Xi, vec3 N, float rough) {
    float a = rough*rough;
    float phi = 2.0*PI*Xi.x;
    float cosTheta = sqrt((1.0-Xi.y)/(1.0+(a*a-1.0)*Xi.y));
    float sinTheta = sqrt(1.0-cosTheta*cosTheta);
    vec3 H = vec3(sinTheta*cos(phi), sinTheta*sin(phi), cosTheta);
    vec3 up = abs(N.z) < 0.999 ? vec3(0.0,0.0,1.0) : vec3(1.0,0.0,0.0);
    vec3 tangent = normalize(cross(up, N));
    vec3 bitangent = cross(N, tangent);
    return normalize(tangent*H.x + bitangent*H.y + N*H.z);
}
"""

# ── Specular prefilter, split-sum first term ─────────────────────────────────
PREFILTER_FRAG = """
#version 330
in vec2 v_ndc;
out vec4 fragColor;
uniform samplerCube envMap;
uniform vec3 dir0; uniform vec3 rightV; uniform vec3 upV;
uniform float roughness;
const float PI = 3.14159265359;
""" + _GGX_HELPERS + """
void main() {
    vec3 N = normalize(dir0 + v_ndc.x*rightV + v_ndc.y*upV);
    vec3 V = N;
    const uint SAMPLE_COUNT = 32u;
    vec3 color = vec3(0.0);
    float totalWeight = 0.0;
    for (uint i = 0u; i < SAMPLE_COUNT; i++) {
        vec2 Xi = Hammersley(i, SAMPLE_COUNT);
        vec3 H = ImportanceSampleGGX(Xi, N, roughness);
        vec3 L = normalize(2.0*dot(V, H)*H - V);
        float NdL = max(dot(N, L), 0.0);
        if (NdL > 0.0) {
            color += texture(envMap, L).rgb * NdL;
            totalWeight += NdL;
        }
    }
    color = totalWeight > 0.0 ? color/totalWeight : texture(envMap, N).rgb;
    fragColor = vec4(color, 1.0);
}
"""

# ── BRDF integration LUT, split-sum second term (env.-independent) ──────────
BRDF_LUT_FRAG = """
#version 330
in vec2 v_ndc;
out vec4 fragColor;
const float PI = 3.14159265359;
""" + _GGX_HELPERS + """
float GeometrySchlickGGX(float NdV, float rough) {
    float a = rough; float k = (a*a)/2.0;
    return NdV / (NdV*(1.0-k)+k);
}
float GeometrySmith(float NdV, float NdL, float rough) {
    return GeometrySchlickGGX(NdV, rough) * GeometrySchlickGGX(NdL, rough);
}
vec2 IntegrateBRDF(float NdV, float roughness) {
    vec3 V = vec3(sqrt(1.0-NdV*NdV), 0.0, NdV);
    float A = 0.0; float B = 0.0;
    vec3 N = vec3(0.0,0.0,1.0);
    const uint SAMPLE_COUNT = 64u;
    for (uint i = 0u; i < SAMPLE_COUNT; i++) {
        vec2 Xi = Hammersley(i, SAMPLE_COUNT);
        vec3 H = ImportanceSampleGGX(Xi, N, roughness);
        vec3 L = normalize(2.0*dot(V, H)*H - V);
        float NdL = max(L.z, 0.0);
        float NdH = max(H.z, 0.0);
        float VdH = max(dot(V, H), 0.0);
        if (NdL > 0.0) {
            float G = GeometrySmith(NdV, NdL, roughness);
            float G_Vis = (G * VdH) / (NdH * NdV + 1e-5);
            float Fc = pow(1.0 - VdH, 5.0);
            A += (1.0-Fc)*G_Vis;
            B += Fc*G_Vis;
        }
    }
    return vec2(A, B) / float(SAMPLE_COUNT);
}
void main() {
    vec2 uv = v_ndc*0.5+0.5;
    vec2 r = IntegrateBRDF(uv.x, uv.y);
    fragColor = vec4(r, 0.0, 1.0);
}
"""

# ── Main forward PBR shader: direct light + IBL diffuse/specular + ──────────
# ── clearcoat + fuzz/sheen, sharing the base vertex shader from gpu_preview ──
IBL_FRAG_SRC = """
#version 330
in vec3 v_worldPos; in vec3 v_normal; in vec3 v_tangent; in vec3 v_bitangent; in vec2 v_uv;
out vec4 fragColor;

uniform sampler2D albedoTex; uniform sampler2D normalTex; uniform sampler2D rmaosTex; uniform sampler2D heightTex;
uniform int hasAlbedo; uniform int hasNormal; uniform int hasRmaos; uniform int hasHeight;

uniform samplerCube irradianceMap;
uniform samplerCube prefilter0; uniform samplerCube prefilter1; uniform samplerCube prefilter2;
uniform samplerCube prefilter3; uniform samplerCube prefilter4;
uniform sampler2D brdfLUT;

uniform vec3 camPos; uniform vec3 lightDir; uniform vec3 lightColor; uniform float lightIntensity;
uniform float iblIntensity;
uniform float metallicMult; uniform float smoothnessMult; uniform float aoPower;
uniform float coatWeight; uniform float coatRoughness;
uniform float fuzzWeight;
uniform float parallaxDepth;

const float PI = 3.14159265359;

float D_GGX(float NdH, float rough){ float a=rough*rough; a=max(a,0.002); float a2=a*a; float d=NdH*NdH*(a2-1.0)+1.0; return a2/(PI*d*d+1e-7); }
float G_Schlick(float Nd, float k){ return Nd/(Nd*(1.0-k)+k+1e-4); }
float G_Smith(float NdV, float NdL, float rough){ float k=(rough+1.0)*(rough+1.0)/8.0; return G_Schlick(NdV,k)*G_Schlick(NdL,k); }
vec3 F_Schlick(float VoH, vec3 F0){ return F0+(1.0-F0)*pow(clamp(1.0-VoH,0.0,1.0),5.0); }
vec3 F_SchlickRough(float VoH, vec3 F0, float rough){ return F0+(max(vec3(1.0-rough),F0)-F0)*pow(clamp(1.0-VoH,0.0,1.0),5.0); }
vec3 ACESFilm(vec3 x){ return (x*(2.51*x+0.03))/(x*(2.43*x+0.59)+0.14); }

vec2 doParallax(vec2 uv, vec3 Vt, float depth){
    if (hasHeight==0 || depth<0.0005) return uv;
    const int STEPS=10; float layerStep=1.0/float(STEPS); float layer=0.0;
    vec2 delta=-Vt.xy*depth/float(STEPS); vec2 cuv=uv;
    for(int i=0;i<STEPS;i++){ float h=texture(heightTex,fract(cuv)).r; if(layer>=h) break; layer+=layerStep; cuv+=delta; }
    return fract(cuv);
}

vec3 sampleSpecIBL(vec3 R, float rough) {
    float lv = clamp(rough, 0.0, 1.0) * 4.0;
    int lo = int(floor(lv));
    if (lo < 0) lo = 0;
    if (lo > 3) lo = 3;
    float frac = lv - float(lo);
    vec3 a, b;
    if      (lo == 0) { a = texture(prefilter0, R).rgb; b = texture(prefilter1, R).rgb; }
    else if (lo == 1) { a = texture(prefilter1, R).rgb; b = texture(prefilter2, R).rgb; }
    else if (lo == 2) { a = texture(prefilter2, R).rgb; b = texture(prefilter3, R).rgb; }
    else              { a = texture(prefilter3, R).rgb; b = texture(prefilter4, R).rgb; }
    return mix(a, b, frac);
}

void main() {
    vec3 Nraw = normalize(v_normal); vec3 Traw = normalize(v_tangent); vec3 Braw = normalize(v_bitangent);
    vec3 Vworld = normalize(camPos - v_worldPos);
    vec3 Vt = vec3(dot(Vworld,Traw), dot(Vworld,Braw), dot(Vworld,Nraw));
    vec2 uv = doParallax(fract(v_uv), Vt, parallaxDepth);

    vec3 albedo = hasAlbedo==1 ? texture(albedoTex, uv).rgb : vec3(0.6);
    albedo = pow(albedo, vec3(2.2));
    vec3 N = Nraw;
    if (hasNormal==1) {
        vec3 ns = texture(normalTex, uv).rgb*2.0-1.0;
        mat3 TBN = mat3(Traw, Braw, N);
        N = normalize(TBN*ns);
    }
    vec4 rmaos = hasRmaos==1 ? texture(rmaosTex, uv) : vec4(0.5,0.0,1.0,1.0);
    float rough = clamp(rmaos.r*max(2.0-smoothnessMult,0.0), 0.02, 1.0);
    float metal = clamp(rmaos.g*metallicMult, 0.0, 1.0);
    float ao = pow(clamp(rmaos.b,0.0,1.0), aoPower);

    vec3 V = Vworld;
    vec3 L = normalize(lightDir);
    vec3 H = normalize(V+L);
    float NdV = max(dot(N,V), 0.001);
    float NdL = max(dot(N,L), 0.0);
    float NdH = max(dot(N,H), 0.0);
    float VoH = max(dot(V,H), 0.0);

    vec3 F0 = mix(vec3(0.04), albedo, metal);

    // Direct sun/key light (Cook-Torrance)
    float D = D_GGX(NdH, rough);
    float G = G_Smith(NdV, NdL, rough);
    vec3 F = F_Schlick(VoH, F0);
    vec3 spec = D*G*F/(4.0*NdV*NdL+0.001);
    vec3 kD = (vec3(1.0)-F)*(1.0-metal);
    vec3 diff = kD*albedo/PI;
    vec3 direct = (diff+spec)*lightColor*lightIntensity*NdL;

    // IBL diffuse (irradiance convolution)
    vec3 Fr = F_SchlickRough(NdV, F0, rough);
    vec3 kD_ibl = (vec3(1.0)-Fr)*(1.0-metal);
    vec3 irradiance = texture(irradianceMap, N).rgb;
    vec3 iblDiffuse = kD_ibl * albedo * irradiance;

    // IBL specular (split-sum: prefiltered env x BRDF LUT)
    vec3 R = reflect(-V, N);
    vec3 prefiltered = sampleSpecIBL(R, rough);
    vec2 envBRDF = texture(brdfLUT, vec2(NdV, rough)).rg;
    vec3 iblSpecular = prefiltered * (F0*envBRDF.x + envBRDF.y);

    vec3 ibl = (iblDiffuse + iblSpecular) * ao * iblIntensity;

    // Clearcoat: fixed-IOR dielectric layer on top (not energy-conserving, preview-grade)
    vec3 coat = vec3(0.0);
    if (coatWeight > 0.001) {
        float cRough = clamp(coatRoughness, 0.02, 1.0);
        float cD = D_GGX(NdH, cRough);
        float cG = G_Smith(NdV, NdL, cRough);
        float cF = 0.04 + 0.96*pow(clamp(1.0-VoH,0.0,1.0),5.0);
        float cSpec = cD*cG*cF/(4.0*NdV*NdL+0.001);
        vec3 coatEnv = sampleSpecIBL(R, cRough) * cF;
        coat = (cSpec*lightColor*lightIntensity*NdL + coatEnv*ao) * coatWeight;
    }

    // Fuzz / sheen: grazing-angle brightening for cloth/fur-style shading
    vec3 fuzz = vec3(0.0);
    if (fuzzWeight > 0.001) {
        float rim = pow(clamp(1.0-NdV,0.0,1.0), 3.0);
        fuzz = albedo * rim * fuzzWeight * (lightIntensity*0.5 + iblIntensity*0.5);
    }

    vec3 color = direct + ibl + coat + fuzz;
    color = ACESFilm(color);
    color = pow(clamp(color,0.0,1.0), vec3(1.0/2.2));
    fragColor = vec4(color, 1.0);
}
"""


class IBLPBRRenderer:
    def __init__(self):
        self.ctx = moderngl.create_standalone_context(require=430)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.textures = {}
        self.az = 0.4
        self.el = 0.2
        self.dist = 2.8
        self._env_key = None
        self._irradiance = None
        self._prefilter = []
        self._fullscreen_prog_cache = {}
        self._mesh_cache = {}
        self._main_prog = self.ctx.program(vertex_shader=_BASE_VERT_SRC,
                                            fragment_shader=IBL_FRAG_SRC)
        self._brdf_lut = self._build_brdf_lut()

    # -- fullscreen-triangle render-to-texture helper ------------------------
    def _fullscreen_vao(self, frag_src):
        prog = self._fullscreen_prog_cache.get(frag_src)
        if prog is None:
            prog = self.ctx.program(vertex_shader=FULLSCREEN_VERT, fragment_shader=frag_src)
            self._fullscreen_prog_cache[frag_src] = prog
        vao = self.ctx.vertex_array(prog, [])
        return prog, vao

    def _render_pass(self, prog, vao, size, set_uniforms):
        tex = self.ctx.texture((size, size), 4, dtype='f2')
        fbo = self.ctx.framebuffer(color_attachments=[tex])
        fbo.use()
        self.ctx.viewport = (0, 0, size, size)
        set_uniforms(prog)
        vao.render(moderngl.TRIANGLES, vertices=3)
        data = fbo.read(components=4, dtype='f2', alignment=1)
        fbo.release(); tex.release()
        return data

    def _build_brdf_lut(self):
        prog, vao = self._fullscreen_vao(BRDF_LUT_FRAG)
        data = self._render_pass(prog, vao, BRDF_LUT_SIZE, lambda p: None)
        tex = self.ctx.texture((BRDF_LUT_SIZE, BRDF_LUT_SIZE), 4, data, dtype='f2')
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        tex.repeat_x = tex.repeat_y = False
        return tex

    def _build_env_cubemap(self, size, sun_az, sun_el, sky_color, horizon_color,
                            ground_color, sun_intensity):
        prog, vao = self._fullscreen_vao(SKY_FRAG)
        cube = self.ctx.texture_cube((size, size), 4, dtype='f2')
        sun_dir = np.array([
            math.cos(sun_el) * math.sin(sun_az),
            math.sin(sun_el),
            math.cos(sun_el) * math.cos(sun_az),
        ], dtype='f4')
        for i, (d, r, u) in enumerate(_FACES):
            def set_u(p, d=d, r=r, u=u):
                p['dir0'].value = tuple(d); p['rightV'].value = tuple(r); p['upV'].value = tuple(u)
                p['sunDir'].value = tuple(sun_dir)
                p['sunIntensity'].value = float(sun_intensity)
                p['skyColor'].value = tuple(sky_color)
                p['horizonColor'].value = tuple(horizon_color)
                p['groundColor'].value = tuple(ground_color)
            data = self._render_pass(prog, vao, size, set_u)
            cube.write(i, data)
        return cube

    def _build_irradiance(self, env_cube):
        prog, vao = self._fullscreen_vao(IRRADIANCE_FRAG)
        cube = self.ctx.texture_cube((IRRADIANCE_SIZE, IRRADIANCE_SIZE), 4, dtype='f2')
        env_cube.use(0)
        for i, (d, r, u) in enumerate(_FACES):
            def set_u(p, d=d, r=r, u=u):
                p['envMap'].value = 0
                p['dir0'].value = tuple(d); p['rightV'].value = tuple(r); p['upV'].value = tuple(u)
            data = self._render_pass(prog, vao, IRRADIANCE_SIZE, set_u)
            cube.write(i, data)
        return cube

    def _build_prefilter(self, env_cube):
        prog, vao = self._fullscreen_vao(PREFILTER_FRAG)
        cubes = []
        env_cube.use(0)
        for rough, size in zip(ROUGHNESS_BANDS, PREFILTER_SIZES):
            cube = self.ctx.texture_cube((size, size), 4, dtype='f2')
            for i, (d, r, u) in enumerate(_FACES):
                def set_u(p, d=d, r=r, u=u, rough=rough):
                    p['envMap'].value = 0
                    p['dir0'].value = tuple(d); p['rightV'].value = tuple(r); p['upV'].value = tuple(u)
                    p['roughness'].value = float(rough)
                data = self._render_pass(prog, vao, size, set_u)
                cube.write(i, data)
            cubes.append(cube)
        return cubes

    def set_environment(self, sun_az=0.5, sun_el=0.8, sky_color=(0.35, 0.55, 0.9),
                         horizon_color=(0.75, 0.8, 0.85), ground_color=(0.12, 0.11, 0.1),
                         sun_intensity=6.0, size=ENV_SIZE):
        """(Re)bakes the environment lighting. Takes on the order of a few
        hundred ms — cache-guarded so it's a no-op if nothing changed, but
        callers should still avoid calling this every single frame (e.g. only
        when the sun direction actually moved, not on every camera-drag render)."""
        key = (round(sun_az, 3), round(sun_el, 3), tuple(sky_color), tuple(horizon_color),
               tuple(ground_color), round(sun_intensity, 3), size)
        if key == self._env_key:
            return
        old_irradiance, old_prefilter = self._irradiance, self._prefilter
        env_cube = self._build_env_cubemap(size, sun_az, sun_el, sky_color,
                                            horizon_color, ground_color, sun_intensity)
        self._irradiance = self._build_irradiance(env_cube)
        self._prefilter = self._build_prefilter(env_cube)
        env_cube.release()
        if old_irradiance is not None:
            old_irradiance.release()
            for c in old_prefilter:
                c.release()
        self._env_key = key

    # -- material texture loading (same API as GPUPBRRenderer) ---------------
    def load_texture(self, name, source):
        try:
            from PIL import Image as PILImage
            import os
            if isinstance(source, np.ndarray):
                arr = source
                if arr.dtype != np.uint8:
                    arr = (arr * 255).astype(np.uint8) if arr.max() <= 1.0 else arr.astype(np.uint8)
                img = PILImage.fromarray(arr)
            elif isinstance(source, PILImage.Image):
                img = source
            elif source and os.path.isfile(str(source)):
                img = PILImage.open(str(source)).convert('RGBA')
            else:
                return
            self.textures[name] = img
        except Exception:
            pass

    def clear(self):
        self.textures.clear()

    def _gpu_tex(self, pil_img, channels=3):
        if pil_img.mode not in ('RGB', 'RGBA'):
            pil_img = pil_img.convert('RGBA' if channels == 4 else 'RGB')
        pil_img = pil_img.transpose(Image.FLIP_TOP_BOTTOM)
        tex = self.ctx.texture(pil_img.size, 4 if pil_img.mode == 'RGBA' else 3, pil_img.tobytes())
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR_MIPMAP_LINEAR)
        tex.repeat_x = tex.repeat_y = True
        tex.build_mipmaps()
        return tex

    def _mesh(self, shape):
        if shape not in self._mesh_cache:
            if shape == 'cube': verts, idx = _gen_cube()
            elif shape == 'plane': verts, idx = _gen_plane()
            elif shape == 'cylinder': verts, idx = _gen_cylinder()
            else: verts, idx = _gen_sphere()
            vbo = self.ctx.buffer(verts.tobytes())
            ibo = self.ctx.buffer(idx.tobytes())
            vao = self.ctx.vertex_array(self._main_prog,
                [(vbo, '3f 3f 3f 2f', 'in_position', 'in_normal', 'in_tangent', 'in_uv')],
                index_buffer=ibo)
            self._mesh_cache[shape] = (vbo, ibo, vao)
        return self._mesh_cache[shape][2]

    def render(self, W=440, H=440, shape='sphere', p=None):
        if p is None:
            p = {}
        if self._irradiance is None:
            self.set_environment()   # bake a sensible default sky if nothing's been baked yet

        mm  = float(p.get('metallic_mult', 1.0))
        sm  = float(p.get('smooth_mult', 1.0))
        aop = float(p.get('ao_power', 1.0))
        til = float(p.get('tiling', 1.0))
        ou  = float(p.get('offset_u', 0.0))
        ov  = float(p.get('offset_v', 0.0))
        li  = float(p.get('light_intensity', 2.0))
        laz = float(p.get('light_az', 0.5))
        lel = float(p.get('light_el', 0.8))
        lc  = p.get('light_color', [1.0, 1.0, 1.0])
        lc  = np.array(lc, dtype='f4') if isinstance(lc, (list, tuple)) else np.array([1, 1, 1], 'f4')
        ibl_intensity = float(p.get('ibl_intensity', 1.0))
        coat_weight   = float(p.get('coat_weight', 0.0))
        coat_rough    = float(p.get('coat_roughness', 0.1))
        fuzz_weight   = float(p.get('fuzz_weight', 0.0))

        rot = _rot_x(self.el) @ _rot_y(self.az); rot3 = rot.astype('f4')
        model = np.eye(4, dtype='f4'); model[:3, :3] = rot
        dist = getattr(self, 'dist', 2.8)
        view = _translate(-dist)
        proj = _perspective(35.0, W / max(H, 1), 0.1, max(dist * 4, 10.0))
        mvp = proj @ view @ model
        Lx = math.cos(lel) * math.sin(laz); Ly = math.sin(lel); Lz = math.cos(lel) * math.cos(laz)
        light_dir = np.array([Lx, Ly, Lz], dtype='f4')

        color_tex = self.ctx.texture((W, H), 4, dtype='f1')
        depth_rb = self.ctx.depth_renderbuffer((W, H))
        fbo = self.ctx.framebuffer(color_attachments=[color_tex], depth_attachment=depth_rb)
        fbo.use()
        self.ctx.viewport = (0, 0, W, H)
        fbo.clear(0.05, 0.05, 0.06, 1.0)

        prog = self._main_prog
        mat_texs = []
        has_albedo = has_normal = has_rmaos = has_height = 0
        unit = 0
        if 'albedo' in self.textures:
            t = self._gpu_tex(self.textures['albedo'], 3); mat_texs.append(t)
            t.use(unit); prog['albedoTex'].value = unit; unit += 1; has_albedo = 1
        if 'normal' in self.textures:
            t = self._gpu_tex(self.textures['normal'], 3); mat_texs.append(t)
            t.use(unit); prog['normalTex'].value = unit; unit += 1; has_normal = 1
        if 'rmaos' in self.textures:
            t = self._gpu_tex(self.textures['rmaos'], 4); mat_texs.append(t)
            t.use(unit); prog['rmaosTex'].value = unit; unit += 1; has_rmaos = 1
        if 'height' in self.textures:
            t = self._gpu_tex(self.textures['height'], 3); mat_texs.append(t)
            t.use(unit); prog['heightTex'].value = unit; unit += 1; has_height = 1

        self._irradiance.use(unit); prog['irradianceMap'].value = unit; unit += 1
        for i, cube in enumerate(self._prefilter):
            cube.use(unit); prog[f'prefilter{i}'].value = unit; unit += 1
        self._brdf_lut.use(unit); prog['brdfLUT'].value = unit; unit += 1

        prog['mvp'].write(mvp.T.tobytes())
        prog['rot'].write(rot3.T.tobytes())
        prog['tiling'].value = til; prog['offsetU'].value = ou; prog['offsetV'].value = ov
        prog['camPos'].value = (0.0, 0.0, dist)
        prog['lightDir'].value = tuple(light_dir)
        prog['lightColor'].value = tuple(lc)
        prog['lightIntensity'].value = li
        prog['iblIntensity'].value = ibl_intensity
        prog['metallicMult'].value = mm
        prog['smoothnessMult'].value = sm
        prog['aoPower'].value = aop
        prog['hasAlbedo'].value = has_albedo
        prog['hasNormal'].value = has_normal
        prog['hasRmaos'].value = has_rmaos
        prog['hasHeight'].value = has_height
        prog['parallaxDepth'].value = float(p.get('parallax_depth', 0.0))
        prog['coatWeight'].value = coat_weight
        prog['coatRoughness'].value = coat_rough
        prog['fuzzWeight'].value = fuzz_weight

        vao = self._mesh(shape)
        vao.render(moderngl.TRIANGLES)
        data = fbo.read(components=3, alignment=1)
        img = Image.frombytes('RGB', (W, H), data).transpose(Image.FLIP_TOP_BOTTOM)

        for t in mat_texs:
            t.release()
        fbo.release(); color_tex.release(); depth_rb.release()
        return img

    def release(self):
        try:
            self.ctx.release()
        except Exception:
            pass
