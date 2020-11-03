# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

import logging
import numpy as np
import os
import sys
import torch
import torch.utils.cpp_extension

#----------------------------------------------------------------------------
# C++/Cuda plugin compiler/loader.

_cached_plugin = None
def _get_plugin():
    # Return cached plugin if already loaded.
    global _cached_plugin
    if _cached_plugin is not None:
        return _cached_plugin

    # Make sure we can find the necessary compiler and libary binaries.
    if os.name == 'nt':
        lib_dir = os.path.dirname(__file__) + r"\..\lib"
        def find_cl_path():
            import glob
            for edition in ['Professional', 'BuildTools', 'Community']:
                paths = sorted(glob.glob(r"C:\Program Files (x86)\Microsoft Visual Studio\*\%s\VC\Tools\MSVC\*\bin\Hostx64\x64" % edition), reverse=True)
                if paths:
                    return paths[0]

        # If cl.exe is not on path, try to find it.
        if os.system("where cl.exe >nul 2>nul") != 0:
            cl_path = find_cl_path()
            if cl_path is None:
                raise RuntimeError("Could not locate a supported Microsoft Visual C++ installation")
            os.environ['PATH'] += ';' + cl_path

    # Compiler options.
    opts = ['-DNVDR_TORCH']

    # Linker options.
    if os.name == 'posix':
        ldflags = ['-lGL', '-lGLEW']
    elif os.name == 'nt':
        libs = ['gdi32', 'glew32s', 'opengl32', 'user32']
        ldflags = ['/LIBPATH:' + lib_dir] + ['/DEFAULTLIB:' + x for x in libs]

    # List of source files.
    source_files = [
        '../common/common.cpp',
        '../common/rasterize.cu',
        '../common/rasterize.cpp',
        '../common/interpolate.cu',
        '../common/texture.cu',
        '../common/texture.cpp',
        '../common/antialias.cu',
        'torch_bindings.cpp',
        'torch_rasterize.cpp',
        'torch_interpolate.cpp',
        'torch_texture.cpp',
        'torch_antialias.cpp',
    ]

    # Some containers set this to contain old architectures that won't compile. We only need the one installed in the machine.
    os.environ['TORCH_CUDA_ARCH_LIST'] = ''

    # Try to detect if a stray lock file is left in cache directory and show a warning. This sometimes happens on Windows if the build is interrupted at just the right moment.
    plugin_name = 'nvdiffrast_plugin'
    try:
        lock_fn = os.path.join(torch.utils.cpp_extension._get_build_directory(plugin_name, False), 'lock')
        if os.path.exists(lock_fn):
            logging.getLogger('nvdiffrast').warning("Lock file exists in build directory: '%s'" % lock_fn)
    except:
        pass

    # Compile and load.
    source_paths = [os.path.join(os.path.dirname(__file__), fn) for fn in source_files]
    torch.utils.cpp_extension.load(name=plugin_name, sources=source_paths, extra_cflags=opts, extra_cuda_cflags=opts, extra_ldflags=ldflags, with_cuda=True, verbose=False)

    # Import, cache, and return the compiled module.
    import nvdiffrast_plugin
    _cached_plugin = nvdiffrast_plugin
    return _cached_plugin

#----------------------------------------------------------------------------
# Log level.
#----------------------------------------------------------------------------

def get_log_level():
    '''Get current log level.

    Returns:
      Current log level in nvdiffrast. See `set_log_level()` for possible values.
    '''
    return _get_plugin().get_log_level()

def set_log_level(level):
    '''Set log level.

    Log levels follow the convention on the C++ side of Torch:
      0 = Info, 
      1 = Warning, 
      2 = Error, 
      3 = Fatal.
    The default log level is 1.

    Args:
      level: New log level as integer. Internal nvdiffrast messages of this 
             severity or higher will be printed, while messages of lower
             severity will be silent.
    '''
    _get_plugin().set_log_level(level)

