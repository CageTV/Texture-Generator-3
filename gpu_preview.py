
"""
gpu_preview.py
GPU-accelerated PBR Material Preview for TG3
"""
import math
import numpy as np
from PIL import Image
import moderngl

def _rot_y(az):
    c, s = math.cos(az), math.sin(az)
    return np.array([[c,0,s],[0,1,0],[-s,0,c]], dtype=np.float32)
def _rot_x(el):
    c, s = math.cos(el), math.sin(el)
    return np.array([[1,0,0],[0,c,-s],[0,s,c]], dtype=np.float32)
def _perspective(fov_deg, aspect, near, far):
    f = 1.0 / math.tan(math.radians(fov_deg)*0.5)
    m = np.zeros((4,4), dtype=np.float32)
    m[0,0] = f / aspect; m[1,1] = f
    m[2,2] = (far+near)/(near-far); m[2,3] = (2*far*near)/(near-far); m[3,2] = -1.0
    return m
def _translate(z):
    m = np.eye(4, dtype=np.float32); m[2,3]=z; return m

_gpu_probe_result = None

def gpu_available():
    """One-time probe: can we actually create a standalone GL context here?
    Cached after first call since context creation is the expensive/failure-prone part
    (missing GPU, missing/old drivers, headless RDP session with no GL, etc.)."""
    global _gpu_probe_result
    if _gpu_probe_result is None:
        try:
            ctx = moderngl.create_standalone_context(require=330)
            ctx.release()
            _gpu_probe_result = True
        except Exception:
            _gpu_probe_result = False
    return _gpu_probe_result

def _gen_plane(size=2.2):
    s=size
    verts=[(-s,-1.0,-s,0,1,0,1,0,0,0,0),(s,-1.0,-s,0,1,0,1,0,0,1,0),(s,-1.0,s,0,1,0,1,0,0,1,1),(-s,-1.0,s,0,1,0,1,0,0,0,1)]
    idx=[0,1,2,0,2,3]
    return np.array(verts,dtype=np.float32),np.array(idx,dtype=np.uint32)
def _gen_cube():
    data=[];idx=[]
    faces=[([(1,-1,-1),(1,-1,1),(1,1,1),(1,1,-1)],(1,0,0),(0,0,1)),([(-1,-1,1),(-1,-1,-1),(-1,1,-1),(-1,1,1)],(-1,0,0),(0,0,-1)),([(-1,1,-1),(1,1,-1),(1,1,1),(-1,1,1)],(0,1,0),(1,0,0)),([(-1,-1,1),(1,-1,1),(1,-1,-1),(-1,-1,-1)],(0,-1,0),(1,0,0)),([(-1,-1,1),(-1,1,1),(1,1,1),(1,-1,1)],(0,0,1),(1,0,0)),([(1,-1,-1),(1,1,-1),(-1,1,-1),(-1,-1,-1)],(0,0,-1),(-1,0,0)),]
    v=0
    for positions,n,t in faces:
        for i,p in enumerate(positions):
            u=[0,1,1,0][i]; vv=[0,0,1,1][i]
            data.append((p[0]*0.92,p[1]*0.92,p[2]*0.92,n[0],n[1],n[2],t[0],t[1],t[2],u,vv))
        idx.extend([v+0,v+1,v+2,v+0,v+2,v+3]); v+=4
    return np.array(data,dtype=np.float32),np.array(idx,dtype=np.uint32)
def _gen_sphere(lat=28,lon=36):
    verts=[]
    for y in range(lat+1):
        v=y/lat; theta=v*math.pi; sin_t=math.sin(theta); cos_t=math.cos(theta)
        for x in range(lon+1):
            u=x/lon; phi=u*2*math.pi; sin_p=math.sin(phi); cos_p=math.cos(phi)
            px=sin_t*cos_p; py=cos_t; pz=sin_t*sin_p
            tx=-sin_p; ty=0.0; tz=cos_p
            verts.append((px,py,pz,px,py,pz,tx,ty,tz,u,1-v))
    idx=[]
    for y in range(lat):
        for x in range(lon):
            i0=y*(lon+1)+x; i1=i0+1; i2=(y+1)*(lon+1)+x; i3=i2+1
            idx.extend([i0,i2,i1,i1,i2,i3])
    return np.array(verts,dtype=np.float32),np.array(idx,dtype=np.uint32)
