# Vendored kernel uapi headers

Copies of Qualcomm kernel uapi headers from
[commaai/agnos-kernel-sdm845](https://github.com/commaai/agnos-kernel-sdm845)
(the kernel shipped on comma 3/3X and its clones), GPL-2.0. Identical to what
`make headers_install` would put in `/usr/include`: `msm_ion.h` had its
kernel-only `__user` annotations stripped.

Why: the on-device scons build needs them, but some device rootfses (e.g. the
C3L clone's AGNOS 12.4) ship a stock linux-libc-dev without them:

- `linux/ion.h`, `linux/msm_ion.h` — `msgq_repo/msgq/visionipc/visionbuf_ion.cc`
- `media/msm_media_info.h` — `system/loggerd/encoder/v4l_encoder.cc`
- `media/msm_camsensor_sdk.h` — `system/camerad/sensors/*.cc`

`SConstruct` adds `include/` to `CPPPATH` only when the system headers are
missing (the tell is `/usr/include/linux/ion.h`), so official AGNOS devices
keep using their own copies. The layout mirrors a sysroot
(`include/linux/...`, `include/media/...`), so includes resolve as
`<linux/ion.h>` / `<media/msm_media_info.h>` like on AGNOS.

These are the legacy (pre-4.12) ION ABI (`ion_allocation_data.handle`,
`ION_IOC_SHARE`/`IMPORT`) that the msm-4.9 kernel on these devices speaks —
the post-4.12 uapi from mainline kernels is incompatible and must not be used
here.