#----------------------------------------------------------------------------
# GL State wrapper.
#----------------------------------------------------------------------------

class RasterizeGLContext:
    def __init__(self, output_db=True, mode='automatic'):
        '''Create a new OpenGL rasterizer context.

        Creating an OpenGL context is a slow operation so you should reuse the same
        context in all calls to `rasterize()` on the same CPU thread. The OpenGL context
        is deleted when the object is destroyed.

        Args:
          output_db (bool): Compute and output image-space derivates of barycentrics.
          mode: OpenGL context handling mode. Valid values are 'manual' and 'automatic'.

        Returns:
          The newly created OpenGL rasterizer context.
        '''
        assert output_db is True or output_db is False
        assert mode in ['automatic', 'manual']
        self.output_db = output_db
        self.mode = mode
        self.cpp_wrapper = _get_plugin().RasterizeGLStateWrapper(output_db, mode == 'automatic')

    def set_context(self):
        '''Set (activate) OpenGL context in the current CPU thread.
           Only available if context was created in manual mode.   
        '''
        assert self.mode == 'manual'
        self.cpp_wrapper.set_context()

    def release_context(self):
        '''Release (deactivate) currently active OpenGL context.
           Only available if context was created in manual mode.
        '''
        assert self.mode == 'manual'
        self.cpp_wrapper.release_context()

#----------------------------------------------------------------------------
# Rasterize.
#----------------------------------------------------------------------------

class _rasterize_func(torch.autograd.Function):
    @staticmethod
    def forward(ctx, glctx, pos, tri, resolution, ranges, grad_db):
        out, out_db = _get_plugin().rasterize_fwd(glctx.cpp_wrapper, pos, tri, resolution, ranges)
        ctx.save_for_backward(pos, tri, out)
        ctx.saved_grad_db = grad_db
        return out, out_db

    @staticmethod
    def backward(ctx, dy, ddb):
        pos, tri, out = ctx.saved_variables
        if ctx.saved_grad_db:
            g_pos = _get_plugin().rasterize_grad_db(pos, tri, out, dy, ddb)
        else:
            g_pos = _get_plugin().rasterize_grad(pos, tri, out, dy)
        return None, g_pos, None, None, None, None

# Op wrapper.
def rasterize(glctx, pos, tri, resolution, ranges=None, grad_db=True):
    '''Rasterize triangles.

    All input tensors must be contiguous and reside in GPU memory except for
    the `ranges` tensor that, if specified, has to reside in CPU memory. The 
    output tensors will be contiguous and reside in GPU memory.

    Args:
        glctx: OpenGL context of type `RasterizeGLContext`.
        pos: Vertex position tensor with dtype `torch.float32`. To enable range
             mode, this tensor should have a 2D shape [num_vertices, 4]. To enable
             instanced mode, use a 3D shape [minibatch_size, num_vertices, 4].
        tri: Triangle tensor with shape [num_triangles, 3] and dtype `torch.int32`.
        resolution: Output resolution as integer tuple (height, width).
        ranges: In range mode, tensor with shape [minibatch_size, 2] and dtype 
                `torch.int32`, specifying start indices and counts into `tri`.
                Ignored in instanced mode.
        grad_db: Propagate gradients of image-space derivatives of barycentrics
                 into `pos` in backward pass. Ignored if OpenGL context was
                 not configured to output image-space derivatives.

    Returns:
        A tuple of two tensors. The first output tensor has shape [minibatch_size, 
        height, width, 4] and contains the main rasterizer output in order (u, v, z/w,
        triangle_id). If the OpenGL context was configured to output image-space
        derivatives of barycentrics, the second output tensor will also have shape
        [minibatch_size, height, width, 4] and contain said derivatives in order
        (du/dX, du/dY, dv/dX, dv/dY). Otherwise it will be an empty tensor with shape
        [minibatch_size, height, width, 0].
    '''
    assert isinstance(glctx, RasterizeGLContext)
    assert grad_db is True or grad_db is False
    grad_db = grad_db and glctx.output_db

    # Sanitize inputs.
    assert isinstance(pos, torch.Tensor) and isinstance(tri, torch.Tensor)
    resolution = tuple(resolution)
    if ranges is None:
        ranges = torch.empty(size=(0, 2), dtype=torch.int32, device='cpu')
    else:
        assert isinstance(ranges, torch.Tensor)

    # Instantiate the function.
    return _rasterize_func.apply(glctx, pos, tri, resolution, ranges, grad_db)