def _gen_cylinder(seg=32):
    verts=[];idx=[]
    for i in range(seg+1):
        a=i/seg*2*math.pi; c=math.cos(a); s=math.sin(a); u=i/seg
        verts.append((c*0.85,-0.85,s*0.85,c,0,s,-s,0,c,u,0))
        verts.append((c*0.85,0.85,s*0.85,c,0,s,-s,0,c,u,1))
    for i in range(seg):
        i0=i*2;i1=i0+1;i2=i0+2;i3=i0+3; idx.extend([i0,i2,i1,i1,i2,i3])
    for cap_y,ny in [(-0.85,-1),(0.85,1)]:
        center_idx=len(verts); verts.append((0,cap_y,0,0,ny,0,1,0,0,0.5,0.5))
        for i in range(seg+1):
            a=i/seg*2*math.pi; c=math.cos(a); s=math.sin(a)
            verts.append((c*0.85,cap_y,s*0.85,0,ny,0,1,0,0,c*0.5+0.5,s*0.5+0.5))
        for i in range(seg):
            if ny>0: idx.extend([center_idx,center_idx+1+i,center_idx+2+i])
            else: idx.extend([center_idx,center_idx+2+i,center_idx+1+i])
    return np.array(verts,dtype=np.float32),np.array(idx,dtype=np.uint32)

VERT_SRC="""#version 330
in vec3 in_position; in vec3 in_normal; in vec3 in_tangent; in vec2 in_uv;
uniform mat4 mvp; uniform mat3 rot; uniform float tiling; uniform float offsetU; uniform float offsetV;
out vec3 v_worldPos; out vec3 v_normal; out vec3 v_tangent; out vec3 v_bitangent; out vec2 v_uv;
void main(){ vec3 pos=rot*in_position; vec3 n=rot*in_normal; vec3 t=rot*in_tangent; v_worldPos=pos; v_normal=n; v_tangent=t; v_bitangent=cross(n,t); v_uv=in_uv*tiling+vec2(offsetU,offsetV); gl_Position=mvp*vec4(pos,1.0); }"""