#----------------------------------------------------------------------------
# Interpolate.
#----------------------------------------------------------------------------

# Output pixel differentials for at least some attributes.
class _interpolate_func_da(torch.autograd.Function):
    @staticmethod
    def forward(ctx, attr, rast, tri, rast_db, diff_attrs_all, diff_attrs_list):
        out, out_da = _get_plugin().interpolate_fwd_da(attr, rast, tri, rast_db, diff_attrs_all, diff_attrs_list)
        ctx.save_for_backward(attr, rast, tri, rast_db)
        ctx.saved_misc = diff_attrs_all, diff_attrs_list
        return out, out_da

    @staticmethod
    def backward(ctx, dy, dda):
        attr, rast, tri, rast_db = ctx.saved_variables
        diff_attrs_all, diff_attrs_list = ctx.saved_misc
        g_attr, g_rast, g_rast_db = _get_plugin().interpolate_grad_da(attr, rast, tri, dy, rast_db, dda, diff_attrs_all, diff_attrs_list)
        return g_attr, g_rast, None, g_rast_db, None, None

# No pixel differential for any attribute.
class _interpolate_func(torch.autograd.Function):
    @staticmethod
    def forward(ctx, attr, rast, tri):
        out, out_da = _get_plugin().interpolate_fwd(attr, rast, tri)
        ctx.save_for_backward(attr, rast, tri)
        return out, out_da

    @staticmethod
    def backward(ctx, dy, _):
        attr, rast, tri = ctx.saved_variables
        g_attr, g_rast = _get_plugin().interpolate_grad(attr, rast, tri, dy)
        return g_attr, g_rast, None

# Op wrapper.
def interpolate(attr, rast, tri, rast_db=None, diff_attrs=None):
    """Interpolate vertex attributes.

    All input tensors must be contiguous and reside in GPU memory. The output tensors
    will be contiguous and reside in GPU memory.

    Args:
        attr: Attribute tensor with dtype `torch.float32`. 
              Shape is [num_vertices, num_attributes] in range mode, or 
              [minibatch_size, num_vertices, num_attributes] in instanced mode.
              Broadcasting is supported along the minibatch axis.
        rast: Main output tensor from `rasterize()`.
        tri: Triangle tensor with shape [num_triangles, 3] and dtype `torch.int32`.
        rast_db: (Optional) Tensor containing image-space derivatives of barycentrics, 
                 i.e., the second output tensor from `rasterize()`. Enables computing
                 image-space derivatives of attributes.
        diff_attrs: (Optional) List of attribute indices for which image-space
                    derivatives are to be computed. Special value 'all' is equivalent
                    to list [0, 1, ..., num_attributes - 1].

    Returns:
        A tuple of two tensors. The first output tensor contains interpolated
        attributes and has shape [minibatch_size, height, width, num_attributes].
        If `rast_db` and `diff_attrs` were specified, the second output tensor contains
        the image-space derivatives of the selected attributes and has shape
        [minibatch_size, height, width, 2 * len(diff_attrs)]. The derivatives of the
        first selected attribute A will be on channels 0 and 1 as (dA/dX, dA/dY), etc.
        Otherwise, the second output tensor will be an empty tensor with shape
        [minibatch_size, height, width, 0].
    """
    # Sanitize the list of pixel differential attributes.
    if diff_attrs is None:
        diff_attrs = []
    elif diff_attrs != 'all':
        diff_attrs = np.asarray(diff_attrs, np.int32)
        assert len(diff_attrs.shape) == 1
        diff_attrs = diff_attrs.tolist()

    diff_attrs_all = int(diff_attrs == 'all')
    diff_attrs_list = [] if diff_attrs_all else diff_attrs

    # Check inputs.
    assert all(isinstance(x, torch.Tensor) for x in (attr, rast, tri))
    if diff_attrs:
        assert isinstance(rast_db, torch.Tensor)

    # Choose stub.
    if diff_attrs:
        return _interpolate_func_da.apply(attr, rast, tri, rast_db, diff_attrs_all, diff_attrs_list)
    else:
        return _interpolate_func.apply(attr, rast, tri)

#----------------------------------------------------------------------------
# Texture
#----------------------------------------------------------------------------

# Linear-mipmap-linear and linear-mipmap-nearest: Mipmaps enabled.
class _texture_func_mip(torch.autograd.Function):
    @staticmethod
    def forward(ctx, filter_mode, tex, uv, uv_da, mip, filter_mode_enum, boundary_mode_enum):
        out = _get_plugin().texture_fwd_mip(tex, uv, uv_da, mip, filter_mode_enum, boundary_mode_enum)
        ctx.save_for_backward(tex, uv, uv_da)
        ctx.saved_misc = filter_mode, mip, filter_mode_enum, boundary_mode_enum
        return out

    @staticmethod
    def backward(ctx, dy):
        tex, uv, uv_da = ctx.saved_variables
        filter_mode, mip, filter_mode_enum, boundary_mode_enum = ctx.saved_misc
        if filter_mode == 'linear-mipmap-linear':
            g_tex, g_uv, g_uv_da = _get_plugin().texture_grad_linear_mipmap_linear(tex, uv, dy, uv_da, mip, filter_mode_enum, boundary_mode_enum)
            return None, g_tex, g_uv, g_uv_da, None, None, None
        else: # linear-mipmap-nearest
            g_tex, g_uv = _get_plugin().texture_grad_linear_mipmap_nearest(tex, uv, dy, uv_da, mip, filter_mode_enum, boundary_mode_enum)
            return None, g_tex, g_uv, None, None, None, None

# Linear and nearest: Mipmaps disabled.
class _texture_func(torch.autograd.Function):
    @staticmethod
    def forward(ctx, filter_mode, tex, uv, filter_mode_enum, boundary_mode_enum):
        out = _get_plugin().texture_fwd(tex, uv, filter_mode_enum, boundary_mode_enum)
        ctx.save_for_backward(tex, uv)
        ctx.saved_misc = filter_mode, filter_mode_enum, boundary_mode_enum
        return out

    @staticmethod
    def backward(ctx, dy):
        tex, uv = ctx.saved_variables
        filter_mode, filter_mode_enum, boundary_mode_enum = ctx.saved_misc
        if filter_mode == 'linear':
            g_tex, g_uv = _get_plugin().texture_grad_linear(tex, uv, dy, filter_mode_enum, boundary_mode_enum)
            return None, g_tex, g_uv, None, None
        else: # nearest
            g_tex = _get_plugin().texture_grad_nearest(tex, uv, dy, filter_mode_enum, boundary_mode_enum)
            return None, g_tex, None, None, None