FRAG_SRC="""#version 330
in vec3 v_worldPos; in vec3 v_normal; in vec3 v_tangent; in vec3 v_bitangent; in vec2 v_uv; out vec4 fragColor;
uniform sampler2D albedoTex; uniform sampler2D normalTex; uniform sampler2D rmaosTex; uniform sampler2D heightTex;
uniform int hasAlbedo; uniform int hasNormal; uniform int hasRmaos; uniform int hasHeight;
uniform vec3 camPos; uniform vec3 lightDir; uniform vec3 lightColor; uniform vec3 ambientColor;
uniform float metallicMult; uniform float smoothnessMult; uniform float aoPower; uniform float lightIntensity;
uniform float parallaxDepth;
const float PI=3.14159265;
float D_GGX(float NdH,float rough){ float a=rough*rough; a=max(a,0.002); float a2=a*a; float d=NdH*NdH*(a2-1.0)+1.0; return a2/(PI*d*d+1e-7); }
float G_Schlick(float Nd,float k){ return Nd/(Nd*(1.0-k)+k+1e-4); }
float G_Smith(float NdV,float NdL,float rough){ float k=(rough+1.0)*(rough+1.0)/8.0; return G_Schlick(NdV,k)*G_Schlick(NdL,k); }
vec3 F_Schlick(float VoH,vec3 F0){ return F0+(1.0-F0)*pow(clamp(1.0-VoH,0.0,1.0),5.0); }
vec3 ACESFilm(vec3 x){ return (x*(2.51*x+0.03))/(x*(2.43*x+0.59)+0.14); }
vec2 doParallax(vec2 uv, vec3 Vt, float depth){
 if(hasHeight==0 || depth<0.0005) return uv;
 const int STEPS=10; float layerStep=1.0/float(STEPS); float layer=0.0;
 vec2 delta=-Vt.xy*depth/float(STEPS); vec2 cuv=uv;
 for(int i=0;i<STEPS;i++){ float h=texture(heightTex,fract(cuv)).r; if(layer>=h) break; layer+=layerStep; cuv+=delta; }
 return fract(cuv);
}
void main(){
 vec3 Nraw=normalize(v_normal); vec3 Traw=normalize(v_tangent); vec3 Braw=normalize(v_bitangent);
 vec3 Vworld=normalize(camPos-v_worldPos);
 vec3 Vt=vec3(dot(Vworld,Traw),dot(Vworld,Braw),dot(Vworld,Nraw));
 vec2 uv=doParallax(fract(v_uv),Vt,parallaxDepth);
 vec3 albedo=hasAlbedo==1?texture(albedoTex,uv).rgb:vec3(0.6); albedo=pow(albedo,vec3(2.2));
 vec3 N=Nraw;
 if(hasNormal==1){ vec3 ns=texture(normalTex,uv).rgb*2.0-1.0; mat3 TBN=mat3(Traw,Braw,N); N=normalize(TBN*ns); }
 vec4 rmaos=hasRmaos==1?texture(rmaosTex,uv):vec4(0.5,0.0,1.0,1.0);
 float rough=clamp(rmaos.r*max(2.0-smoothnessMult,0.0),0.02,1.0);
 float metal=clamp(rmaos.g*metallicMult,0.0,1.0);
 float ao=pow(clamp(rmaos.b,0.0,1.0),aoPower);
 vec3 V=Vworld; vec3 L=normalize(lightDir); vec3 H=normalize(V+L);
 float NdL=max(dot(N,L),0.0); float NdV=max(dot(N,V),0.001); float NdH=max(dot(N,H),0.0); float VoH=max(dot(V,H),0.0);
 vec3 F0=mix(vec3(0.04),albedo,metal); float D=D_GGX(NdH,rough); float G=G_Smith(NdV,NdL,rough); vec3 F=F_Schlick(VoH,F0);
 vec3 spec=D*G*F/(4.0*NdV*NdL+0.001); vec3 kD=(vec3(1.0)-F)*(1.0-metal); vec3 diff=kD*albedo/PI;
 vec3 color=(diff+spec)*lightColor*lightIntensity*NdL; color+=ambientColor*albedo*ao;
 color=ACESFilm(color); color=pow(clamp(color,0.0,1.0),vec3(1.0/2.2)); fragColor=vec4(color,1.0);
}"""