# Op wrapper.
def texture(tex, uv, uv_da=None, mip=None, filter_mode='auto', boundary_mode='wrap', max_mip_level=None):
    """Perform texture sampling.

    All input tensors must be contiguous and reside in GPU memory. The output tensor
    will be contiguous and reside in GPU memory.

    Args:
        tex: Texture tensor with dtype `torch.float32`. For 2D textures, must have shape
             [minibatch_size, tex_height, tex_width, tex_channels]. For cube map textures,
             must have shape [minibatch_size, 6, tex_height, tex_width, tex_channels] where
             tex_width and tex_height are equal. Note that `boundary_mode` must also be set 
             to 'cube' to enable cube map mode. Broadcasting is supported along the minibatch axis.
        uv: Tensor containing per-pixel texture coordinates. When sampling a 2D texture, 
            must have shape [minibatch_size, height, width, 2]. When sampling a cube map
            texture, must have shape [minibatch_size, height, width, 3].
        uv_da: (Optional) Tensor containing image-space derivatives of texture coordinates.
               Must have same shape as `uv` except for the last dimension that is to be twice
               as long.
        mip: (Optional) Preconstructed mipmap stack from a `texture_construct_mip()` call. If not
             specified, the mipmap stack is constructed internally and discarded afterwards.
        filter_mode: Texture filtering mode to be used. Valid values are 'auto', 'nearest', 
                     'linear', 'linear-mipmap-nearest', and 'linear-mipmap-linear'. Mode 'auto'
                     selects 'linear' if `uv_da` is not specified, and 'linear-mipmap-linear'
                     when `uv_da` is specified, these being the highest-quality modes possible
                     depending on the availability of the image-space derivatives of the texture
                     coordinates.
        boundary_mode: Valid values are 'wrap', 'clamp', 'zero', and 'cube'. If `tex` defines a
                       cube map, this must be set to 'cube'. The default mode 'wrap' takes fractional
                       part of texture coordinates. Mode 'clamp' clamps texture coordinates to the
                       centers of the boundary texels. Mode 'zero' virtually extends the texture with
                       all-zero values in all directions.
        max_mip_level: If specified, limits the number of mipmaps constructed and used in mipmap-based
                       filter modes.

    Returns:
        A tensor containing the results of the texture sampling with shape
        [minibatch_size, height, width, tex_channels].
    """

    # Default filter mode.
    if filter_mode == 'auto':
        filter_mode = 'linear-mipmap-linear' if (uv_da is not None) else 'linear'

    # Sanitize inputs.
    if max_mip_level is None:
        max_mip_level = -1
    else:
        max_mip_level = int(max_mip_level)
        assert max_mip_level >= 0

    # Check inputs.
    assert isinstance(tex, torch.Tensor) and isinstance(uv, torch.Tensor)
    if 'mipmap' in filter_mode:
        assert isinstance(uv_da, torch.Tensor)

    # If mipping disabled via max level=0, we may as well use simpler filtering internally.
    if max_mip_level == 0 and filter_mode in ['linear-mipmap-nearest', 'linear-mipmap-linear']:
        filter_mode = 'linear'

    # Convert filter mode to internal enumeration.
    filter_mode_dict = {'nearest': 0, 'linear': 1, 'linear-mipmap-nearest': 2, 'linear-mipmap-linear': 3}
    filter_mode_enum = filter_mode_dict[filter_mode]

    # Convert boundary mode to internal enumeration.
    boundary_mode_dict = {'cube': 0, 'wrap': 1, 'clamp': 2, 'zero': 3}
    boundary_mode_enum = boundary_mode_dict[boundary_mode]

    # Construct a mipmap if necessary.
    if 'mipmap' in filter_mode:
        if mip is not None:
            assert isinstance(mip, _get_plugin().TextureMipWrapper)
        else:
            mip = _get_plugin().texture_construct_mip(tex, max_mip_level, boundary_mode == 'cube')

    # Choose stub.
    if filter_mode == 'linear-mipmap-linear' or filter_mode == 'linear-mipmap-nearest':
        return _texture_func_mip.apply(filter_mode, tex, uv, uv_da, mip, filter_mode_enum, boundary_mode_enum)
    else:
        return _texture_func.apply(filter_mode, tex, uv, filter_mode_enum, boundary_mode_enum)
        
# Mipmap precalculation for cases where the texture stays constant.
def texture_construct_mip(tex, max_mip_level=None, cube_mode=False):
    """Construct a mipmap stack for a texture.

    This function can be used for constructing a mipmap stack for a texture that is known to remain
    constant. This avoids reconstructing it every time `texture()` is called.

    Args:
        tex: Texture tensor with the same constraints as in `texture()`.
        max_mip_level: If specified, limits the number of mipmaps constructed.
        cube_mode: Must be set to True if `tex` specifies a cube map texture.

    Returns:
        An opaque object containing the mipmap stack. This can be supplied in a call to `texture()` 
        in the `mip` argument.
    """

    assert isinstance(tex, torch.Tensor)
    assert cube_mode is True or cube_mode is False
    if max_mip_level is None:
        max_mip_level = -1
    else:
        max_mip_level = int(max_mip_level)
        assert max_mip_level >= 0
    return _get_plugin().texture_construct_mip(tex, max_mip_level, cube_mode)

#----------------------------------------------------------------------------
# Antialias.
#----------------------------------------------------------------------------

class _antialias_func(torch.autograd.Function):
    @staticmethod
    def forward(ctx, color, rast, pos, tri, topology_hash, pos_gradient_boost):
        out, work_buffer = _get_plugin().antialias_fwd(color, rast, pos, tri, topology_hash)
        ctx.save_for_backward(color, rast, pos, tri)
        ctx.saved_misc = pos_gradient_boost, work_buffer
        return out

    @staticmethod
    def backward(ctx, dy):
        color, rast, pos, tri = ctx.saved_variables
        pos_gradient_boost, work_buffer = ctx.saved_misc
        g_color, g_pos = _get_plugin().antialias_grad(color, rast, pos, tri, dy, work_buffer)
        if pos_gradient_boost != 1.0:
            g_pos = g_pos * pos_gradient_boost
        return g_color, None, g_pos, None, None, None

# Op wrapper.
def antialias(color, rast, pos, tri, topology_hash=None, pos_gradient_boost=1.0):
    """Perform antialiasing.

    All input tensors must be contiguous and reside in GPU memory. The output tensor
    will be contiguous and reside in GPU memory.

    Args:
        color: Input image to antialias with shape [minibatch_size, height, width, num_channels].
        rast: Main output tensor from `rasterize()`.
        pos: Vertex position tensor used in the rasterization operation.
        tri: Triangle tensor used in the rasterization operation.
        topology_hash: (Optional) Preconstructed topology hash for the triangle tensor. If not
                       specified, the topology hash is constructed internally and discarded afterwards.
        pos_gradient_boost: (Optional) Multiplier for gradients propagated to `pos`.

    Returns:
        A tensor containing the antialiased image with the same shape as `color` input tensor.
    """

    # Check inputs.
    assert all(isinstance(x, torch.Tensor) for x in (color, rast, pos, tri))

    # Construct topology hash unless provided by user.
    if topology_hash is not None:
        assert isinstance(topology_hash, _get_plugin().TopologyHashWrapper)
    else:
        topology_hash = _get_plugin().antialias_construct_topology_hash(tri)

    # Instantiate the function.
    return _antialias_func.apply(color, rast, pos, tri, topology_hash, pos_gradient_boost)

# Topology hash precalculation for cases where the triangle array stays constant.
def antialias_construct_topology_hash(tri):
    """Construct a topology hash for a triangle tensor.

    This function can be used for constructing a topology hash for a triangle tensor that is 
    known to remain constant. This avoids reconstructing it every time `antialias()` is called.

    Args:
        tri: Triangle tensor with shape [num_triangles, 3]. Must be contiguous and reside in
             GPU memory.

    Returns:
        An opaque object containing the topology hash. This can be supplied in a call to 
        `antialias()` in the `topology_hash` argument.
    """
    assert isinstance(tri, torch.Tensor)
    return _get_plugin().antialias_construct_topology_hash(tri)

#----------------------------------------------------------------------------