class GPUPBRRenderer:
    def __init__(self):
        self.textures={}; self.az=0.4; self.el=0.2; self.dist=2.8
    def load_texture(self,name,source):
        try:
            from PIL import Image as PILImage; import os
            if isinstance(source,np.ndarray):
                arr=source
                if arr.dtype!=np.uint8:
                    if arr.max()<=1.0: arr=(arr*255).astype(np.uint8)
                    else: arr=arr.astype(np.uint8)
                img=PILImage.fromarray(arr)
            elif isinstance(source,PILImage.Image): img=source
            elif source and os.path.isfile(str(source)): img=PILImage.open(str(source)).convert('RGBA')
            else: return
            self.textures[name]=img
        except: pass
    def clear(self): self.textures.clear()
    def _make_gpu_tex(self,ctx,pil_img,channels=3):
        if pil_img.mode not in ('RGB','RGBA'): pil_img=pil_img.convert('RGBA' if channels==4 else 'RGB')
        w,h=pil_img.size; pil_img=pil_img.transpose(Image.FLIP_TOP_BOTTOM); data=pil_img.tobytes()
        tex=ctx.texture((w,h),4 if pil_img.mode=='RGBA' else 3,data); tex.filter=(moderngl.LINEAR,moderngl.LINEAR_MIPMAP_LINEAR); tex.repeat_x=tex.repeat_y=True; tex.build_mipmaps(); return tex
    def render(self,W=440,H=440,shape='sphere',p=None):
        if p is None: p={}
        try:
            mm=float(p.get('metallic_mult',1.0)); sm=float(p.get('smooth_mult',1.0)); aop=float(p.get('ao_power',1.0)); til=float(p.get('tiling',1.0)); ou=float(p.get('offset_u',0.0)); ov=float(p.get('offset_v',0.0)); li=float(p.get('light_intensity',2.0)); laz=float(p.get('light_az',0.5)); lel=float(p.get('light_el',0.8)); lc=p.get('light_color',[1.0,1.0,1.0])
            import math as _m
            if isinstance(lc,(list,tuple)): lc=np.array(lc,dtype=np.float32)
            else: lc=np.array([1,1,1],dtype=np.float32)
            dist=getattr(self,'dist',2.8)
            rot=_rot_x(self.el)@_rot_y(self.az); rot3=rot.astype(np.float32)
            model=np.eye(4,dtype=np.float32); model[:3,:3]=rot; view=_translate(-dist); proj=_perspective(35.0,W/max(H,1),0.1,max(dist*4,10.0)); mvp=proj@view@model
            Lx=_m.cos(lel)*_m.sin(laz); Ly=_m.sin(lel); Lz=_m.cos(lel)*_m.cos(laz); light_dir=np.array([Lx,Ly,Lz],dtype=np.float32)
            ctx=moderngl.create_standalone_context(require=330); ctx.enable(moderngl.DEPTH_TEST)
            prog=ctx.program(vertex_shader=VERT_SRC,fragment_shader=FRAG_SRC)
            if shape=='cube': verts,idx=_gen_cube()
            elif shape=='plane': verts,idx=_gen_plane()
            elif shape=='cylinder': verts,idx=_gen_cylinder()
            else: verts,idx=_gen_sphere()
            vbo=ctx.buffer(verts.tobytes()); ibo=ctx.buffer(idx.tobytes()); vao=ctx.vertex_array(prog,[(vbo,'3f 3f 3f 2f','in_position','in_normal','in_tangent','in_uv')],index_buffer=ibo)
            color_tex=ctx.texture((W,H),4,dtype='f1'); depth_rb=ctx.depth_renderbuffer((W,H)); fbo=ctx.framebuffer(color_attachments=[color_tex],depth_attachment=depth_rb); fbo.use(); ctx.viewport=(0,0,W,H); fbo.clear(0.07,0.07,0.08,1.0)
            has_albedo=0;has_normal=0;has_rmaos=0;has_height=0
            if 'albedo' in self.textures:
                try: t=self._make_gpu_tex(ctx,self.textures['albedo'],3); t.use(0); has_albedo=1
                except: pass
            if 'normal' in self.textures:
                try: t=self._make_gpu_tex(ctx,self.textures['normal'],3); t.use(1); has_normal=1
                except: pass
            if 'rmaos' in self.textures:
                try: t=self._make_gpu_tex(ctx,self.textures['rmaos'],4); t.use(2); has_rmaos=1
                except: pass
            if 'height' in self.textures:
                try: t=self._make_gpu_tex(ctx,self.textures['height'],3); t.use(3); has_height=1
                except: pass
            prog['mvp'].write(mvp.T.tobytes()); prog['rot'].write(rot3.T.tobytes()); prog['camPos'].value=(0.0,0.0,dist); prog['lightDir'].value=(float(light_dir[0]),float(light_dir[1]),float(light_dir[2])); prog['lightColor'].value=(float(lc[0]),float(lc[1]),float(lc[2])); prog['ambientColor'].value=(0.04,0.04,0.05); prog['metallicMult'].value=mm; prog['smoothnessMult'].value=sm; prog['aoPower'].value=aop; prog['tiling'].value=til; prog['offsetU'].value=ou; prog['offsetV'].value=ov; prog['lightIntensity'].value=li; prog['hasAlbedo'].value=has_albedo; prog['hasNormal'].value=has_normal; prog['hasRmaos'].value=has_rmaos; prog['hasHeight'].value=has_height; prog['parallaxDepth'].value=float(p.get('parallax_depth',0.0))
            if has_albedo: prog['albedoTex'].value=0
            if has_normal: prog['normalTex'].value=1
            if has_rmaos: prog['rmaosTex'].value=2
            if has_height: prog['heightTex'].value=3
            vao.render(moderngl.TRIANGLES); data=fbo.read(components=3,alignment=1); img=Image.frombytes('RGB',(W,H),data).transpose(Image.FLIP_TOP_BOTTOM); ctx.release(); return img
        except Exception as e:
            from PIL import ImageDraw; err_img=Image.new('RGB',(W,H),(18,18,20)); d=ImageDraw.Draw(err_img); d.text((10,10),f"GPU preview failed:\n{e}",fill=(200,100,100)); return err_img
